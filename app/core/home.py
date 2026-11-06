"""The shelves each source puts on its own front page.

What the panel shows before anything is typed: what is trending, what was added
lately, today's top ten, the latest anime episodes. None of it is curated here —
these are the source's own lists, read out of the page it serves to any visitor.

Items come back in exactly the shape ``/api/search`` returns for that source, so
the grid, the detail modal and the request flow need no new vocabulary.
"""

import json
import logging
import threading
import time

from bs4 import BeautifulSoup

from app.core import animeunity, page

logger = logging.getLogger(__name__)

# A "trending" shelf fifteen minutes stale is indistinguishable from a fresh
# one; an hour is not.
_TTL = 15 * 60

# Only two keys are ever live. The bound exists so a pathological run of domain
# rotations cannot grow the dict.
_MAX_ENTRIES = 8

# How many titles of a shelf are worth rendering. The source sends thirty.
_SHELF_CAP = 30

# AnimeUnity's featured carousel is the one shelf longer than that: forty-three
# at the time of writing, and cutting it at thirty hid a third of the box.
_FEATURED_CAP = 60

# How long a start page that is missing its lazy-loaded shelves is kept. Short,
# because the missing part is a failure to retry, not a quiet fact about the
# source; but not zero, or a source that is down is asked on every page load.
_PARTIAL_TTL = 60

_cache: dict[tuple, tuple[float, list]] = {}
_cache_lock = threading.Lock()


def _now() -> float:
    """Wall clock, indirected so tests can expire an entry without sleeping."""
    return time.time()


# The panel's own headings. The source's label is Italian copy it can re-word
# tomorrow, so the stable machine name is what the shelf is keyed on and this is
# what gets shown. A shelf nobody here has heard of still renders, under the
# source's own label — better a heading we did not choose than a vetrina that
# silently disappears the month the source adds one.
_LABELS = {
    ("streamingcommunity", "trending"): "Di tendenza",
    ("streamingcommunity", "latest"): "Aggiunti di recente",
    ("streamingcommunity", "top10"): "Top 10 di oggi",
    ("streamingcommunity", "upcoming"): "In arrivo",
    ("animeunity", "latest_episodes"): "Ultimi episodi",
    ("animeunity", "featured"): "In evidenza",
}


# ── Cache ─────────────────────────────────────────────────────────────────────

def _cache_key(source: str, host: str) -> tuple:
    """Keyed on the host, not just the source.

    This is the structural half of surviving a domain rotation: the old
    domain's shelves cannot be served because they cannot be reached. The
    explicit half is clear_cache(), called where the domain is written.
    """
    return (source, host)


def _cached(key: tuple) -> list | None:
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at < _now():
            _cache.pop(key, None)
            return None
        return value


def _store(key: tuple, value: list, ttl: int = _TTL) -> None:
    with _cache_lock:
        if len(_cache) >= _MAX_ENTRIES:
            now = _now()
            for stale in [k for k, (exp, _) in _cache.items() if exp < now]:
                _cache.pop(stale, None)
            if len(_cache) >= _MAX_ENTRIES:
                _cache.pop(next(iter(_cache)), None)
        _cache[key] = (_now() + ttl, value)


def clear_cache(source: str | None = None) -> None:
    """Drop cached shelves, for one source or all of them."""
    with _cache_lock:
        if source is None:
            _cache.clear()
            return
        for key in [k for k in _cache if k[0] == source]:
            _cache.pop(key, None)


# ── StreamingCommunity ────────────────────────────────────────────────────────

def _sc_shelf(slider: dict) -> dict | None:
    """One slider of the source as a shelf, or None when it holds nothing usable."""
    label = slider.get("label")
    # Every genre slider is called "genre" and tells itself apart by its label,
    # so the label has to be in the key or twenty-six shelves share one.
    key = f"genre:{label}" if slider.get("name") == "genre" else (slider.get("name") or label)
    if not key:
        return None
    items = [t for t in (page.normalize_title(raw)
                         for raw in (slider.get("titles") or [])[:_SHELF_CAP]) if t]
    if not items:
        return None
    return _shelf("streamingcommunity", key, items, fallback=label)


def _sc_lazy_shelves(domain: str, props: dict) -> list[dict]:
    """The shelves the home page only loads as you scroll: upcoming, then a
    slider for every genre. Fetched together here, because a panel that renders
    the whole start page at once has no scroll to wait for."""
    specs = [{"name": "upcoming", "title_type": None, "genre": None}]
    for genre in props.get("genres") or []:
        name = genre.get("name") if isinstance(genre, dict) else None
        if name:
            specs.append({"name": "genre", "title_type": None, "genre": name})
    return [s for s in (_sc_shelf(raw) for raw in page.fetch_sliders(domain, specs)) if s]


def _sc_shelves(domain: str) -> tuple[list[dict], bool]:
    """Every box of the source's home: the sliders in the page itself (trending,
    recently added, today's top ten), then the lazy ones.

    The second element says whether the lazy part arrived. A failure there
    costs the extra shelves and nothing else — the page's own sliders are
    already in hand and worth showing.
    """
    props = page.fetch_site_props(domain)
    shelves = [s for s in (_sc_shelf(raw) for raw in props.get("sliders") or []) if s]
    try:
        shelves.extend(_sc_lazy_shelves(domain, props))
    except Exception as exc:
        logger.warning("Cannot read the lazy shelves of %s: %s", domain, exc)
        return shelves, False
    return shelves, True


# ── AnimeUnity ────────────────────────────────────────────────────────────────

def _attr_json(soup, attribute: str, element: str | None = None):
    """One element attribute carrying escaped JSON, decoded.

    BeautifulSoup unescapes the attribute itself, so there is no
    ``html.unescape()`` here — the payload is escaped exactly once, and a second
    pass would corrupt any ``&amp;amp;`` inside it.

    Found by attribute rather than by tag name where it can be: these are custom
    elements whose names are the source's to change.
    """
    node = soup.find(element, attrs={attribute: True}) if element else None
    if node is None:
        node = soup.find(attrs={attribute: True})
    if node is None:
        return None
    try:
        return json.loads(node[attribute])
    except Exception as exc:
        logger.warning("AnimeUnity %s attribute could not be read: %s", attribute, exc)
        return None


def _au_shelves() -> list[dict]:
    """The home page's own lists: the latest episodes, and the featured carousel.

    Both ride in element attributes as escaped JSON rather than in an API.
    """
    soup = BeautifulSoup(animeunity.fetch_home_html(), "lxml")

    latest_raw = (_attr_json(soup, "items-json") or {}).get("data") or []
    # Two new episodes of the same series are two rows and one card.
    latest, seen = [], set()
    for row in latest_raw:
        item = animeunity.normalize_title(row.get("anime") or {})
        if item and item["id"] not in seen:
            seen.add(item["id"])
            latest.append(item)

    featured_raw = _attr_json(soup, "animes", element="the-carousel") or []
    featured = [t for t in (animeunity.normalize_title(r) for r in featured_raw) if t]

    shelves = []
    for key, items, cap in (("latest_episodes", latest, _SHELF_CAP),
                            ("featured", featured, _FEATURED_CAP)):
        if items:
            shelves.append(_shelf("animeunity", key, items[:cap]))
    return shelves


# ── Assembly ──────────────────────────────────────────────────────────────────

def _shelf(source: str, key: str, items: list, fallback: str | None = None) -> dict:
    return {
        "key": key,
        "title": _LABELS.get((source, key)) or fallback or key,
        "items": items,
    }


def shelves(source: str, domain: str) -> list[dict]:
    """The front-page shelves for *source*, cached for a quarter of an hour.

    A failed fetch is **not** cached, which is where this parts company with
    ``metadata``. That one caches a miss for ten minutes because a title with no
    plot usually still has none ten minutes later. A home page that failed is a
    flap, and remembering it would blank the start page for every user for the
    duration — so the answer is an empty list now and a fresh try on the next
    page load.
    """
    key = _cache_key(source, domain)
    hit = _cached(key)
    if hit is not None:
        return hit

    try:
        result, complete = (_au_shelves(), True) if source == "animeunity" \
            else _sc_shelves(domain)
    except Exception:
        logger.exception("Cannot read the front page of %s", source)
        return []

    _store(key, result, _TTL if complete else _PARTIAL_TTL)
    return result
