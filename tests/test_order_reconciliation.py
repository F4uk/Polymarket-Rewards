"""Phase 8: order confirmation windows and idempotent reconciliation."""
from __future__ import annotations

from tests.conftest import make_order_manager
from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import TOKEN_A, market_fixture, orderbook_fixture


def _market(**kwargs):
    kwargs.setdefault("orderPriceMinTickSize", 0.01)
    return market_fixture(**kwargs)


def _book(clock, token_id=TOKEN_A, best_bid=0.60, best_ask=0.62):
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


def _make_om(clock, books=None):
    api = FakeAPIClient(
        markets=[_market()],
        orderbooks=books if books is not None else {TOKEN_A: _book(clock)},
    )
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = _market()
    return om, clob


def test_buy_success_invisible_not_duplicated(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    assert resp is not None
    order_id = resp["id"]
    clob.remove_open_order(order_id)  # propagation delay

    om.check_orders()
    om.check_orders()

    buys = [c for c in clob.post_order_calls if c["order"].side == "BUY"]
    assert len(buys) == 1
    # 仍在确认窗口内：不得被误判为已取消
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] in (
        "PENDING_CONFIRMATION",
        "SUBMITTED",
    )


def test_confirmation_window_blocks_same_fingerprint(fake_clock):
    om, clob = _make_om(fake_clock)
    r1 = om.place_order(
        "market-1", TOKEN_A, "SELL", 0.60, 100.0,
        purpose="FAST_EXIT", generation=1,
    )
    r2 = om.place_order(
        "market-1", TOKEN_A, "SELL", 0.60, 100.0,
        purpose="FAST_EXIT", generation=1,
    )
    assert r1 is not None
    assert r2 is None
    assert om.metrics["blocked_duplicate_count"] >= 1


def test_timeout_moves_order_to_unknown_then_reconciles(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]
    clob.remove_open_order(order_id)

    # 未超时：保持 PENDING_CONFIRMATION
    om._confirm_pending_orders()
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] == "PENDING_CONFIRMATION"

    fake_clock.advance(10.0)
    om._confirm_pending_orders()
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] == "UNKNOWN"
    assert om.metrics["unknown_order_count"] >= 1

    # 对账：无订单、无成交、无持仓 -> FAILED
    om._reconcile_unknown_orders()
    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})


def test_unknown_sell_not_retried_before_reconcile(fake_clock):
    om, clob = _make_om(fake_clock)
    state = om._new_inventory_state("market-1", TOKEN_A, 0.60, 100.0)
    state["sell_order_id"] = "sell-unknown"
    state["sell_order_status"] = "UNKNOWN"
    state["remaining_size"] = 100.0
    om.inventory_exits[TOKEN_A] = state

    assert (
        om._submit_inventory_sell(TOKEN_A, 0.60, 100.0, "EMERGENCY_EXIT") is None
    )
    assert len([c for c in clob.post_order_calls if c["order"].side == "SELL"]) == 0


def test_unknown_buy_filled_reconciles_to_inventory(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]
    clob.remove_open_order(order_id)
    clob.fill_order(order_id, 40.0)  # trade history proves the fill
    fake_clock.advance(10.0)

    om.check_orders()

    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})
    assert om.inventory_exits[TOKEN_A]["confirmed_filled_size"] == 40.0


def test_cancel_pending_still_processes_new_fills(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]
    assert om.cancel_order(order_id)
    assert order_id in om.cancel_pending_tracking

    # 取消尚未传播，订单又成交了 25
    clob.fill_order(order_id, 25.0)
    om.check_orders()

    assert order_id not in om.cancel_pending_tracking
    assert om.inventory_exits[TOKEN_A]["confirmed_filled_size"] == 25.0


def test_micro_position_jitter_requires_confirmations(fake_clock):
    om, clob = _make_om(fake_clock)
    om.active_orders.setdefault("market-1", {}).setdefault(TOKEN_A, {})["SELL"] = {
        "order_id": "sell-1",
        "token_id": TOKEN_A,
        "side": "SELL",
        "price": 0.60,
        "size": 100.0,
        "exposure": 0.0,
        "created_at": fake_clock.monotonic(),
        "submitted_at": fake_clock.monotonic(),
        "status": "LIVE",
        "purpose": "LIMITED_WAIT_EXIT",
        "generation": 1,
    }
    state = om._new_inventory_state("market-1", TOKEN_A, 0.60, 100.0)
    state["sell_order_id"] = "sell-1"
    state["sell_order_status"] = "LIVE"
    state["state"] = "LIMITED_WAIT"
    om.inventory_exits[TOKEN_A] = state
    om.get_positions = lambda **kwargs: [
        {"asset": TOKEN_A, "size": 100.5, "avgPrice": 0.60}
    ]

    om.check_positions_and_hedge()
    assert "sell-1" not in clob.cancelled
    assert om.position_diff_confirmations.get(TOKEN_A) == 1

    om.check_positions_and_hedge()
    assert "sell-1" in clob.cancelled
    assert om.position_diff_confirmations.get(TOKEN_A) == 0
    # 调整后重新挂出 SELL
    sells_after = [
        c
        for c in clob.post_order_calls
        if c["order"].side == "SELL" and c["order"].size > 0
    ]
    assert len(sells_after) >= 1


def test_emergency_bypasses_position_confirmation(fake_clock):
    om, clob = _make_om(fake_clock)
    om.active_orders.setdefault("market-1", {}).setdefault(TOKEN_A, {})["SELL"] = {
        "order_id": "sell-1",
        "token_id": TOKEN_A,
        "side": "SELL",
        "price": 0.56,
        "size": 100.0,
        "exposure": 0.0,
        "created_at": fake_clock.monotonic(),
        "submitted_at": fake_clock.monotonic(),
        "status": "LIVE",
        "purpose": "EMERGENCY_EXIT",
        "generation": 1,
    }
    state = om._new_inventory_state("market-1", TOKEN_A, 0.60, 100.0)
    state["sell_order_id"] = "sell-1"
    state["sell_order_status"] = "LIVE"
    state["state"] = "EMERGENCY_EXIT"
    om.inventory_exits[TOKEN_A] = state
    om.get_positions = lambda **kwargs: [
        {"asset": TOKEN_A, "size": 100.5, "avgPrice": 0.60}
    ]

    om.check_positions_and_hedge()
    assert om.position_diff_confirmations.get(TOKEN_A, 0) == 0


def test_fingerprint_distinguishes_reward_buy_and_exit_sell(fake_clock):
    om, clob = _make_om(fake_clock)
    buy = om.place_order(
        "market-1", TOKEN_A, "BUY", 0.60, 100.0, purpose="REWARD_BUY"
    )
    sell = om.place_order(
        "market-1", TOKEN_A, "SELL", 0.60, 100.0,
        purpose="FAST_EXIT", generation=1,
    )
    assert buy is not None
    assert sell is not None
