"""Stable identity resolution for source items and daily briefings."""

from __future__ import annotations

from hashlib import sha256
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def briefing_id(category: str, publication_date: str) -> str:
    """Return the only allowed identity for a category/date briefing."""

    return f"{category}-{publication_date}"


def canonicalize_source_url(url: str) -> str:
    """Canonicalize a public URL for deterministic URL-derived identities.

    Fragments and common analytics parameters are not publication identity.
    Other query parameters are retained and sorted because they may identify a
    real article endpoint.
    """

    parsed = urlsplit(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {"fbclid", "gclid", "mc_cid", "mc_eid"}
    ]
    hostname = (parsed.hostname or "").lower()
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{hostname}:{parsed.port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit(
        (parsed.scheme.lower(), netloc, path, urlencode(sorted(query)), "")
    )


def source_namespace(source_name: str) -> str:
    """Return a stable machine namespace from a configured source key.

    Source names are configuration identifiers in the current adapter API,
    but callers may spell separators differently.  Removing separators makes
    ``OpenReview``, ``Open Review`` and ``openreview`` one namespace.  arXiv
    source aliases share the globally stable ``arxiv`` namespace.
    """

    normalized = re.sub(r"[^a-z0-9]", "", source_name.lower())
    if normalized.startswith("arxiv"):
        return "arxiv"
    return normalized or "source"


def resolve_item_id(
    *,
    source_name: str,
    source_url: str,
    external_id: Optional[str] = None,
    explicit_id: Optional[str] = None,
) -> str:
    """Resolve a stable id without using title, summary, or timestamps.

    Explicit ids are trusted as already-canonical input and validated later.
    Known source identities get readable ids where that is unambiguous.  All
    other stable external identities and URLs use a SHA-256 digest of that
    stable identity, never of mutable content.
    """

    if explicit_id:
        return explicit_id

    namespace = source_namespace(source_name)
    stable_external = (external_id or "").strip()
    source_lower = source_name.lower()
    if stable_external and "arxiv" in source_lower:
        arxiv_id = stable_external.rsplit("/", 1)[-1]
        if re.fullmatch(r"\d{4}\.\d{4,5}(?:v\d+)?", arxiv_id):
            return f"arxiv-{arxiv_id}"

    if stable_external:
        digest = sha256(
            f"external:{namespace}:{stable_external}".encode("utf-8")
        ).hexdigest()
        return f"{namespace}-{digest[:24]}"

    canonical_url = canonicalize_source_url(source_url)
    digest = sha256(f"url:{namespace}:{canonical_url}".encode("utf-8")).hexdigest()
    return f"{namespace}-{digest[:24]}"
