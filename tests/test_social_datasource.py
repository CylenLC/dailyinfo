"""Tests for SocialDataSource — stable identity, lookback filter, cross-source dedup."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.datasource import Item
from social.datasource import SocialDataSource
from social.fetch_cursor import FetchCursorStore
from social.models import SocialItem, utcnow_naive
from social.seen_store import SocialSeenStore, stable_social_identity

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
            "first_seen": (utcnow_naive() - timedelta(days=40)).isoformat(),
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

    def test_fetch_researcher_watch_empty_group(
        self, base_config, base_defaults, mocker
    ):
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
        mocker.patch(
            "scripts.social.agent_reach.AgentReachAdapter", return_value=mock_reach
        )

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

    def test_fetch_researcher_single_failure_does_not_block(
        self, base_config, base_defaults, mocker
    ):
        """Failure for one researcher doesn't block others."""
        mock_reach = mocker.MagicMock()

        def fake_posts(handle, limit):
            if handle == "@fail":
                raise Exception("network error")
            return []

        mock_reach.x_user_posts.side_effect = fake_posts
        mocker.patch(
            "scripts.social.agent_reach.AgentReachAdapter", return_value=mock_reach
        )

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

    def test_fetch_search_multiple_queries_dedup(
        self, base_config, base_defaults, mocker
    ):
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
                        # Must be naive UTC: the incremental window compares
                        # against utcnow_naive(), and datetime.now() is local
                        # time (8h ahead here), landing outside the window.
                        published_at=utcnow_naive(),
                    )
                ]
            return []

        mock_reach.x_search.side_effect = fake_search
        mocker.patch(
            "scripts.social.agent_reach.AgentReachAdapter", return_value=mock_reach
        )

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


class TestIncrementalWindow:
    """Each run must admit only items newer than the previous fetch.

    Regression: fetching used a fixed ``lookback_hours`` slab, so every run
    re-considered the same 24h of tweets and relied entirely on the seen store.
    """

    @staticmethod
    def _tweet(item_id, published_at):
        return SocialItem(
            platform="x",
            item_id=item_id,
            author_name="Author",
            author_handle="@author",
            text=f"content {item_id}",
            url=f"https://x.com/author/status/{item_id}",
            published_at=published_at,
        )

    def _make_ds(self, tmp_path, mocker, returned, config_extra=None):
        mock_reach = mocker.MagicMock()
        mock_reach.x_search.side_effect = lambda query, limit: (
            returned if query == "q1" else []
        )
        mocker.patch(
            "scripts.social.agent_reach.AgentReachAdapter", return_value=mock_reach
        )
        config = {
            "name": "x_ai_search",
            "type": "social",
            "category": "social",
            "mode": "search",
            "queries": ["q1"],
            **(config_extra or {}),
        }
        ds = SocialDataSource(
            config,
            {"lookback_hours": 24, "model": "stub/model"},
            reach=mock_reach,
            cursor_store=FetchCursorStore(path=tmp_path / "cursor.json"),
        )
        # Isolate the seen store so dedup cannot mask window behaviour.
        ds._filter_seen = lambda items: items
        return ds

    def test_first_run_excludes_yesterday(self, tmp_path, mocker):
        """No cursor yet: only today's items pass, not a rolling 24h."""
        now = utcnow_naive()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        ds = self._make_ds(
            tmp_path,
            mocker,
            [
                self._tweet("yesterday", midnight - timedelta(hours=2)),
                self._tweet("today", midnight + timedelta(minutes=1)),
            ],
        )
        ids = [i.extra["canonical_id"] for i in ds.fetch()]
        assert any("today" in i for i in ids)
        assert not any("yesterday" in i for i in ids)

    def test_second_run_excludes_items_before_cursor(self, tmp_path, mocker):
        """After committing a cursor, older-than-cursor items are dropped."""
        now = utcnow_naive()
        cursor_at = now - timedelta(minutes=30)

        store = FetchCursorStore(path=tmp_path / "cursor.json")
        store.record_fetch("x_ai_search", cursor_at)

        ds = self._make_ds(
            tmp_path,
            mocker,
            [
                self._tweet("old", cursor_at - timedelta(minutes=10)),
                self._tweet("new", cursor_at + timedelta(minutes=10)),
            ],
        )
        ids = [i.extra["canonical_id"] for i in ds.fetch()]
        assert any("new" in i for i in ids)
        assert not any("old" in i for i in ids)

    def test_item_posted_during_fetch_is_kept(self, tmp_path, mocker):
        """window_end is snapshotted pre-call; mid-fetch posts must survive."""
        ds = self._make_ds(
            tmp_path,
            mocker,
            [self._tweet("mid", utcnow_naive() + timedelta(seconds=30))],
        )
        assert len(ds.fetch()) == 1

    def test_commit_advances_cursor_to_window_end(self, tmp_path, mocker):
        ds = self._make_ds(tmp_path, mocker, [self._tweet("a", utcnow_naive())])
        ds.fetch()
        _, window_end = ds.fetch_window
        ds.commit_fetch_cursor(item_count=1)

        assert ds.cursor_store.last_fetch_at("x_ai_search") == window_end

    def test_fetch_window_is_none_before_fetch(self, tmp_path, mocker):
        ds = self._make_ds(tmp_path, mocker, [])
        assert ds.fetch_window == (None, None)


class TestHandleValidation:
    """Invalid handles used to return 0 items silently, forever."""

    def _ds(self, handle, mocker):
        mock_reach = mocker.MagicMock()
        mock_reach.x_user_posts.return_value = []
        mocker.patch(
            "scripts.social.agent_reach.AgentReachAdapter", return_value=mock_reach
        )
        ds = SocialDataSource(
            {
                "name": "x_ai_researchers",
                "type": "social",
                "category": "social",
                "mode": "researcher_watch",
                # Without this the group lookup resolves to "" and bails out
                # before handle validation is ever reached.
                "watchlist": "ai_ml",
            },
            {"lookback_hours": 24, "model": "stub/model"},
            reach=mock_reach,
            researchers={
                "groups": {"ai_ml": [{"name": "X", "handle": handle, "enabled": True}]}
            },
        )
        return ds, mock_reach

    def test_hyphenated_handle_is_rejected_without_calling_backend(
        self, mocker, capsys
    ):
        """`@szymon-sidor` is not a legal X handle — never worth a network call."""
        ds, mock_reach = self._ds("@szymon-sidor", mocker)
        assert ds.fetch() == []
        mock_reach.x_user_posts.assert_not_called()
        assert "invalid X handle" in capsys.readouterr().out

    def test_overlong_handle_is_rejected(self, mocker):
        ds, mock_reach = self._ds("@" + "a" * 16, mocker)
        assert ds.fetch() == []
        mock_reach.x_user_posts.assert_not_called()

    def test_valid_handle_reaching_backend_but_empty_warns(self, mocker, capsys):
        """Valid handle + zero posts is a real signal, not silence."""
        ds, mock_reach = self._ds("@jasonwei20", mocker)
        assert ds.fetch() == []
        mock_reach.x_user_posts.assert_called_once()
        assert "returned no posts" in capsys.readouterr().out
