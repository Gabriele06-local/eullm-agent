from datetime import UTC, datetime, timedelta

import pytest

from editor.scoring import FORMULA_VERSION, WEIGHTS, Member, hype

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)


def ago(hours: float) -> datetime:
    return NOW - timedelta(hours=hours)


def test_rss_only_topic_declares_missing_components():
    h = hype([Member(1, "rss", ago(2)), Member(2, "rss", ago(3))], NOW)
    assert set(h.components) == {"velocity", "coverage"}
    assert h.missing == ["community", "code", "models", "research"]
    assert h.version == FORMULA_VERSION
    # velocity: 2 recent, no baseline -> ratio 4 -> 0.8; coverage: 2 sources -> 0.25
    expected = 100 * (0.3 * 0.8 + 0.2 * 0.25) / 0.5
    assert h.score == pytest.approx(expected, abs=0.01)


def test_stale_arxiv_items_leave_research_unmeasured():
    stale = [Member(1, "arxiv", ago(24 * 5))]
    h = hype(stale, NOW)
    assert "research" not in h.components and "research" in h.missing
    fresh = [Member(2, "arxiv", ago(2))]
    assert "research" in hype(fresh, NOW).components


def test_score_is_bounded_and_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)
    members = [Member(i, k, ago(1), {"points": 5000, "comments": 900, "stars": 90000, "trending": 900})
               for i, k in enumerate(["hackernews", "github", "huggingface", "arxiv", "rss", "rss"] * 3)]
    h = hype(members, NOW)
    assert 0 <= h.score <= 100 and not h.missing
    assert hype([], NOW).score == 0


def test_cooling_topic_scores_lower_than_rising_one():
    rising = [Member(i, "rss", ago(1 + i)) for i in range(4)]
    cooling = [Member(i, "rss", ago(30 + i * 5)) for i in range(4)]
    assert hype(rising, NOW).score > hype(cooling, NOW).score


def test_github_growth_uses_observations():
    obs = [(ago(30), {"stars": 1000}), (ago(20), {"stars": 1100}), (ago(1), {"stars": 1500})]
    fast = Member(1, "github", ago(24 * 30), {"stars": 1500}, obs)
    slow = Member(1, "github", ago(24 * 30), {"stars": 1500}, [(ago(30), {"stars": 1490}), (ago(1), {"stars": 1500})])
    assert hype([fast], NOW).components["code"] > hype([slow], NOW).components["code"]
    single = Member(1, "github", ago(48), {"stars": 400})  # 200 stars/day since creation
    assert hype([single], NOW).components["code"] == pytest.approx(0.6321, abs=1e-3)


def test_outlets_of_one_country_are_one_voice():
    """Francesco 2026-11-07: twenty American outlets on the same fact are not
    twenty confirmations; Italian, French and German ones are three."""
    us = [Member(i, "rss", ago(1), voice="country:US") for i in range(5)]
    europe = [Member(10 + i, "rss", ago(1), voice=f"country:{c}") for i, c in enumerate(["IT", "FR", "DE"])]
    assert hype(us, NOW).components["coverage"] == 0
    assert hype(europe, NOW).components["coverage"] == pytest.approx(0.5)
    assert hype(us, NOW).score < hype(europe, NOW).score
