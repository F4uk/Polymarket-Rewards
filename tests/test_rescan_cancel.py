"""Focused regression tests: cancel reward BUYs before the blocking rescan.

Covers cancellation-before-scan ordering, inventory SELL preservation,
cancellation-uncertainty deferral, fill-during-cancel deferral, scan-failure
behavior, and fresh BUY placement for retained markets.
"""
from __future__ import annotations

import pytest

from main import perform_market_rescan
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


def _book(clock, token_id, best_bid=0.60, best_ask=0.62):
    bids = [
        {"price": f"{best_bid - i * 0.01:.2f}", "size": "200"}
        for i in range(4)
    ]
    asks = [
        {"price": f"{best_ask + i * 0.01:.2f}", "size": "200"}
        for i in range(2)
    ]
    book = orderbook_fixture(token_id, bids=bids, asks=asks)
    book["_received_at"] = clock.monotonic()
    return book


def _make_env(clock):
    m1 = _market("market-1")
    books = {
        TOKEN_A: _book(clock, TOKEN_A, 0.60, 0.62),
        TOKEN_B: _book(clock, TOKEN_B, 0.40, 0.42),
    }
    api = FakeAPIClient(markets=[m1], orderbooks=books)
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = m1
    return om, clob, m1


class RecordingMarketManager:
    """Minimal MarketManager stand-in that records call order."""

    def __init__(
        self,
        selected=None,
        all_markets=None,
        fail_scan=False,
        previous_market_ids=None,
    ):
        self.calls = []
        self.selected = list(selected or [])
        self.all_markets = list(
            all_markets if all_markets is not None else self.selected
        )
        self.fail_scan = fail_scan
        self._previous_market_ids = set(previous_market_ids or [])

    def update_selected_market_ids(self):
        self.calls.append("update_selected_market_ids")
        return set(self._previous_market_ids)

    def scan_rewards_markets(self):
        self.calls.append("scan_rewards_markets")
        if self.fail_scan:
            raise RuntimeError("simulated scan failure")
        return list(self.all_markets)

    def filter_markets(self):
        self.calls.append("filter_markets")
        return list(self.selected)

    def get_selected_market_ids(self):
        self.calls.append("get_selected_market_ids")
        return {m.get("market_id") for m in self.selected}

    def get_selected_markets(self):
        return list(self.selected)


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


def test_buy_cancellation_happens_before_scan(fake_clock):
    om, clob, m1 = _make_env(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    assert resp is not None

    mm = RecordingMarketManager(selected=[])
    events = []
    mm.calls = events
    original_cancel = om.cancel_buy_orders_for_rescan
    original_confirm = om.confirm_buy_cancellations_for_rescan

    def spy_cancel():
        events.append("cancel")
        return original_cancel()

    def spy_confirm():
        events.append("confirm")
        return original_confirm()

    om.cancel_buy_orders_for_rescan = spy_cancel
    om.confirm_buy_cancellations_for_rescan = spy_confirm

    result = perform_market_rescan(om, mm)

    assert result["scanned"] is True
    assert events.index("cancel") < events.index("confirm")
    assert events.index("cancel") < events.index("scan_rewards_markets")
    assert events.index("confirm") < events.index("scan_rewards_markets")
    assert events.index("scan_rewards_markets") < events.index("filter_markets")


@pytest.mark.parametrize(
    "purpose",
    ["FAST_EXIT", "LIMITED_WAIT_EXIT", "EMERGENCY_EXIT"],
)
def test_rescan_cancel_preserves_inventory_sell(fake_clock, purpose):
    om, clob, m1 = _make_env(fake_clock)
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

    assert om.cancel_buy_orders_for_rescan() == 1
    confirmation = om.confirm_buy_cancellations_for_rescan()

    assert confirmation["scan_ready"] is True
    assert "BUY" not in om.active_orders["market-1"][TOKEN_A]
    sell = om.active_orders["market-1"][TOKEN_A]["SELL"]
    assert sell["order_id"] == "sell-1"
    assert sell["purpose"] == purpose
    assert "sell-1" not in clob.cancelled


def test_cancel_uncertainty_defers_scan(fake_clock):
    om, clob, m1 = _make_env(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]
    # Cancel API succeeds, but the BUY stays visible in open orders.
    clob.cancel_keep_order = True

    mm = RecordingMarketManager(selected=[m1])
    result = perform_market_rescan(om, mm)

    assert result["scanned"] is False
    assert result["cancellation"]["scan_ready"] is False
    assert "scan_rewards_markets" not in mm.calls
    assert "filter_markets" not in mm.calls
    assert order_id in om.cancel_pending_tracking
    buys = [c for c in clob.post_order_calls if c["order"].side == "BUY"]
    assert len(buys) == 1  # no new BUY placed


def test_unknown_buy_blocks_rescan(fake_clock):
    om, clob, m1 = _make_env(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] = "UNKNOWN"
    clob.cancel_fail = True  # cancellation cannot be resolved

    mm = RecordingMarketManager(selected=[m1])
    result = perform_market_rescan(om, mm)

    assert result["scanned"] is False
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] == "UNKNOWN"
    assert "scan_rewards_markets" not in mm.calls
    assert "filter_markets" not in mm.calls


def test_fill_during_cancel_defers_scan_and_preserves_inventory(fake_clock):
    om, clob, m1 = _make_env(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]

    assert om.cancel_buy_orders_for_rescan() == 1
    # Fill appears while cancellation is still propagating.
    clob.fill_order(order_id, 40.0)

    mm = RecordingMarketManager(selected=[m1])
    result = perform_market_rescan(om, mm)

    assert result["scanned"] is False
    assert result["cancellation"]["filled_during_cancel"] is True
    assert "scan_rewards_markets" not in mm.calls
    state = om.inventory_exits[TOKEN_A]
    assert state["confirmed_filled_size"] == 40.0
    assert state["processed_fill_size"] == 40.0
    assert order_id not in om.cancel_pending_tracking
    buys = [c for c in clob.post_order_calls if c["order"].side == "BUY"]
    assert len(buys) == 1  # fill not double-processed, no duplicate BUY


def test_scan_failure_after_cancel_does_not_recreate_buy(fake_clock):
    om, clob, m1 = _make_env(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    assert resp is not None
    _register_sell(om, clob, "sell-1", "FAST_EXIT")

    mm = RecordingMarketManager(selected=[m1], fail_scan=True)
    with pytest.raises(RuntimeError):
        perform_market_rescan(om, mm)

    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})
    assert om.active_orders["market-1"][TOKEN_A]["SELL"]["order_id"] == "sell-1"
    assert "sell-1" not in clob.cancelled
    buys = [c for c in clob.post_order_calls if c["order"].side == "BUY"]
    assert len(buys) == 1  # only the pre-scan BUY was ever placed


def test_retained_market_gets_fresh_buy_after_successful_rescan(fake_clock):
    om, clob, m1 = _make_env(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    original_buy_id = resp["id"]

    mm = RecordingMarketManager(
        selected=[m1],
        all_markets=[m1],
        previous_market_ids={"market-1"},
    )
    result = perform_market_rescan(om, mm)

    assert result["scanned"] is True
    assert result["retained_placed"] == 1
    assert original_buy_id in clob.cancelled
    assert "BUY" in om.active_orders["market-1"][TOKEN_A]
    assert (
        om.active_orders["market-1"][TOKEN_A]["BUY"]["order_id"]
        != original_buy_id
    )
