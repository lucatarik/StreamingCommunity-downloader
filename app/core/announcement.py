"""AnimeUnity's current address, read from a public Telegram post.

A public channel's posts are readable without an account. The embed form of a
post's link (``?embed=1&mode=tme&userpic=0``) is the lightest page Telegram
serves for it — the message widget and little else — so this needs no
credentials, no Telegram client and no login: a plain GET, like the page
StreamingCommunity's recovery reads.

The post is somebody else's text and is treated as such. It names where to look
and nothing more: every host it yields goes through ``domain_recovery``'s guard
and verification, and is proposed rather than adopted.
"""

import logging
import os
import re
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from app.core.headers import get_headers

logger = logging.getLogger(__name__)

# Overridable because the channel is somebody else's and may move or be
# replaced by a better one. Like DOMAIN_SOURCE_URL it is a constant with an env
# override and **must not become a settings field**: it decides what the panel
# reads, and the host list it yields is only ever a list of candidates.
ANNOUNCEMENT_URL = os.getenv(
    "ANIMEUNITY_ANNOUNCEMENT_URL",
    "https://t.me/indirizziaggiornatieurostreaming/4940?embed=1&mode=tme&userpic=0",
)

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_BARE = re.compile(r"\b(?:www\.)?animeunity\.[a-z]{2,}\b", re.IGNORECASE)


def extract_hosts(text: str) -> list[str]:
    """Hosts in *text* that carry the name, in order, deduplicated.

    Only the name is looked for here, not whether the host is acceptable: that is
    the guard's job and it is stricter. This only keeps the channel's own links,
    and the other sites listed beside AnimeUnity, from being reported as refused
    candidates.
    """
    hosts: list[str] = []

    def add(host: str) -> None:
        host = host.lower().strip(".")
        if "animeunity" in host and host not in hosts:
            hosts.append(host)

    for url in _URL.findall(text or ""):
        add(urlparse(url).hostname or "")
    for match in _BARE.findall(text or ""):
        add(match)
    return hosts


def _page_text(html: str) -> str:
    """Everything on the page that can hold the address.

    The post's text, the targets of links whose visible text is something else,
    and the page's own description — which is what a single-post link answers
    with when it does not render the message widget.
    """
    soup = BeautifulSoup(html, "lxml")
    parts: list[str] = []
    for node in soup.select(".tgme_widget_message_text"):
        parts.append(node.get_text(" ", strip=True))
        parts.extend(a["href"] for a in node.select("a[href]"))
    for prop in ("og:description", "twitter:description"):
        meta = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if meta and meta.get("content"):
            parts.append(meta["content"])
    return "\n".join(parts)


def fetch_hosts(url: str | None = None) -> list[str]:
    """The AnimeUnity hosts the post names now. Raises on a page that cannot be read."""
    target = url or ANNOUNCEMENT_URL
    if urlparse(target).scheme != "https":
        raise ValueError("the announcement must be read over https")
    response = requests.get(target, headers={"user-agent": get_headers()}, timeout=10)
    response.raise_for_status()
    return extract_hosts(_page_text(response.text))
