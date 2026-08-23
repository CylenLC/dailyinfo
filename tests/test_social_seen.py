"""Tests for social pipeline integration — seen dedup + incremental run policy."""

from pathlib import Path

import pytest

from scripts.social.seen_store import SocialSeenStore


class TestSocialSeenStoreIntegration:
    def test_duplicate_across_sources_filtered(self, tmp_path):
        """Same tweet fetched by two sources is deduped in second run."""
        store = SocialSeenStore(tmp_path / "seen.json")

        # Simulate source1 finding tweet x:123
        store.record("x:123", _fake_social_item("x:123"))

        # Source2 also finds x:123
        assert store.is_seen("x:123") is True

    def test_different_items_not_deduplicated(self, tmp_path):
        """Different tweets are not incorrectly deduped."""
        store = SocialSeenStore(tmp_path / "seen.json")
        store.record("x:111", _fake_social_item("x:111"))
        assert store.is_seen("x:222") is False

    def test_same_text_different_id_not_deduplicated(self, tmp_path):
        """Same text but different item IDs are treated as distinct."""
        store = SocialSeenStore(tmp_path / "seen.json")
        store.record("x:111", _fake_social_item("x:111"))
        assert store.is_seen("x:222") is False  # Different ID = different item

    def test_url_deduplication_after_conversion(self, tmp_path):
        """URL-normalized items are deduped."""
        store = SocialSeenStore(tmp_path / "seen.json")

        # Simulate storing from a URL-based item
        store.record("x.com/karpathy/status/123", _fake_url_item("x.com/karpathy/status/123"))

        # Same tweet with twitter.com
        assert store.is_seen("twitter.com/karpathy/status/123") is False  # Different key
        # But if normalized first:
        from scripts.social.seen_store import _normalize_social_url

        assert _normalize_social_url("https://twitter.com/karpathy/status/123") == "x.com/karpathy/status/123"


def _fake_social_item(item_id: str):
    from scripts.social.models import SocialItem
    from datetime import datetime

    return SocialItem(
        platform="x",
        item_id=item_id,
        author_name="Test",
        author_handle="@test",
        text="test",
        url=f"https://x.com/test/status/{item_id}",
        published_at=datetime.utcnow(),
    )


def _fake_url_item(url: str):
    from scripts.datasource import Item

    return Item(title="Test", date="2026-10-12", url=url)
