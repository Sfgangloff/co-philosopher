"""Load ``data/journals.yaml`` into normalized records for embedding."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import yaml
from slugify import slugify

from cophilo.config import Config


@dataclass(frozen=True)
class Journal:
    slug: str
    name: str
    publisher: str | None
    scope: str | None
    typical_length: str | None
    max_words: int | None
    open_access: bool
    url: str | None
    issn: str | None

    @property
    def embedding_text(self) -> str:
        """The text a semantic query is matched against.

        Scope carries the topical signal; name and publisher disambiguate.
        Length/ISSN are metadata, not retrieval signal, so they are excluded.
        """
        parts = [self.name]
        if self.scope:
            parts.append(self.scope)
        if self.publisher:
            parts.append(f"Publisher: {self.publisher}")
        return ". ".join(parts)

    @property
    def content_hash(self) -> str:
        h = hashlib.sha1()
        h.update(self.embedding_text.encode("utf-8"))
        h.update(f"|oa={int(self.open_access)}|url={self.url or ''}".encode())
        return h.hexdigest()


def journals_path(cfg: Config):
    return cfg.data_dir / "journals.yaml"


def load_journals(cfg: Config) -> list[Journal]:
    path = journals_path(cfg)
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("journals") or []

    seen: set[str] = set()
    out: list[Journal] = []
    for e in entries:
        name = (e.get("name") or "").strip()
        if not name:
            continue
        slug = slugify(name)
        # Names are unique in practice; de-dupe defensively so the slug can
        # serve as a stable upsert key.
        if slug in seen:
            continue
        seen.add(slug)
        mw = e.get("max_words")
        out.append(
            Journal(
                slug=slug,
                name=name,
                publisher=(e.get("publisher") or None),
                scope=(e.get("scope") or None),
                typical_length=(e.get("typical_length") or None),
                max_words=int(mw) if isinstance(mw, int) else None,
                open_access=bool(e.get("open_access")),
                url=(e.get("url") or None),
                issn=(e.get("issn") or None),
            )
        )
    return out


def source_hash(records: list[Journal]) -> str:
    """A fingerprint of the whole catalog; changes iff a rebuild is needed."""
    h = hashlib.sha1()
    for r in sorted(records, key=lambda j: j.slug):
        h.update(r.slug.encode("utf-8"))
        h.update(r.content_hash.encode("utf-8"))
    return h.hexdigest()
