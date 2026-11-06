"""The archive: each source's catalogue under its own filters.

The trap worth pinning is AnimeUnity's whole-year fetch. A year is a few hundred
titles behind a thirty-a-page endpoint, and the helper a search goes through
stops at sixty — so a year read the obvious way is silently cut to a fifth.
"""

import pytest

from app.auth.permissions import ALL_PERMISSIONS
from app.core import animeunity, archive, page
from tests.conftest import do_setup, make_user, session_for


@pytest.fixture(autouse=True)
def _empty_cache():
    archive.clear_cache()
    yield
    archive.clear_cache()


# ── AnimeUnity ────────────────────────────────────────────────────────────────

def _record(n, created, title=None):
    return {"id": n, "slug": f"a-{n}", "title_eng": title or f"Anime {n}", "type": "TV",
            "imageurl": f"https://cdn.test/{n}.jpg", "created_at": created, "dub": 0}


class _Archive:
    """The source's archive: a fixed set of records, thirty to an answer."""

    def __init__(self, records):
        self.records = records
        self.bodies = []

    def post(self, body):
        self.bodies.append(dict(body))
        offset = body.get("offset", 0)
        rows = self.records[offset:offset + 30]

        class _R:
            ok = True
            status_code = 200

            def json(_self):
                return {"records": rows, "tot": len(self.records)}

        return _R()


@pytest.fixture
def au(monkeypatch):
    state = _Archive([])
    monkeypatch.setattr(animeunity, "_post_archive", state.post)
    monkeypatch.setattr(animeunity, "host", lambda: "www.animeunity.test")
    monkeypatch.setattr(archive, "_au_archive_page",
                        lambda: ([{"id": 51, "name": "Action"}, {"id": 9, "name": "Fantasy"}], 1966))
    return state


def test_a_year_is_fetched_whole_and_not_cut_at_sixty(au):
    au.records = [_record(n, f"2026-01-{(n % 28) + 1:02d} 10:00:00") for n in range(1, 98)]

    result = archive.browse("animeunity", "www.animeunity.test", {"year": ["2026"]})

    assert len(result["items"]) == 97
    assert result["has_more"] is False
    assert [b["offset"] for b in au.bodies] == [0, 30, 60, 90]


def test_a_year_is_shown_newest_added_first(au):
    au.records = [_record(1, "2026-01-05 10:00:00"), _record(2, "2026-09-01 10:00:00"),
                  _record(3, "2026-03-01 10:00:00")]

    result = archive.browse("animeunity", "www.animeunity.test", {"year": ["2026"]})

    assert [i["id"] for i in result["items"]] == ["2-a-2", "3-a-3", "1-a-1"]


def test_an_order_the_person_chose_is_left_as_the_source_gave_it(au):
    au.records = [_record(1, "2026-01-05 10:00:00"), _record(2, "2026-09-01 10:00:00")]

    result = archive.browse("animeunity", "www.animeunity.test",
                            {"year": ["2026"], "order": ["Popolarità"]})

    assert [i["id"] for i in result["items"]] == ["1-a-1", "2-a-2"]
    assert au.bodies[0]["order"] == "Popolarità"


def test_the_walk_stops_on_a_short_page(au):
    au.records = [_record(n, "2026-01-01 00:00:00") for n in range(1, 31)]

    archive.browse("animeunity", "www.animeunity.test", {"year": ["2026"]})

    # Exactly thirty: the source's total says that was all of it. Asking for a
    # second page would be a request to someone else's server for nothing.
    assert [b["offset"] for b in au.bodies] == [0]


def test_a_year_fetched_once_is_not_fetched_again(au):
    au.records = [_record(1, "2026-01-01 00:00:00")]

    archive.browse("animeunity", "www.animeunity.test", {"year": ["2026"]})
    archive.browse("animeunity", "www.animeunity.test", {"year": ["2026"]})

    assert len(au.bodies) == 1


def test_without_a_year_the_archive_pages_thirty_at_a_time(au):
    au.records = [_record(n, "2026-01-01 00:00:00") for n in range(1, 66)]

    second = archive.browse("animeunity", "www.animeunity.test", {}, page_no=2)

    assert au.bodies[0]["offset"] == 30
    assert len(second["items"]) == 30
    assert second["has_more"] is True
    assert second["total"] == 65


def test_the_last_page_says_so(au):
    au.records = [_record(n, "2026-01-01 00:00:00") for n in range(1, 66)]

    third = archive.browse("animeunity", "www.animeunity.test", {}, page_no=3)

    assert len(third["items"]) == 5
    assert third["has_more"] is False


def test_genres_go_to_the_source_as_objects(au):
    archive.browse("animeunity", "www.animeunity.test",
                   {"genre": ["51", "9"], "dubbed": ["1"], "q": ["naruto"]})

    body = au.bodies[0]
    assert body["genres"] == [{"id": 51, "name": "Action"}, {"id": 9, "name": "Fantasy"}]
    assert body["dubbed"] == 1
    assert body["title"] == "naruto"


def test_the_archive_opens_on_this_year_dubbed(au):
    from datetime import date
    defn = archive.definition("animeunity", "www.animeunity.test")

    assert defn["defaults"] == {"year": str(date.today().year), "dubbed": "1"}


def test_the_year_list_runs_from_next_year_to_the_oldest_record(au):
    from datetime import date
    years = [o["value"] for f in archive.definition("animeunity", "www.animeunity.test")["filters"]
             if f["key"] == "year" for o in f["options"]]

    assert years[0] == str(date.today().year + 1)
    assert years[-1] == "1966"


def test_an_archive_page_that_cannot_be_read_still_gives_the_other_filters(monkeypatch):
    def boom():
        raise RuntimeError("down")

    monkeypatch.setattr(archive, "_au_archive_page", boom)

    defn = archive.definition("animeunity", "www.animeunity.test")

    assert "order" in [f["key"] for f in defn["filters"]]


# ── Validation ────────────────────────────────────────────────────────────────

def test_a_value_outside_the_options_is_refused(au):
    with pytest.raises(ValueError):
        archive.browse("animeunity", "www.animeunity.test", {"order": ["'; drop table"]})


def test_a_genre_the_source_does_not_list_is_refused(au):
    with pytest.raises(ValueError):
        archive.browse("animeunity", "www.animeunity.test", {"genre": ["99999"]})


def test_a_parameter_nobody_defined_is_ignored(au):
    archive.browse("animeunity", "www.animeunity.test", {"flavour": ["mint"]})

    assert "flavour" not in au.bodies[0]


# ── StreamingCommunity ────────────────────────────────────────────────────────

class _Json:
    def __init__(self, body, status_code=200):
        self._body = body
        self.status_code = status_code

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _title(n):
    return {"id": n, "name": f"Title {n}", "slug": f"t-{n}", "type": "movie", "score": "7.0",
            "last_air_date": "2026-01-01", "images": []}


@pytest.fixture
def sc(monkeypatch):
    state = {"calls": [], "body": {"data": [_title(1)], "next_page_url": None, "total": 1},
             "status": 200}

    props = {"genres": [{"id": 4, "name": "Azione", "type": "movie"}],
             "countries": [{"id": 229, "code": "US", "name": "United States"}]}

    def fake_get_localized(domain, path, **kwargs):
        state["calls"].append((domain, path, kwargs.get("params")))
        return _Json(state["body"], state["status"])

    monkeypatch.setattr(page, "get_localized", fake_get_localized)
    monkeypatch.setattr(page, "fetch_site_props", lambda domain, path="", **k: props)
    return state


def test_the_default_order_is_release_date(sc):
    archive.browse("streamingcommunity", "source.test", {})

    params = dict(sc["calls"][0][2])
    assert params["sort"] == "release_date"


def test_the_filters_reach_the_source_in_its_own_spelling(sc):
    archive.browse("streamingcommunity", "source.test", {
        "type": ["movie"], "genre": ["4"], "country": ["229"], "year": ["2020"],
        "score": ["7"], "q": ["dune"]})

    params = sc["calls"][0][2]
    assert ("genre[]", "4") in params
    assert ("country[]", "229") in params
    assert ("type", "movie") in params
    assert ("year", "2020") in params
    assert ("score", "7") in params
    assert ("search", "dune") in params


def test_more_pages_follow_the_sources_own_say_so(sc):
    sc["body"] = {"data": [_title(1)], "next_page_url": "https://x/archive?page=2", "total": 130}

    result = archive.browse("streamingcommunity", "source.test", {})

    assert result["has_more"] is True
    assert result["total"] == 130
    assert result["items"][0]["id"] == 1


def test_past_the_page_a_visitor_may_read_it_is_the_end_and_costs_no_request(sc):
    result = archive.browse("streamingcommunity", "source.test", {}, page_no=archive.SC_MAX_PAGE + 1)

    assert result["items"] == [] and result["has_more"] is False
    assert sc["calls"] == []


def test_a_503_is_the_end_and_not_a_fault(sc):
    sc["status"] = 503

    result = archive.browse("streamingcommunity", "source.test", {}, page_no=5)

    assert result["items"] == [] and result["has_more"] is False


def test_the_last_permitted_page_offers_no_next_one(sc):
    sc["body"] = {"data": [_title(1)], "next_page_url": "https://x/archive?page=18", "total": 5000}

    result = archive.browse("streamingcommunity", "source.test", {}, page_no=archive.SC_MAX_PAGE)

    assert result["has_more"] is False


def test_the_options_are_the_sources_own(sc):
    defn = archive.definition("streamingcommunity", "source.test")
    by_key = {f["key"]: f for f in defn["filters"]}

    assert by_key["genre"]["options"] == [{"value": "4", "name": "Azione"}]
    assert by_key["country"]["options"][0]["name"] == "United States"
    assert [o["value"] for o in by_key["age"]["options"]] == ["7", "12", "14", "16", "18"]
    assert defn["defaults"] == {"sort": "release_date"}


def test_the_older_years_are_decades(sc):
    years = [o["value"] for o in archive._sc_years(2026)]

    assert "2001" in years and "2000" in years and "1990" in years
    assert "1995" not in years          # the source only knows the decade
    assert len(years) == len(set(years))


def test_a_rotation_cannot_serve_the_old_hosts_options(sc, monkeypatch):
    archive.definition("streamingcommunity", "old.test")
    monkeypatch.setattr(page, "fetch_site_props",
                        lambda d, p="", **k: {"genres": [{"id": 1, "name": "Nuovo"}], "countries": []})

    fresh = archive.definition("streamingcommunity", "new.test")

    assert [o["name"] for f in fresh["filters"] if f["key"] == "genre" for o in f["options"]] == ["Nuovo"]


# ── The endpoint ──────────────────────────────────────────────────────────────

@pytest.fixture
def signed_in(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return session_for(client, user.id)


def test_the_endpoint_passes_the_filters_through(client, signed_in, monkeypatch):
    from app.routers import archive as router
    seen = {}

    def fake_browse(source, host, raw, page_no):
        seen.update(source=source, host=host, raw=raw, page=page_no)
        return {"items": [], "page": page_no, "has_more": False, "total": 0}

    monkeypatch.setattr(router, "configured_domain", lambda: "trusted.example")
    monkeypatch.setattr(router.archive, "browse", fake_browse)

    res = client.get("/api/archive?genre=4&genre=7&year=2020&page=2")

    assert res.status_code == 200
    assert seen["raw"] == {"genre": ["4", "7"], "year": ["2020"]}
    assert seen["page"] == 2
    # Never from the caller: it decides where the request goes.
    assert seen["host"] == "trusted.example"


def test_a_bad_value_is_a_422_not_a_forward(client, signed_in, monkeypatch):
    from app.routers import archive as router
    monkeypatch.setattr(router, "configured_domain", lambda: "trusted.example")
    monkeypatch.setattr(router.archive, "definition",
                        lambda source, host: {"filters": [{"key": "sort", "label": "Ordina",
                                                           "kind": "select",
                                                           "options": [{"value": "name", "name": "N"}]}],
                                              "defaults": {}})

    assert client.get("/api/archive?sort=evil").status_code == 422


def test_streamingcommunity_needs_a_configured_domain(client, signed_in, monkeypatch):
    from app.routers import archive as router
    monkeypatch.setattr(router, "configured_domain", lambda: "")

    assert client.get("/api/archive").status_code == 409
    assert client.get("/api/archive/filters").status_code == 409


def test_animeunity_needs_no_domain(client, signed_in, monkeypatch):
    from app.routers import archive as router
    monkeypatch.setattr(router, "configured_domain", lambda: "")
    monkeypatch.setattr(router.archive, "definition", lambda source, host: {"filters": [], "defaults": {}})

    assert client.get("/api/archive/filters?source=animeunity").status_code == 200


def test_an_unknown_source_is_refused(client, signed_in):
    assert client.get("/api/archive?source=elsewhere").status_code == 422
