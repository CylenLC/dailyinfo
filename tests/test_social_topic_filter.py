"""Tests for build_topic_matcher (timeline/list topic scoping)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from social.datasource import build_topic_matcher  # noqa: E402

# ---------------------------------------------------------------------------
# No filter configured
# ---------------------------------------------------------------------------


def test_no_terms_returns_none():
    """A source without topic_filter must not filter anything."""
    assert build_topic_matcher(None) is None
    assert build_topic_matcher([]) is None


def test_blank_terms_are_ignored():
    assert build_topic_matcher(["", "   "]) is None


# ---------------------------------------------------------------------------
# Whole-word matching (the substring bug)
# ---------------------------------------------------------------------------


def test_short_term_does_not_match_inside_words():
    """Regression: 'ai' as a substring matched repair/again/said.

    A watch-servicing anecdote scored as an AI post in a live run.
    """
    matcher = build_topic_matcher(["ai"])

    assert not matcher("When a mechanical watch gets a full service")
    assert not matcher("again and again we said this")
    assert not matcher("repair the sail on the boat")


def test_short_term_matches_as_a_word():
    matcher = build_topic_matcher(["ai"])

    assert matcher("AI models are improving")
    assert matcher("thoughts on ai, briefly")


def test_matching_is_case_insensitive():
    matcher = build_topic_matcher(["LLM"])

    assert matcher("new llm release")
    assert matcher("New LLM Release")


# ---------------------------------------------------------------------------
# Multi-word terms and plurals
# ---------------------------------------------------------------------------


def test_multi_word_term_tolerates_extra_whitespace():
    matcher = build_topic_matcher(["data center"])

    assert matcher("data center buildout")
    assert matcher("data  center buildout")


def test_trailing_plural_is_accepted():
    """Observed live: '...debates about datacenters...' must match."""
    matcher = build_topic_matcher(["datacenter", "chip", "robot"])

    assert matcher("debates about datacenters and China")
    assert matcher("chips are in short supply")
    assert matcher("humanoid robots at scale")


def test_regex_metacharacters_in_terms_are_literal():
    """Terms are user config, so '.' or '+' must not act as regex."""
    matcher = build_topic_matcher(["c++", "node.js"])

    assert matcher("writing c++ today")
    assert matcher("node.js runtime")
    assert not matcher("nodexjs runtime")


def test_any_term_matching_is_enough():
    matcher = build_topic_matcher(["ai", "quantum"])

    assert matcher("quantum error correction")
    assert not matcher("a story about sailing")
