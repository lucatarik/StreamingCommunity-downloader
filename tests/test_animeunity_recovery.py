"""AnimeUnity's host is recovered the way StreamingCommunity's domain is: found,
checked, and *proposed*.

What is different is where candidates come from. StreamingCommunity has a page
that publishes its current address; AnimeUnity has none, so the candidates are
the one name it has always used under the TLDs it has been seen on. That makes
the guard the whole of the safety story, and the cases worth pinning are the
ones that try to get around it: a different name, a different depth, and a
parked page that says "animeunity" but is not the site.
"""

import json

import pytest

from app import config
from app.auth.permissions import ALL_PERMISSIONS
from app.core import animeunity, domain_recovery, home
from tests.conftest import do_setup, make_user, session_for


@pytest.fixture(autouse=True)
def _reset_module_state():
    domain_recovery.clear_pending_animeunity()
    domain_recovery._last_au_check_at = None
    yield
    domain_recovery.clear_pending_animeunity()
    domain_recovery._last_au_check_at = None


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.setattr(
        domain_recovery.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))])


class _Page:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


REAL_PAGE = ('<html><head><meta name="csrf-token" content="t"><title>AnimeUnity</title></head>'
             '<body><layout-items items-json="{}"></layout-items></body></html>')
PARKED_PAGE = "<html><head><title>animeunity.xyz is for sale</title></head></html>"


@pytest.fixture
def broken_host(monkeypatch, tmp_path, public_dns):
    """The configured host is dead; one of the usual names answers as the site."""
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"domain": "example.test"}), encoding="utf-8")
    monkeypatch.setattr(config, "DATA_FILE", str(data_file))

    pages = {"www.animeunity.to": REAL_PAGE}

    class _Scraper:
        def get(self, url, **kwargs):
            host = url.split("/")[2]
            if host not in pages:
                raise RuntimeError("Cannot reach")
            return _Page(pages[host])

    monkeypatch.setattr(animeunity, "_get_scraper", lambda: _Scraper())
    # A name that does not exist fails to resolve, as it does for real. Only the
    # ones the fake site serves exist.
    def resolve(host, *a, **k):
        if host not in pages:
            raise OSError("Name or service not known")
        return [(2, 1, 6, "", ("93.184.216.34", 443))]

    monkeypatch.setattr(domain_recovery.socket, "getaddrinfo", resolve)
    return {"pages": pages, "data_file": data_file}


def _stored(data_file):
    return json.loads(data_file.read_text(encoding="utf-8"))


# ── The guard ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", [
    "www.animeunity.to",
    "animeunity.so",
    "www.animeunity.xyz",
])
def test_the_usual_name_under_any_tld_has_a_plausible_shape(host):
    assert domain_recovery._check_shape(host, domain_recovery.AU_NAME_PATTERN) is None


@pytest.mark.parametrize("host", [
    # The part that decides where the traffic lands is the attacker's.
    "animeunity.attacker.tld",
    "www.animeunity.attacker.tld",
    # Another source's name is not AnimeUnity's.
    "streamingcommunityz.rodeo",
    "animeunity-free.to",
    "evil.to",
])
def test_anything_else_is_refused(host):
    assert domain_recovery._check_shape(host, domain_recovery.AU_NAME_PATTERN) is not None


def test_the_streamingcommunity_guard_still_refuses_animeunitys_name():
    """Each source has its own name; neither may be adopted as the other."""
    assert domain_recovery._check_shape("www.animeunity.to") is not None


def test_a_parked_page_that_says_the_name_is_not_the_site(monkeypatch):
    """hosts.verify_animeunity would accept this — it looks only for the word.
    A host found by guessing has to show what the real page is made of."""
    class _Scraper:
        def get(self, url, **kwargs):
            return _Page(PARKED_PAGE)

    monkeypatch.setattr(animeunity, "_get_scraper", lambda: _Scraper())

    assert domain_recovery.verify_animeunity("www.animeunity.xyz") is False


def test_a_page_with_the_markers_is_the_site(monkeypatch):
    class _Scraper:
        def get(self, url, **kwargs):
            return _Page(REAL_PAGE)

    monkeypatch.setattr(animeunity, "_get_scraper", lambda: _Scraper())

    assert domain_recovery.verify_animeunity("www.animeunity.to") is True


# ── The cycle ─────────────────────────────────────────────────────────────────

def test_a_working_host_is_left_alone(broken_host, monkeypatch):
    monkeypatch.setattr(config, "configured_animeunity_host", lambda: "www.animeunity.to")

    result = domain_recovery.run_check_animeunity()

    assert result["current_ok"] is True
    assert result["candidate"] is None
    assert domain_recovery.pending_animeunity() is None


def test_forcing_a_check_does_not_go_looking_for_a_second_host(broken_host, monkeypatch):
    monkeypatch.setattr(config, "configured_animeunity_host", lambda: "www.animeunity.to")
    broken_host["pages"]["www.animeunity.tv"] = REAL_PAGE

    result = domain_recovery.run_check_animeunity(force=True)

    assert result["candidate"] is None


def test_a_candidate_is_proposed_and_not_written(broken_host):
    result = domain_recovery.run_check_animeunity()

    assert result["candidate"] == "www.animeunity.to"
    assert result["applied"] is False
    assert domain_recovery.pending_animeunity()["host"] == "www.animeunity.to"
    # The whole point: proposing is not adopting.
    assert "animeunity_domain" not in _stored(broken_host["data_file"])


def test_a_candidate_that_resolves_but_is_not_the_site_is_reported_not_proposed(broken_host):
    broken_host["pages"]["www.animeunity.to"] = PARKED_PAGE

    result = domain_recovery.run_check_animeunity()

    assert result["candidate"] is None
    assert result["rejected"] == [{"host": "www.animeunity.to", "reason": "non verificato"}]


def test_names_that_do_not_exist_are_not_worth_reporting(broken_host):
    result = domain_recovery.run_check_animeunity()

    assert result["rejected"] == []


def test_a_candidate_resolving_to_a_private_address_is_rejected(broken_host, monkeypatch):
    monkeypatch.setattr(
        domain_recovery.socket, "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("192.168.1.10", 443))])

    result = domain_recovery.run_check_animeunity()

    assert result["candidate"] is None
    assert {"host": "www.animeunity.to", "reason": "risolve a un indirizzo non pubblico"} \
        in result["rejected"]


def test_auto_apply_writes_the_host_and_forgets_what_the_old_one_taught(broken_host):
    config.save_settings({**config.get_settings(), "domain_auto_apply": True})
    animeunity._csrf = (10 ** 12, "token-from-the-old-host")
    home._store(("animeunity", "www.animeunity.co"), [{"key": "x"}])

    result = domain_recovery.run_check_animeunity()

    assert result["applied"] is True
    assert _stored(broken_host["data_file"])["animeunity_domain"] == "www.animeunity.to"
    assert domain_recovery.pending_animeunity() is None
    assert animeunity._csrf is None
    assert home._cached(("animeunity", "www.animeunity.co")) is None


def test_a_check_inside_the_throttle_window_does_nothing(broken_host):
    domain_recovery.run_check_animeunity()
    second = domain_recovery.run_check_animeunity()

    assert second["checked"] is False


def test_the_two_sources_do_not_share_a_throttle(broken_host):
    domain_recovery._last_check_at = domain_recovery._now()

    assert domain_recovery.run_check_animeunity()["checked"] is True


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _csrf(admin):
    return {"X-CSRF-Token": admin[1]}


@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return user, session_for(client, user.id)


def test_the_candidate_endpoint_reports_both_sources_apart(client, admin):
    domain_recovery._set_pending({"host": "streamingcommunityz.rodeo", "version": "v9",
                                  "found_at": 0})
    domain_recovery._set_pending_animeunity({"host": "www.animeunity.to", "found_at": 0})

    body = client.get("/api/domain/candidate").json()

    assert body["candidate"]["host"] == "streamingcommunityz.rodeo"
    assert body["animeunity_candidate"]["host"] == "www.animeunity.to"
    domain_recovery.clear_pending()


def test_applying_the_animeunity_candidate_writes_it(client, admin, broken_host):
    domain_recovery._set_pending_animeunity({"host": "www.animeunity.to", "found_at": 0})

    res = client.post("/api/domain/candidate/apply", headers=_csrf(admin),
                      json={"domain": "www.animeunity.to", "source": "animeunity"})

    assert res.status_code == 200
    assert _stored(broken_host["data_file"])["animeunity_domain"] == "www.animeunity.to"


def test_the_host_in_the_body_is_a_token_not_an_instruction(client, admin, broken_host):
    domain_recovery._set_pending_animeunity({"host": "www.animeunity.to", "found_at": 0})

    res = client.post("/api/domain/candidate/apply", headers=_csrf(admin),
                      json={"domain": "attacker.example", "source": "animeunity"})

    assert res.status_code == 409
    assert "animeunity_domain" not in _stored(broken_host["data_file"])


def test_a_streamingcommunity_candidate_is_not_applied_as_animeunitys(client, admin, broken_host):
    domain_recovery._set_pending({"host": "streamingcommunityz.rodeo", "version": "v9",
                                  "found_at": 0})

    res = client.post("/api/domain/candidate/apply", headers=_csrf(admin),
                      json={"domain": "streamingcommunityz.rodeo", "source": "animeunity"})

    assert res.status_code == 409
    domain_recovery.clear_pending()


def test_dismissing_clears_only_the_named_source(client, admin):
    domain_recovery._set_pending({"host": "streamingcommunityz.rodeo", "version": "v9",
                                  "found_at": 0})
    domain_recovery._set_pending_animeunity({"host": "www.animeunity.to", "found_at": 0})

    res = client.post("/api/domain/candidate/dismiss?source=animeunity", headers=_csrf(admin))

    assert res.status_code == 200
    assert domain_recovery.pending_animeunity() is None
    assert domain_recovery.pending() is not None
    domain_recovery.clear_pending()
