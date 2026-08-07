"""Focused regression tests: cancel reward BUYs before the blocking rescan.

Covers cancellation-before-scan ordering, inventory SELL preservation, cancel
failure skipping the scan, and fresh BUY placement for retained markets.
"""
from __future__ import annotations

import pytest

import main as main_module
from config import config
from tests.conftest import make_order_manager, register_fake_orders
from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import TOKEN_A, TOKEN_B, market_fixture, orderbook_fixture


def _market(market_id="market-1", tokens=None):
    return market_fixture(
        market_id=market_id,
        orderPriceMinTickSize=0.01,
        tokens=tokens
        if tokens is not None
        else [
            {"token_id": TOKEN_A, "outcome": "YES"},
            {"token_id": TOKEN_B, "outcome": "NO"},
        ],
    )


def _make_om(clock):
    api = FakeAPIClient(markets=[_market()], orderbooks={})
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = _market()
    return om, clob


def _register_sell(om, clob, order_id, purpose):
    register_fake_orders(
        om,
        {
            order_id: {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "SELL",
                "price": 0.55,
                "size": 100.0,
                "status": "LIVE",
                "purpose": purpose,
            }
        },
    )
    clob.open_orders.append(
        {
            "id": order_id,
            "token_id": TOKEN_A,
            "price": 0.55,
            "size": 100.0,
            "filled": 0.0,
            "remaining": 100.0,
            "side": "SELL",
        }
    )


@pytest.mark.parametrize(
    "purpose",
    ["FAST_EXIT", "LIMITED_WAIT_EXIT", "EMERGENCY_EXIT"],
)
def test_rescan_cancel_preserves_inventory_sell(fake_clock, purpose):
    om, clob = _make_om(fake_clock)
    register_fake_orders(
        om,
        {
            "buy-1": {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "BUY",
                "price": 0.50,
                "size": 100.0,
                "status": "LIVE",
                "purpose": "REWARD_BUY",
            }
        },
    )
    clob.open_orders.append(
        {
            "id": "buy-1",
            "token_id": TOKEN_A,
            "price": 0.50,
            "size": 100.0,
            "filled": 0.0,
            "remaining": 100.0,
            "side": "BUY",
        }
    )
    _register_sell(om, clob, "sell-1", purpose)

    cancelled, total = om.cancel_reward_buys_for_rescan()

    assert (cancelled, total) == (1, 1)
    assert "BUY" not in om.active_orders["market-1"][TOKEN_A]
    sell = om.active_orders["market-1"][TOKEN_A]["SELL"]
    assert sell["order_id"] == "sell-1"
    assert sell["purpose"] == purpose
    assert "sell-1" not in clob.cancelled


class _MainOrderManager:
    """Minimal OrderManager stand-in for exercising the real main loop."""

    def __init__(self, events, cancel_result=(1, 1)):
        self.events = events
        self.cancel_result = cancel_result
        self.startup_open_orders_blocked = False
        self.market_data_cache = {}
        self.place_calls = 0

    def reconcile_startup(self):
        return {
            "open_orders": 0,
            "buys_cancelled": 0,
            "sells_imported": 0,
            "positions_imported": 0,
            "open_orders_query_ok": True,
        }

    def check_positions_and_hedge(self):
        return {}

    def check_orders(self):
        return {}

    def maybe_reenter_markets(self, markets):
        return {}

    def get_order_statistics(self):
        return {
            "active_orders_count": 0,
            "active_markets_count": 0,
            "total_exposure_usdc": 0.0,
            "filled_buy_orders_count": 0,
            "subscribed_tokens_count": 0,
        }

    def get_active_orders(self, market_id=None):
        return {}

    def adjust_orders_to_reward_boundaries(self, markets):
        return {}

    def cancel_reward_buys_for_rescan(self):
        self.events.append("cancel_reward_buys_for_rescan")
        return self.cancel_result

    def refresh_market_selection(self, previous, new, selected):
        self.events.append("refresh_market_selection")
        return {
            "retained": sorted(set(previous) & set(new)),
            "removed": sorted(set(previous) - set(new)),
            "added": sorted(set(new) - set(previous)),
            "cancelled_buys": 0,
            "placed_markets": 0,
        }

    def place_market_orders(self, market, orderbooks):
        self.place_calls += 1
        phase = "startup_place" if self.place_calls == 1 else "rescan_place"
        self.events.append(f"{phase}:{market.get('market_id')}")
        return {}

    def cancel_all_buy_orders(self):
        return 0


class _MainMarketManager:
    """Minimal MarketManager stand-in that records one startup + one rescan."""

    def __init__(self, events, market, previous_market_ids=None):
        self.events = events
        self.market = market
        self.previous_market_ids = set(previous_market_ids or [])
        self.scan_calls = 0
        self.filter_calls = 0

    def scan_rewards_markets(self):
        self.scan_calls += 1
        name = "startup_scan" if self.scan_calls == 1 else "rescan_scan"
        self.events.append(name)
        return [self.market]

    def filter_markets(self):
        self.filter_calls += 1
        name = "startup_filter" if self.filter_calls == 1 else "rescan_filter"
        self.events.append(name)
        return [self.market]

    def update_selected_market_ids(self):
        self.events.append("update_selected_market_ids")
        main_module.running = False  # exit main loop after this rescan pass
        return set(self.previous_market_ids)

    def get_selected_market_ids(self):
        self.events.append("get_selected_market_ids")
        return {self.market["market_id"]}

    def get_selected_markets(self):
        return [self.market]


def _run_main_loop(monkeypatch, om, mm):
    monkeypatch.setattr(main_module, "PolymarketAPIClient", lambda: FakeAPIClient())
    monkeypatch.setattr(main_module, "MarketMakingStrategy", lambda: object())
    monkeypatch.setattr(main_module, "RiskManager", lambda: object())
    monkeypatch.setattr(main_module, "OrderManager", lambda **kwargs: om)
    monkeypatch.setattr(main_module, "MarketManager", lambda api: mm)
    monkeypatch.setitem(config.config, "update_interval_seconds", 0)
    monkeypatch.setitem(config.config, "order_check_interval_seconds", 0)
    monkeypatch.setitem(config.config, "orderbook_update_interval_seconds", 0)
    monkeypatch.setattr(main_module, "running", True)
    main_module.main()


def test_buy_cancellation_happens_before_scan(monkeypatch):
    events = []
    om = _MainOrderManager(events, cancel_result=(1, 1))
    mm = _MainMarketManager(events, _market())

    _run_main_loop(monkeypatch, om, mm)

    assert events.index("cancel_reward_buys_for_rescan") < events.index(
        "rescan_scan"
    )
    assert events.index("rescan_scan") < events.index("rescan_filter")


def test_cancel_failure_skips_scan(monkeypatch):
    events = []
    om = _MainOrderManager(events, cancel_result=(0, 1))
    mm = _MainMarketManager(events, _market())

    _run_main_loop(monkeypatch, om, mm)

    assert "cancel_reward_buys_for_rescan" in events
    assert "rescan_scan" not in events
    assert "rescan_filter" not in events
    assert "refresh_market_selection" not in events


def test_retained_market_gets_fresh_buy(monkeypatch):
    events = []
    om = _MainOrderManager(events, cancel_result=(1, 1))
    mm = _MainMarketManager(
        events, _market(), previous_market_ids={"market-1"}
    )

    _run_main_loop(monkeypatch, om, mm)

    assert events.index("cancel_reward_buys_for_rescan") < events.index(
        "rescan_scan"
    )
    assert events.index("refresh_market_selection") < events.index(
        "rescan_place:market-1"
    )
