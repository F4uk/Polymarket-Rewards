"""Phase 0: deterministic regression baseline for existing core behavior."""
from __future__ import annotations

from py_clob_client_v2.clob_types import OrderType

from config import config
from market_making_strategy import MarketMakingStrategy
from risk_manager import RiskManager
from tests.conftest import make_order_manager
from tests.fakes import FakeClobClient, FakeClock
from tests.fixtures import market_fixture, orderbook_fixture


def test_config_defaults_are_present():
    assert config.max_markets >= 0
    assert config.order_size_multiplier > 0
    assert config.spread_range["max"] is None or config.spread_range["max"] > 0


def test_strategy_normalize_price_floors_and_clamps():
    strategy = MarketMakingStrategy()
    assert strategy.normalize_price(0.156, 0.01) == 0.15
    assert strategy.normalize_price(0.1567, 0.001) == 0.156
    assert strategy.normalize_price(-0.5, 0.01) == 0.01
    assert strategy.normalize_price(2.0, 0.01) == 1.0


def test_strategy_calculate_order_size():
    strategy = MarketMakingStrategy()
    market = market_fixture(rewards_min_size=50)
    assert strategy.calculate_order_size(market, multiplier=2.0) == 100
    assert strategy.calculate_order_size(market, multiplier=0.5) == 50


def test_strategy_reward_range_boundaries():
    strategy = MarketMakingStrategy()
    buy, sell = strategy.calculate_reward_range(0.50, 3.0)
    assert buy < 0.50 < sell


def test_risk_manager_exposure_never_negative():
    rm = RiskManager(max_exposure_per_market_usdc=100.0)
    assert rm.can_place_order("m1", 0.5, 100, "BUY")
    assert rm.add_exposure("m1", 50.0)
    rm.remove_exposure("m1", 200.0)
    assert rm.get_market_exposure("m1") == 0.0
    rm.remove_filled_order_exposure("m1", 0.5, 100)
    assert rm.get_market_exposure("m1") == 0.0


def test_risk_manager_sell_does_not_count_exposure():
    rm = RiskManager(max_exposure_per_market_usdc=100.0)
    assert rm.can_place_order("m1", 0.5, 1000, "SELL")
    assert rm.calculate_exposure(0.5, 1000, "SELL") == 0.0


def test_baseline_order_place_and_cancel():
    clock = FakeClock()
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(clob_client=clob, clock=clock)
    om.market_data_cache["m1"] = market_fixture("m1")

    response = om.place_order(
        market_id="m1",
        token_id="token-a",
        side="BUY",
        price=0.50,
        size=100.0,
        order_type=OrderType.GTC,
    )
    assert response is not None
    order_id = response.get("id")
    assert order_id in {o["id"] for o in clob.open_orders}
    assert om.get_active_orders("m1")["token-a"]["BUY"]["order_id"] == order_id

    assert om.cancel_order(order_id) is True
    assert order_id not in {o["id"] for o in clob.open_orders}
    assert "BUY" not in om.get_active_orders("m1").get("token-a", {})


def test_baseline_order_statistics():
    om = make_order_manager()
    stats = om.get_order_statistics()
    assert stats["active_orders_count"] == 0
    assert stats["active_markets_count"] == 0
    assert stats["total_exposure_usdc"] == 0.0
    assert stats["filled_buy_orders_count"] == 0


def test_baseline_reward_boundary_with_orderbook():
    strategy = MarketMakingStrategy()
    book = orderbook_fixture(
        "t",
        bids=[{"price": "0.60", "size": "100"}, {"price": "0.59", "size": "100"}],
        asks=[{"price": "0.62", "size": "100"}],
    )
    prices = strategy.calculate_order_prices(book, 3.0, market=market_fixture())
    assert prices is not None
    assert 0.0 < prices["buy_price"] < prices["sell_price"] <= 1.0
