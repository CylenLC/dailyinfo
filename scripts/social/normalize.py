"""Parse raw backend output into normalized SocialItem instances.

Supports:
- twitter-cli (JSON output from `twitter user-posts` and `twitter search`)
- OpenCLI (YAML/JSON output from `opencli twitter ...`)
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from social.models import SocialItem, utcnow_naive

# ---------------------------------------------------------------------------
# Twitter-CLI parsers
# ---------------------------------------------------------------------------


def _parse_twitter_user_posts(raw: str, *, backend: str) -> list[SocialItem]:
    """Parse `twitter user-posts -n N` JSON output (compact or full format)."""
    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Fallback: try YAML parsing for non-compact output
        try:
            import yaml

            parsed = yaml.safe_load(raw)
            if isinstance(parsed, dict) and "data" in parsed:
                data = parsed["data"]
            else:
                data = parsed
        except ImportError:
            return []
        except Exception:
            return []

    # Handle both formats:
    # Compact: [{"id": "...", "author": "@user", "text": "...", ...}]
    # Full:    [{"id_str": "...", "user": {"name": "...", "screen_name": "..."}, "full_text": "...", ...}]
    if isinstance(data, dict) and "data" in data:
        # YAML format: {"ok": true, "data": [...]}
        tweets = data["data"]
    elif isinstance(data, list):
        tweets = data
    else:
        return []

    if not isinstance(tweets, list):
        return []

    items = []
    for tweet in tweets:
        if not isinstance(tweet, dict):
            continue

        item = _try_parse_twitter_tweet(tweet, backend=backend, mode="researcher_watch")
        if item:
            items.append(item)

    return items


def _parse_twitter_search(raw: str, *, backend: str) -> list[SocialItem]:
    """Parse `twitter search "query" -n N` JSON output (compact or full format)."""
    if not raw.strip():
        return []

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        # Fallback: try YAML parsing for non-compact output
        try:
            import yaml

            parsed = yaml.safe_load(raw)
            if isinstance(parsed, dict) and "data" in parsed:
                data = parsed["data"]
            else:
                data = parsed
        except ImportError:
            return []
        except Exception:
            return []

    # Handle both formats
    if isinstance(data, dict) and "data" in data:
        # YAML format: {"ok": true, "data": [...]}
        tweets = data["data"]
    elif isinstance(data, list):
        tweets = data
    else:
        return []

    if not isinstance(tweets, list):
        return []

    items = []
    for tweet in tweets:
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
    """Convert a single twitter-cli JSON tweet dict to SocialItem.

    Supports both compact format (twitter -c) and full format:
    - Compact: {"id": "...", "author": "@user", "text": "...", "likes": N, "rts": N, "time": "..."}
    - Full:    {"id_str": "...", "id": ..., "user": {"name": "...", "screen_name": "..."}, "full_text": "...", ...}
    """
    if not tweet:
        return None

    # Detect format and extract ID
    tweet_id = str(tweet.get("id_str") or tweet.get("id") or "")
    if not tweet_id:
        return None

    # Author — three shapes are supported:
    #   compact (-c):     "author": "@karpathy"
    #   twitter-cli v1:   "author": {"name": ..., "screenName": ...}
    #   legacy full:      "user":   {"name": ..., "screen_name": ...}
    author_field = tweet.get("author")
    if isinstance(author_field, str):
        author_handle = author_field.lstrip("@")
        author_name = str(tweet.get("author_name") or "Unknown")
    elif isinstance(author_field, dict):
        author_name = str(author_field.get("name") or "Unknown")
        author_handle = str(
            author_field.get("screenName") or author_field.get("screen_name") or ""
        )
    else:
        user = tweet.get("user") or {}
        author_name = str(user.get("name") or tweet.get("author_name") or "Unknown")
        author_handle = str(user.get("screen_name") or tweet.get("author_handle") or "")

    if author_handle:
        if not author_handle.startswith("@"):
            author_handle = f"@{author_handle}"

    # Text - handle both formats
    text = str(
        tweet.get("full_text") or tweet.get("text") or tweet.get("content") or ""
    )

    # URL - construct if missing
    url = str(
        tweet.get("url")
        or tweet.get("permalink")
        or tweet.get("link")
        or f"https://x.com/i/status/{tweet_id}"
    )

    # Timestamp. Prefer unambiguous absolute forms; twitter-cli v1 emits
    # createdAtISO / createdAt, legacy full emits created_at. The compact (-c)
    # "time" field ("Aug 02 03:00") carries NO YEAR and is only used as a last
    # resort — see _parse_twitter_timestamp for how the year is inferred.
    created_at = (
        tweet.get("createdAtISO")
        or tweet.get("createdAt")
        or tweet.get("created_at")
        or tweet.get("published_at")
        or tweet.get("time", "")
    )
    published_at = _parse_twitter_timestamp(created_at)

    # Engagement. twitter-cli v1 nests these under "metrics"; compact output
    # only carries likes/rts; legacy full uses *_count keys.
    metrics = tweet.get("metrics") if isinstance(tweet.get("metrics"), dict) else {}
    likes = _safe_int(
        metrics.get("likes")
        or tweet.get("favorite_count")
        or tweet.get("likes")
        or tweet.get("like_count")
    )
    reposts = _safe_int(
        metrics.get("retweets")
        or tweet.get("retweet_count")
        or tweet.get("retweets")
        or tweet.get("reposts")
        or tweet.get("rts")  # compact format uses "rts"
    )
    replies = _safe_int(
        metrics.get("replies") or tweet.get("reply_count") or tweet.get("replies")
    )
    quotes = _safe_int(
        metrics.get("quotes") or tweet.get("quote_count") or tweet.get("quotes")
    )

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
    """Parse a Twitter timestamp into **naive UTC**.

    Handles, in order of preference:

    - ``Sun Aug 02 03:00:09 +0000 2026``  (twitter-cli ``createdAt``)
    - ``2026-08-02T03:00:09Z`` / ``+08:00`` (``createdAtISO``)
    - ``2026-08-02 03:00:09`` (assumed already UTC)
    - ``Aug 02 03:00`` (compact ``-c`` output — **no year**, inferred below)

    Returns ``utcnow_naive()`` only when nothing parses. Callers that need to
    distinguish "unknown time" from "just posted" should use
    :func:`parse_twitter_timestamp_strict`, which returns ``None`` instead.
    """
    parsed = parse_twitter_timestamp_strict(ts)
    return parsed if parsed is not None else utcnow_naive()


def parse_twitter_timestamp_strict(ts: str) -> datetime | None:
    """Like :func:`_parse_twitter_timestamp` but returns ``None`` on failure.

    Aware timestamps are *converted* to UTC (not merely stripped of tzinfo —
    that silently shifted any non-``+0000`` offset by hours).
    """
    if not ts or not ts.strip():
        return None

    ts = ts.strip()

    # Absolute formats, all carrying a year.
    patterns = (
        "%a %b %d %H:%M:%S %z %Y",
        "%a %b %d %H:%M:%S %Z %Y",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
    )
    for pattern in patterns:
        try:
            dt = datetime.strptime(ts, pattern)
        except ValueError:
            continue
        return _to_naive_utc(dt)

    # ISO 8601 with offset (e.g. "+08:00") that strptime %z rejects on some
    # Python builds.
    try:
        return _to_naive_utc(datetime.fromisoformat(ts.replace("Z", "+00:00")))
    except ValueError:
        pass

    # Compact "-c" output: "Aug 02 03:00" — no year. Assume the most recent
    # occurrence: current year, but if that lands in the future (e.g. parsing
    # "Dec 30" on Jan 02) it must belong to the previous year.
    #
    # The year is injected into the *input* rather than parsed bare and then
    # .replace()d: parsing a day-without-year is deprecated in 3.13+ and fails
    # outright on Feb 29.
    now = utcnow_naive()
    for pattern in ("%Y %b %d %H:%M", "%Y %b %d"):
        for year in (now.year, now.year - 1):
            try:
                candidate = datetime.strptime(f"{year} {ts}", pattern)
            except ValueError:
                continue
            if candidate <= now + timedelta(days=1):
                return candidate
        # Pattern matched but every candidate was in the future — fall through.

    return None


def _to_naive_utc(dt: datetime) -> datetime:
    """Normalize a datetime to naive UTC, converting any offset properly."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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
        return _parse_opencli_tweets_json(
            data, backend=backend, handle=handle, query=query
        )
    except (json.JSONDecodeError, ValueError):
        pass

    # Fallback: YAML (if pyyaml available)
    try:
        import yaml

        data = yaml.safe_load(raw)
        if isinstance(data, list):
            return _parse_opencli_tweets_json(
                data, backend=backend, handle=handle, query=query
            )
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
    tweet_id = str(
        tweet.get("id") or tweet.get("tweet_id") or tweet.get("id_str") or ""
    )
    if not tweet_id:
        return None

    # Author
    author_name = str(
        tweet.get("author", {}).get("name") or tweet.get("author_name") or "Unknown"
    )
    author_handle = str(
        tweet.get("author", {}).get("username")
        or tweet.get("author_handle")
        or tweet.get("screen_name")
        or ""
    )
    if author_handle and not author_handle.startswith("@"):
        author_handle = f"@{author_handle}"

    # Text
    text = str(
        tweet.get("text") or tweet.get("content") or tweet.get("full_text") or ""
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

    # Engagement (flexible key names)
    likes = _safe_int(
        tweet.get("favorite_count") or tweet.get("likes") or tweet.get("like_count")
    )
    reposts = _safe_int(
        tweet.get("retweet_count") or tweet.get("retweets") or tweet.get("repost_count")
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
