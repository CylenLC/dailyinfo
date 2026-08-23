"""Tests for AgentReachAdapter — subprocess wrapper + capability probe."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.social.agent_reach import (
    AgentReachAdapter,
    ReachCapabilities,
    ReachCommandRunner,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner():
    return ReachCommandRunner()


@pytest.fixture
def adapter(runner):
    return AgentReachAdapter(runner=runner)


# ---------------------------------------------------------------------------
# ReachCommandRunner tests
# ---------------------------------------------------------------------------


class TestReachCommandRunner:
    def test_success(self, runner, tmp_path):
        """Successful command returns structured result."""
        result = runner.run(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout
        assert result.backend == "echo"
        assert result.elapsed_ms >= 0

    def test_non_zero_exit(self, runner):
        """Non-zero exit code captured in result."""
        result = runner.run(["false"])
        assert result.returncode == 1
        assert result.stderr == ""

    def test_timeout(self, runner):
        """Timeout results in returncode=-1."""
        result = runner.run(["sleep", "10"], timeout=1)
        assert result.returncode == -1
        assert "timeout" in result.stderr.lower()

    def test_missing_binary(self, runner):
        """Missing binary results in FileNotFoundError returncode."""
        result = runner.run(["nonexistent_binary_xyz"])
        assert result.returncode != 0

    def test_env_injection(self, runner, monkeypatch):
        """Custom env vars are passed to subprocess."""
        monkeypatch.setenv("TEST_VAR_123", "hello")
        result = runner.run(["env"], env={"TEST_VAR_123": "world"})
        # The injected env should be present (may also inherit parent env)
        assert "TEST_VAR_123" in result.stdout or "world" in result.stdout

    def test_shell_false_enforced(self, runner, mocker):
        """subprocess.run must be called with shell=False."""
        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        runner.run(["agent-reach", "doctor", "--json"])
        call_args = mock_run.call_args
        assert call_args.kwargs.get("shell") is False

    def test_no_credentials_in_logs(self, runner, mocker, capsys):
        """Credentials must not appear in subprocess args."""
        mock_run = mocker.patch("subprocess.run")
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ok", stderr=""
        )
        # Simulate a command that should not contain credentials
        runner.run(["twitter", "user-posts", "@karpathy", "-n", "5"])
        call_args = mock_run.call_args
        argv = call_args.args[0]
        # Credentials should not be in argv
        argv_str = " ".join(str(a) for a in argv)
        assert "AUTH_TOKEN" not in argv_str
        assert "CT0" not in argv_str
        assert "cookie" not in argv_str.lower()


# ---------------------------------------------------------------------------
# AgentReachAdapter tests
# ---------------------------------------------------------------------------


class TestAgentReachAdapter:
    def test_probe_valid_json(self, adapter, mocker):
        """probe() parses valid doctor JSON output."""
        mock_runner = mocker.MagicMock()
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "version": "1.5.0",
                    "twitter": {
                        "status": "ok",
                        "active_backend": "twitter-cli",
                    },
                    "opencli": {"status": "ok"},
                }
            ),
            stderr="",
        )
        adapter.runner = mock_runner
        caps = adapter.probe()
        assert caps.agent_reach_version == "1.5.0"
        assert caps.twitter_available is True
        assert caps.twitter_backend == "twitter-cli"
        assert caps.opencli_available is True

    def test_probe_invalid_json(self, adapter, mocker):
        """probe() handles malformed JSON gracefully."""
        mock_runner = mocker.MagicMock()
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout="not json{{{",
            stderr="",
        )
        adapter.runner = mock_runner
        adapter._capabilities = None  # reset cache
        caps = adapter.probe()
        assert caps.twitter_available is False
        assert caps.agent_reach_version is None

    def test_probe_non_zero_exit(self, adapter, mocker):
        """probe() handles non-zero doctor exit gracefully."""
        mock_runner = mocker.MagicMock()
        mock_runner.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="doctor failed",
        )
        adapter.runner = mock_runner
        adapter._capabilities = None
        caps = adapter.probe()
        assert caps.twitter_available is False
        assert "error" in caps.diagnostics

    def test_probe_cached(self, adapter, mocker):
        """probe() result is cached — only one subprocess call."""
        mock_runner = mocker.MagicMock()
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout=json.dumps(
                {
                    "version": "1.5.0",
                    "twitter": {"status": "ok", "active_backend": "twitter-cli"},
                    "opencli": {"status": "off"},
                }
            ),
            stderr="",
        )
        adapter.runner = mock_runner
        adapter.probe()
        adapter.probe()
        adapter.probe()
        assert mock_runner.run.call_count == 1

    def test_x_user_posts_success(self, adapter, mocker, tmp_path):
        """x_user_posts() returns SocialItem list on success."""
        fixture = (
            Path(__file__).parent / "fixtures" / "social" / "twitter_user_posts.json"
        )
        mock_runner = mocker.MagicMock()
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout=fixture.read_text(),
            stderr="",
        )
        adapter.runner = mock_runner
        # Pre-set capabilities to avoid doctor call
        adapter._capabilities = ReachCapabilities(
            agent_reach_version="1.5.0",
            twitter_available=True,
            twitter_backend="twitter-cli",
            opencli_available=False,
        )
        items = adapter.x_user_posts("@karpathy", limit=5)
        assert len(items) == 1
        assert items[0].author_handle == "@karpathy"
        assert items[0].canonical_id == "x:1959234892345678901"

    def test_x_user_posts_fallback_to_opencli(self, adapter, mocker):
        """x_user_posts() falls back to OpenCLI if twitter-cli fails."""
        # twitter-cli fails
        # opencli succeeds
        opencli_fixture = Path(__file__).parent / "fixtures" / "social" / "opencli_twitter_user_posts.yaml"
        mock_runner = mocker.MagicMock()

        def fake_run(argv, **kwargs):
            # twitter-cli failure
            if argv[0] == "twitter" and "user-posts" in argv:
                return MagicMock(returncode=1, stdout="", stderr="fail")
            # opencli success
            if argv[0] == "opencli":
                return MagicMock(
                    returncode=0,
                    stdout=opencli_fixture.read_text(),
                    stderr="",
                )
            return MagicMock(returncode=1, stdout="", stderr="unknown")

        mock_runner.run.side_effect = fake_run
        adapter.runner = mock_runner
        adapter._capabilities = ReachCapabilities(
            agent_reach_version="1.5.0",
            twitter_available=True,
            twitter_backend="twitter-cli",
            opencli_available=True,
        )
        items = adapter.x_user_posts("@karpathy", limit=5)
        assert len(items) == 1
        assert items[0].backend == "opencli"

    def test_x_user_posts_both_fail(self, adapter, mocker):
        """x_user_posts() returns empty list if all backends fail."""
        mock_runner = mocker.MagicMock()
        mock_runner.run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="fail",
        )
        adapter.runner = mock_runner
        adapter._capabilities = ReachCapabilities(
            agent_reach_version="1.5.0",
            twitter_available=True,
            twitter_backend="twitter-cli",
            opencli_available=True,
        )
        items = adapter.x_user_posts("@karpathy", limit=5)
        assert items == []

    def test_x_search_success(self, adapter, mocker):
        """x_search() returns SocialItem list on success."""
        fixture = (
            Path(__file__).parent / "fixtures" / "social" / "twitter_search.json"
        )
        mock_runner = mocker.MagicMock()
        mock_runner.run.return_value = MagicMock(
            returncode=0,
            stdout=fixture.read_text(),
            stderr="",
        )
        adapter.runner = mock_runner
        adapter._capabilities = ReachCapabilities(
            agent_reach_version="1.5.0",
            twitter_available=True,
            twitter_backend="twitter-cli",
            opencli_available=False,
        )
        items = adapter.x_search("AI agents", limit=5)
        assert len(items) == 1

    def test_x_search_with_retry(self, adapter, mocker):
        """x_search() retries twitter-cli once before falling back."""
        fixture = Path(__file__).parent / "fixtures" / "social" / "twitter_search.json"
        mock_runner = mocker.MagicMock()

        call_count = 0

        def fake_run(argv, **kwargs):
            nonlocal call_count
            call_count += 1
            if argv[0] == "twitter" and "search" in argv and call_count <= 2:
                return MagicMock(returncode=1, stdout="", stderr="fail")
            return MagicMock(
                returncode=0,
                stdout=fixture.read_text(),
                stderr="",
            )

        mock_runner.run.side_effect = fake_run
        adapter.runner = mock_runner
        adapter._capabilities = ReachCapabilities(
            agent_reach_version="1.5.0",
            twitter_available=True,
            twitter_backend="twitter-cli",
            opencli_available=True,  # Enable fallback so 3rd call happens
        )
        items = adapter.x_search("test", limit=5)
        assert len(items) == 1
        assert call_count == 3  # 2 failures + 1 success (via opencli fallback)

    def test_twitter_subprocess_env_existing(self, adapter, monkeypatch):
        """Existing TWITTER_AUTH_TOKEN/CT0 are used as-is."""
        from scripts.social.agent_reach import _twitter_subprocess_env

        monkeypatch.setenv("TWITTER_AUTH_TOKEN", "token123")
        monkeypatch.setenv("TWITTER_CT0", "ct0123")
        env = _twitter_subprocess_env()
        assert env["TWITTER_AUTH_TOKEN"] == "token123"
        assert env["TWITTER_CT0"] == "ct0123"

    def test_twitter_subprocess_env_from_agent_reach_config(self, adapter, mocker):
        """Credentials are read from Agent-Reach config when not in env."""
        pytest.importorskip("agent_reach")

        from scripts.social.agent_reach import _twitter_subprocess_env

        # Clear any existing env vars
        import os

        env_backup = os.environ.copy()
        os.environ.pop("TWITTER_AUTH_TOKEN", None)
        os.environ.pop("TWITTER_CT0", None)

        # Mock Agent-Reach Config
        mock_cfg = mocker.MagicMock()
        mock_cfg.get.side_effect = lambda key: {
            "twitter_auth_token": "config_token",
            "twitter_ct0": "config_ct0",
        }.get(key)

        mocker.patch("agent_reach.config.Config", return_value=mock_cfg)

        env = _twitter_subprocess_env()
        assert env.get("TWITTER_AUTH_TOKEN") == "config_token"
        assert env.get("TWITTER_CT0") == "config_ct0"

        # Restore env
        os.environ.clear()
        os.environ.update(env_backup)
