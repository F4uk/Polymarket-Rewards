"""Phase 2: side-aware tick rounding and reward boundary inset."""
from __future__ import annotations

import pytest

from market_making_strategy import MarketMakingStrategy, reward_spread_decimal
from tests.fixtures import market_fixture


@pytest.fixture
def strategy():
    return MarketMakingStrategy()


@pytest.mark.parametrize(
    "price,tick,expected",
    [
        (0.156, 0.01, 0.15),
        (0.1567, 0.001, 0.156),
        (0.15999, 0.0001, 0.1599),
        (0.15, 0.1, 0.1),
        (0.0, 0.01, 0.01),
        (2.0, 0.01, 1.0),
    ],
)
def test_buy_rounds_down(strategy, price, tick, expected):
    assert strategy.round_price_to_tick(price, tick, "BUY") == expected


@pytest.mark.parametrize(
    "price,tick,expected",
    [
        (0.151, 0.01, 0.16),
        (0.1501, 0.001, 0.151),
        (0.15991, 0.0001, 0.16),
        (0.15, 0.1, 0.2),
        (0.05, 0.1, 0.1),
        (2.0, 0.01, 1.0),
        (0.001, 0.01, 0.01),
    ],
)
def test_sell_rounds_up(strategy, price, tick, expected):
    assert strategy.round_price_to_tick(price, tick, "SELL") == expected


def test_unknown_side_rejected(strategy):
    with pytest.raises(ValueError):
        strategy.round_price_to_tick(0.5, 0.01, "HOLD")
    with pytest.raises(ValueError):
        strategy.normalize_price(0.5, 0.01, side="")


def test_normalize_price_defaults_to_buy(strategy):
    assert strategy.normalize_price(0.156, 0.01) == 0.15
    assert strategy.normalize_price(0.156, 0.01, "SELL") == 0.16


def test_immediate_exit_price_never_above_best_bid(strategy):
    assert strategy.immediate_exit_price(0.15, 0.01) == 0.15
    assert strategy.immediate_exit_price(0.155, 0.01) == 0.15
    assert strategy.immediate_exit_price(0.159, 0.01) == 0.15
    assert strategy.immediate_exit_price(0.1599, 0.001) == 0.159


def test_all_supported_tick_sizes(strategy):
    for tick in (0.1, 0.01, 0.001, 0.0001):
        buy = strategy.round_price_to_tick(0.3333, tick, "BUY")
        sell = strategy.round_price_to_tick(0.3333, tick, "SELL")
        assert buy <= 0.3333 <= sell
        # 结果必须落在 tick 网格上
        steps_buy = round(round(buy / tick, 10))
        steps_sell = round(round(sell / tick, 10))
        assert abs(buy - steps_buy * tick) < 1e-9
        assert abs(sell - steps_sell * tick) < 1e-9


def test_reward_boundary_has_no_fixed_one_cent_inset(strategy):
    market = market_fixture(rewards_max_spread=3.0)
    buy, sell = strategy.calculate_reward_range(0.50, 3.0, market=market)
    assert buy == 0.47  # 0.50 - 0.03，而不是旧的 0.46
    assert sell == 0.53  # 0.50 + 0.03，而不是旧的 0.54


def test_reward_boundary_inset_ticks(monkeypatch, strategy):
    monkeypatch.setenv("REWARD_BOUNDARY_INSET_TICKS", "1")
    from config import Config

    cfg = Config()
    monkeypatch.setattr("market_making_strategy.config", cfg)
    market = market_fixture(rewards_max_spread=3.0)
    buy, sell = strategy.calculate_reward_range(0.50, 3.0, market=market)
    assert buy == 0.48
    assert sell == 0.52


def test_reward_spread_decimal_helper():
    assert reward_spread_decimal(3.0, 0, 0.01) == 0.03
    assert reward_spread_decimal(3.0, 1, 0.01) == 0.02
    assert reward_spread_decimal(0.5, 0, 0.01) == 0.005
    assert reward_spread_decimal(0.1, 0, 0.01) == 0.001
    assert reward_spread_decimal(0.01, 1, 0.01) == 0.0


def test_actual_buy_price_is_tick_aligned(strategy):
    from tests.fixtures import orderbook_fixture

    book = orderbook_fixture(
        "t",
        bids=[{"price": "0.601", "size": "100"}, {"price": "0.59", "size": "100"}],
        asks=[{"price": "0.62", "size": "100"}],
    )
    price = strategy.calculate_actual_buy_price(book, 0.47)
    assert price is not None
    assert abs(price - round(price, 3)) < 1e-9


def test_hedge_sell_at_best_bid_stays_executable(strategy):
    from tests.fixtures import market_fixture

    market = market_fixture()
    price = strategy.calculate_hedge_sell_price(
        0.15,
        market=market,
        best_bid_price=0.155,
        max_bid_gap=0.05,
    )
    assert price <= 0.155
    assert price == 0.15
