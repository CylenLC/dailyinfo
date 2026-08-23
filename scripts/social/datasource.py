"""SocialDataSource — fetch items from social platforms via Agent-Reach."""

from datetime import datetime, timedelta
from typing import Any

from scripts.datasource import DataSource, Item, NOW
from scripts.social.agent_reach import AgentReachAdapter, ReachCapabilities
from scripts.social.models import SocialItem
from scripts.social.normalize import _parse_twitter_search, _parse_twitter_user_posts
from scripts.social.seen_store import stable_social_identity


class SocialDataSource(DataSource):
    """DataSource subclass for social intelligence sources.

    Requires `reach` (AgentReachAdapter) and optionally `researchers` config
    in the context dict.
    """

    def __init__(
        self,
        config: dict,
        defaults: dict,
        reach: AgentReachAdapter | None = None,
        researchers: dict | None = None,
    ):
        super().__init__(config, defaults)
        self.reach = reach or AgentReachAdapter()
        self.researchers = researchers or {}
        self._capabilities: ReachCapabilities | None = None

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
        """
        mode = self.config.get("mode", "search")

        if mode == "researcher_watch":
            return self._fetch_researchers()
        elif mode == "search":
            return self._fetch_search()
        else:
            print(
                f"  [WARN] {self.name}: unknown mode {mode!r}, skipping",
                flush=True,
            )
            return []

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

            try:
                social_items = self.reach.x_user_posts(
                    handle,
                    limit=max_per_researcher,
                )

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
    # Helpers
    # ------------------------------------------------------------------

    def _within_lookback(self, published_at: datetime) -> bool:
        """Check if item is within the configured lookback window."""
        cutoff = NOW - timedelta(hours=self.lookback_hours)
        # published_at is naive UTC, cutoff is naive Beijing — approximate check
        # Since we don't know exact timezone of published_at, use a generous window
        lookback_seconds = self.lookback_hours * 3600
        now_ts = datetime.utcnow().timestamp()
        pub_ts = published_at.timestamp()
        age_seconds = now_ts - pub_ts
        return age_seconds <= lookback_seconds

    def _to_item(self, social: SocialItem, researcher: dict | None = None) -> Item:
        """Convert SocialItem to DailyInfo Item."""
        item = social.to_dailyinfo_item(datetime.utcnow().strftime("%Y-%m-%d"))

        if researcher:
            item.extra["researcher"] = {
                "name": researcher.get("name", ""),
                "handle": researcher.get("handle", ""),
                "topics": researcher.get("topics", []),
                "weight": researcher.get("weight", 1.0),
            }
            item.extra["source_ref"] = f"researcher:{researcher.get('handle', '')}"

        return item
