"""Tests for social.fetch_cursor — incremental window computation."""

import json
from datetime import datetime, timedelta

from social.fetch_cursor import FetchCursorStore, start_of_utc_day

NOW = datetime(2026, 8, 23, 13, 42, 3)


def _store(tmp_path):
    return FetchCursorStore(path=tmp_path / "cursor.json")


# ---------------------------------------------------------------------------
# start_of_utc_day
# ---------------------------------------------------------------------------


def test_start_of_utc_day_truncates_time():
    assert start_of_utc_day(NOW) == datetime(2026, 8, 23, 0, 0, 0)


def test_start_of_utc_day_is_idempotent():
    midnight = datetime(2026, 8, 23, 0, 0, 0)
    assert start_of_utc_day(midnight) == midnight


# ---------------------------------------------------------------------------
# First run / no cursor
# ---------------------------------------------------------------------------


def test_first_run_starts_at_midnight_today(tmp_path):
    """With no cursor, only today's items qualify — not a rolling 24h slab."""
    store = _store(tmp_path)
    assert store.last_fetch_at("x_ai_search") is None
    assert store.window_start("x_ai_search", now=NOW) == datetime(2026, 8, 23, 0, 0)


def test_missing_file_is_not_an_error(tmp_path):
    store = FetchCursorStore(path=tmp_path / "nope" / "cursor.json")
    assert store.last_fetch_at("anything") is None


def test_corrupt_file_falls_back_to_empty(tmp_path):
    path = tmp_path / "cursor.json"
    path.write_text("{not json", encoding="utf-8")
    assert FetchCursorStore(path=path).last_fetch_at("x") is None


# ---------------------------------------------------------------------------
# Incremental behaviour
# ---------------------------------------------------------------------------


def test_cursor_from_earlier_today_is_used(tmp_path):
    """A same-day cursor wins over midnight — that is the incremental part."""
    store = _store(tmp_path)
    earlier = datetime(2026, 8, 23, 11, 30, 0)
    store.record_fetch("x_ai_search", earlier)

    assert store.window_start("x_ai_search", now=NOW) == earlier


def test_stale_cursor_is_clamped_to_today(tmp_path):
    """A cursor from last week must not drag in a week of backlog."""
    store = _store(tmp_path)
    store.record_fetch("x_ai_search", NOW - timedelta(days=7))

    assert store.window_start("x_ai_search", now=NOW) == datetime(2026, 8, 23, 0, 0)


def test_cursors_are_independent_per_source(tmp_path):
    store = _store(tmp_path)
    store.record_fetch("x_ai_search", datetime(2026, 8, 23, 12, 0))

    assert store.window_start("x_ai_search", now=NOW) == datetime(2026, 8, 23, 12, 0)
    assert store.window_start("x_ai_researchers", now=NOW) == datetime(
        2026, 8, 23, 0, 0
    )


def test_record_fetch_advances_cursor(tmp_path):
    store = _store(tmp_path)
    store.record_fetch("s", datetime(2026, 8, 23, 10, 0))
    store.record_fetch("s", datetime(2026, 8, 23, 13, 0))

    assert store.last_fetch_at("s") == datetime(2026, 8, 23, 13, 0)


# ---------------------------------------------------------------------------
# max_lookback_hours clamp
# ---------------------------------------------------------------------------


def test_max_lookback_tightens_window(tmp_path):
    """The later of (midnight, now - max_lookback) wins."""
    store = _store(tmp_path)
    # midnight is 13.7h back; a 2h rolling floor is tighter.
    got = store.window_start("s", now=NOW, max_lookback_hours=2)
    assert got == NOW - timedelta(hours=2)


def test_max_lookback_never_loosens_past_midnight(tmp_path):
    store = _store(tmp_path)
    got = store.window_start("s", now=NOW, max_lookback_hours=72)
    assert got == datetime(2026, 8, 23, 0, 0)


# ---------------------------------------------------------------------------
# max_backfill_hours (loosens past midnight for low-volume sources)
# ---------------------------------------------------------------------------


def test_max_backfill_loosens_past_midnight(tmp_path):
    """Niche queries can have zero same-day posts, so allow a wider floor."""
    store = _store(tmp_path)
    got = store.window_start("s", now=NOW, max_backfill_hours=72)
    assert got == NOW - timedelta(hours=72)


def test_max_backfill_shorter_than_today_keeps_midnight(tmp_path):
    """A backfill narrower than the elapsed day must not tighten the window."""
    store = _store(tmp_path)
    got = store.window_start("s", now=NOW, max_backfill_hours=2)
    assert got == datetime(2026, 8, 23, 0, 0)


def test_cursor_still_wins_over_backfill(tmp_path):
    """Backfill only widens the floor; a newer cursor must still gate."""
    store = _store(tmp_path)
    cursor_at = NOW - timedelta(minutes=30)
    store.record_fetch("s", cursor_at)

    got = store.window_start("s", now=NOW, max_backfill_hours=72)
    assert got == cursor_at


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_cursor_survives_reload(tmp_path):
    path = tmp_path / "cursor.json"
    FetchCursorStore(path=path).record_fetch("s", NOW, item_count=8)

    reloaded = FetchCursorStore(path=path)
    assert reloaded.last_fetch_at("s") == NOW


def test_record_fetch_writes_readable_json(tmp_path):
    path = tmp_path / "cursor.json"
    FetchCursorStore(path=path).record_fetch("s", NOW, item_count=8)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sources"]["s"]["last_item_count"] == 8
    assert data["sources"]["s"]["last_fetch_at"] == NOW.isoformat()
