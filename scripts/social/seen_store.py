"""Social item identity and seen-state management.

Uses stable `platform:item_id` canonical identity instead of URL,
with a global cross-source dedup layer and a per-source layer.
"""

import json
import pathlib
from datetime import datetime, timedelta
from typing import Any

from scripts.social.models import Item, SocialItem

_STATE_DIR = pathlib.Path.home() / ".myagentdata" / "dailyinfo" / "state"


# ---------------------------------------------------------------------------
# Global social seen store (cross-source dedup)
# ---------------------------------------------------------------------------

class SocialSeenStore:
    """Persistent global seen-state for all social items.

    Prevents re-processing the same tweet across multiple sources
    (e.g. x_ai_researchers vs x_ai_search).
    """

    def __init__(self, path: pathlib.Path | None = None):
        self.path = path or (_STATE_DIR / "social_seen.json")
        self._data: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {"version": 1, "items": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and "items" in raw:
                return raw
            return {"version": 1, "items": raw if isinstance(raw, dict) else {}}
        except Exception:
            return {"version": 1, "items": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def is_seen(self, canonical_id: str) -> bool:
        """Check if item has been seen before."""
        return canonical_id in self._data.get("items", {})

    def record(self, canonical_id: str, item: SocialItem | Item) -> None:
        """Record item as seen.

        Stores metadata for future pruning/debugging.
        """
        items = self._data.setdefault("items", {})
        if canonical_id not in items:
            items[canonical_id] = {
                "first_seen": datetime.utcnow().isoformat(),
                "published_at": (
                    item.published_at.isoformat()
                    if isinstance(item, SocialItem)
                    else item.extra.get("published_at", "")
                ),
                "source": item.extra.get("source_ref", "unknown") if isinstance(item, Item) else getattr(item, "source_ref", ""),
                "url": item.url,
            }

    def prune(self, max_age_days: int = 30) -> int:
        """Remove entries older than max_age_days. Returns count removed."""
        cutoff = datetime.utcnow() - timedelta(days=max_age_days)
        items = self._data.get("items", {})
        to_remove = []

        for cid, meta in items.items():
            try:
                first_seen = datetime.fromisoformat(meta.get("first_seen", ""))
                if first_seen < cutoff:
                    to_remove.append(cid)
            except (ValueError, TypeError):
                to_remove.append(cid)

        for cid in to_remove:
            del items[cid]

        if to_remove:
            self._save()

        return len(to_remove)

    def commit_items(self, items: list[Item]) -> None:
        """Record multiple items as seen."""
        for item in items:
            canonical = item.extra.get("canonical_id")
            if canonical:
                self.record(canonical, item)


# ---------------------------------------------------------------------------
# Per-source identity resolution
# ---------------------------------------------------------------------------

def stable_social_identity(item: Item) -> str:
    """Return stable identity for a social Item.

    Priority:
    1. canonical_id from extra (set by SocialDataSource)
    2. URL as fallback (for non-social items)
    """
    canonical = item.extra.get("canonical_id")
    if canonical:
        return canonical

    url = item.url
    if url:
        # Normalize URL
        url = _normalize_social_url(url)
        return url

    # Last resort: title + date (fragile but better than nothing)
    return f"title:{item.title}:{item.date}"


def _normalize_social_url(url: str) -> str:
    """Normalize social URLs: strip scheme, www, trailing slash."""
    import re

    url = url.strip()
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    url = url.rstrip("/")
    # x.com and twitter.com are the same
    url = re.sub(r"^(x|twitter)\.com", "x.com", url)
    return url


# ---------------------------------------------------------------------------
# SocialDataSource override for DataSource
# ---------------------------------------------------------------------------

class SocialDataSourceMixin:
    """Mixin providing stable identity for social sources."""

    def item_identity(self, item: Item) -> str:
        """Use canonical_id for social items."""
        return stable_social_identity(item)
