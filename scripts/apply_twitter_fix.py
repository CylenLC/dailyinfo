#!/usr/bin/env python3
"""Apply the twitter-cli Issue #79 cookie fix.

GitHub Issue: https://github.com/agent-reach/twitter-cli/issues/79
Fix: send auth cookies and use the /home endpoint for ClientTransaction init.

Required for twitter-cli v0.8.5 on x.com (2025 redesign). Without it, every
search / user-posts command fails with HTTP 404, which surfaces in DailyInfo
as Pipeline 7 returning 0 items from all social sources.

Usage:
    python3 scripts/apply_twitter_fix.py            # apply the patch
    python3 scripts/apply_twitter_fix.py --verify   # read-only status check
    python3 scripts/apply_twitter_fix.py --revert   # restore from backup
    python3 scripts/apply_twitter_fix.py --client-py /path/to/client.py

``--verify`` is strictly read-only: it never creates a backup and never
writes to the target file.
"""

import argparse
import glob
import os
import sys
from pathlib import Path

OLD_SNIPPET = """            home_page = cffi_session.get(
                "https://x.com", headers=ct_headers, timeout=10,
            )"""

NEW_SNIPPET = """            # Issue #79: Add auth cookies to the request so the homepage
            # contains the ondemand.s file reference (otherwise parsing fails)
            if self._auth_token and self._ct0:
                ct_headers["Cookie"] = f"auth_token={self._auth_token}; ct0={self._ct0}"

            # Issue #79: Use /home (authenticated app shell) instead of bare /
            home_page = cffi_session.get(
                "https://x.com/home", headers=ct_headers, timeout=10,
            )"""

_SEARCH_GLOBS = (
    "~/.local/share/uv/tools/twitter-cli/lib/python*/site-packages/twitter_cli/client.py",
    "~/.local/pipx/venvs/twitter-cli/lib/python*/site-packages/twitter_cli/client.py",
)


def discover_client_py() -> Path | None:
    """Locate twitter_cli/client.py without hardcoding a Python version.

    Prefers an importable twitter_cli (respects the active venv), then falls
    back to well-known uv / pipx tool install locations.
    """
    try:
        import twitter_cli  # noqa: PLC0415

        if twitter_cli.__file__:
            candidate = Path(twitter_cli.__file__).parent / "client.py"
            if candidate.exists():
                return candidate
    except ImportError:
        pass

    for pattern in _SEARCH_GLOBS:
        matches = sorted(glob.glob(os.path.expanduser(pattern)))
        if matches:
            return Path(matches[-1])
    return None


def is_applied(content: str) -> bool:
    return "https://x.com/home" in content and "auth_token=" in content


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--verify", action="store_true", help="read-only status check (no writes)"
    )
    group.add_argument(
        "--revert", action="store_true", help="restore client.py from backup"
    )
    parser.add_argument(
        "--client-py",
        type=Path,
        default=None,
        help="path to twitter_cli/client.py (default: auto-discover)",
    )
    args = parser.parse_args(argv)

    client_py = args.client_py or discover_client_py()
    if client_py is None:
        print("ERROR: could not locate twitter_cli/client.py")
        print("       pass an explicit path with --client-py")
        return 2
    if not client_py.exists():
        print(f"ERROR: no such file: {client_py}")
        return 2

    print(f"target: {client_py}")
    content = client_py.read_text()
    backup_path = client_py.with_suffix(".py.backup")

    # --verify is read-only; check before any write happens.
    if args.verify:
        if is_applied(content):
            print("OK: fix is applied")
            return 0
        print("NOT APPLIED: run without --verify to patch")
        return 1

    if args.revert:
        if not backup_path.exists():
            print(f"ERROR: no backup at {backup_path}")
            return 1
        client_py.write_text(backup_path.read_text())
        print(f"reverted from {backup_path}")
        return 0

    if is_applied(content):
        print("OK: fix already applied, nothing to do")
        return 0

    if OLD_SNIPPET not in content:
        print("ERROR: expected code block not found — twitter-cli version differs")
        print("       patch manually in _ensure_client_transaction()")
        return 1

    if not backup_path.exists():
        backup_path.write_text(content)
        print(f"backup created: {backup_path}")

    client_py.write_text(content.replace(OLD_SNIPPET, NEW_SNIPPET, 1))
    print("applied Issue #79 fix:")
    print("  - auth cookies added to ClientTransaction init")
    print("  - endpoint changed to https://x.com/home")
    return 0


if __name__ == "__main__":
    sys.exit(main())
