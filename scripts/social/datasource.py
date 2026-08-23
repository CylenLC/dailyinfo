"""SocialDataSource — fetch items from social platforms via Agent-Reach."""

import re
from datetime import datetime, timedelta

from datasource import DataSource, Item
from social.agent_reach import AgentReachAdapter, ReachCapabilities
from social.fetch_cursor import FetchCursorStore
from social.models import SocialItem, utcnow_naive
from social.seen_store import stable_social_identity

# X/Twitter handles: 1-15 chars, letters/digits/underscore only. A handle like
# "@szymon-sidor" can never resolve, and the backend just returns 0 items with
# no error — so reject it up front and say why.
_HANDLE_RE = re.compile(r"^@[A-Za-z0-9_]{1,15}$")

# Tolerance on the window's upper bound. The window end is snapshotted before
# the backend call, so posts created mid-fetch arrive with a later timestamp.
_FUTURE_SKEW_GRACE = timedelta(hours=1)


def build_topic_matcher(terms: list[str] | None):
    """Build a whole-word topic matcher, or None when no filter is configured.

    Plain substring matching is unusable for short terms: ``"ai"`` matches
    "repair"/"again"/"said", so a watch-servicing anecdote reads as an AI post.
    Terms are matched on word boundaries instead. Multi-word terms (``"data
    center"``) allow any run of whitespace so "data  center" still matches, and
    a trailing plural ``s`` is accepted so "datacenters" matches "datacenter".
    """
    cleaned = [t.strip().lower() for t in (terms or []) if t and t.strip()]
    if not cleaned:
        return None

    patterns = []
    for term in cleaned:
        core = r"\s+".join(re.escape(w) for w in term.split())
        # ``\b`` only asserts a word/non-word transition, so it never fires next
        # to a symbol: "c++" would be unmatchable with a trailing \b. Pick the
        # boundary style from the term's own edge characters.
        prefix = r"\b" if term[0].isalnum() or term[0] == "_" else r"(?<!\w)"
        if term[-1].isalnum() or term[-1] == "_":
            suffix = r"s?\b"
        else:
            suffix = r"(?!\w)"
        patterns.append(prefix + core + suffix)

    compiled = re.compile("|".join(patterns))

    def matches(text: str) -> bool:
        return bool(compiled.search(text.lower()))

    return matches


class SocialDataSource(DataSource):
    """DataSource subclass for social intelligence sources.

    Requires `reach` (AgentReachAdapter) and optionally `researchers` config
    in the context dict.

    Fetching is **incremental**: each run only admits items published after the
    previous successful fetch (never earlier than the start of the current UTC
    day). See :mod:`social.fetch_cursor`.
    """

    def __init__(
        self,
        config: dict,
        defaults: dict,
        reach: AgentReachAdapter | None = None,
        researchers: dict | None = None,
        cursor_store: FetchCursorStore | None = None,
    ):
        super().__init__(config, defaults)
        self.reach = reach or AgentReachAdapter()
        self.researchers = researchers or {}
        self._capabilities: ReachCapabilities | None = None
        self.cursor_store = cursor_store or FetchCursorStore()
        # Resolved once per instance so every item in a run is judged against
        # the same window, and so the value can be logged/asserted.
        self._window_start: datetime | None = None
        self._window_end: datetime | None = None

    # ------------------------------------------------------------------
    # Stable identity override
    # ------------------------------------------------------------------

    def item_identity(self, item: Item) -> str:
        """Use canonical_id for social items (platform:item_id)."""
        return stable_social_identity(item)

    # ------------------------------------------------------------------
    # Capability probe (cached)
    # ------------------------------------------------------------------

    def _get_capabilities(self) -> ReachCapabilities:
        if self._capabilities is None:
            self._capabilities = self.reach.probe()
        return self._capabilities

    # ------------------------------------------------------------------
    # Fetch dispatch
    # ------------------------------------------------------------------

    def fetch(self) -> list[Item]:
        """Fetch items based on configured mode.

        Supports:
        - researcher_watch: fetch posts from configured researchers
        - search: fetch search results for configured queries

        Results are passed through the per-source seen filter (keyed on
        ``canonical_id``) so items already committed by a previous run are
        dropped, matching the behaviour of the RSS/scrape/API sources.
        """
        mode = self.config.get("mode", "search")

        # Resolve the incremental window once for this whole fetch.
        self._window_end = utcnow_naive()
        self._window_start = self.cursor_store.window_start(
            self.name,
            now=self._window_end,
            max_lookback_hours=self.config.get("max_lookback_hours"),
            max_backfill_hours=self.config.get("max_backfill_hours"),
        )

        if mode == "researcher_watch":
            items = self._fetch_researchers()
        elif mode == "search":
            items = self._fetch_search()
        elif mode == "timeline":
            items = self._fetch_timeline()
        elif mode == "list":
            items = self._fetch_list()
        else:
            print(
                f"  [WARN] {self.name}: unknown mode {mode!r}, skipping",
                flush=True,
            )
            return []

        return self._filter_seen(items)

    @property
    def fetch_window(self) -> tuple[datetime | None, datetime | None]:
        """The ``(start, end)`` naive-UTC window used by the last ``fetch()``.

        Both entries are ``None`` before the first fetch.
        """
        return self._window_start, self._window_end

    def commit_fetch_cursor(self, item_count: int | None = None) -> None:
        """Advance this source's cursor to the end of the last fetch window.

        Called by the pipeline only after the briefing is saved, so a failed
        run re-examines the same window instead of skipping past it.
        """
        self.cursor_store.record_fetch(
            self.name,
            self._window_end or utcnow_naive(),
            item_count=item_count,
        )

    # ------------------------------------------------------------------
    # Researcher watch
    # ------------------------------------------------------------------

    def _fetch_researchers(self) -> list[Item]:
        """Fetch posts from configured researcher handles."""
        group_name = self.config.get("watchlist", "")
        groups = self.researchers.get("groups", {})
        researcher_list = groups.get(group_name, [])

        if not researcher_list:
            print(
                f"  [WARN] {self.name}: no researchers in group {group_name!r}",
                flush=True,
            )
            return []

        all_items = []
        max_per_researcher = self.config.get("max_items_per_researcher", 10)

        for researcher in researcher_list:
            if not researcher.get("enabled", True):
                continue

            handle = researcher.get("handle", "")
            if not handle:
                continue

            if not _HANDLE_RE.match(handle):
                # Would silently return 0 items forever otherwise.
                print(
                    f"  [WARN] {self.name}: invalid X handle {handle!r} "
                    "(letters/digits/underscore only, max 15) — skipping",
                    flush=True,
                )
                continue

            try:
                social_items = self.reach.x_user_posts(
                    handle,
                    limit=max_per_researcher,
                )

                if not social_items:
                    print(
                        f"  [WARN] {self.name}: {handle} returned no posts "
                        "(renamed, protected, or rate-limited?)",
                        flush=True,
                    )
                    continue

                for social in social_items:
                    # Filter to lookback window
                    if not self._within_lookback(social.published_at):
                        continue

                    # Convert to DailyInfo Item
                    item = self._to_item(social, researcher=researcher)
                    all_items.append(item)

            except Exception as exc:
                # Single researcher failure must not block others
                print(
                    f"  [WARN] {self.name}: fetch failed for {handle}: {exc}",
                    flush=True,
                )
                continue

        return all_items

    # ------------------------------------------------------------------
    # Keyword search
    # ------------------------------------------------------------------

    def _fetch_search(self) -> list[Item]:
        """Fetch search results for configured queries."""
        queries = self.config.get("queries") or [self.config.get("query", "")]
        queries = [q for q in queries if q]  # Filter empty

        if not queries:
            print(
                f"  [WARN] {self.name}: no queries configured",
                flush=True,
            )
            return []

        all_items = []
        max_per_query = self.config.get("max_items_per_query", 10)
        seen_canonical: dict[str, bool] = {}

        for query in queries:
            try:
                social_items = self.reach.x_search(query, limit=max_per_query)

                for social in social_items:
                    # Filter to lookback window
                    if not self._within_lookback(social.published_at):
                        continue

                    # Deduplicate within same run across queries
                    cid = social.canonical_id
                    if cid in seen_canonical:
                        # Merge matched queries for duplicate items
                        for existing in all_items:
                            if stable_social_identity(existing) == cid:
                                existing.extra.setdefault("matched_queries", [])
                                if query not in existing.extra["matched_queries"]:
                                    existing.extra["matched_queries"].append(query)
                                break
                        continue

                    seen_canonical[cid] = True

                    # Convert to DailyInfo Item
                    item = self._to_item(social)
                    item.extra["query"] = query
                    item.extra["matched_queries"] = [query]

                    all_items.append(item)

            except Exception as exc:
                print(
                    f"  [WARN] {self.name}: search failed for {query!r}: {exc}",
                    flush=True,
                )
                continue

        return all_items

    # ------------------------------------------------------------------
    # Timeline / List (no keywords needed)
    # ------------------------------------------------------------------

    def _fetch_timeline(self) -> list[Item]:
        """Fetch the home timeline — "what is being discussed today".

        Unlike ``search`` this needs no keywords, but it also has no inherent
        topic scope, so ``topic_filter`` is applied when configured.
        """
        limit = self.config.get("max_items_per_fetch", 40)
        feed_type = self.config.get("feed_type", "following")

        try:
            social_items = self.reach.x_timeline(limit, feed_type=feed_type)
        except Exception as exc:
            print(f"  [WARN] {self.name}: timeline fetch failed: {exc}", flush=True)
            return []

        if not social_items:
            print(
                f"  [WARN] {self.name}: timeline returned no posts "
                f"(feed_type={feed_type!r}, session expired or rate-limited?)",
                flush=True,
            )
            return []

        return self._collect(social_items, origin=f"timeline:{feed_type}")

    def _fetch_list(self) -> list[Item]:
        """Fetch a curated X List — topic scope without keyword guessing."""
        list_id = str(self.config.get("list_id", "")).strip()
        if not list_id:
            print(f"  [WARN] {self.name}: no list_id configured", flush=True)
            return []

        limit = self.config.get("max_items_per_fetch", 40)
        try:
            social_items = self.reach.x_list(list_id, limit)
        except Exception as exc:
            print(f"  [WARN] {self.name}: list fetch failed: {exc}", flush=True)
            return []

        if not social_items:
            print(
                f"  [WARN] {self.name}: list {list_id} returned no posts "
                f"(wrong id, private list, or rate-limited?)",
                flush=True,
            )
            return []

        return self._collect(social_items, origin=f"list:{list_id}")

    def _collect(self, social_items: list, *, origin: str) -> list[Item]:
        """Window-filter, topic-filter and de-duplicate a batch of items."""
        topic_matcher = build_topic_matcher(self.config.get("topic_filter"))
        min_engagement = self.config.get("min_engagement", 0)

        items: list[Item] = []
        seen_canonical: set[str] = set()
        dropped_topic = 0
        dropped_engagement = 0

        for social in social_items:
            if not self._within_lookback(social.published_at):
                continue

            cid = social.canonical_id
            if cid in seen_canonical:
                continue

            if topic_matcher and not topic_matcher(social.text or ""):
                dropped_topic += 1
                continue

            if min_engagement:
                # SocialItem carries flat metrics (likes/reposts), not a dict.
                score = (social.likes or 0) + (social.reposts or 0)
                if score < min_engagement:
                    dropped_engagement += 1
                    continue

            seen_canonical.add(cid)
            item = self._to_item(social)
            item.extra["origin"] = origin
            items.append(item)

        if dropped_topic or dropped_engagement:
            print(
                f"    {self.name}: dropped {dropped_topic} off-topic, "
                f"{dropped_engagement} low-engagement",
                flush=True,
            )
        return items

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _within_lookback(self, published_at: datetime) -> bool:
        """Check whether an item falls inside this run's incremental window.

        The window is ``(window_start, window_end]`` where ``window_start`` is
        the later of "last successful fetch" and "start of the current UTC day"
        — see :mod:`social.fetch_cursor`. That is what makes each run pick up
        only tweets posted since the previous run, bounded to today, instead of
        re-scanning a fixed ``lookback_hours`` slab on every run.

        ``published_at`` (from ``parse_twitter_timestamp_strict``) and
        ``utcnow_naive()`` are both naive UTC, so direct comparison is correct.
        Do not compare against ``datasource.NOW`` here — that is naive
        *Beijing* time and would skew the window by 8 hours.
        """
        if self._window_start is None:
            # fetch() always sets this; guard covers direct unit-test calls.
            self._window_end = utcnow_naive()
            self._window_start = self.cursor_store.window_start(
                self.name,
                now=self._window_end,
                max_lookback_hours=self.config.get("max_lookback_hours"),
                max_backfill_hours=self.config.get("max_backfill_hours"),
            )

        if published_at <= self._window_start:
            return False

        # Upper bound only guards against absurd timestamps (parse errors, a
        # badly skewed clock). It is deliberately generous: ``_window_end`` is
        # snapshotted before the backend call, so a tweet posted during the
        # fetch legitimately carries a slightly later timestamp and must not be
        # dropped — if it were, the cursor would advance past it and it would
        # never be pushed.
        end = (self._window_end or utcnow_naive()) + _FUTURE_SKEW_GRACE
        return published_at <= end

    def _to_item(self, social: SocialItem, researcher: dict | None = None) -> Item:
        """Convert SocialItem to DailyInfo Item."""
        item = social.to_dailyinfo_item(utcnow_naive().strftime("%Y-%m-%d"))

        if researcher:
            item.extra["researcher"] = {
                "name": researcher.get("name", ""),
                "handle": researcher.get("handle", ""),
                "topics": researcher.get("topics", []),
                "weight": researcher.get("weight", 1.0),
            }
            item.extra["source_ref"] = f"researcher:{researcher.get('handle', '')}"

        return item
