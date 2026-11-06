"""AnimeUnity's address, read from a public Telegram post.

A public channel's post is a web page anyone can GET, so there is nothing to log
in to. What is worth pinning is what is taken out of that page, that a post
which cannot be read leaves the TLD probe to do its job, and that a host which
came from the post is still only a candidate behind the same guard.
"""

import json
from types import SimpleNamespace

import pytest

from app import config
from app.core import animeunity, announcement, domain_recovery

# What the live single-post link answers: no message widget, the text in the
# page's description.
SINGLE_POST = """<html><head><title>Telegram: View @indirizziaggiornatieurostreaming</title>
<meta property="og:description" content="ANIME

AnimeUnity
https://www.animeunity.co/

AnimeWorld
https://www.animeworld.ac

MangaWorld
https://www.mangaworld.mx"></head><body></body></html>"""

# What the feed layout answers: the message widget, with a link whose visible
# text says something else.
WIDGET = """<html><body><div class="tgme_widget_message_text">
Nuovo sito <a href="https://www.animeunity.so/">clicca qui</a></div></body></html>"""


class _Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def post(monkeypatch):
    state = {"html": SINGLE_POST, "status": 200, "calls": []}

    def fake_get(url, *args, **kwargs):
        state["calls"].append(url)
        return _Response(state["html"], state["status"])

    monkeypatch.setattr(announcement.requests, "get", fake_get)
    return state


# ── Reading the post ──────────────────────────────────────────────────────────

def test_the_host_is_read_from_the_pages_description(post):
    assert announcement.fetch_hosts() == ["www.animeunity.co"]


def test_the_other_sites_listed_beside_it_are_not_candidates(post):
    assert "www.animeworld.ac" not in announcement.fetch_hosts()


def test_a_link_hidden_behind_other_text_is_found(post):
    post["html"] = WIDGET

    assert announcement.fetch_hosts() == ["www.animeunity.so"]


def test_a_bare_name_without_a_scheme_is_found():
    assert announcement.extract_hosts("Ci siamo spostati su animeunity.cc") == ["animeunity.cc"]


def test_a_host_named_twice_is_listed_once():
    assert announcement.extract_hosts("animeunity.so e https://animeunity.so/x") == ["animeunity.so"]


def test_a_post_that_names_nothing_gives_nothing(post):
    post["html"] = "<html><head></head><body>Torneremo presto</body></html>"

    assert announcement.fetch_hosts() == []


def test_it_reads_the_configured_post(post):
    announcement.fetch_hosts()

    assert post["calls"] == [announcement.ANNOUNCEMENT_URL]


def test_an_address_that_is_not_https_is_refused(post):
    with pytest.raises(ValueError):
        announcement.fetch_hosts("http://t.me/s/x/1")

    assert post["calls"] == []


def test_an_unreadable_page_raises(post):
    post["status"] = 500

    with pytest.raises(RuntimeError):
        announcement.fetch_hosts()


# ── Where it meets the recovery check ─────────────────────────────────────────

@pytest.fixture
def broken_host(monkeypatch, tmp_path):
    """The configured host is dead; .so and .to answer as the site."""
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({"domain": "example.test"}), encoding="utf-8")
    monkeypatch.setattr(config, "DATA_FILE", str(data_file))
    domain_recovery.clear_pending_animeunity()
    domain_recovery._last_au_check_at = None

    page = ('<html><head><meta name="csrf-token" content="t"></head><body>animeunity'
            '<layout-items items-json="{}"></layout-items></body></html>')
    served = {"www.animeunity.so", "www.animeunity.to"}

    class _Scraper:
        def get(self, url, **kwargs):
            if url.split("/")[2] not in served:
                raise RuntimeError("down")
            return SimpleNamespace(text=page, status_code=200, raise_for_status=lambda: None)

    def resolve(host, *a, **k):
        if host in served or host.endswith(".evil.tld"):
            return [(2, 1, 6, "", ("93.184.216.34", 443))]
        raise OSError("Name or service not known")

    monkeypatch.setattr(animeunity, "_get_scraper", lambda: _Scraper())
    monkeypatch.setattr(domain_recovery.socket, "getaddrinfo", resolve)
    yield data_file
    domain_recovery.clear_pending_animeunity()
    domain_recovery._last_au_check_at = None


def test_the_announced_host_is_preferred_over_guessing(broken_host, monkeypatch):
    # .to would win a plain TLD walk; the announcement names .so.
    monkeypatch.setattr(announcement, "fetch_hosts", lambda url=None: ["www.animeunity.so"])

    result = domain_recovery.run_check_animeunity()

    assert result["via"] == "announcement"
    assert result["candidate"] == "www.animeunity.so"


def test_an_unreadable_post_falls_back_to_the_tld_list(broken_host, monkeypatch):
    def down(url=None):
        raise RuntimeError("t.me is unreachable")

    monkeypatch.setattr(announcement, "fetch_hosts", down)

    result = domain_recovery.run_check_animeunity()

    assert result["via"] == "tld"
    assert result["candidate"] is not None


def test_a_post_naming_nothing_falls_back_too(broken_host, monkeypatch):
    monkeypatch.setattr(announcement, "fetch_hosts", lambda url=None: [])

    assert domain_recovery.run_check_animeunity()["via"] == "tld"


def test_the_host_the_panel_already_uses_is_not_a_candidate(broken_host, monkeypatch):
    """The post usually names the address that is already configured."""
    monkeypatch.setattr(config, "configured_animeunity_host", lambda: "www.animeunity.xyz")
    monkeypatch.setattr(announcement, "fetch_hosts", lambda url=None: ["www.animeunity.xyz"])

    result = domain_recovery.run_check_animeunity()

    assert result["via"] == "tld"


def test_a_host_from_the_post_is_still_behind_the_guard(broken_host, monkeypatch):
    """The post is text anyone with the channel can edit. It says where to look;
    it does not decide what is adopted."""
    monkeypatch.setattr(announcement, "fetch_hosts",
                        lambda url=None: ["www.animeunity.evil.tld"])

    result = domain_recovery.run_check_animeunity()

    assert result["candidate"] != "www.animeunity.evil.tld"
    assert {"host": "www.animeunity.evil.tld", "reason": "non è un dominio di secondo livello"} \
        in result["rejected"]


def test_a_host_from_the_post_is_proposed_not_adopted(broken_host, monkeypatch):
    monkeypatch.setattr(announcement, "fetch_hosts", lambda url=None: ["www.animeunity.so"])

    domain_recovery.run_check_animeunity()

    assert domain_recovery.pending_animeunity()["host"] == "www.animeunity.so"
    assert "animeunity_domain" not in json.loads(broken_host.read_text(encoding="utf-8"))
