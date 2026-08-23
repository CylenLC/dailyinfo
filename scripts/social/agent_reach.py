"""Agent-Reach adapter — wraps `agent-reach` CLI as a stable DailyInfo interface."""

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from scripts.social.models import CommandResult, ReachCapabilities
from scripts.social.normalize import _parse_opencli_twitter, _parse_twitter_search, _parse_twitter_user_posts


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_REACH_BIN = "agent-reach"
_DOCTOR_TIMEOUT = 15
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

        # Parse doctor output
        twitter_info = data.get("twitter", {})
        twitter_status = twitter_info.get("status", "off")
        twitter_backend = twitter_info.get("active_backend")

        opencli_info = data.get("opencli", {})
        opencli_status = opencli_info.get("status", "off")

        # Extract version if available
        version = data.get("version")

        self._capabilities = ReachCapabilities(
            agent_reach_version=version,
            twitter_available=twitter_status == "ok",
            twitter_backend=twitter_backend if twitter_status == "ok" else None,
            opencli_available=opencli_status == "ok",
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

        # Try preferred backend first
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

        # Try preferred backend with one retry
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

    # ------------------------------------------------------------------
    # Private: twitter-cli backend
    # ------------------------------------------------------------------

    def _x_user_posts_twitter_cli(self, handle: str, limit: int) -> list:
        """Call `twitter user-posts @handle -n limit`."""
        env = _twitter_subprocess_env()
        result = self.runner.run(
            ["twitter", "user-posts", handle, "-n", str(limit)],
            env=env,
            timeout=_FETCH_TIMEOUT,
        )

        if result.returncode != 0:
            return []

        return _parse_twitter_user_posts(result.stdout, backend="twitter-cli")

    def _x_search_twitter_cli(self, query: str, limit: int) -> list:
        """Call `twitter search "query" -n limit`."""
        env = _twitter_subprocess_env()
        result = self.runner.run(
            ["twitter", "search", query, "-n", str(limit)],
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
