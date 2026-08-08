"""Final reward correctness, tick-context, and quote-safety regressions."""
from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from market_making_strategy import MarketMakingStrategy
from market_manager import MarketManager
from reward_calculator import (
    calculate_q_one_q_two,
    calculate_size_cutoff_adjusted_midpoint,
    estimate_our_planned_buy_score,
)
from tests.conftest import make_order_manager
from tests.fakes import FakeAPIClient, FakeClobClient
from tests.fixtures import TOKEN_A, TOKEN_B, market_fixture, orderbook_fixture


def _strategy_config(**overrides):
    values = dict(
        max_orderbook_age_seconds=3.0,
        spread_range={"min": None, "max": 0.05},
        min_exit_depth_multiplier=1.2,
        price_cliff_threshold=0.05,
        min_protection_size_multiplier=1.0,
        exit_immediate_max_loss_bps=300.0,
        reward_boundary_inset_ticks=0,
        order_size_multiplier=1.0,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_reward_adjusted_midpoint_differs_and_execution_unchanged():
    book = orderbook_fixture(
        "t",
        bids=[
            {"price": "0.50", "size": "1000"},
            {"price": "0.45", "size": "1000"},
        ],
        asks=[
            {"price": "0.52", "size": "10"},
            {"price": "0.80", "size": "1000"},
        ],
    )
    strategy = MarketMakingStrategy()
    market = market_fixture(rewards_max_spread=5.0, rewards_min_size=100)

    ordinary_mid = strategy.calculate_mid_price(book)
    adjusted_mid = calculate_size_cutoff_adjusted_midpoint(book, 100)
    assert ordinary_mid == pytest.approx(0.51)
    assert adjusted_mid == pytest.approx(0.65)

    # Reward scoring uses the adjusted midpoint: the same planned BUY scores
    # near the adjusted mid and scores zero against the ordinary mid.
    score_adj = estimate_our_planned_buy_score(
        0.63, None, 100, adjusted_mid, None, 5.0, 1.0, 100
    )
    score_ordinary = estimate_our_planned_buy_score(
        0.63, None, 100, ordinary_mid, None, 5.0, 1.0, 100
    )
    assert score_adj > 0
    assert score_ordinary == 0

    # Execution quote pricing still uses the ordinary midpoint.
    prices = strategy.calculate_order_prices(book, 5.0, market=market)
    assert prices["mid_price"] == pytest.approx(ordinary_mid)
    assert prices["buy_price"] == pytest.approx(0.46)


def test_our_score_uses_actual_yes_and_no_buys_only():
    market = market_fixture(rewards_max_spread=5.0, rewards_min_size=10)
    yes_book = orderbook_fixture(
        "yes",
        bids=[
            {"price": "0.60", "size": "100"},
            {"price": "0.59", "size": "100"},
        ],
        asks=[
            {"price": "0.62", "size": "100"},
            {"price": "0.63", "size": "100"},
        ],
    )
    no_book = orderbook_fixture(
        "no",
        bids=[
            {"price": "0.37", "size": "100"},
            {"price": "0.36", "size": "100"},
        ],
        asks=[
            {"price": "0.38", "size": "100"},
            {"price": "0.39", "size": "100"},
        ],
    )
    strategy = MarketMakingStrategy()
    yes_prices = strategy.calculate_order_prices(yes_book, 5.0, market=market)
    no_prices = strategy.calculate_order_prices(no_book, 5.0, market=market)
    yes_actual = strategy.calculate_actual_buy_price(
        yes_book, yes_prices["buy_price"], market=market
    )
    no_actual = strategy.calculate_actual_buy_price(
        no_book, no_prices["buy_price"], market=market
    )
    mid_yes = calculate_size_cutoff_adjusted_midpoint(yes_book, 10)
    mid_no = calculate_size_cutoff_adjusted_midpoint(no_book, 10)

    both = estimate_our_planned_buy_score(
        yes_actual, no_actual, 100, mid_yes, mid_no, 5.0, 1.0, 10
    )
    only_yes = estimate_our_planned_buy_score(
        yes_actual, None, 100, mid_yes, mid_no, 5.0, 1.0, 10
    )
    assert both == pytest.approx(36.0)
    assert only_yes == pytest.approx(12.0)


def test_competitor_range_uses_official_spread_not_our_inset(monkeypatch):
    monkeypatch.setattr(
        "market_making_strategy.config",
        _strategy_config(reward_boundary_inset_ticks=1),
    )
    market = market_fixture(rewards_max_spread=5.0)
    buy, sell = MarketMakingStrategy().calculate_reward_range(
        0.50, 5.0, market=market
    )
    assert buy == 0.46
    assert sell == 0.54

    # 0.545 is inside the official 5c range but outside our 1-tick inset.
    book = {"bids": [{"price": "0.545", "size": "100"}], "asks": []}
    q_one, q_two = calculate_q_one_q_two(
        book, None, 0.50, v=5.0, b=1.0, rewards_max_spread=5.0
    )
    assert q_one > 0
    assert q_two == 0


def test_our_planned_size_below_rewards_min_size_does_not_qualify():
    below = estimate_our_planned_buy_score(
        0.49, 0.49, 5, 0.50, 0.50, 3.0, 1.0, 10
    )
    at_min = estimate_our_planned_buy_score(
        0.49, 0.49, 10, 0.50, 0.50, 3.0, 1.0, 10
    )
    assert below == 0
    assert at_min > 0


@pytest.mark.parametrize("tick", [0.005, 0.0025])
def test_current_official_ticks_round_side_aware(tick):
    strategy = MarketMakingStrategy()
    buy = strategy.round_price_to_tick(0.3333, tick, "BUY")
    sell = strategy.round_price_to_tick(0.3333, tick, "SELL")
    assert buy <= 0.3333 <= sell
    assert round(buy / tick, 10).is_integer()
    assert round(sell / tick, 10).is_integer()


def test_sdk_rounding_config_supports_new_official_ticks():
    from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG

    assert "0.005" in ROUNDING_CONFIG
    assert "0.0025" in ROUNDING_CONFIG


def test_requote_preserves_non_default_market_tick(fake_clock):
    market = market_fixture(
        rewards_max_spread=2.0, orderPriceMinTickSize=0.001
    )
    book = orderbook_fixture(
        TOKEN_A,
        bids=[
            {"price": "0.600", "size": "200"},
            {"price": "0.580", "size": "200"},
            {"price": "0.579", "size": "200"},
            {"price": "0.578", "size": "200"},
        ],
        asks=[
            {"price": "0.615", "size": "200"},
            {"price": "0.616", "size": "200"},
        ],
    )
    book["_received_at"] = fake_clock.monotonic()
    api = FakeAPIClient(markets=[market], orderbooks={TOKEN_A: book})
    clob = FakeClobClient(clock=fake_clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=fake_clock)
    om.market_data_cache["market-1"] = market
    om.active_orders.setdefault("market-1", {}).setdefault(TOKEN_A, {})["BUY"] = {
        "order_id": "buy-1",
        "token_id": TOKEN_A,
        "side": "BUY",
        "price": 0.585,
        "size": 100.0,
        "exposure": 58.5,
        "created_at": fake_clock.monotonic() - 60.0,
        "created_at_monotonic": fake_clock.monotonic() - 60.0,
        "submitted_at": fake_clock.monotonic() - 60.0,
        "status": "LIVE",
        "purpose": "REWARD_BUY",
        "generation": 0,
    }

    om.adjust_orders_to_reward_boundaries([market])
    om.adjust_orders_to_reward_boundaries([market])

    assert clob.cancelled == ["buy-1"]
    om._process_cancel_pending()
    assert om.maybe_reenter_markets([market])[TOKEN_A] is True
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["price"] == 0.587


def test_strict_ahead_protection_excludes_same_and_lower_prices(monkeypatch):
    monkeypatch.setattr(
        "market_making_strategy.config",
        _strategy_config(min_protection_size_multiplier=1.0),
    )
    book = orderbook_fixture(
        "t",
        bids=[
            {"price": "0.50", "size": "100"},
            {"price": "0.45", "size": "100"},
            {"price": "0.44", "size": "100"},
            {"price": "0.43", "size": "100"},
        ],
        asks=[
            {"price": "0.52", "size": "100"},
            {"price": "0.53", "size": "100"},
        ],
    )
    can, info = MarketMakingStrategy().can_place_buy_order_safely(
        book, 0.48, 0.52, 100, 0.48
    )
    assert can
    assert info["strict_ahead_protection_size"] == 100
    assert info["total_protection_size"] == 100


def test_best_bid_buy_is_rejected_not_improved_by_reward_score(monkeypatch):
    monkeypatch.setattr(
        "market_making_strategy.config",
        _strategy_config(min_protection_size_multiplier=1.0),
    )
    book = orderbook_fixture(
        "t",
        bids=[
            {"price": "0.50", "size": "100"},
            {"price": "0.45", "size": "100"},
            {"price": "0.44", "size": "100"},
            {"price": "0.43", "size": "100"},
        ],
        asks=[
            {"price": "0.52", "size": "100"},
            {"price": "0.53", "size": "100"},
        ],
    )
    can, info = MarketMakingStrategy().can_place_buy_order_safely(
        book, 0.48, 0.52, 100, 0.50
    )
    assert not can
    assert info["our_position"] == 1


def test_normal_market_places_one_reward_buy_per_token(monkeypatch, fake_clock):
    monkeypatch.setattr(
        "market_making_strategy.config",
        _strategy_config(),
    )
    market = market_fixture()
    book_a = orderbook_fixture(
        TOKEN_A,
        bids=[
            {"price": "0.60", "size": "200"},
            {"price": "0.59", "size": "200"},
            {"price": "0.58", "size": "200"},
            {"price": "0.57", "size": "200"},
        ],
        asks=[
            {"price": "0.62", "size": "200"},
            {"price": "0.63", "size": "200"},
        ],
    )
    book_a["_received_at"] = fake_clock.monotonic()
    book_b = orderbook_fixture(
        TOKEN_B,
        bids=[
            {"price": "0.40", "size": "200"},
            {"price": "0.39", "size": "200"},
            {"price": "0.38", "size": "200"},
            {"price": "0.37", "size": "200"},
        ],
        asks=[
            {"price": "0.42", "size": "200"},
            {"price": "0.43", "size": "200"},
        ],
    )
    book_b["_received_at"] = fake_clock.monotonic()
    api = FakeAPIClient(
        markets=[market],
        orderbooks={TOKEN_A: book_a, TOKEN_B: book_b},
    )
    clob = FakeClobClient(clock=fake_clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=fake_clock)

    om.place_market_orders(market, {})

    buys = [c for c in clob.post_order_calls if c["order"].side == "BUY"]
    buy_tokens = [c["order"].token_id for c in buys]
    assert len(buy_tokens) == 2
    assert len(set(buy_tokens)) == 2


def test_reward_ratio_uses_adjusted_midpoint_and_actual_planned_buys(
    monkeypatch, fake_clock
):
    monkeypatch.setattr(
        "market_making_strategy.config",
        _strategy_config(order_size_multiplier=1.0),
    )
    market = market_fixture(rewards_max_spread=5.0, rewards_min_size=10)
    yes_book = orderbook_fixture(
        TOKEN_A,
        bids=[
            {"price": "0.60", "size": "200"},
            {"price": "0.59", "size": "200"},
            {"price": "0.58", "size": "200"},
            {"price": "0.57", "size": "200"},
        ],
        asks=[
            {"price": "0.62", "size": "200"},
            {"price": "0.63", "size": "200"},
        ],
    )
    no_book = orderbook_fixture(
        TOKEN_B,
        bids=[
            {"price": "0.37", "size": "200"},
            {"price": "0.36", "size": "200"},
            {"price": "0.35", "size": "200"},
            {"price": "0.34", "size": "200"},
        ],
        asks=[
            {"price": "0.38", "size": "200"},
            {"price": "0.39", "size": "200"},
        ],
    )
    for book in (yes_book, no_book):
        book["_received_at"] = fake_clock.monotonic()
    manager = MarketManager(
        FakeAPIClient(
            markets=[market],
            orderbooks={TOKEN_A: yes_book, TOKEN_B: no_book},
        )
    )

    ratio = manager.calculate_reward_ratio(
        market, {TOKEN_A: yes_book, TOKEN_B: no_book}
    )
    assert ratio > 0
    assert math.isfinite(ratio)
