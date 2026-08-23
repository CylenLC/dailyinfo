"""Regression tests for Twitter timestamp parsing.

Bug: the compact ``-c`` output field ``"time": "Aug 02 03:00"`` matched none of
the parser's patterns, so every item silently fell back to ``utcnow_naive()``.
Effect: all items looked "just posted", the lookback/incremental window never
filtered anything, and old tweets kept getting pushed.
"""

from datetime import datetime, timedelta

from social.normalize import (
    _parse_twitter_timestamp,
    _parse_twitter_user_posts,
    parse_twitter_timestamp_strict,
)
from social.models import utcnow_naive


class TestAbsoluteFormats:
    def test_twitter_cli_created_at(self):
        """twitter-cli --json `createdAt`."""
        got = parse_twitter_timestamp_strict("Sun Aug 02 03:00:09 +0000 2026")
        assert got == datetime(2026, 8, 2, 3, 0, 9)

    def test_iso_with_z(self):
        got = parse_twitter_timestamp_strict("2026-08-02T03:00:09Z")
        assert got == datetime(2026, 8, 2, 3, 0, 9)

    def test_nonzero_offset_is_converted_not_stripped(self):
        """Regression: `.replace(tzinfo=None)` shifted non-UTC offsets by hours."""
        got = parse_twitter_timestamp_strict("2026-08-02T11:00:09+08:00")
        assert got == datetime(2026, 8, 2, 3, 0, 9)

    def test_naive_string_assumed_utc(self):
        got = parse_twitter_timestamp_strict("2026-08-02 03:00:09")
        assert got == datetime(2026, 8, 2, 3, 0, 9)


class TestCompactNoYearFormat:
    def test_compact_time_parses_instead_of_falling_back(self):
        """The core bug: "Aug 02 03:00" must parse, not become "now"."""
        got = parse_twitter_timestamp_strict("Aug 02 03:00")
        assert got is not None
        assert (got.month, got.day, got.hour, got.minute) == (8, 2, 3, 0)

    def test_compact_year_is_inferred_not_future(self):
        """Inferred year never lands more than a day ahead of now."""
        got = parse_twitter_timestamp_strict("Dec 30 23:59")
        assert got is not None
        assert got <= utcnow_naive() + timedelta(days=1)

    def test_unparseable_returns_none_in_strict_mode(self):
        assert parse_twitter_timestamp_strict("not a date") is None

    def test_empty_returns_none_in_strict_mode(self):
        assert parse_twitter_timestamp_strict("") is None
        assert parse_twitter_timestamp_strict("   ") is None

    def test_lenient_mode_falls_back_to_now(self):
        before = utcnow_naive()
        got = _parse_twitter_timestamp("not a date")
        assert before <= got <= utcnow_naive()


class TestEndToEndTweetParsing:
    def test_full_json_shape_yields_real_date_and_metrics(self):
        """twitter-cli --json: nested author + metrics + createdAt."""
        raw = """
        {
          "ok": true,
          "data": [
            {
              "id": "2083749667410727319",
              "text": "real content",
              "author": {"name": "Andrej Karpathy", "screenName": "karpathy"},
              "createdAt": "Sun Aug 02 03:00:09 +0000 2026",
              "metrics": {"likes": 100, "retweets": 20,
                          "replies": 5, "quotes": 2}
            }
          ]
        }
        """
        items = _parse_twitter_user_posts(raw, backend="twitter-cli")
        assert len(items) == 1
        item = items[0]
        assert item.published_at == datetime(2026, 8, 2, 3, 0, 9)
        assert item.author_handle == "@karpathy"
        assert item.author_name == "Andrej Karpathy"
        # Compact output drops these two entirely; full JSON carries them.
        assert item.replies == 5
        assert item.quotes == 2

    def test_compact_shape_still_supported(self):
        """Compact `-c`: string author, "rts", year-less "time"."""
        raw = """
        [
          {
            "id": "123",
            "text": "compact content",
            "author": "@karpathy",
            "time": "Aug 02 03:00",
            "likes": 10,
            "rts": 3
          }
        ]
        """
        items = _parse_twitter_user_posts(raw, backend="twitter-cli")
        assert len(items) == 1
        item = items[0]
        assert item.author_handle == "@karpathy"
        assert item.likes == 10
        assert item.reposts == 3
        # The whole point: not today's date.
        assert (item.published_at.month, item.published_at.day) == (8, 2)
