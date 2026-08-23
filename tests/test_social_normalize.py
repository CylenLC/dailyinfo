"""Tests for social normalization — backend output → SocialItem."""

from datetime import datetime

import pytest

from social.models import SocialItem, utcnow_naive
from social.normalize import (
    _parse_opencli_twitter,
    _parse_twitter_search,
    _parse_twitter_user_posts,
)


class TestTwitterCLIParser:
    def test_parse_user_posts_valid(self):
        """Parse valid twitter-cli user-posts JSON output."""
        raw = """
        [
          {
            "id_str": "1959234892345678901",
            "id": 1959234892345678901,
            "full_text": "Hello world",
            "user": {
              "name": "Test User",
              "screen_name": "testuser"
            },
            "created_at": "Mon Oct 12 07:32:15 +0000 2026",
            "favorite_count": 100,
            "retweet_count": 20,
            "reply_count": 5,
            "quote_count": 2
          }
        ]
        """
        items = _parse_twitter_user_posts(raw, backend="twitter-cli")
        assert len(items) == 1
        assert items[0].author_name == "Test User"
        assert items[0].author_handle == "@testuser"
        assert items[0].text == "Hello world"
        assert items[0].likes == 100
        assert items[0].reposts == 20
        assert items[0].platform == "x"

    def test_parse_user_posts_empty(self):
        """Empty input returns empty list."""
        assert _parse_twitter_user_posts("", backend="twitter-cli") == []

    def test_parse_user_posts_malformed_json(self):
        """Malformed JSON returns empty list."""
        items = _parse_twitter_user_posts("not json", backend="twitter-cli")
        assert items == []

    def test_parse_user_posts_missing_id(self):
        """Tweets without id are skipped."""
        raw = '[{"full_text": "no id here", "user": {"name": "X", "screen_name": "x"}}]'
        items = _parse_twitter_user_posts(raw, backend="twitter-cli")
        assert len(items) == 0

    def test_canonical_id_format(self):
        """canonical_id is platform:item_id."""
        raw = '[{"id_str": "123", "text": "hi", "user": {"name": "A", "screen_name": "a"}, "created_at": "Mon Oct 12 00:00:00 +0000 2026"}]'
        items = _parse_twitter_user_posts(raw, backend="twitter-cli")
        assert items[0].canonical_id == "x:123"

    def test_timestamp_parsing_variants(self):
        """Various Twitter timestamp formats are parsed correctly."""
        # Standard Twitter format
        ts1 = _parse_twitter_user_posts(
            '[{"id_str": "1", "text": "x", "user": {"name": "A", "screen_name": "a"}, "created_at": "Mon Oct 12 07:32:15 +0000 2026"}]',
            backend="twitter-cli",
        )[0].published_at
        assert ts1.year == 2026
        assert ts1.month == 10
        assert ts1.day == 12

        # ISO format fallback
        ts2 = _parse_twitter_user_posts(
            '[{"id_str": "2", "text": "x", "user": {"name": "A", "screen_name": "a"}, "created_at": "2026-10-12T07:32:15Z"}]',
            backend="twitter-cli",
        )[0].published_at
        assert ts2.year == 2026

    def test_author_handle_normalization(self):
        """Handle is prefixed with @ if missing."""
        raw = '[{"id_str": "1", "text": "x", "user": {"name": "A", "screen_name": "karpathy"}, "created_at": "Mon Oct 12 00:00:00 +0000 2026"}]'
        items = _parse_twitter_user_posts(raw, backend="twitter-cli")
        assert items[0].author_handle == "@karpathy"

    def test_search_parser(self):
        """Search results are parsed into SocialItem list."""
        raw = '[{"id_str": "456", "text": "search result", "user": {"name": "Searcher", "screen_name": "searcher"}, "created_at": "Mon Oct 12 00:00:00 +0000 2026", "favorite_count": 50}]'
        items = _parse_twitter_search(raw, backend="twitter-cli")
        assert len(items) == 1
        assert items[0].source_mode == "search"


class TestOpenCLIParser:
    def test_parse_json_format(self):
        """OpenCLI JSON output is parsed correctly."""
        raw = """
        [
          {
            "id": "789",
            "text": "OpenCLI test",
            "author": {
              "name": "OpenCLI User",
              "username": "ocluser"
            },
            "created_at": "2026-10-12T07:32:15Z",
            "favorite_count": 75,
            "retweet_count": 10
          }
        ]
        """
        items = _parse_opencli_twitter(raw, backend="opencli", handle="@ocluser")
        assert len(items) == 1
        assert items[0].backend == "opencli"
        assert items[0].author_handle == "@ocluser"

    def test_parse_yaml_format(self):
        """OpenCLI YAML output is parsed if pyyaml available."""
        yaml = pytest.importorskip("yaml")
        raw = """
- id: "999"
  text: "YAML test"
  author:
    name: "YAML User"
    username: "ymluser"
  created_at: "2026-10-12T07:32:15Z"
  favorite_count: 30
"""
        items = _parse_opencli_twitter(raw, backend="opencli", handle="@ymluser")
        assert len(items) == 1
        assert items[0].text == "YAML test"

    def test_empty_opencli_output(self):
        """Empty OpenCLI output returns empty list."""
        assert _parse_opencli_twitter("", backend="opencli", handle="@test") == []

    def test_malformed_opencli_output(self):
        """Malformed OpenCLI output returns empty list."""
        assert _parse_opencli_twitter("{{{", backend="opencli", handle="@test") == []


class TestSocialItem:
    def test_to_dailyinfo_item(self):
        """SocialItem.to_dailyinfo_item() produces correct Item."""
        social = SocialItem(
            platform="x",
            item_id="12345",
            author_name="Test Author",
            author_handle="@testauthor",
            text="This is a test tweet content",
            url="https://x.com/testauthor/status/12345",
            published_at=datetime(2026, 10, 12, 7, 32, 0),
            likes=100,
            reposts=25,
            replies=5,
            backend="twitter-cli",
            source_mode="researcher_watch",
        )
        item = social.to_dailyinfo_item("2026-10-12")
        assert item.title == "@testauthor: This is a test tweet content"
        assert item.date == "2026-10-12"
        assert item.url == "https://x.com/testauthor/status/12345"
        assert item.extra["canonical_id"] == "x:12345"
        assert item.extra["engagement"]["likes"] == 100

    def test_title_truncation(self):
        """Long tweets are truncated in title."""
        long_text = "x" * 150
        social = SocialItem(
            platform="x",
            item_id="1",
            author_name="A",
            author_handle="@a",
            text=long_text,
            url="https://x.com/a/status/1",
            published_at=utcnow_naive(),
        )
        item = social.to_dailyinfo_item("2026-10-12")
        assert len(item.title) < 150
        assert "..." in item.title

    def test_engagement_none_omitted(self):
        """None engagement values are omitted from extra."""
        social = SocialItem(
            platform="x",
            item_id="1",
            author_name="A",
            author_handle="@a",
            text="test",
            url="https://x.com/a/status/1",
            published_at=utcnow_naive(),
            likes=None,
            reposts=None,
        )
        item = social.to_dailyinfo_item("2026-10-12")
        assert "engagement" not in item.extra
