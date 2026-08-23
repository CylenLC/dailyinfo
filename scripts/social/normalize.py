"""Parse raw backend output into normalized SocialItem instances.

Supports:
- twitter-cli (JSON output from `twitter user-posts` and `twitter search`)
- OpenCLI (YAML/JSON output from `opencli twitter ...`)
"""

import json
import re
from datetime import datetime, timezone
from typing import Any

from scripts.social.models import SocialItem


# ---------------------------------------------------------------------------
# Twitter-CLI parsers
# ---------------------------------------------------------------------------

def _parse_twitter_user_posts(raw: str, *, backend: str) -> list[SocialItem]:
    """Parse `twitter user-posts -n N` JSON output."""
    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(data, list):
        return []

    items = []
    for tweet in data:
        item = _try_parse_twitter_tweet(tweet, backend=backend, mode="researcher_watch")
        if item:
            items.append(item)

    return items


def _parse_twitter_search(raw: str, *, backend: str) -> list[SocialItem]:
    """Parse `twitter search "query" -n N` JSON output."""
    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    if not isinstance(data, list):
        return []

    items = []
    for tweet in data:
        item = _try_parse_twitter_tweet(tweet, backend=backend, mode="search")
        if item:
            items.append(item)

    return items


def _try_parse_twitter_tweet(
    tweet: dict[str, Any],
    *,
    backend: str,
    mode: str,
) -> SocialItem | None:
    """Convert a single twitter-cli JSON tweet dict to SocialItem."""
    if not tweet:
        return None

    tweet_id = str(tweet.get("id_str") or tweet.get("id") or "")
    if not tweet_id:
        return None

    # Author
    user = tweet.get("user", {})
    author_name = str(user.get("name") or tweet.get("author_name") or "Unknown")
    author_handle = str(
        user.get("screen_name")
        or tweet.get("author_handle")
        or ""
    )
    if author_handle and not author_handle.startswith("@"):
        author_handle = f"@{author_handle}"

    # Text
    text = str(
        tweet.get("full_text")
        or tweet.get("text")
        or tweet.get("content")
        or ""
    )

    # URL
    url = str(
        tweet.get("url")
        or tweet.get("permalink")
        or tweet.get("link")
        or f"https://x.com/i/status/{tweet_id}"
    )

    # Timestamp
    created_at = tweet.get("created_at") or tweet.get("published_at") or ""
    published_at = _parse_twitter_timestamp(created_at)

    # Engagement
    likes = _safe_int(tweet.get("favorite_count") or tweet.get("likes"))
    reposts = _safe_int(tweet.get("retweet_count") or tweet.get("reposts"))
    replies = _safe_int(tweet.get("reply_count") or tweet.get("replies"))
    quotes = _safe_int(tweet.get("quote_count") or tweet.get("quotes"))

    return SocialItem(
        platform="x",
        item_id=tweet_id,
        author_name=author_name,
        author_handle=author_handle,
        text=text,
        url=url,
        published_at=published_at,
        likes=likes,
        reposts=reposts,
        replies=replies,
        quotes=quotes,
        backend=backend,
        source_mode=mode,
        raw_metadata=tweet,
    )


def _parse_twitter_timestamp(ts: str) -> datetime:
    """Parse Twitter date formats like 'Mon Oct 12 12:34:56 +0000 2026'."""
    if not ts:
        return datetime.utcnow()

    # Try standard Twitter format
    patterns = [
        "%a %b %d %H:%M:%S %z %Y",
        "%a %b %d %H:%M:%S %Z %Y",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
    ]

    for pattern in patterns:
        try:
            dt = datetime.strptime(ts.strip(), pattern)
            # Convert to naive UTC
            if dt.tzinfo:
                dt = dt.replace(tzinfo=None)
            return dt
        except ValueError:
            continue

    # Fallback: now
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# OpenCLI parsers
# ---------------------------------------------------------------------------

def _parse_opencli_twitter(
    raw: str,
    *,
    backend: str,
    handle: str | None = None,
    query: str | None = None,
) -> list[SocialItem]:
    """Parse OpenCLI Twitter output (JSON or YAML)."""
    if not raw.strip():
        return []

    # Try JSON first
    try:
        data = json.loads(raw)
        return _parse_opencli_tweets_json(data, backend=backend, handle=handle, query=query)
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: YAML (if pyyaml available)
    try:
        import yaml

        data = yaml.safe_load(raw)
        if isinstance(data, list):
            return _parse_opencli_tweets_json(data, backend=backend, handle=handle, query=query)
    except ImportError:
        pass
    except Exception:
        pass

    return []


def _parse_opencli_tweets_json(
    data: list[dict[str, Any]],
    *,
    backend: str,
    handle: str | None,
    query: str | None,
) -> list[SocialItem]:
    """Normalize OpenCLI tweet list."""
    items = []

    for tweet in data:
        if not isinstance(tweet, dict):
            continue

        # Detect mode from context
        mode = "researcher_watch" if handle else "search"

        item = _try_parse_opencli_tweet(tweet, backend=backend, mode=mode)
        if item:
            items.append(item)

    return items


def _try_parse_opencli_tweet(
    tweet: dict[str, Any],
    *,
    backend: str,
    mode: str,
) -> SocialItem | None:
    """Convert a single OpenCLI tweet dict to SocialItem."""
    tweet_id = str(tweet.get("id") or tweet.get("tweet_id") or tweet.get("id_str") or "")
    if not tweet_id:
        return None

    # Author
    author_name = str(tweet.get("author", {}).get("name") or tweet.get("author_name") or "Unknown")
    author_handle = str(
        tweet.get("author", {}).get("username")
        or tweet.get("author_handle")
        or tweet.get("screen_name")
        or ""
    )
    if author_handle and not author_handle.startswith("@"):
        author_handle = f"@{author_handle}"

    # Text
    text = str(tweet.get("text") or tweet.get("content") or tweet.get("full_text") or "")

    # URL
    url = str(
        tweet.get("url")
        or tweet.get("permalink")
        or tweet.get("link")
        or f"https://x.com/i/status/{tweet_id}"
    )

    # Timestamp
    created_at = tweet.get("created_at") or tweet.get("published_at") or ""
    published_at = _parse_twitter_timestamp(created_at)

    # Engagement (flexible key names)
    likes = _safe_int(
        tweet.get("favorite_count")
        or tweet.get("likes")
        or tweet.get("like_count")
    )
    reposts = _safe_int(
        tweet.get("retweet_count")
        or tweet.get("retweets")
        or tweet.get("repost_count")
    )
    replies = _safe_int(tweet.get("reply_count") or tweet.get("replies"))
    quotes = _safe_int(tweet.get("quote_count") or tweet.get("quotes"))

    return SocialItem(
        platform="x",
        item_id=tweet_id,
        author_name=author_name,
        author_handle=author_handle,
        text=text,
        url=url,
        published_at=published_at,
        likes=likes,
        reposts=reposts,
        replies=replies,
        quotes=quotes,
        backend=backend,
        source_mode=mode,
        raw_metadata=tweet,
    )


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _safe_int(value: Any) -> int | None:
    """Convert to int or return None."""
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None
