"""Tests for SocialDataSource — stable identity, lookback filter, cross-source dedup."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.datasource import Item
from scripts.social.datasource import SocialDataSource
from scripts.social.models import SocialItem
from scripts.social.seen_store import SocialSeenStore, stable_social_identity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_config():
    return {
        "name": "x_ai_researchers",
        "display_name": "AI Researchers on X",
        "type": "social",
        "category": "social",
        "mode": "researcher_watch",
        "lookback_hours": 24,
        "max_items_per_researcher": 5,
    }


@pytest.fixture
def base_defaults():
    return {
        "lookback_hours": 24,
        "model": "deepseek-v4-flash",
    }


@pytest.fixture
def sample_social_item():
    return SocialItem(
        platform="x",
        item_id="1959234892345678901",
        author_name="Andrej Karpathy",
        author_handle="@karpathy",
        text="New paper on multimodal hydrology!",
        url="https://x.com/karpathy/status/1959234892345678901",
        published_at=datetime.now(timezone.utc).replace(tzinfo=None),
        likes=1294,
        reposts=201,
        backend="twitter-cli",
        source_mode="researcher_watch",
        source_ref="researcher:@karpathy",
    )


# ---------------------------------------------------------------------------
# stable_social_identity tests
# ---------------------------------------------------------------------------


class TestStableSocialIdentity:
    def test_canonical_id_priority(self):
        """canonical_id in extra takes priority over URL."""
        item = Item(
            title="Test",
            date="2026-10-12",
            url="https://x.com/karpathy/status/123",
            extra={"canonical_id": "x:999"},
        )
        assert stable_social_identity(item) == "x:999"

    def test_url_fallback(self):
        """URL is used when canonical_id is absent."""
        item = Item(
            title="Test",
            date="2026-10-12",
            url="https://x.com/karpathy/status/123",
        )
        assert stable_social_identity(item) == "x.com/karpathy/status/123"

    def test_url_normalization_twitter_com(self):
        """twitter.com URLs normalized to x.com."""
        item = Item(
            title="Test",
            date="2026-10-12",
            url="https://twitter.com/karpathy/status/123",
        )
        assert stable_social_identity(item) == "x.com/karpathy/status/123"

    def test_url_normalization_scheme_stripped(self):
        """Scheme is stripped from URL."""
        item = Item(
            title="Test",
            date="2026-10-12",
            url="https://x.com/karpathy/status/123",
        )
        identity = stable_social_identity(item)
        assert not identity.startswith("http")

    def test_url_normalization_trailing_slash(self):
        """Trailing slash is removed."""
        item = Item(
            title="Test",
            date="2026-10-12",
            url="https://x.com/karpathy/status/123/",
        )
        identity = stable_social_identity(item)
        assert not identity.endswith("/")

    def test_empty_url_fallback(self):
        """Empty URL falls back to title+date."""
        item = Item(title="Test", date="2026-10-12", url="")
        identity = stable_social_identity(item)
        assert identity.startswith("title:Test:")


# ---------------------------------------------------------------------------
# SocialSeenStore tests
# ---------------------------------------------------------------------------


class TestSocialSeenStore:
    def test_is_seen_false_initially(self, tmp_path):
        store = SocialSeenStore(tmp_path / "seen.json")
        assert store.is_seen("x:123") is False

    def test_record_and_is_seen(self, tmp_path, sample_social_item):
        store = SocialSeenStore(tmp_path / "seen.json")
        store.record("x:12345", sample_social_item)
        assert store.is_seen("x:12345") is True

    def test_persist_and_reload(self, tmp_path, sample_social_item):
        path = tmp_path / "seen.json"
        store1 = SocialSeenStore(path)
        store1.record("x:12345", sample_social_item)
        store1._save()

        store2 = SocialSeenStore(path)
        assert store2.is_seen("x:12345") is True

    def test_prune_old_entries(self, tmp_path, sample_social_item):
        path = tmp_path / "seen.json"
        store = SocialSeenStore(path)

        # Add an entry with old timestamp
        old_id = "x:old"
        store._data.setdefault("items", {})[old_id] = {
            "first_seen": (datetime.utcnow() - timedelta(days=40)).isoformat(),
            "published_at": "",
            "source": "test",
            "url": "https://x.com/old",
        }
        store._save()

        removed = store.prune(max_age_days=30)
        assert removed >= 1
        assert store.is_seen(old_id) is False

    def test_prune_does_not_remove_recent(self, tmp_path, sample_social_item):
        path = tmp_path / "seen.json"
        store = SocialSeenStore(path)
        store.record("x:12345", sample_social_item)
        removed = store.prune(max_age_days=30)
        assert removed == 0
        assert store.is_seen("x:12345") is True

    def test_commit_items_from_item_list(self, tmp_path, sample_social_item):
        path = tmp_path / "seen.json"
        store = SocialSeenStore(path)

        items = [
            Item(
                title="Test",
                date="2026-10-12",
                url="https://x.com/a/status/1",
                extra={"canonical_id": "x:1"},
            ),
            Item(
                title="Test 2",
                date="2026-10-12",
                url="https://x.com/b/status/2",
                extra={"canonical_id": "x:2"},
            ),
        ]
        store.commit_items(items)
        assert store.is_seen("x:1") is True
        assert store.is_seen("x:2") is True


# ---------------------------------------------------------------------------
# SocialDataSource tests
# ---------------------------------------------------------------------------


class TestSocialDataSource:
    def test_item_identity_uses_canonical_id(self, base_config, base_defaults, mocker):
        """SocialDataSource.item_identity() returns canonical_id when available."""
        mocker.patch("scripts.social.agent_reach.AgentReachAdapter")

        ds = SocialDataSource(
            base_config,
            base_defaults,
            reach=MagicMock(),
        )
        item = Item(
            title="Test",
            date="2026-10-12",
            extra={"canonical_id": "x:12345"},
        )
        assert ds.item_identity(item) == "x:12345"

    def test_fetch_researcher_watch_empty_group(self, base_config, base_defaults, mocker):
        """fetch() returns empty list if no researchers in group."""
        mocker.patch("scripts.social.agent_reach.AgentReachAdapter")
        config = {**base_config, "watchlist": "nonexistent_group"}
        ds = SocialDataSource(
            config,
            base_defaults,
            reach=MagicMock(),
            researchers={"groups": {}},
        )
        items = ds.fetch()
        assert items == []

    def test_fetch_researcher_disabled(self, base_config, base_defaults, mocker):
        """Disabled researchers are skipped."""
        mock_reach = mocker.MagicMock()
        mocker.patch("scripts.social.agent_reach.AgentReachAdapter", return_value=mock_reach)

        researchers = {
            "groups": {
                "ai_ml": [
                    {"name": "Test", "handle": "@test", "enabled": False},
                ]
            }
        }
        ds = SocialDataSource(
            base_config,
            base_defaults,
            reach=mock_reach,
            researchers=researchers,
        )
        items = ds.fetch()
        assert items == []
        mock_reach.x_user_posts.assert_not_called()

    def test_fetch_researcher_single_failure_does_not_block(self, base_config, base_defaults, mocker):
        """Failure for one researcher doesn't block others."""
        mock_reach = mocker.MagicMock()

        def fake_posts(handle, limit):
            if handle == "@fail":
                raise Exception("network error")
            return []

        mock_reach.x_user_posts.side_effect = fake_posts
        mocker.patch("scripts.social.agent_reach.AgentReachAdapter", return_value=mock_reach)

        researchers = {
            "groups": {
                "ai_ml": [
                    {"name": "Fail User", "handle": "@fail", "enabled": True},
                    {"name": "Ok User", "handle": "@ok", "enabled": True},
                ]
            }
        }
        ds = SocialDataSource(
            base_config,
            base_defaults,
            reach=mock_reach,
            researchers=researchers,
        )
        items = ds.fetch()  # Should not raise
        assert items == []

    def test_fetch_search_multiple_queries_dedup(self, base_config, base_defaults, mocker):
        """Duplicate items across queries are merged with matched_queries."""
        mock_reach = mocker.MagicMock()

        def fake_search(query, limit):
            if query == "AI for Science":
                return [
                    SocialItem(
                        platform="x",
                        item_id="duplicate_tweet",
                        author_name="Author",
                        author_handle="@author",
                        text="Duplicate content",
                        url="https://x.com/author/status/dup",
                        published_at=datetime.now(),
                    )
                ]
            return []

        mock_reach.x_search.side_effect = fake_search
        mocker.patch("scripts.social.agent_reach.AgentReachAdapter", return_value=mock_reach)

        config = {
            **base_config,
            "mode": "search",
            "queries": ["AI for Science", "hydrology deep learning"],
        }
        ds = SocialDataSource(
            config,
            base_defaults,
            reach=mock_reach,
        )
        items = ds.fetch()
        # Only one item should be returned (deduped)
        assert len(items) == 1
        # matched_queries should contain both
        assert "AI for Science" in items[0].extra["matched_queries"]
