"""Per-source incremental fetch cursors.

Pipeline 7 used to filter items with a fixed ``lookback_hours`` window, which
meant every run re-considered the same 24 hours of tweets and relied entirely
on the seen-store to suppress them. This module records *when a source was last
fetched* so each run only admits items published since then.

The window a run uses is ``[lower_bound, now]`` where::

    lower_bound = max(last_fetch_at, start_of_today_utc)   # if cursor exists
                = start_of_today_utc                       # first ever run

Bounding by the start of the current UTC day is what makes "only today's
tweets" hold on the very first run and after a long gap (e.g. the machine was
off for a week) — without it, a stale cursor would drag in a week of backlog.

All timestamps are **naive UTC**, matching ``social.models.utcnow_naive`` and
``social.normalize.parse_twitter_timestamp_strict``.
"""

import json
import pathlib
from datetime import datetime, timedelta

from social.models import utcnow_naive

_STATE_DIR = pathlib.Path.home() / ".myagentdata" / "dailyinfo" / "state"


def start_of_utc_day(moment: datetime | None = None) -> datetime:
    """Return midnight (naive UTC) of the day containing ``moment``."""
    ref = moment or utcnow_naive()
    return ref.replace(hour=0, minute=0, second=0, microsecond=0)


class FetchCursorStore:
    """Persistent ``source name -> last successful fetch time`` map."""

    def __init__(self, path: pathlib.Path | None = None):
        self.path = path or (_STATE_DIR / "social_fetch_cursor.json")
        self._data: dict = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "sources": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "sources": {}}
        if isinstance(raw, dict) and isinstance(raw.get("sources"), dict):
            return raw
        return {"version": 1, "sources": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def last_fetch_at(self, source_name: str) -> datetime | None:
        """Return the last recorded fetch time, or ``None`` if never fetched."""
        entry = self._data.get("sources", {}).get(source_name)
        if not isinstance(entry, dict):
            return None
        raw = entry.get("last_fetch_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except (ValueError, TypeError):
            return None

    def window_start(
        self,
        source_name: str,
        *,
        now: datetime | None = None,
        max_lookback_hours: int | None = None,
        max_backfill_hours: int | None = None,
    ) -> datetime:
        """Compute the lower bound of this run's fetch window (naive UTC).

        By default never earlier than the start of the current UTC day, so a
        missing or very old cursor cannot pull in yesterday's backlog.

        ``max_lookback_hours`` *tightens* the bound: it is never further back
        than that many hours (a rolling window instead of a calendar day).

        ``max_backfill_hours`` *loosens* it past midnight, for low-volume
        sources where a calendar day contains no matching posts at all. This
        stays safe because the cursor still applies on top — once a run has
        seen a window, the cursor moves past it, so a wider floor only matters
        on a source's very first run (or after a gap) and never re-admits
        anything already committed.
        """
        ref = now or utcnow_naive()
        floor = start_of_utc_day(ref)

        if max_backfill_hours is not None:
            # The *earlier* of the two is the looser constraint.
            floor = min(floor, ref - timedelta(hours=max_backfill_hours))

        if max_lookback_hours is not None:
            rolling_floor = ref - timedelta(hours=max_lookback_hours)
            # The *later* of the two floors is the tighter constraint.
            floor = max(floor, rolling_floor)

        cursor = self.last_fetch_at(source_name)
        if cursor is None:
            return floor
        return max(cursor, floor)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def record_fetch(
        self,
        source_name: str,
        fetched_at: datetime | None = None,
        *,
        item_count: int | None = None,
    ) -> None:
        """Advance a source's cursor and persist immediately."""
        moment = fetched_at or utcnow_naive()
        sources = self._data.setdefault("sources", {})
        entry = sources.setdefault(source_name, {})
        entry["last_fetch_at"] = moment.isoformat()
        if item_count is not None:
            entry["last_item_count"] = item_count
        self._save()
