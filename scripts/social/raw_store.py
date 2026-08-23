"""Raw social data persistence — saves before dedup/AI so nothing is lost."""

import json
import pathlib
from typing import Any

from social.models import SocialItem, utcnow_naive

_RAW_ROOT = pathlib.Path.home() / ".myagentdata" / "dailyinfo" / "raw" / "social"


# ---------------------------------------------------------------------------
# Raw store
# ---------------------------------------------------------------------------


class SocialRawStore:
    """Persist raw SocialItem batches to raw/social/<date>/<run_id>/."""

    def __init__(self, base_dir: pathlib.Path | None = None):
        self.base_dir = base_dir or _RAW_ROOT

    def write_run(
        self,
        run_id: str,
        agent_reach_version: str | None,
        twitter_backend: str | None,
        source_results: dict[str, dict[str, Any]],
        items_by_source: dict[str, list[SocialItem]],
    ) -> pathlib.Path:
        """Write raw run data to disk.

        Creates:
            <base>/<YYYY-MM-DD>/<run_id>/
                manifest.json
                <source1>.jsonl
                <source2>.jsonl

        Args:
            run_id: ISO timestamp run identifier
            agent_reach_version: Agent-Reach version string
            twitter_backend: Active Twitter backend (e.g. "twitter-cli")
            source_results: Per-source fetch metadata (status, fetched, error)
            items_by_source: Per-source SocialItem lists

        Returns:
            Path to the run directory
        """
        date_str = run_id[:10]  # YYYY-MM-DD
        run_dir = self.base_dir / date_str / run_id
        run_dir.mkdir(parents=True, exist_ok=True)

        # Write manifest
        manifest = {
            "schema_version": 1,
            "run_id": run_id,
            "started_at": utcnow_naive().isoformat(),
            "agent_reach": {
                "version": agent_reach_version,
                "twitter_backend": twitter_backend,
            },
            "sources": source_results,
        }
        (run_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # Write per-source JSONL
        for source_name, items in items_by_source.items():
            self._write_jsonl(run_dir / f"{source_name}.jsonl", items)

        return run_dir

    def _write_jsonl(self, path: pathlib.Path, items: list[SocialItem]) -> None:
        """Write SocialItem list as JSONL (one JSON object per line)."""
        lines = []
        for item in items:
            record = _social_item_to_json(item)
            lines.append(json.dumps(record, ensure_ascii=False))

        path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def _social_item_to_json(item: SocialItem) -> dict[str, Any]:
    """Convert SocialItem to JSON-serializable dict for raw storage."""
    return {
        "schema_version": 1,
        "canonical_id": item.canonical_id,
        "platform": item.platform,
        "item_id": item.item_id,
        "author_name": item.author_name,
        "author_handle": item.author_handle,
        "text": item.text,
        "url": item.url,
        "published_at": item.published_at.isoformat(),
        "engagement": {
            "likes": item.likes,
            "reposts": item.reposts,
            "replies": item.replies,
            "quotes": item.quotes,
        },
        "backend": item.backend,
        "retrieval": {
            "mode": item.source_mode,
            "source": item.source_ref,
        },
        "fetched_at": utcnow_naive().isoformat(),
    }
