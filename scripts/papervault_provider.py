"""PaperVault corpus provider used by the conference pipeline.

PaperVault aggregates metadata for 120 CS conference series (649k papers,
2000-2026) into a public Hugging Face dataset.  Each JSONL record only has
six fields (``conf``, ``paper_name``, ``paper_authors``, ``paper_url``,
``paper_abstract``, ``paper_code``) — no reviews, decisions, dates, or IDs —
so this provider derives a stable identity from conf+title and emits the
same normalized paper dict the OpenReview provider produces.  The decompressed
cache is a multi-GB JSONL stream, so it is read line-by-line with a
line-number cursor for resumable checkpointing.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable, Iterator
import unicodedata

from openreview_provider import SubmissionPage, VenueCapabilities

DEFAULT_HF_REPO_ID = "youngfish42/PaperVault"
DEFAULT_HF_FILENAME = "cache/cache.jsonl.gz"

# PaperVault encodes the venue series plus a 4-digit year (e.g. ``NIPS2023``,
# ``CVPR2025``); NeurIPS keeps its historical ``NIPS`` prefix.
_CONF_PATTERN = re.compile(r"^([A-Za-z]+)(\d{4})$")

Downloader = Callable[[str, str, str], Path]


class PaperVaultProviderError(RuntimeError):
    """Base error for a single PaperVault source."""


class PaperVaultConfigError(PaperVaultProviderError):
    """Raised when source configuration is incomplete or invalid."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def normalize_title(title: Any) -> str:
    text = unicodedata.normalize("NFKC", str(title or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def parse_conf(conf: Any) -> tuple[str, int] | None:
    """Split a PaperVault ``conf`` value into ``(series, year)``."""

    match = _CONF_PATTERN.match(str(conf or "").strip())
    if not match:
        return None
    return match.group(1).upper(), int(match.group(2))


def _http_url(value: Any) -> str:
    text = str(value or "").strip()
    return text if text.startswith(("http://", "https://")) else ""


class PaperVaultProvider:
    """Stream one venue-series-filtered view of the PaperVault corpus."""

    # The corpus has no forum replies, so unchanged records never need to be
    # re-polled; the conference pipeline uses this to skip re-staging work.
    recheck_unchanged = False

    def __init__(self, config: dict, downloader: Downloader | None = None):
        self.config = config
        self.repo_id = str(config.get("hf_repo_id") or DEFAULT_HF_REPO_ID).strip()
        self.filename = str(config.get("hf_filename") or DEFAULT_HF_FILENAME).strip()
        series = config.get("venue_series") or []
        self.series_whitelist = {str(s).strip().upper() for s in series if str(s).strip()}
        if not self.series_whitelist:
            raise PaperVaultConfigError("venue_series is required and must not be empty")
        self.min_year = int(config.get("min_year") or 0)
        self.venue_label = (
            str(config.get("venue_label") or "").strip()
            or f"papervault:{'+'.join(sorted(self.series_whitelist))}"
        )
        self._downloader = downloader or self._default_downloader

    @staticmethod
    def _default_downloader(repo_id: str, filename: str, repo_type: str) -> Path:
        try:
            from huggingface_hub import hf_hub_download
        except ImportError as exc:
            raise PaperVaultConfigError(
                "huggingface_hub is not installed; run `uv sync` or `dailyinfo install`"
            ) from exc
        return Path(
            hf_hub_download(repo_id=repo_id, filename=filename, repo_type=repo_type)
        )

    def _download(self) -> Path:
        path = self._downloader(self.repo_id, self.filename, "dataset")
        if not Path(path).exists():
            raise PaperVaultProviderError(
                f"PaperVault download returned a missing file: {path}"
            )
        return Path(path)

    def discover_venue(self) -> VenueCapabilities:
        # Config-derived and therefore stable across runs: these values feed
        # the run config hash and the venues table.
        return VenueCapabilities(
            venue_id=self.venue_label,
            submission_invitation=f"papervault:{self.filename}",
        )

    def _matches(self, record: dict) -> bool:
        parsed = parse_conf(record.get("conf"))
        if parsed is None:
            return False
        series, year = parsed
        if series not in self.series_whitelist:
            return False
        return not self.min_year or year >= self.min_year

    def iter_submission_pages(
        self,
        capabilities: VenueCapabilities,
        min_cdate: int | None = None,
        after_id: str | None = None,
        page_size: int = 5000,
        total_hint: int | None = None,
    ) -> Iterator[SubmissionPage]:
        """Yield pages of normalized records with a line-number cursor.

        ``after_id`` is the 1-based raw line number last consumed.  Resume
        still decompresses from the start of the gzip stream, but skipped
        lines avoid JSON parsing and filtering entirely.  The cursor is only
        stable within one HF snapshot; ``hf_hub_download`` caches per commit,
        so a resumed run almost always continues on the same file.  If the
        upstream file changed in between, the identity dedup downstream turns
        the worst case into a delayed discovery, never duplicate events.
        """

        del min_cdate, total_hint  # the corpus has no dates; scans are full-pass
        skip_until = int(after_id or 0)
        page_size = max(1, int(page_size))
        path = self._download()
        papers: list[dict] = []
        page_number = 0
        lines_in_page = 0
        last_line_no = skip_until
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line_no, line in enumerate(handle, 1):
                if line_no <= skip_until:
                    continue
                lines_in_page += 1
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or not self._matches(record):
                    continue
                papers.append(self.normalize_record(record))
                if len(papers) >= page_size:
                    page_number += 1
                    last_line_no = line_no
                    yield SubmissionPage(
                        papers=papers,
                        cursor_after=str(last_line_no),
                        total=None,
                        page_number=page_number,
                        raw_count=lines_in_page,
                    )
                    papers = []
                    lines_in_page = 0
        if papers or lines_in_page:
            page_number += 1
            # The trailing partial page consumed every remaining line; the
            # cursor must land on the last line of the file even when the
            # final buffered chunk ends on a line that failed JSON parsing.
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as tail:
                last_line_no = max(last_line_no, skip_until + sum(1 for _ in tail))
            yield SubmissionPage(
                papers=papers,
                cursor_after=str(last_line_no),
                total=None,
                page_number=page_number,
                raw_count=lines_in_page,
            )

    def fetch_forum(self, forum_id: str, capabilities: VenueCapabilities):
        """PaperVault snapshots carry no replies; the staged paper is reused."""

        del forum_id, capabilities
        return None, []

    def normalize_record(self, record: dict) -> dict:
        conf = str(record.get("conf") or "").strip()
        title = str(record.get("paper_name") or "").strip()
        identity = "pv-" + _stable_hash(
            {"conf": conf, "title": normalize_title(title)}
        )[:16]
        url = _http_url(record.get("paper_url"))
        authors = record.get("paper_authors")
        if not isinstance(authors, list):
            authors = [authors] if authors else []
        return {
            "id": identity,
            "forum_id": identity,
            "number": None,
            "title": title,
            "abstract": str(record.get("paper_abstract") or "").strip(),
            "authors": [str(author) for author in authors],
            "keywords": [],
            "venue": conf,
            "venue_id": conf,
            "status": "accepted",
            "pdf": url,
            "forum_url": url,
            "code_url": _http_url(record.get("paper_code")),
            "cdate": 0,
            "mdate": 0,
            "invitations": [],
            "camera_ready": False,
        }
