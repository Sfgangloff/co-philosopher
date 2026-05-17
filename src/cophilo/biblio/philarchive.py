"""PhilArchive search client.

PhilArchive exposes search results as an RSS 1.0 (RDF) feed at
``/s/<url-encoded query>?format=rss``. No API key is required and, unlike
philpapers.org, philarchive.org is not behind a bot challenge. We parse the
feed with the stdlib XML parser (numeric character references are decoded
for us) and normalize each item into a :class:`BiblioEntry`.

Feed item shape::

    <item rdf:about="https://philarchive.org/rec/LISFWD">
      <title>List, Christian: Free Will, Determinism, and ...</title>
      <link>https://philarchive.org/rec/LISFWD</link>
      <description>_Noûs_ 48 (1):156-178. 2014I argue that ...</description>
    </item>

``title`` is ``"<authors>: <work title>"``; ``description`` is an optional
``_journal_ vol(iss):pp. year`` preamble glued directly to the abstract.
"""

from __future__ import annotations

import re
from typing import Protocol
from urllib.parse import quote
from xml.etree import ElementTree as ET

import httpx

from cophilo.biblio.schemas import BiblioEntry
from cophilo.config import Config


class Fetcher(Protocol):
    """Minimal HTTP surface, injected so tests need no network."""

    def get(self, url: str) -> str: ...


class HttpxFetcher:
    def __init__(self, user_agent: str, timeout: float = 30.0) -> None:
        self._headers = {"User-Agent": user_agent, "Accept": "application/rss+xml, */*"}
        self._timeout = timeout

    def get(self, url: str) -> str:
        resp = httpx.get(
            url, headers=self._headers, timeout=self._timeout, follow_redirects=True
        )
        resp.raise_for_status()
        return resp.text


# --- parsing -------------------------------------------------------------

_AUTHOR_SPLIT_RE = re.compile(r"\s*;\s*|\s+&\s+")
# The citation year is glued directly to the abstract's first word
# (".. 2017This chapter .."), so we can't require a trailing word boundary.
# Anchor on a non-digit before a plausible 1500–2099 year instead.
_YEAR_RE = re.compile(r"(?<!\d)(1[5-9]\d{2}|20\d{2})(?!\d)")


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# PhilArchive appends a "(direct link)" anchor to many abstracts.
_DIRECT_LINK_RE = re.compile(r"\s*\(\s*direct link\s*\)\s*$", re.IGNORECASE)


def _local(tag: str) -> str:
    """Strip an XML namespace, returning the local tag name."""
    return tag.rsplit("}", 1)[-1]


def _clean(text: str) -> str:
    """Drop embedded HTML markup and collapse whitespace.

    PhilArchive descriptions sometimes append a ``<div><a>direct link</a></div>``
    to the abstract; titles/journals are plain but cleaned defensively.
    """
    text = _TAG_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    return _DIRECT_LINK_RE.sub("", text).strip()


def _parse_title(raw: str) -> tuple[list[str], str]:
    """``"List, Christian: Title"`` → (["List, Christian"], "Title").

    Author names use ``,`` / ``;`` / ``&`` but never ``": "``, so the first
    ``": "`` reliably separates the author list from the work title (which
    may itself contain colons).
    """
    raw = _clean(raw)
    head, sep, tail = raw.partition(": ")
    if not sep:
        return [], raw
    authors = [a.strip() for a in _AUTHOR_SPLIT_RE.split(head) if a.strip()]
    return authors, tail.strip()


def _parse_description(raw: str) -> tuple[str | None, int | None, str | None]:
    """Split an optional ``_journal_ … year`` preamble from the abstract."""
    text = _clean(raw or "")
    if not text:
        return None, None, None

    journal: str | None = None
    rest = text
    if text.startswith("_"):
        end = text.find("_", 1)
        if end != -1:
            journal = text[1:end].strip() or None
            rest = text[end + 1 :].lstrip(" .")

    year: int | None = None
    abstract = rest
    # The citation year, when present, sits in the first ~60 chars right
    # before the abstract (e.g. "48 (1):156-178. 2014I argue that...").
    m = _YEAR_RE.search(rest[:60])
    if m and (journal is not None or m.start() <= 20):
        year = int(m.group(1))
        abstract = rest[m.end() :].lstrip(" .—-:")

    abstract = abstract.strip() or None
    return journal, year, abstract


def _external_id(link: str) -> str:
    return link.rstrip("/").rsplit("/", 1)[-1]


def parse_feed(xml_text: str) -> list[BiblioEntry]:
    """Parse a PhilArchive RSS feed into normalized entries."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:  # pragma: no cover - surfaced to caller
        raise ValueError(f"PhilArchive returned unparseable XML: {e}") from e

    entries: list[BiblioEntry] = []
    for el in root.iter():
        if _local(el.tag) != "item":
            continue
        fields: dict[str, str] = {}
        for child in el:
            name = _local(child.tag)
            if name in {"title", "link", "description"} and child.text:
                fields[name] = child.text
        link = fields.get("link", "").strip()
        title_raw = fields.get("title", "").strip()
        if not link or not title_raw:
            continue
        authors, title = _parse_title(title_raw)
        journal, year, abstract = _parse_description(fields.get("description", ""))
        entries.append(
            BiblioEntry(
                source="philarchive",
                external_id=_external_id(link),
                title=title,
                authors=authors,
                journal=journal,
                year=year,
                abstract=abstract,
                url=link,
            )
        )
    return entries


# --- public API ----------------------------------------------------------


def search_url(cfg: Config, query: str) -> str:
    return f"{cfg.philarchive_base_url}/s/{quote(query.strip(), safe='')}?format=rss"


def search(
    cfg: Config,
    query: str,
    *,
    limit: int = 25,
    fetcher: Fetcher | None = None,
) -> list[BiblioEntry]:
    """Search PhilArchive for ``query``; return up to ``limit`` entries.

    ``fetcher`` is injectable so tests can avoid the network.
    """
    query = query.strip()
    if not query:
        raise ValueError("query must be non-empty")
    if fetcher is None:
        fetcher = HttpxFetcher(cfg.http_user_agent)
    xml_text = fetcher.get(search_url(cfg, query))
    entries = parse_feed(xml_text)
    return entries[: max(0, limit)]
