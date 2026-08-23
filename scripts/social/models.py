"""Core data models for social intelligence pipeline."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow_naive() -> datetime:
    """Return current UTC time as naive datetime.

    Replaces deprecated datetime.utcnow() removed in Python 3.13.
    Returns naive datetime (no tzinfo) to maintain backward compatibility
    with existing code that expects naive datetimes.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass
class CommandResult:
    """Result from a subprocess call to Agent-Reach backend."""

    stdout: str
    stderr: str
    returncode: int
    backend: str
    elapsed_ms: int


@dataclass
class ReachCapabilities:
    """Agent-Reach capability snapshot from `doctor --json`."""

    agent_reach_version: str | None
    twitter_available: bool
    twitter_backend: str | None
    opencli_available: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class SocialItem:
    """Normalized social media item from any platform."""

    platform: str
    item_id: str

    author_name: str
    author_handle: str

    text: str
    url: str
    published_at: datetime

    # Engagement metrics (platform-specific)
    likes: int | None = None
    reposts: int | None = None
    replies: int | None = None
    quotes: int | None = None

    # Metadata
    backend: str = ""
    source_mode: str = ""
    source_ref: str = ""
    raw_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def canonical_id(self) -> str:
        """Stable identity: platform + native item ID."""
        return f"{self.platform}:{self.item_id}"

    def to_dailyinfo_item(self, lookup_date: str) -> "Item":
        """Convert to DailyInfo Item for downstream pipeline."""
        # Build title: truncated text with author handle
        text = _collapse_whitespace(self.text)
        if len(text) > 100:
            text = text[:97] + "..."
        title = f"{self.author_handle}: {text}"

        # Format published date
        pub_date = self.published_at.strftime("%Y-%m-%d")

        # Build engagement string for extra metadata
        engagement = {}
        if self.likes is not None:
            engagement["likes"] = self.likes
        if self.reposts is not None:
            engagement["reposts"] = self.reposts
        if self.replies is not None:
            engagement["replies"] = self.replies
        if self.quotes is not None:
            engagement["quotes"] = self.quotes

        extra = {
            "canonical_id": self.canonical_id,
            "platform": self.platform,
            "item_id": self.item_id,
            "author": self.author_name,
            "handle": self.author_handle,
            "published_at": self.published_at.isoformat(),
            "backend": self.backend,
            "source_mode": self.source_mode,
            "source_ref": self.source_ref,
        }
        if engagement:
            extra["engagement"] = engagement

        return Item(
            title=title,
            date=pub_date,
            url=self.url,
            content=self.text,
            extra=extra,
        )


@dataclass
class Item:
    """Lightweight DailyInfo item (mirrors datasource.Item)."""

    title: str
    date: str
    url: str = ""
    content: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _collapse_whitespace(text: str) -> str:
    """Collapse multiple whitespace characters into single spaces."""
    import re

    text = re.sub(r"\s+", " ", text)
    return text.strip()
