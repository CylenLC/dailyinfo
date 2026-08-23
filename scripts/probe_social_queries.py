#!/usr/bin/env python3
"""Probe X/Twitter search queries for relevance before committing them to config.

Runs each candidate query through twitter-cli and prints what actually comes
back, so query tuning is driven by observed results rather than guesswork.

Usage::

    env -u PYTHONPATH .venv/bin/python scripts/probe_social_queries.py
    env -u PYTHONPATH .venv/bin/python scripts/probe_social_queries.py -n 8

Note: this is a diagnostic tool, not part of the pipeline. It makes live search
calls, so keep -n small to avoid rate limiting.
"""

import argparse
import json
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from social.agent_reach import _resolve_bin, _twitter_subprocess_env  # noqa: E402

# Topic vocabulary for the AI-for-Science / earth-observation domain this
# project targets. A result counts as on-topic when it mentions at least one.
DOMAIN_TERMS = (
    "foundation model",
    "remote sensing",
    "earth observation",
    "satellite",
    "geospatial",
    "hydrolog",
    "streamflow",
    "watershed",
    "rainfall",
    "runoff",
    "climate",
    "weather",
    "sentinel",
    "landsat",
    "ai for science",
    "scientific discovery",
    "protein",
    "materials discovery",
    "physics-informed",
    "pinn",
    "emulator",
    "downscal",
    "dataset",
    "benchmark",
    "arxiv",
    "paper",
    "preprint",
)

# Terms that mark the noise we actually observed: book ads, course promos,
# generic AI-hype threads.
NOISE_TERMS = (
    "buy now",
    "free course",
    "bookmark this",
    "link in bio",
    "discount",
    "e-book",
    "ebook",
    "sign up",
    "giveaway",
    "follow me",
    "my new book",
    "promo",
)

# Candidate strategies. Each entry is (label, query).
CANDIDATES = [
    # Round 2: find a usable hydrology query. Round 1 showed
    # '"deep learning" streamflow OR runoff' is poisoned by *election* runoff
    # coverage, and '"hydrology deep learning"' as an exact phrase returns 0.
    ("hydro: quoted domain phrase", '"hydrological model" "machine learning"'),
    ("hydro: quoted domain phrase", '"streamflow prediction"'),
    ("hydro: quoted domain phrase", '"rainfall-runoff" model'),
    ("hydro: LSTM angle", '"streamflow" LSTM'),
    ("hydro: exclude politics", '"deep learning" runoff -election -senate -voter'),
    ("hydro: journal angle", "hydrology deep learning arxiv"),
    # Re-confirm the AI-for-Science slot; the bare and min_faves forms both sat
    # at 50% in round 1.
    ("science: narrower", '"AI for science" research -job -hiring'),
    ("science: method angle", '"scientific discovery" "foundation model"'),
    ("science: venue angle", '"machine learning" "for science" arxiv'),
]


def run_search(query: str, limit: int) -> tuple[list[dict], str]:
    """Return (rows, error). Uses compact -c output; rows may be empty."""
    binary = _resolve_bin("twitter")
    argv = [binary, "-c", "search", query, "-n", str(limit)]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=90,
            env=_twitter_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return [], "timeout"

    if proc.returncode != 0:
        return [], f"exit {proc.returncode}: {proc.stderr.strip()[:120]}"

    raw = proc.stdout.strip()
    if not raw:
        return [], "empty stdout"

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # twitter-cli emits YAML on some error paths (e.g. not_found).
        return [], f"non-JSON output: {raw[:120]}"

    if isinstance(data, dict):
        if data.get("ok") is False:
            err = data.get("error") or {}
            return [], f"api error: {err}"
        rows = data.get("data") or []
    else:
        rows = data
    return [r for r in rows if isinstance(r, dict)], ""


def classify(text: str) -> tuple[bool, bool]:
    """Return (on_topic, promo_noise) for one tweet body."""
    low = text.lower()
    return (
        any(term in low for term in DOMAIN_TERMS),
        any(term in low for term in NOISE_TERMS),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--limit", type=int, default=6)
    ap.add_argument("--delay", type=float, default=2.0)
    args = ap.parse_args()

    print(f"Probing {len(CANDIDATES)} queries, -n {args.limit}\n")
    summary = []

    for label, query in CANDIDATES:
        rows, err = run_search(query, args.limit)
        print("=" * 74)
        print(f"[{label}]  {query}")
        if err:
            print(f"  !! {err}")
            summary.append((label, query, 0, 0, 0, err))
            time.sleep(args.delay)
            continue

        on_topic = noise = 0
        for r in rows:
            text = (r.get("text") or "").replace("\n", " ")
            hit, promo = classify(text)
            on_topic += hit
            noise += promo
            mark = "OK " if hit and not promo else ("AD " if promo else "-- ")
            author = r.get("author") or "?"
            print(f"  {mark} {author:<20} {text[:66]}")

        total = len(rows)
        pct = (100 * on_topic / total) if total else 0
        print(f"  -> {on_topic}/{total} on-topic ({pct:.0f}%), {noise} promo")
        summary.append((label, query, total, on_topic, noise, ""))
        time.sleep(args.delay)

    print("\n" + "=" * 74)
    print("SUMMARY (sorted by on-topic ratio)\n")
    ranked = sorted(
        summary,
        key=lambda s: (s[3] / s[2]) if s[2] else -1,
        reverse=True,
    )
    for label, query, total, hits, noise, err in ranked:
        if err:
            print(f"  ERR   {query[:52]:<52} {err[:20]}")
            continue
        pct = (100 * hits / total) if total else 0
        print(
            f"  {pct:3.0f}%  {query[:52]:<52} "
            f"{hits}/{total} topical, {noise} ad  [{label}]"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
