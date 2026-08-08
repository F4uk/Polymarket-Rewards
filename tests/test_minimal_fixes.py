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
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.57)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    state = om.inventory_exits[TOKEN_A]
    old_sell_id = state["sell_order_id"]
    assert old_sell_id is not None

    # 深度进一步恶化到保护区间 -> 需要取消旧 SELL 并重新挂保护价
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


def test_legacy_buy_cancel_failure_keeps_block(fake_clock):
    om, clob = _make_om(fake_clock)
    om.get_positions = lambda **kwargs: []
    clob.open_orders = [
        {
            "id": "buy-old",
            "side": "BUY",
            "token_id": TOKEN_A,
            "price": 0.50,
            "size": 100.0,
            "filled": 0.0,
            "remaining": 100.0,
        }
    ]
    clob.cancel_fail = True

    result = om.reconcile_startup()
    assert result["buy_cancellations_ok"] is False
    assert result["buy_cancel_failures"] == 1
    assert om.startup_open_orders_blocked is True

    assert om.maybe_reenter_markets([_market()]) == {}
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0

    assert om.retry_startup_reconciliation() is False
    assert om.startup_open_orders_blocked is True


def test_legacy_buy_still_present_after_cancel_keeps_block(fake_clock):
    om, clob = _make_om(fake_clock)
    om.get_positions = lambda **kwargs: []
    clob.open_orders = [
        {
            "id": "buy-old",
            "side": "BUY",
            "token_id": TOKEN_A,
            "price": 0.50,
            "size": 100.0,
            "filled": 0.0,
            "remaining": 100.0,
        }
    ]
    clob.cancel_keep_order = True  # 取消接口成功，但订单仍存在

    result = om.reconcile_startup()
    assert result["buys_cancelled"] == 1
    assert result["buy_cancellations_ok"] is False
    assert om.startup_open_orders_blocked is True

    assert om.retry_startup_reconciliation() is False
    assert om.startup_open_orders_blocked is True


def test_main_startup_reconciliation_exception_fails_closed():
    from types import SimpleNamespace

    import main as main_module

    fake_om = SimpleNamespace(startup_open_orders_blocked=False)
    main_module._keep_startup_blocked(fake_om)
    assert fake_om.startup_open_orders_blocked is True


def test_legacy_buy_not_canceled_response_keeps_block(fake_clock):
    om, clob = _make_om(fake_clock)
    om.get_positions = lambda **kwargs: []
    clob.open_orders = [
        {
            "id": "buy-old",
            "side": "BUY",
            "token_id": TOKEN_A,
            "price": 0.50,
            "size": 100.0,
            "filled": 0.0,
            "remaining": 100.0,
        }
    ]
    clob.cancel_not_canceled = True

    result = om.reconcile_startup()
    assert result["buy_cancellations_ok"] is False
    assert result["buy_cancel_failures"] == 1
    assert result["buys_cancelled"] == 0
    assert om.startup_open_orders_blocked is True

    assert om.maybe_reenter_markets([_market()]) == {}
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0
    assert om.retry_startup_reconciliation() is False


def test_legacy_buy_cancel_response_missing_field_keeps_block(fake_clock):
    om, clob = _make_om(fake_clock)
    om.get_positions = lambda **kwargs: []
    clob.open_orders = [
        {
            "id": "buy-old",
            "side": "BUY",
            "token_id": TOKEN_A,
            "price": 0.50,
            "size": 100.0,
            "filled": 0.0,
            "remaining": 100.0,
        }
    ]
    # 生产响应缺失 not_canceled 字段
    clob.cancel_order = lambda payload: {"canceled": ["buy-old"]}

    result = om.reconcile_startup()
    assert result["buy_cancellations_ok"] is False
    assert result["buy_cancel_failures"] == 1
    assert result["buys_cancelled"] == 0
    assert om.startup_open_orders_blocked is True

    assert om.maybe_reenter_markets([_market()]) == {}
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0

    assert om.retry_startup_reconciliation() is False
    assert om.startup_open_orders_blocked is True


def test_generic_cancel_not_canceled_retains_ownership_and_exposure(fake_clock):
    om, clob = _make_om(fake_clock)
    response = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = response["id"]
    clob.cancel_not_canceled = True

    assert om.cancel_order(order_id) is False
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["order_id"] == order_id
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)
    assert order_id in {order["id"] for order in clob.open_orders}
    assert order_id not in om.cancel_pending_tracking


def test_rescan_rejected_cancel_is_not_counted(fake_clock):
    om, clob = _make_om(fake_clock)
    response = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = response["id"]
    clob.cancel_not_canceled = True

    assert om.cancel_reward_buys_for_rescan() == (0, 1)
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["order_id"] == order_id
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)


@pytest.mark.parametrize(
    "cancel_response",
    [
        {"not_canceled": {}},
        {"canceled": ["order-1"]},
        {"canceled": {}, "not_canceled": {}},
        {"canceled": ["order-1"], "not_canceled": []},
        {"canceled": [], "not_canceled": {}},
    ],
)
def test_malformed_cancel_response_never_succeeds(fake_clock, cancel_response):
    om, clob = _make_om(fake_clock)
    response = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = response["id"]
    clob.cancel_order = lambda _payload: cancel_response

    assert om.cancel_order(order_id) is False
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["order_id"] == order_id
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)
    assert order_id not in om.cancel_pending_tracking


def test_shutdown_mixed_cancel_results_preserve_failed_orders(fake_clock):
    om, clob = _make_om(fake_clock)
    order_ids = []
    for token_id in ("token-a", "token-b", "token-c"):
        response = om.place_order("market-1", token_id, "BUY", 0.50, 100.0)
        order_ids.append(response["id"])

    confirmed_id, rejected_id, exception_id = order_ids

    def mixed_cancel(payload):
        order_id = payload.orderID
        if order_id == confirmed_id:
            clob.open_orders = [
                order for order in clob.open_orders if order.get("id") != order_id
            ]
            return {"canceled": [order_id], "not_canceled": {}}
        if order_id == rejected_id:
            return {"canceled": [], "not_canceled": {order_id: "rejected"}}
        raise RuntimeError("simulated cancel failure")

    clob.cancel_order = mixed_cancel

    assert om.cancel_all_buy_orders() == 1
    assert confirmed_id in om.cancel_pending_tracking
    assert "BUY" not in om.active_orders["market-1"].get("token-a", {})
    assert om.active_orders["market-1"]["token-b"]["BUY"]["order_id"] == rejected_id
    assert om.active_orders["market-1"]["token-c"]["BUY"]["order_id"] == exception_id
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(150.0)


def test_post_submit_cancel_rejection_keeps_unknown_ownership(
    monkeypatch, fake_clock
):
    om, clob = _make_om(fake_clock)
    add_exposure = om.risk_manager.add_exposure

    def fail_normal_reservation(market_id, exposure, *, allow_over_limit=False):
        if allow_over_limit:
            return add_exposure(
                market_id, exposure, allow_over_limit=allow_over_limit
            )
        return False

    monkeypatch.setattr(
        om.risk_manager, "add_exposure", fail_normal_reservation
    )
    clob.cancel_not_canceled = True

    assert om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0) is None

    order_id = clob.open_orders[0]["id"]
    tracked = om.active_orders["market-1"][TOKEN_A]["BUY"]
    assert tracked["order_id"] == order_id
    assert tracked["status"] == "UNKNOWN"
    assert tracked["exposure"] == pytest.approx(50.0)
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)
    assert order_id not in om.cancel_pending_tracking


def test_cancel_pending_buy_blocks_different_price_until_reconciled(fake_clock):
    market = _market(tokens=[{"token_id": TOKEN_A, "outcome": "YES"}])
    om, clob = _make_om(
        fake_clock, {TOKEN_A: _book(fake_clock)}, market=market
    )
    response = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    old_order_id = response["id"]
    clob.cancel_keep_order = True

    assert om.cancel_reward_buys_for_rescan() == (1, 1)
    assert old_order_id in om.cancel_pending_tracking

    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(
        fake_clock, best_bid=0.65, best_ask=0.67
    )
    initial_buy_posts = len(
        [call for call in clob.post_order_calls if call["order"].side == "BUY"]
    )
    results = om.place_market_orders(market, {})

    assert results.get(TOKEN_A, False) is False
    assert old_order_id in om.cancel_pending_tracking
    assert len(
        [call for call in clob.post_order_calls if call["order"].side == "BUY"]
    ) == initial_buy_posts

    clob.remove_open_order(old_order_id)
    om._process_cancel_pending()
    assert old_order_id not in om.cancel_pending_tracking

    om.place_market_orders(market, {})
    buy_posts = [call for call in clob.post_order_calls if call["order"].side == "BUY"]
    assert len(buy_posts) == initial_buy_posts + 1
    assert float(buy_posts[-1]["order"].price) != pytest.approx(0.50)


def test_post_submit_cancel_rejection_keeps_risk_through_reconciliation(
    monkeypatch, fake_clock
):
    om, clob = _make_om(fake_clock)
    om.risk_manager.max_exposure_per_market_usdc = 75.0
    add_exposure = om.risk_manager.add_exposure

    def fail_normal_reservation(market_id, exposure, *, allow_over_limit=False):
        if allow_over_limit:
            return add_exposure(
                market_id, exposure, allow_over_limit=allow_over_limit
            )
        return False

    monkeypatch.setattr(
        om.risk_manager, "add_exposure", fail_normal_reservation
    )
    clob.cancel_not_canceled = True

    assert om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0) is None
    order_id = clob.open_orders[0]["id"]
    tracked = om.active_orders["market-1"][TOKEN_A]["BUY"]
    assert tracked["status"] == "UNKNOWN"
    assert tracked["exposure"] == pytest.approx(50.0)
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)

    assert om.place_order("market-1", TOKEN_B, "BUY", 0.50, 100.0) is None
    assert len(clob.open_orders) == 1

    om.get_positions = lambda **_kwargs: []
    om._reconcile_unknown_orders()
    assert tracked["status"] == "LIVE"
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)

    clob.cancel_not_canceled = False
    assert om.cancel_order(order_id) is True
    om._process_cancel_pending()
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(0.0)

    om._process_cancel_pending()
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(0.0)


def test_cancel_pending_query_failures_retain_ownership_and_exposure(fake_clock):
    om, clob = _make_om(fake_clock)
    response = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = response["id"]
    assert om.cancel_order(order_id) is True
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)

    clob.fail_open_orders = True
    clob.fail_trades = True
    om._process_cancel_pending()
    assert order_id in om.cancel_pending_tracking
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)

    clob.fail_open_orders = False
    om._process_cancel_pending()
    assert order_id in om.cancel_pending_tracking
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)

    clob.fail_trades = False
    om._process_cancel_pending()
    assert order_id not in om.cancel_pending_tracking
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(0.0)


def test_cancel_pending_sell_query_failures_never_submit_replacement(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.57)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    state = om.inventory_exits[TOKEN_A]
    sell_id = state["sell_order_id"]
    clob.cancel_keep_order = True
    assert om.cancel_order(sell_id) is True
    assert state["sell_order_status"] == "CANCEL_PENDING"

    clob.fail_open_orders = True
    clob.fail_trades = True
    for _ in range(3):
        om.check_inventory_exits()

    assert state["sell_order_id"] == sell_id
    assert state["sell_order_status"] == "CANCEL_PENDING"
    assert sell_id in {order["id"] for order in clob.open_orders}
    assert len([c for c in clob.post_order_calls if c["order"].side == "SELL"]) == 1


def test_missing_live_with_unavailable_evidence_enters_unknown(fake_clock):
    om, clob = _make_om(fake_clock)
    response = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = response["id"]
    tracked = om.active_orders["market-1"][TOKEN_A]["BUY"]
    tracked["status"] = "LIVE"
    clob.remove_open_order(order_id)
    clob.fail_trades = True

    def fail_positions(**_kwargs):
        raise RuntimeError("simulated positions failure")

    om.get_positions = fail_positions
    om.check_orders()

    assert tracked["status"] == "UNKNOWN"
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["order_id"] == order_id
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)

    om.check_orders()
    assert tracked["status"] == "UNKNOWN"
    assert om.risk_manager.get_market_exposure("market-1") == pytest.approx(50.0)
