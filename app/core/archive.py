"""The archive of each source: the whole catalogue under the source's own filters.

Search finds a title you can name; the archive is for browsing what is there —
by kind, genre, country, year, rating. Both sources have the page and both build
it from an endpoint the panel can call, so the filters shown here are theirs, not
an invented vocabulary:

- StreamingCommunity: ``GET /archive`` answers its Laravel paginator as JSON to an
  XHR request, sixty titles a page, under ``type``, ``genre[]``, ``country[]``,
  ``year``, ``score``, ``views``, ``service``, ``quality``, ``age`` and ``sort``.
- AnimeUnity: ``POST /archivio/get-animes``, thirty a page, under ``title``,
  ``type``, ``year``, ``order``, ``status``, ``season``, ``genres`` and ``dubbed``.

The filter *definitions* — which filters exist, their options, their defaults —
are built here and sent to the page, which renders whatever it is given. The same
definition validates the request, so what the page offers and what the server
accepts cannot drift apart.

Items come back in the shape ``/api/search`` returns for that source, so the card,
the detail page and the request flow need nothing new.
"""

import json
import logging
import threading
import time
from datetime import date

from bs4 import BeautifulSoup

from app.core import animeunity, page
from app.core.headers import get_headers

logger = logging.getLogger(__name__)

SOURCES = ("streamingcommunity", "animeunity")

# StreamingCommunity answers sixty a page, and stops at a thousand titles for a
# visitor without a premium account (a 503 past that). Seventeen pages is those
# thousand; asking further would only collect the refusal.
SC_PAGE_SIZE = 60
SC_MAX_PAGE = 17

# AnimeUnity's archive page size is the source's own, and a whole year is
# fetched by walking it. The bound is a ceiling for a source that never answers
# short, not a number a real year comes near (a year is a few hundred titles).
AU_PAGE_SIZE = animeunity.ARCHIVE_PAGE
AU_MAX_PAGES = 60

# A filter's options change when the source ships a new genre, not by the hour.
_OPTIONS_TTL = 6 * 3600

# A year fetched in full is a dozen requests to someone else's server. Held for
# a few minutes so that toggling a filter back and forth costs them once.
_FULL_TTL = 10 * 60

_MAX_ENTRIES = 16

_cache: dict[tuple, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _now() -> float:
    """Wall clock, indirected so tests can expire an entry without sleeping."""
    return time.time()


def _cached(key: tuple):
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < _now():
            _cache.pop(key, None)
            return None
        return value


def _store(key: tuple, value, ttl: int) -> None:
    with _cache_lock:
        if len(_cache) >= _MAX_ENTRIES:
            now = _now()
            for stale in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(stale, None)
            if len(_cache) >= _MAX_ENTRIES:
                _cache.pop(next(iter(_cache)), None)
        _cache[key] = (_now() + ttl, value)


def clear_cache(source: str | None = None) -> None:
    """Drop what was learned about a source, for one of them or all.

    Every key carries the host, so a rotation cannot serve the old domain's
    options; this is the explicit half, called where a host is written.
    """
    with _cache_lock:
        if source is None:
            _cache.clear()
            return
        for key in [k for k in _cache if k[0] == source]:
            _cache.pop(key, None)


# ── Definitions ───────────────────────────────────────────────────────────────
#
# One filter is {key, label, kind, options?}. `key` is the query parameter, and
# `kind` is how the page draws it and how the server validates it:
#   text    free text, trimmed
#   select  one of the options
#   multi   any number of the options
#   toggle  "1" or absent

def _opt(value, name) -> dict:
    return {"value": str(value), "name": name}


def _flt(key: str, label: str, kind: str, options: list[dict] | None = None) -> dict:
    flt = {"key": key, "label": label, "kind": kind}
    if options is not None:
        flt["options"] = options
    return flt


_SC_SORTS = [
    _opt("release_date", "Data di uscita"),
    _opt("last_air_date", "Ultima messa in onda"),
    _opt("created_at", "Data di aggiunta"),
    _opt("score", "Voto"),
    _opt("views", "Visualizzazioni"),
    _opt("name", "Nome"),
]
_SC_TYPES = [_opt("tv", "Serie TV"), _opt("movie", "Film")]
_SC_VIEWS = [_opt(v, n) for v, n in (
    (25_000, "25k+"), (50_000, "50k+"), (75_000, "75k+"), (100_000, "100k+"),
    (250_000, "250k+"), (500_000, "500k+"), (1_000_000, "1M+"), (2_000_000, "2M+"),
    (5_000_000, "5M+"), (10_000_000, "10M+"))]
_SC_SERVICES = [_opt(v, n) for v, n in (
    ("netflix", "Netflix"), ("prime", "Prime Video"), ("disney", "Disney+"),
    ("apple", "Apple TV+"), ("now", "NOW"), ("hbo", "HBO Max"))]
_SC_QUALITIES = [_opt(q, q) for q in ("HD", "SD", "TS", "CAM")]
_SC_AGES = [_opt(a, f"{a}+") for a in (7, 12, 14, 16, 18)]
_SC_SCORES = [_opt(s, f"{s}+") for s in range(1, 11)]


def _sc_years(current: int) -> list[dict]:
    """The site's own year list: every year after 2000, then one entry a decade.

    The decade is what the source filters on for the older ones (``1990`` is the
    nineties), so offering 1994 would be offering a value it does not know.
    """
    seen, years = set(), []
    for year in range(current, 1909, -1):
        value = year if year > 2000 else (year // 10) * 10
        if value not in seen:
            seen.add(value)
            years.append(_opt(value, str(value) if value > 2000 else f"Anni {value}"))
    return years


def _sc_definition(props: dict) -> dict:
    genres = [_opt(g["id"], g["name"]) for g in props.get("genres") or []
              if isinstance(g, dict) and g.get("id") is not None and g.get("name")]
    countries = sorted(
        (_opt(c["id"], c["name"]) for c in props.get("countries") or []
         if isinstance(c, dict) and c.get("id") is not None and c.get("name")),
        key=lambda o: o["name"])
    return {
        "source": "streamingcommunity",
        "filters": [
            _flt("q", "Titolo", "text"),
            _flt("sort", "Ordina per", "select", _SC_SORTS),
            _flt("type", "Tipo", "select", _SC_TYPES),
            _flt("genre", "Genere", "multi", genres),
            _flt("country", "Paese", "multi", countries),
            _flt("year", "Anno", "select", _sc_years(date.today().year)),
            _flt("score", "Voto minimo", "select", _SC_SCORES),
            _flt("views", "Visualizzazioni", "select", _SC_VIEWS),
            _flt("service", "Servizio", "select", _SC_SERVICES),
            _flt("quality", "Qualità", "select", _SC_QUALITIES),
            _flt("age", "Età", "select", _SC_AGES),
        ],
        # Newest release first, which is what the source's own page opens on.
        "defaults": {"sort": "release_date"},
    }


# AnimeUnity's own lists, as its archive page builds them in its script.
_AU_ORDERS = ["Lista A-Z", "Lista Z-A", "Popolarità", "Valutazione"]
_AU_STATUSES = ["In Corso", "Terminato", "In Uscita", "Droppato"]
_AU_TYPES = ["TV", "TV Short", "OVA", "ONA", "Special", "Movie"]
_AU_SEASONS = ["Inverno", "Primavera", "Estate", "Autunno"]

# The oldest record when the archive page cannot be read for it.
_AU_FALLBACK_OLDEST = 1966


def _au_definition(genres: list[dict], oldest: int) -> dict:
    # The source lists next year too: titles are announced before they air.
    current = date.today().year
    years = [_opt(y, str(y)) for y in range(current + 1, oldest - 1, -1)]
    return {
        "source": "animeunity",
        "filters": [
            _flt("q", "Titolo", "text"),
            _flt("genre", "Genere", "multi",
                 [_opt(g["id"], g["name"]) for g in genres]),
            _flt("year", "Anno", "select", years),
            _flt("order", "Ordina per", "select", [_opt(o, o) for o in _AU_ORDERS]),
            _flt("status", "Stato", "select", [_opt(s, s) for s in _AU_STATUSES]),
            _flt("type", "Tipo", "select", [_opt(t, t) for t in _AU_TYPES]),
            _flt("season", "Stagione", "select", [_opt(s, s) for s in _AU_SEASONS]),
            _flt("dubbed", "Doppiato in italiano", "toggle"),
        ],
        # The archive opens on this year, dubbed in Italian: what a person
        # who comes here usually wants is what is airing now.
        "defaults": {"year": str(current), "dubbed": "1"},
    }


def _au_archive_page() -> tuple[list[dict], int]:
    """The genres and the oldest year, read out of AnimeUnity's archive page."""
    r = animeunity._get_scraper().get(f"https://{animeunity.host()}/archivio", timeout=15)
    r.raise_for_status()
    node = BeautifulSoup(r.text, "lxml").find("archivio")
    if node is None:
        raise LookupError("the archive page carries no <archivio> element")
    genres = json.loads(node.get("all_genres") or "[]")
    try:
        oldest = int(node.get("anime_oldest_date") or _AU_FALLBACK_OLDEST)
    except ValueError:
        oldest = _AU_FALLBACK_OLDEST
    return [g for g in genres if isinstance(g, dict) and "id" in g and g.get("name")], oldest


def definition(source: str, host: str) -> dict:
    """The filters the archive of *source* offers, with their options and defaults."""
    if source not in SOURCES:
        raise ValueError(f"Sorgente sconosciuta: {source}")
    key = (source, host, "definition")
    hit = _cached(key)
    if hit is not None:
        return hit

    if source == "animeunity":
        try:
            genres, oldest = _au_archive_page()
        except Exception as exc:
            # The genres are a convenience. Without them the other filters still
            # work, and the page says nothing about it rather than failing whole.
            logger.warning("Cannot read the AnimeUnity archive page: %s", exc)
            result = _au_definition([], _AU_FALLBACK_OLDEST)
            _store(key, result, 60)
            return result
        result = _au_definition(genres, oldest)
    else:
        props = page.fetch_site_props(host, "/archive")
        result = _sc_definition(props)

    _store(key, result, _OPTIONS_TTL)
    return result


# ── Validation ────────────────────────────────────────────────────────────────

def clean(defn: dict, raw: dict) -> dict:
    """The request's filters, checked against the definition, or ValueError.

    Unknown keys are ignored — a page that adds a parameter must not break an
    older server — but a known key with a value outside its options is refused:
    it would otherwise be sent to the source as typed.
    """
    out: dict = {}
    for flt in defn["filters"]:
        key, kind = flt["key"], flt["kind"]
        values = [v for v in (raw.get(key) or []) if v != ""]
        if not values:
            continue
        if kind == "text":
            text = values[0].strip()[:200]
            if text:
                out[key] = text
        elif kind == "toggle":
            if values[0] == "1":
                out[key] = True
        else:
            allowed = {o["value"] for o in flt["options"]}
            for v in values:
                if v not in allowed:
                    raise ValueError(f"Valore non valido per «{flt['label']}»: {v[:60]}")
            out[key] = values if kind == "multi" else values[0]
    return out


# ── StreamingCommunity ────────────────────────────────────────────────────────

def _empty(page_no: int) -> dict:
    return {"items": [], "page": page_no, "has_more": False, "total": 0}


def _sc_browse(domain: str, flt: dict, page_no: int) -> dict:
    if page_no > SC_MAX_PAGE:
        return _empty(page_no)

    params: list[tuple[str, str]] = [
        ("lang", "it"), ("page", str(page_no)), ("sort", flt.get("sort") or "release_date")]
    if flt.get("q"):
        params.append(("search", flt["q"]))
    for key in ("type", "year", "score", "views", "service", "quality", "age"):
        if flt.get(key):
            params.append((key, flt[key]))
    for key in ("genre", "country"):
        # The brackets are the source's own: it reads them as arrays.
        params.extend((f"{key}[]", v) for v in flt.get(key, []))

    response = page.get_localized(
        domain, "/archive", params=params, timeout=15,
        headers={"user-agent": get_headers(), "accept": "application/json",
                 "x-requested-with": "XMLHttpRequest"})
    if response.status_code == 503:
        # Past the page limit for a visitor without premium: the end, not a fault.
        return _empty(page_no)
    response.raise_for_status()

    body = response.json()
    rows = body.get("data") or body.get("titles") or []
    items = [t for t in (page.normalize_title(raw) for raw in rows) if t]
    return {
        "items": items,
        "page": page_no,
        "has_more": bool(body.get("next_page_url")) and page_no < SC_MAX_PAGE,
        "total": body.get("total"),
    }


# ── AnimeUnity ────────────────────────────────────────────────────────────────

def _au_filters(defn: dict, flt: dict) -> dict:
    names = {o["value"]: o["name"] for f in defn["filters"] if f["key"] == "genre"
             for o in f["options"]}
    return {
        "title": flt.get("q"),
        "type": flt.get("type"),
        "year": int(flt["year"]) if flt.get("year") else None,
        "order": flt.get("order"),
        "status": flt.get("status"),
        "season": flt.get("season"),
        "genres": [{"id": int(g), "name": names[g]} for g in flt.get("genre", [])],
        "dubbed": bool(flt.get("dubbed")),
    }


def _au_fetch_all(filters: dict) -> list[dict]:
    """Every record matching *filters*, by walking the archive a page at a time."""
    records: list[dict] = []
    offset = 0
    for _ in range(AU_MAX_PAGES):
        # The first page's worth of each answer, as the source's own page keeps
        # it, so the offset it is given means what it meant to the source.
        batch, total = animeunity.browse(filters, offset)
        batch = batch[:AU_PAGE_SIZE]
        if not batch:
            break
        records.extend(batch)
        offset += len(batch)
        if len(batch) < AU_PAGE_SIZE or (total is not None and offset >= total):
            break
    return records


def _au_items(records: list[dict]) -> list[dict]:
    items, seen = [], set()
    for raw in records:
        item = animeunity.normalize_title(raw)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        items.append(item)
    return items


def _au_browse(defn: dict, flt: dict, page_no: int) -> dict:
    filters = _au_filters(defn, flt)

    if filters["year"]:
        # A year is a few hundred titles and the source pages them thirty at a
        # time in an order of its own, so the page cannot be both short and
        # right: it takes the whole year, and puts it newest-added first. An
        # order the person chose is theirs and is left as the source gave it.
        key = (animeunity.host(), "year", json.dumps(filters, sort_keys=True))
        records = _cached(key)
        if records is None:
            records = _au_fetch_all(filters)
            if not filters["order"]:
                records = sorted(records, key=lambda r: r.get("created_at") or "",
                                 reverse=True)
            _store(key, records, _FULL_TTL)
        items = _au_items(records)
        return {"items": items, "page": 1, "has_more": False, "total": len(items)}

    offset = (page_no - 1) * AU_PAGE_SIZE
    records, total = animeunity.browse(filters, offset)
    records = records[:AU_PAGE_SIZE]
    items = _au_items(records)
    more = len(records) >= AU_PAGE_SIZE and (total is None or offset + len(records) < total)
    return {"items": items, "page": page_no, "has_more": more, "total": total}


# ── Entry point ───────────────────────────────────────────────────────────────

def browse(source: str, host: str, raw_filters: dict, page_no: int = 1) -> dict:
    """One page of the archive, as ``{items, page, has_more, total}``.

    ``raw_filters`` maps a filter key to the list of values the request carried.
    ``AnimeUnity`` with a year answers the whole year on page one.
    """
    defn = definition(source, host)
    flt = clean(defn, raw_filters)
    if source == "animeunity":
        return _au_browse(defn, flt, page_no)
    return _sc_browse(host, flt, page_no)
