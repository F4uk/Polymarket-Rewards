"""Regression tests for the two P1 reward-scoring fixes."""
from __future__ import annotations

import pytest

from market_manager import MarketManager
from market_making_strategy import MarketMakingStrategy
from reward_calculator import calculate_q_one_q_two, estimate_our_score
from tests.fakes import FakeAPIClient
from tests.fixtures import TOKEN_A, TOKEN_B, market_fixture, orderbook_fixture


def _reward_market(**extra):
    return market_fixture(
        market_id="reward-test",
        rewards_min_size=10,
        rewards_max_spread=5,
        rewards_config=[
            {"rate_per_day": 1.0},
            {"rate_per_day": 2.0},
        ],
        **extra,
    )


def _reward_orderbooks():
    return {
        TOKEN_A: orderbook_fixture(TOKEN_A),
        TOKEN_B: orderbook_fixture(TOKEN_B, bids=[], asks=[]),
    }


def test_reward_ratio_uses_total_daily_rate_and_config_sum_fallback(monkeypatch):
    import reward_calculator as rc

    monkeypatch.setattr(
        rc, "estimate_competitor_total_score", lambda *a, **k: (0.0, 0.0)
    )
    monkeypatch.setattr(rc, "estimate_our_planned_buy_score", lambda *a, **k: 1.0)
    monkeypatch.setattr(
        MarketMakingStrategy,
        "calculate_order_size",
        lambda self, market, multiplier=None: float(market.get("rewards_min_size", 0)),
    )

    manager = MarketManager(FakeAPIClient())
    orderbooks = _reward_orderbooks()

    with_root_rate = _reward_market(total_daily_rate=3.0)
    assert manager.calculate_reward_ratio(with_root_rate, orderbooks) == pytest.approx(
        3.0 / 10.0
    )

    fallback = _reward_market()
    assert manager.calculate_reward_ratio(fallback, orderbooks) == pytest.approx(
        3.0 / 10.0
    )


def test_complement_token_scoring_uses_complement_midpoint():
    mid_price = 0.80
    v = 5.0
    m_prime_book = {
        "bids": [{"price": "0.18", "size": "100"}],
        "asks": [{"price": "0.22", "size": "100"}],
    }
    empty_book = {"bids": [], "asks": []}

    q_one, q_two = calculate_q_one_q_two(
        empty_book, m_prime_book, mid_price, v=v, b=1.0, rewards_max_spread=v
    )
    assert q_one > 0.0
    assert q_two > 0.0

    common = dict(
        our_buy_price=0.78,
        our_sell_price=0.82,
        our_size=100.0,
        mid_price=mid_price,
        v=v,
        b=1.0,
        rewards_max_spread=v,
    )
    score_without = estimate_our_score(orderbook_m_prime=None, **common)
    score_with = estimate_our_score(
        orderbook_m_prime={"bids": [], "asks": []}, **common
    )
    assert score_with > score_without


def test_zero_own_score_returns_zero_when_competitor_is_zero(monkeypatch):
    import reward_calculator as rc

    monkeypatch.setattr(
        rc, "estimate_competitor_total_score", lambda *a, **k: (0.0, 0.0)
    )
    monkeypatch.setattr(rc, "estimate_our_planned_buy_score", lambda *a, **k: 0.0)
    monkeypatch.setattr(
        MarketMakingStrategy,
        "calculate_order_size",
        lambda self, market, multiplier=None: float(market.get("rewards_min_size", 0)),
    )

    manager = MarketManager(FakeAPIClient())
    assert manager.calculate_reward_ratio(
        _reward_market(total_daily_rate=3.0), _reward_orderbooks()
    ) == 0.0


def test_positive_own_score_with_zero_competitor_keeps_full_share(monkeypatch):
    import reward_calculator as rc

    monkeypatch.setattr(
        rc, "estimate_competitor_total_score", lambda *a, **k: (0.0, 0.0)
    )
    monkeypatch.setattr(rc, "estimate_our_planned_buy_score", lambda *a, **k: 1.0)
    monkeypatch.setattr(
        MarketMakingStrategy,
        "calculate_order_size",
        lambda self, market, multiplier=None: float(market.get("rewards_min_size", 0)),
    )

    manager = MarketManager(FakeAPIClient())
    assert manager.calculate_reward_ratio(
        _reward_market(total_daily_rate=3.0), _reward_orderbooks()
    ) == pytest.approx(3.0 / 10.0)
