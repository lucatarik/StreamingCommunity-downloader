"""Finding the source domain again after it rotates.

The source's domain changes every few weeks. Until now that meant the panel
simply stopped working: searches failed, nothing said why, and an administrator
had to go and find the new address themselves.

A third-party page publishes the current one. This module reads it, checks the
candidate is plausible, proves it actually serves the source, and — by default —
*proposes* it rather than adopting it. That default is the whole design. The
page is editable by people we do not control, and the domain is not a cosmetic
setting: every search, every image fetch and every download referer goes to
whatever host is configured here. Adopting one because a web page said so, with
nobody looking, hands a stranger the ability to point this panel wherever they
like. `domain_auto_apply` exists for deployments that want it anyway, and is off
unless asked for.

The candidate is held in memory, not in data.json. It is discovery state, not
configuration, and a candidate frozen across a restart could be applied long
after the page it came from had changed again.
"""

import ipaddress
import logging
import os
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from app import config
from app.core.headers import get_headers
from app.core import animeunity, archive, home, metadata
from app.core.page import get_domain_version

logger = logging.getLogger(__name__)

# Where candidates come from. Overridable because the page is somebody else's
# and may be deleted, renamed, or replaced by a better source.
CANDIDATES_URL = os.getenv(
    "DOMAIN_SOURCE_URL",
    "https://telegra.ph/Link-Aggiornato-StreamingCommunity-09-29",
)

# Which registrable names may ever be adopted automatically.
#
# A module constant with an env override rather than a settings field, on
# purpose: this is the check that stops an edited page from redirecting the
# panel to an arbitrary host, and a text box in the UI that relaxes it would be
# a loaded gun. A genuine rebrand fails closed — the candidate is logged and
# reported as rejected, and an administrator types the new name in by hand,
# which is a decision made by a person.
NAME_PATTERN = re.compile(
    os.getenv("DOMAIN_NAME_PATTERN", r"^streaming(community|unity)[a-z0-9-]{0,12}$")
)

_HOST_RE = re.compile(r"^[a-z0-9.-]{4,253}$")

# Hosts that are never a public streaming site, whatever DNS says.
_LOCAL_SUFFIXES = (".local", ".internal", ".localhost", ".home", ".lan")

# AnimeUnity moves between TLDs and publishes no page that says where. So its
# candidates are not read from anywhere: they are the one name it has always
# gone by, tried under the TLDs it (or its clones) have used. The name and the
# list are module constants with env overrides and **must not become settings
# fields**, for the same reason NAME_PATTERN must not: a text box that relaxes
# this is a text box that relaxes the guard. A TLD nobody foresaw is typed in by
# hand under Settings › Sorgente, which is a decision made by a person.
AU_NAME_PATTERN = re.compile(os.getenv("ANIMEUNITY_NAME_PATTERN", r"^animeunity$"))
AU_TLDS = tuple(
    t.strip().lower().lstrip(".")
    for t in os.getenv(
        "ANIMEUNITY_DOMAIN_TLDS",
        "so,to,co,tv,cc,bz,ws,org,net,com,me,sh,io,vip,xyz,site,top,one,info",
    ).split(",")
    if t.strip()
)

# Probing a TLD costs a DNS lookup and, for the few that exist, a page load. Dead
# ones answer at once; this bounds the ones that do not.
_AU_PROBE_TIMEOUT = 8
_AU_PROBE_WORKERS = 8

# The page carries a handful of links, most of them telegra.ph's own. Anything
# past this is not a candidate list, it is a different page.
_MAX_CANDIDATES = 5

# The reactive trigger fires whenever the configured domain fails to verify,
# which on a broken panel is every page load of every user. Without a floor
# between checks that becomes a small stampede against someone else's page.
_MIN_CHECK_INTERVAL = 600

_lock = threading.Lock()
_pending: dict | None = None
_last_check_at: float | None = None
# AnimeUnity has its own candidate and its own throttle: the two sources move
# independently, and one's check must not postpone the other's.
_pending_au: dict | None = None
_last_au_check_at: float | None = None


def _now() -> float:
    """Monotonic clock, indirected so tests can move it without touching time."""
    return time.monotonic()


# ── Candidates ────────────────────────────────────────────────────────────────

def fetch_candidates(url: str | None = None) -> list[str]:
    """Hosts linked from the source-of-truth page, in order, deduplicated."""
    target = url or CANDIDATES_URL
    response = requests.get(target, headers={"user-agent": get_headers()}, timeout=10)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "lxml")
    hosts: list[str] = []
    for anchor in soup.find_all("a"):
        href = (anchor.get("href") or "").strip()
        if not href:
            continue
        parsed = urlparse(href)
        # Keep the scheme: is_plausible refuses anything but https, and it must
        # see what the page actually published rather than an assumed default.
        host = (parsed.hostname or "").lower().strip(".")
        if not host or parsed.scheme != "https":
            continue
        if host not in hosts:
            hosts.append(host)
    return hosts[:_MAX_CANDIDATES]


# ── The guard ─────────────────────────────────────────────────────────────────

def _check_shape(host: str, name_pattern: re.Pattern | None = None) -> str | None:
    """Syntactic rejection reason, or None if the host looks like a candidate.

    ``name_pattern`` is the source's own allowed name; StreamingCommunity's is
    the default. Everything else — second-level only, no IPs, no local names —
    is the same for both.
    """
    if not host or not _HOST_RE.match(host):
        return "forma non valida"
    if host.endswith(_LOCAL_SUFFIXES) or host == "localhost":
        return "nome locale"

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return "indirizzo IP"

    labels = host.split(".")
    if len(labels) == 3 and labels[0] == "www":
        labels = labels[1:]
    if len(labels) != 2:
        # The load-bearing rule. Checking only the first label would accept
        # streamingcommunity.attacker.tld, where the part that decides where the
        # traffic goes is the attacker's. Multi-part suffixes (.co.uk) are
        # rejected as collateral; typing the domain by hand still works.
        return "non è un dominio di secondo livello"

    name = labels[0]
    if not (name_pattern or NAME_PATTERN).match(name):
        return f"nome «{name}» non riconosciuto"
    return None


def _resolves_to_public(host: str) -> bool:
    """Whether every address the host resolves to is a public one.

    Without this, a candidate could point the panel's own fetches at the network
    it is running inside — the panel is a willing HTTP client with an image
    proxy attached.
    """
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        logger.info("Candidate %s does not resolve: %s", host, exc)
        return False
    if not infos:
        return False

    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            logger.warning("Candidate %s resolves to a non-public address %s", host, address)
            return False
    return True


def is_plausible(host: str, name_pattern: re.Pattern | None = None) -> tuple[bool, str]:
    """Whether this host may be adopted, and why not when it may not.

    The reason is returned rather than logged and dropped: a rebranded source is
    indistinguishable from an attack from in here, and the administrator who has
    to tell them apart needs to see what was refused.
    """
    reason = _check_shape(host, name_pattern)
    if reason:
        return False, reason
    if not _resolves_to_public(host):
        return False, "risolve a un indirizzo non pubblico"
    return True, ""


def verify(host: str) -> str | None:
    """The site version this host serves, or None if it does not serve the source.

    Deliberately stricter than ``PUT /api/domain``, which accepts a host whose
    version string comes back empty: an administrator typing a domain in is
    making a decision, and a page we do not control is not. Here an empty
    version is not proof of anything, so it is not enough.
    """
    try:
        version = get_domain_version(host)
    except Exception as exc:
        logger.info("Candidate %s failed verification: %s", host, exc)
        return None
    return version or None


# ── Pending candidate ─────────────────────────────────────────────────────────

def pending() -> dict | None:
    with _lock:
        return dict(_pending) if _pending else None


def clear_pending() -> None:
    global _pending
    with _lock:
        _pending = None


def _set_pending(candidate: dict | None) -> None:
    global _pending
    with _lock:
        _pending = candidate


def apply_candidate(host: str) -> str:
    """Write a candidate as the configured domain, re-verifying it first.

    Returns the site version. Raises RuntimeError if the host no longer checks
    out — a candidate can go stale between being proposed and being confirmed.
    """
    ok, reason = is_plausible(host)
    if not ok:
        raise RuntimeError(f"Dominio non adottabile: {reason}")
    version = verify(host)
    if version is None:
        raise RuntimeError(f"«{host}» non risponde come la sorgente attesa")

    config.update_data({"domain": host})
    # The shelves and the artwork are the old domain's until they are dropped.
    home.clear_cache("streamingcommunity")
    archive.clear_cache("streamingcommunity")
    metadata.clear_cache()
    clear_pending()
    logger.info("Source domain set to %s (version %s)", host, version)
    return version


# ── AnimeUnity ────────────────────────────────────────────────────────────────

def pending_animeunity() -> dict | None:
    with _lock:
        return dict(_pending_au) if _pending_au else None


def clear_pending_animeunity() -> None:
    global _pending_au
    with _lock:
        _pending_au = None


def _set_pending_animeunity(candidate: dict | None) -> None:
    global _pending_au
    with _lock:
        _pending_au = candidate


def verify_animeunity(host: str) -> bool:
    """Whether *host* serves AnimeUnity's own front page.

    Stricter than ``hosts.verify_animeunity``, which a typed-in host goes
    through and which only looks for the word "animeunity" in the page — a parked
    domain with the name in its title passes that. A host found by guessing has
    to show what the real page is made of: the CSRF meta the archive endpoint
    needs, and one of the two components the start page is read from.
    """
    try:
        response = animeunity._get_scraper().get(
            f"https://{host}/", headers={"user-agent": get_headers()},
            timeout=_AU_PROBE_TIMEOUT)
        response.raise_for_status()
    except Exception as exc:
        logger.info("Candidate %s failed verification: %s", host, exc)
        return False
    text = response.text.lower()
    return ("animeunity" in text and 'name="csrf-token"' in text
            and ("layout-items" in text or "the-carousel" in text))


def animeunity_candidates(current: str) -> list[str]:
    """``www.animeunity.<tld>`` for every TLD on the list, but the one in use."""
    return [h for h in (f"www.animeunity.{tld}" for tld in AU_TLDS) if h != current]


def _announced_hosts() -> list[str]:
    """The hosts AnimeUnity's public announcement names, or none.

    Never raises: a channel that is down, a post that was deleted or a layout
    that changed must leave the TLD probe to do its job, not break the check.
    """
    try:
        from app.core import announcement
        return announcement.fetch_hosts()
    except Exception as exc:
        logger.warning("Cannot read the AnimeUnity announcement: %s", exc)
        return []


def _probe_animeunity(host: str) -> tuple[str, str | None]:
    """``(host, None)`` for a usable candidate; otherwise why not.

    The reason ``""`` means the name does not exist at all — most of the list,
    most of the time — and is not worth a line in the report.
    """
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return host, ""
    ok, reason = is_plausible(host, AU_NAME_PATTERN)
    if not ok:
        return host, reason
    if not verify_animeunity(host):
        return host, "non verificato"
    return host, None


def apply_animeunity_candidate(host: str) -> None:
    """Write a candidate as AnimeUnity's host, re-verifying it first.

    Raises RuntimeError if it no longer checks out. Writes what Settings ›
    Sorgente writes, and drops everything that was learned from the old host: the
    CSRF token belongs to the host that issued it, and the shelves and archive
    options are its pages.
    """
    ok, reason = is_plausible(host, AU_NAME_PATTERN)
    if not ok:
        raise RuntimeError(f"Dominio non adottabile: {reason}")
    if not verify_animeunity(host):
        raise RuntimeError(f"«{host}» non risponde come AnimeUnity")

    config.update_data({"animeunity_domain": host})
    animeunity.forget_csrf()
    home.clear_cache("animeunity")
    archive.clear_cache("animeunity")
    clear_pending_animeunity()
    logger.info("AnimeUnity host set to %s", host)


def run_check_animeunity(force: bool = False) -> dict:
    """One pass for AnimeUnity: does the configured host answer, and if not,
    which of the usual names does.

    ``force`` only bypasses the throttle. A working host is never replaced —
    there is no list to read that could say it should be — so, unlike the
    StreamingCommunity check, pressing the button on a healthy panel does not go
    looking for a second one.
    """
    global _last_au_check_at

    result: dict = {
        "current": config.configured_animeunity_host(),
        "current_ok": False,
        "candidate": None,
        "applied": False,
        "rejected": [],
        "checked": False,
        "via": None,
    }

    if not force:
        with _lock:
            if _last_au_check_at is not None and _now() - _last_au_check_at < _MIN_CHECK_INTERVAL:
                logger.debug("AnimeUnity check skipped: within the throttle window")
                return result

    with _lock:
        _last_au_check_at = _now()
    result["checked"] = True

    result["current_ok"] = verify_animeunity(result["current"])
    if result["current_ok"]:
        _set_pending_animeunity(None)
        return result

    # The announcement is the one place that actually says where the site is
    # now. Guessing TLDs is the fallback for when it cannot be read or names
    # nothing usable.
    named = [h for h in _announced_hosts() if h != result["current"]]
    candidates = named or animeunity_candidates(result["current"])
    result["via"] = "announcement" if named else "tld"
    with ThreadPoolExecutor(max_workers=_AU_PROBE_WORKERS) as pool:
        probes = list(pool.map(_probe_animeunity, candidates))

    for host, reason in probes:
        if reason is None:
            result["candidate"] = host
            break
        if reason:
            result["rejected"].append({"host": host, "reason": reason})

    if result["candidate"] is None:
        return result

    if _settings().get("domain_auto_apply"):
        try:
            apply_animeunity_candidate(result["candidate"])
            result["applied"] = True
            _announce(
                "applied",
                f"L'indirizzo di AnimeUnity è cambiato: ora è «{result['candidate']}». "
                "Il pannello lo ha applicato da solo.",
            )
        except RuntimeError as exc:
            logger.warning("Auto-apply of %s failed: %s", result["candidate"], exc)
        return result

    _set_pending_animeunity({
        "host": result["candidate"],
        "found_at": time.time(),
    })
    _announce(
        "found",
        f"AnimeUnity non risponde più su «{result['current']}». "
        f"Trovato «{result['candidate']}»: apri il pannello per applicarlo.",
    )
    _broadcast_candidate()
    return result


# ── The cycle ─────────────────────────────────────────────────────────────────

def _settings() -> dict:
    return config.get_settings()


def run_check(force: bool = False) -> dict:
    """One pass: is the configured domain alive, and if not, what could replace it.

    ``force`` is the "Controlla ora" button: it bypasses the throttle and looks
    for a candidate even when the current domain is working, because an
    administrator who pressed it wants an answer rather than a shrug.
    """
    global _last_check_at

    result: dict = {
        "current": config.configured_domain(),
        "current_ok": False,
        "candidate": None,
        "version": None,
        "applied": False,
        "rejected": [],
        "checked": False,
    }

    if not force:
        with _lock:
            if _last_check_at is not None and _now() - _last_check_at < _MIN_CHECK_INTERVAL:
                logger.debug("Domain check skipped: within the throttle window")
                return result

    with _lock:
        _last_check_at = _now()
    result["checked"] = True

    if result["current"]:
        result["current_ok"] = verify(result["current"]) is not None

    # A working domain does not need replacing. Only an explicit request looks
    # further, so the ordinary periodic check costs one request to the source
    # and nothing to anybody else.
    if result["current_ok"] and not force:
        _set_pending(None)
        return result

    try:
        candidates = fetch_candidates()
    except Exception as exc:
        logger.warning("Cannot read the domain source page: %s", exc)
        return result

    for host in candidates:
        if host == result["current"]:
            continue
        ok, reason = is_plausible(host)
        if not ok:
            logger.info("Rejected candidate %s: %s", host, reason)
            result["rejected"].append({"host": host, "reason": reason})
            continue
        version = verify(host)
        if version is None:
            result["rejected"].append({"host": host, "reason": "non verificato"})
            continue

        result["candidate"] = host
        result["version"] = version
        break

    if result["candidate"] is None:
        return result

    if _settings().get("domain_auto_apply"):
        try:
            apply_candidate(result["candidate"])
            result["applied"] = True
            _announce(
                "applied",
                f"Il dominio della sorgente è cambiato: ora è «{result['candidate']}». "
                "Il pannello lo ha applicato da solo.",
            )
        except RuntimeError as exc:
            logger.warning("Auto-apply of %s failed: %s", result["candidate"], exc)
        return result

    _set_pending({
        "host": result["candidate"],
        "version": result["version"],
        "found_at": time.time(),
    })
    _announce(
        "found",
        f"La sorgente non risponde più su «{result['current'] or 'nessun dominio'}». "
        f"Trovato «{result['candidate']}»: apri il pannello per applicarlo.",
    )
    _broadcast_candidate()
    return result


# ── Telling somebody ──────────────────────────────────────────────────────────

def _announce(kind: str, message: str) -> None:
    """Notify whoever can act on this, and never let that break the check.

    ``app.requests.notify`` is imported here rather than at module scope: it
    reaches the database and the job manager, and app.core has no business
    depending on the request system at import time. The event vocabulary still
    lives in one place — this only picks from it.
    """
    try:
        from app.requests import notify as notify_module

        event = {
            "found": notify_module.SOURCE_DOMAIN_FOUND,
            "applied": notify_module.SOURCE_DOMAIN_APPLIED,
        }[kind]
        notify_module.notify(
            event,
            message,
            notify_module.settings_manager_ids(),
            title="Dominio sorgente",
            # The panel itself owns this one when there are no accounts: an open
            # installation still needs to be told its source moved.
            panel_wide=True,
        )
    except Exception:
        logger.exception("Cannot announce the domain change (%s)", kind)


def _broadcast_candidate() -> None:
    """Nudge open tabs so the banner appears without a reload."""
    try:
        from app.jobs import job_manager

        job_manager.broadcast({"type": "domain_candidate"})
    except Exception:
        logger.debug("Cannot broadcast the domain candidate", exc_info=True)


# ── The periodic check ────────────────────────────────────────────────────────

async def domain_watch_loop():
    """Check periodically, shaped exactly like the followed-series poller.

    Sleeps first for the same reason that one does: the lifespan must not do
    network I/O, and a fresh install has no domain to check yet.
    """
    import asyncio

    while True:
        try:
            interval = int(_settings().get("domain_check_interval_minutes") or 360)
        except (TypeError, ValueError):
            interval = 360
        await asyncio.sleep(max(30, interval) * 60)

        if not _settings().get("domain_auto_check_enabled", True):
            continue
        try:
            await asyncio.to_thread(run_check)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Domain check failed")
        # Its own try: one source being unreachable is no reason to skip the other.
        try:
            await asyncio.to_thread(run_check_animeunity)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("AnimeUnity host check failed")
