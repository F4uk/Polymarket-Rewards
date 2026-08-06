"""Targeted regression tests for the PR #1 blocking-defect fixes."""
from __future__ import annotations

import pytest

from tests.conftest import make_order_manager
from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import TOKEN_A, TOKEN_B, market_fixture, orderbook_fixture


def _market(**kwargs):
    kwargs.setdefault("orderPriceMinTickSize", 0.01)
    return market_fixture(**kwargs)


def _book(clock, best_bid=0.60, best_ask=0.62):
    bids = [
        {"price": f"{best_bid - i * 0.01:.2f}", "size": "200"}
        for i in range(4)
    ]
    asks = [
        {"price": f"{best_ask + i * 0.01:.2f}", "size": "200"}
        for i in range(2)
    ]
    book = orderbook_fixture(TOKEN_A, bids=bids, asks=asks)
    book["_received_at"] = clock.monotonic()
    return book


def _make_om(clock, books=None, market=None):
    api = FakeAPIClient(
        markets=[market or _market()],
        orderbooks=books if books is not None else {TOKEN_A: _book(clock)},
    )
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = market or _market()
    return om, clob


def test_wall_and_monotonic_use_different_start_points():
    clock = FakeClock(wall_start=10_000.0, mono_start=0.0)
    om, clob = _make_om(clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]
    record = om.active_orders["market-1"][TOKEN_A]["BUY"]
    assert record["created_at"] == 10_000.0  # wall clock for display
    assert record["submitted_at"] == 0.0  # monotonic for elapsed time
    assert record["created_at_monotonic"] == 0.0

    # 确认超时按 monotonic 计算：只推进 monotonic，wall 不动
    clob.remove_open_order(order_id)
    om._confirm_pending_orders()
    assert record["status"] == "PENDING_CONFIRMATION"
    clock.mono = 6.0
    om._confirm_pending_orders()
    assert record["status"] == "UNKNOWN"


def test_confirmation_timeout_enters_unknown(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    clob.remove_open_order(resp["id"])
    fake_clock.advance(6.0)
    om._confirm_pending_orders()
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] == "UNKNOWN"


def test_cumulative_partial_fills_30_40_75_not_missed(fake_clock):
    om, clob = _make_om(fake_clock)
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 30.0) is True
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 40.0) is True
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 75.0) is True
    state = om.inventory_exits[TOKEN_A]
    assert state["confirmed_filled_size"] == 75.0
    assert state["processed_fill_size"] == 75.0


def test_query_order_filled_size_paginates_all_fills(fake_clock):
    om, clob = _make_om(fake_clock)
    clob.trade_pages = [
        [{"taker_order_id": "order-x", "size": "30", "maker_orders": []}],
        [{"taker_order_id": "order-x", "size": "45", "maker_orders": []}],
    ]
    assert om._query_order_filled_size("order-x") == 75.0


def test_partial_fill_exposure_recorded_once(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]
    clob.remove_open_order(order_id)
    clob.fill_order(order_id, 30.0)
    om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] = "LIVE"

    om.check_orders()
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(15.0)
    assert om.risk_manager.filled_orders_exposure.get("market-1", 0.0) == pytest.approx(15.0)

    om.check_orders()
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(15.0)
    assert om.risk_manager.filled_orders_exposure.get("market-1", 0.0) == pytest.approx(15.0)


def test_sell_cancel_propagation_late_fill_no_oversell(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.56)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    state = om.inventory_exits[TOKEN_A]
    old_sell_id = state["sell_order_id"]
    assert old_sell_id is not None

    # 深度进一步恶化 -> 紧急退出要求取消旧 SELL
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(fake_clock, best_bid=0.54)
    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["sell_order_status"] == "CANCEL_PENDING"
    assert state["sell_order_id"] == old_sell_id  # 取消确认前保留旧订单身份
    assert len([c for c in clob.post_order_calls if c["order"].side == "SELL"]) == 1

    # 取消传播期间旧 SELL late fill 40
    clob.fill_order(old_sell_id, 40.0)
    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["sold_size"] == 40.0
    assert state["remaining_size"] == 60.0
    new_sells = [
        c for c in clob.post_order_calls if c["order"].side == "SELL" and c["order"].size > 0
    ]
    assert new_sells[-1]["order"].size <= 60.0  # 不超卖


def test_startup_open_orders_failure_blocks_new_buy(fake_clock):
    om, clob = _make_om(fake_clock)
    clob.fail_open_orders = True
    result = om.reconcile_startup()
    assert result["open_orders_query_ok"] is False
    assert om.startup_open_orders_blocked is True

    assert om.maybe_reenter_markets([_market()]) == {}
    placed = om.place_market_orders(_market(), {})
    assert placed and all(v is False for v in placed.values())
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0

    clob.fail_open_orders = False
    om.get_positions = lambda **kwargs: []
    assert om.retry_startup_reconciliation() is True
    assert om.startup_open_orders_blocked is False


def test_unknown_reconcile_query_failure_keeps_unknown(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    clob.remove_open_order(resp["id"])
    fake_clock.advance(6.0)
    om._confirm_pending_orders()
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] == "UNKNOWN"

    clob.fail_open_orders = True
    clob.fail_trades = True

    def _fail_positions(**kwargs):
        raise RuntimeError("simulated positions failure")

    om.get_positions = _fail_positions
    om._reconcile_unknown_orders()
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] == "UNKNOWN"


def test_passive_wait_config_triggers_emergency(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.58)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "LIMITED_WAIT"

    # 超过 EXIT_PASSIVE_WAIT_SECONDS（15s）但远未到 EXIT_MAX_HOLD_SECONDS（90s）
    fake_clock.advance(16.0)
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(fake_clock, best_bid=0.58)
    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert state["sell_order_status"] == "CANCEL_PENDING"


def test_pending_reorder_without_orderbook_does_not_buy(fake_clock):
    om, clob = _make_om(fake_clock, books={})
    om.pending_reorder_tokens[TOKEN_A] = {
        "market_id": "market-1",
        "side": "BUY",
        "last_attempt_time": fake_clock.monotonic() - 10.0,
        "target_price": 0.59,
        "order_size": 100,
        "safety_info": {"reason": "test"},
    }
    om.adjust_orders_to_reward_boundaries([_market()])
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0
    assert TOKEN_A in om.pending_reorder_tokens


def test_pending_reorder_with_stale_book_does_not_buy(fake_clock):
    stale = _book(fake_clock)
    stale["_received_at"] = fake_clock.monotonic() - 10.0
    om, clob = _make_om(fake_clock, books={TOKEN_A: stale})
    om.pending_reorder_tokens[TOKEN_A] = {
        "market_id": "market-1",
        "side": "BUY",
        "last_attempt_time": fake_clock.monotonic() - 10.0,
        "target_price": 0.59,
        "order_size": 100,
        "safety_info": {"reason": "test"},
    }
    om.adjust_orders_to_reward_boundaries([_market()])
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0
    assert TOKEN_A in om.pending_reorder_tokens


def test_replacement_sell_resets_processed_and_flat_no_third_sell(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.56)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    state = om.inventory_exits[TOKEN_A]
    old_sell_id = state["sell_order_id"]

    clob.fill_order(old_sell_id, 40.0)
    om.check_inventory_exits()
    assert state["sold_size"] == 40.0
    assert state["processed_sell_size"] == 40.0
    assert state["remaining_size"] == 60.0

    # 取消剩余部分并确认 -> 替换 SELL 只挂剩余 60
    assert om.cancel_order(old_sell_id)
    om.check_inventory_exits()
    assert state["sold_size"] == 40.0
    assert state["processed_sell_size"] == 0.0  # 新订单从 0 开始累计
    new_sell_id = state["sell_order_id"]
    assert new_sell_id is not None and new_sell_id != old_sell_id

    # 替换 SELL 成交剩余 60 -> 最终 FLAT，且不得出现第三张 SELL
    clob.fill_order(new_sell_id, 60.0)
    om.check_inventory_exits()
    assert state["state"] == "FLAT"
    assert state["remaining_size"] == 0.0
    sells = [c for c in clob.post_order_calls if c["order"].side == "SELL"]
    assert len(sells) == 2


def test_retry_startup_full_reconcile_positions_failure_keeps_block(fake_clock):
    om, clob = _make_om(fake_clock)

    def _fail_positions(**kwargs):
        raise RuntimeError("simulated positions failure")

    om.get_positions = _fail_positions
    result = om.reconcile_startup()
    assert result["open_orders_query_ok"] is True
    assert result["positions_query_ok"] is False
    assert om.startup_open_orders_blocked is True

    # 重试必须重新完成完整启动对账；positions 仍失败 -> 保持阻断
    assert om.retry_startup_reconciliation() is False
    assert om.startup_open_orders_blocked is True

    om.get_positions = lambda **kwargs: []
    assert om.retry_startup_reconciliation() is True
    assert om.startup_open_orders_blocked is False


def test_startup_positions_empty_vs_failure_distinct(fake_clock):
    om, clob = _make_om(fake_clock)
    om.get_positions = lambda **kwargs: []
    result = om.reconcile_startup()
    assert result["positions_query_ok"] is True
    assert om.startup_open_orders_blocked is False

    om2, clob2 = _make_om(fake_clock)

    def _fail_positions(**kwargs):
        raise RuntimeError("simulated positions failure")

    om2.get_positions = _fail_positions
    result2 = om2.reconcile_startup()
    assert result2["positions_query_ok"] is False
    assert om2.startup_open_orders_blocked is True
    assert result2["positions_imported"] == 0
