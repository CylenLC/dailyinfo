"""Agent-Reach adapter — wraps `agent-reach` CLI as a stable DailyInfo interface."""

import json
import os
import shutil
import subprocess
import sys
import time

from social.models import CommandResult, ReachCapabilities
from social.normalize import (
    _parse_opencli_twitter,
    _parse_twitter_search,
    _parse_twitter_user_posts,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def _resolve_bin(name: str) -> str:
    """Resolve a console script installed alongside the running interpreter.

    Both agent-reach and twitter-cli are declared dependencies (see the
    ``social`` extra), so their entry points live in the same bin/ directory as
    ``sys.executable``. Prefer that over PATH so a venv run never picks up a
    stale global install, and fall back to the bare name for PATH lookup.
    """
    candidate = os.path.join(os.path.dirname(sys.executable), name)
    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which(name) or name


_AGENT_REACH_BIN = _resolve_bin("agent-reach")
_TWITTER_BIN = _resolve_bin("twitter")
# `doctor --json` probes 12 channels and measured 8.7-10.2s warm on this host,
# so 15s raced the cold path — a timeout silently degrades every capability to
# False and Pipeline 7 then reports "0 items" with no error.
_DOCTOR_TIMEOUT = 60
_FETCH_TIMEOUT = 45


# ---------------------------------------------------------------------------
# ReachCommandRunner — single entry point for all subprocess calls
# ---------------------------------------------------------------------------


class ReachCommandRunner:
    """Execute Agent-Reach subprocess commands with consistent safety rules."""

    def run(
        self,
        argv: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = _FETCH_TIMEOUT,
    ) -> CommandResult:
        """Run an agent-reach subcommand and return structured output.

        Never logs full argv with credentials. Handles timeout, non-zero exit,
        and empty/malformed output.
        """
        start = time.monotonic()

        # Build safe environment
        safe_env = os.environ.copy()
        if env:
            safe_env.update(env)

        try:
            proc = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=safe_env,
            )
        except subprocess.TimeoutExpired:
            elapsed = int((time.monotonic() - start) * 1000)
            return CommandResult(
                stdout="",
                stderr=f"timeout after {timeout}s",
                returncode=-1,
                backend=argv[0] if argv else "unknown",
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            elapsed = int((time.monotonic() - start) * 1000)
            return CommandResult(
                stdout="",
                stderr=str(exc),
                returncode=-1,
                backend=argv[0] if argv else "unknown",
                elapsed_ms=elapsed,
            )

        elapsed = int((time.monotonic() - start) * 1000)
        return CommandResult(
            stdout=proc.stdout,
            stderr=proc.stderr,
            returncode=proc.returncode,
            backend=argv[0] if argv else "unknown",
            elapsed_ms=elapsed,
        )


# ---------------------------------------------------------------------------
# AgentReachAdapter — stable DailyInfo interface
# ---------------------------------------------------------------------------


class AgentReachAdapter:
    """Wrap Agent-Reach capabilities behind a stable DailyInfo contract.

    DailyInfo never calls twitter-cli / OpenCLI directly. All backend
    routing, credential lookup, and fallback logic lives here.
    """

    def __init__(self, runner: ReachCommandRunner | None = None):
        self.runner = runner or ReachCommandRunner()
        self._capabilities: ReachCapabilities | None = None

    # ------------------------------------------------------------------
    # Capability probe (run once per Pipeline 6 invocation)
    # ------------------------------------------------------------------

    @staticmethod
    def _installed_version() -> str | None:
        """Read the installed agent-reach version from package metadata."""
        try:
            import importlib.metadata as md

            return md.version("agent-reach")
        except Exception:
            return None

    def probe(self) -> ReachCapabilities:
        """Run `agent-reach doctor --json` and return capability snapshot."""
        if self._capabilities is not None:
            return self._capabilities

        result = self.runner.run(
            [_AGENT_REACH_BIN, "doctor", "--json"],
            timeout=_DOCTOR_TIMEOUT,
        )

        if result.returncode != 0:
            # Doctor failed — return degraded capabilities
            self._capabilities = ReachCapabilities(
                agent_reach_version=None,
                twitter_available=False,
                twitter_backend=None,
                opencli_available=False,
                diagnostics={"error": result.stderr or "doctor command failed"},
            )
            return self._capabilities

        try:
            data = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            self._capabilities = ReachCapabilities(
                agent_reach_version=None,
                twitter_available=False,
                twitter_backend=None,
                opencli_available=False,
                diagnostics={"error": "doctor output is not valid JSON"},
            )
            return self._capabilities

        # Parse doctor output (supports both v0.1.0 and v1.5.0 formats)
        twitter_info = data.get("twitter", {})
        twitter_status = twitter_info.get("status", "off")
        twitter_backend = twitter_info.get("active_backend")

        opencli_info = data.get("opencli", {})
        opencli_status = opencli_info.get("status", "off")

        # Version: `doctor --json` emits only per-channel objects (no top-level
        # "version" key), so fall back to installed package metadata. Without
        # this the log always read "Agent Reach unknown".
        version = data.get("version") or self._installed_version()

        # Twitter is available if status is "ok" (ready) or "warn" (installed but needs config)
        # "warn" means twitter-cli is installed but needs Cookie-Editor configuration
        twitter_ready = twitter_status in ("ok", "warn")
        opencli_ready = opencli_status in ("ok", "warn")

        # active_backend is null until a channel is actually exercised, so fall
        # back to the first advertised backend for display purposes.
        if twitter_ready and not twitter_backend:
            advertised = twitter_info.get("backends") or []
            twitter_backend = advertised[0] if advertised else None

        self._capabilities = ReachCapabilities(
            agent_reach_version=version,
            twitter_available=twitter_ready,
            twitter_backend=twitter_backend if twitter_ready else None,
            opencli_available=opencli_ready,
            diagnostics=data,
        )

        return self._capabilities

    # ------------------------------------------------------------------
    # X / Twitter operations
    # ------------------------------------------------------------------

    def x_user_posts(
        self,
        handle: str,
        limit: int,
        *,
        preferred_backend: str = "twitter-cli",
    ) -> list:
        """Fetch recent posts from a Twitter/X user.

        Uses `twitter user-posts @handle -n limit` as primary path.

        Args:
            handle: Twitter handle (e.g. "@karpathy")
            limit: Max posts to fetch
            preferred_backend: Preferred backend (twitter-cli | OpenCLI)

        Returns:
            List of SocialItem (possibly empty on failure)
        """
        caps = self.probe()

        # twitter-cli is a declared dependency (see the `social` extra), so its
        # `twitter` console script is on PATH — no local-checkout fallback.
        if preferred_backend == "twitter-cli" and caps.twitter_available:
            items = self._x_user_posts_twitter_cli(handle, limit)
            if items:
                return items

        # Fallback to OpenCLI
        if caps.opencli_available:
            items = self._x_user_posts_opencli(handle, limit)
            if items:
                return items

        # Both failed — return empty (caller logs the failure)
        return []

    def x_search(
        self,
        query: str,
        limit: int,
        *,
        preferred_backend: str = "twitter-cli",
    ) -> list:
        """Search X/Twitter for a keyword query.

        Uses `twitter search "query" -n limit` as primary path with retry.

        Args:
            query: Search query string
            limit: Max results to fetch
            preferred_backend: Preferred backend (twitter-cli | OpenCLI)

        Returns:
            List of SocialItem (possibly empty on failure)
        """
        caps = self.probe()

        # twitter-cli is a declared dependency (see the `social` extra), so its
        # `twitter` console script is on PATH — no local-checkout fallback.
        if preferred_backend == "twitter-cli" and caps.twitter_available:
            items = self._x_search_twitter_cli(query, limit)
            if items:
                return items

            # Retry once
            items = self._x_search_twitter_cli(query, limit)
            if items:
                return items

        # Fallback to OpenCLI
        if caps.opencli_available:
            items = self._x_search_opencli(query, limit)
            if items:
                return items

        return []

    def x_timeline(
        self,
        limit: int,
        *,
        feed_type: str = "following",
    ) -> list:
        """Fetch the authenticated account's home timeline.

        This is the "what is the community actually talking about today" path,
        as opposed to ``x_search`` which needs you to guess keywords up front.

        Quality depends entirely on the follow graph: ``following`` only ever
        returns accounts the session user follows, and ``for-you`` is
        algorithmic (observed to surface spam and non-tech noise). For a
        topic-scoped briefing prefer :meth:`x_list`, which is independent of
        who the account follows.

        Args:
            limit: Max tweets to fetch
            feed_type: ``following`` (chronological, only accounts you follow)
                or ``for-you`` (algorithmic, pulls in outside accounts)

        Returns:
            List of SocialItem (possibly empty on failure)
        """
        caps = self.probe()
        if not caps.twitter_available:
            return []

        items = self._x_timeline_twitter_cli(limit, feed_type)
        if items:
            return items

        # Retry once — the timeline endpoint is flaky under rate limiting.
        return self._x_timeline_twitter_cli(limit, feed_type)

    def x_list(self, list_id: str, limit: int) -> list:
        """Fetch tweets from a curated X List.

        Preferred over :meth:`x_timeline` for a topic briefing: a List is a
        hand-picked account set, so it needs no keyword guessing *and* is not
        constrained by the session account's follow graph.

        Args:
            list_id: Numeric X List id (the digits in /i/lists/<id>)
            limit: Max tweets to fetch

        Returns:
            List of SocialItem (possibly empty on failure)
        """
        caps = self.probe()
        if not caps.twitter_available:
            return []

        items = self._x_list_twitter_cli(list_id, limit)
        if items:
            return items

        return self._x_list_twitter_cli(list_id, limit)

    # ------------------------------------------------------------------
    # Private: twitter-cli backend
    # ------------------------------------------------------------------

    def _x_timeline_twitter_cli(self, limit: int, feed_type: str) -> list:
        """Call `twitter feed -t <type> -n limit --json`.

        ``--no-include-promoted`` drops ads, which are pure noise in a
        briefing. Uses full ``--json`` for the same reason as the other
        commands: compact output has no year in its timestamps.
        """
        env = _twitter_subprocess_env()
        result = self.runner.run(
            [
                _TWITTER_BIN,
                "feed",
                "-t",
                feed_type,
                "-n",
                str(limit),
                "--no-include-promoted",
                "--json",
            ],
            env=env,
            timeout=_FETCH_TIMEOUT,
        )

        if result.returncode != 0:
            return []

        return _parse_twitter_search(result.stdout, backend="twitter-cli")

    def _x_list_twitter_cli(self, list_id: str, limit: int) -> list:
        """Call `twitter list <list_id> -n limit --json`."""
        env = _twitter_subprocess_env()
        result = self.runner.run(
            [_TWITTER_BIN, "list", str(list_id), "-n", str(limit), "--json"],
            env=env,
            timeout=_FETCH_TIMEOUT,
        )

        if result.returncode != 0:
            return []

        return _parse_twitter_search(result.stdout, backend="twitter-cli")

    def _x_user_posts_twitter_cli(self, handle: str, limit: int) -> list:
        """Call `twitter user-posts @handle -n limit --json`.

        Uses the full ``--json`` schema rather than compact ``-c``: compact
        emits ``"time": "Aug 02 03:00"`` with **no year**, which made every
        item fall back to "now" and silently disabled the lookback window.
        Full output carries ``createdAt``/``createdAtISO`` plus reply/quote
        metrics that compact drops.
        """
        env = _twitter_subprocess_env()
        result = self.runner.run(
            [_TWITTER_BIN, "user-posts", handle, "-n", str(limit), "--json"],
            env=env,
            timeout=_FETCH_TIMEOUT,
        )

        if result.returncode != 0:
            return []

        return _parse_twitter_user_posts(result.stdout, backend="twitter-cli")

    def _x_search_twitter_cli(self, query: str, limit: int) -> list:
        """Call `twitter search "query" -t latest -n limit --json`.

        ``-t latest`` is essential for a *daily* briefing. X's default "Top"
        tab ranks by relevance, so a niche research phrase returns its
        all-time best matches (observed: 2021-2026 spread for
        '"streamflow" LSTM') and almost nothing from the current window.
        """
        env = _twitter_subprocess_env()
        result = self.runner.run(
            [
                _TWITTER_BIN,
                "search",
                query,
                "-t",
                "latest",
                "-n",
                str(limit),
                "--json",
            ],
            env=env,
            timeout=_FETCH_TIMEOUT,
        )

        if result.returncode != 0:
            return []

        return _parse_twitter_search(result.stdout, backend="twitter-cli")

    # ------------------------------------------------------------------
    # Private: OpenCLI backend
    # ------------------------------------------------------------------

    def _x_user_posts_opencli(self, handle: str, limit: int) -> list:
        """Call `opencli twitter user-posts @handle -n limit`."""
        result = self.runner.run(
            ["opencli", "twitter", "user-posts", handle, "-n", str(limit)],
            timeout=_FETCH_TIMEOUT,
        )

        if result.returncode != 0:
            return []

        return _parse_opencli_twitter(result.stdout, backend="opencli", handle=handle)

    def _x_search_opencli(self, query: str, limit: int) -> list:
        """Call `opencli twitter search "query" -n limit`."""
        result = self.runner.run(
            ["opencli", "twitter", "search", query, "-n", str(limit)],
            timeout=_FETCH_TIMEOUT,
        )

        if result.returncode != 0:
            return []

        return _parse_opencli_twitter(result.stdout, backend="opencli", query=query)


# ---------------------------------------------------------------------------
# Credential helpers
# ---------------------------------------------------------------------------


def _twitter_subprocess_env() -> dict[str, str]:
    """Build environment for twitter-cli subprocess.

    Priority:
    1. Existing env vars (already set by scheduler or user)
    2. Agent-Reach config (read-only, from ~/.agent-reach/config.yaml)

    Never mutates os.environ. Never logs credentials.
    """
    env = os.environ.copy()

    # If already set, use them
    if env.get("TWITTER_AUTH_TOKEN") and env.get("TWITTER_CT0"):
        return env

    # Try Agent-Reach config (best-effort, no hard dependency)
    try:
        from agent_reach.config import Config

        cfg = Config()
        token = cfg.get("twitter_auth_token")
        ct0 = cfg.get("twitter_ct0")
        if token:
            env["TWITTER_AUTH_TOKEN"] = str(token)
        if ct0:
            env["TWITTER_CT0"] = str(ct0)
    except Exception:
        # Agent-Reach not installed or config unavailable — silent fallback
        pass

    return env
