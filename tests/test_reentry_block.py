"""Phase 5: block BUY replenishment until inventory is flat."""
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


def _make_om(clock, books=None, positions=None, open_orders=None):
    api = FakeAPIClient(
        markets=[_market()],
        orderbooks=books if books is not None else {TOKEN_A: _book(clock)},
        positions=positions if positions is not None else [],
    )
    clob = FakeClobClient(clock=clock)
    if open_orders:
        clob.open_orders = list(open_orders)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = _market()
    return om, clob


def _place_buy(om, clob, price=0.50, size=100.0):
    resp = om.place_order(
        market_id="market-1",
        token_id=TOKEN_A,
        side="BUY",
        price=price,
        size=size,
    )
    assert resp is not None
    return resp["id"]


def test_full_fill_does_not_rebuy(fake_clock):
    om, clob = _make_om(fake_clock)
    order_id = _place_buy(om, clob)
    clob.fill_order(order_id, 100.0)

    om.check_orders()

    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})
    buys = [c for c in clob.post_order_calls if c["order"].side == "BUY"]
    assert len(buys) == 1  # no unconditional re-buy after fill
    sells = [c for c in clob.post_order_calls if c["order"].side == "SELL"]
    assert len(sells) >= 1  # inventory exit started
    assert om.inventory_exits[TOKEN_A]["confirmed_filled_size"] == 100.0


def test_partial_fill_cancels_remaining_and_does_not_rebuy(fake_clock):
    om, clob = _make_om(fake_clock)
    order_id = _place_buy(om, clob)
    clob.fill_order(order_id, 30.0)  # partial, order stays open

    om.check_orders()

    assert order_id in clob.cancelled
    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})
    state = om.inventory_exits[TOKEN_A]
    assert state["confirmed_filled_size"] == 30.0
    buys = [c for c in clob.post_order_calls if c["order"].side == "BUY"]
    assert len(buys) == 1  # never re-submitted the full BUY


def test_partial_fill_only_processes_new_delta(fake_clock):
    om, clob = _make_om(fake_clock)
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.50, 30.0)
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.50, 60.0)
    state = om.inventory_exits[TOKEN_A]
    assert state["confirmed_filled_size"] == 60.0
    assert state["processed_fill_size"] == 60.0
    # 第二次调用有新增差额；同一数值再次调用则被幂等拒绝
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.50, 60.0) is False


def _register_sell(om, status="LIVE", purpose="LIMITED_WAIT_EXIT"):
    om.active_orders.setdefault("market-1", {}).setdefault(TOKEN_A, {})["SELL"] = {
        "order_id": "sell-1",
        "token_id": TOKEN_A,
        "side": "SELL",
        "price": 0.60,
        "size": 100.0,
        "exposure": 0.0,
        "created_at": om._now(),
        "submitted_at": om._now(),
        "status": status,
        "purpose": purpose,
        "generation": 1,
    }


def test_active_sell_blocks_reentry(fake_clock):
    om, clob = _make_om(fake_clock)
    _register_sell(om, status="LIVE")
    results = om.maybe_reenter_markets([_market()])
    assert results[TOKEN_A] is False
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0
    assert om.metrics["blocked_reentry_count"] >= 1


def test_pending_confirmation_sell_blocks_reentry(fake_clock):
    om, clob = _make_om(fake_clock)
    _register_sell(om, status="PENDING_CONFIRMATION")
    results = om.maybe_reenter_markets([_market()])
    assert results[TOKEN_A] is False


def test_unknown_sell_blocks_reentry(fake_clock):
    om, clob = _make_om(fake_clock)
    _register_sell(om, status="UNKNOWN")
    assert om.has_inventory_or_pending_exit(TOKEN_A) is True
    results = om.maybe_reenter_markets([_market()])
    assert results[TOKEN_A] is False


def test_inventory_state_blocks_reentry(fake_clock):
    om, clob = _make_om(fake_clock)
    om.inventory_exits[TOKEN_A] = om._new_inventory_state(
        "market-1", TOKEN_A, 0.60, 100.0
    )
    results = om.maybe_reenter_markets([_market()])
    assert results[TOKEN_A] is False


def test_cooldown_blocks_reentry(fake_clock):
    om, clob = _make_om(fake_clock)
    state = om._new_inventory_state("market-1", TOKEN_A, 0.60, 100.0)
    state["state"] = "FLAT"
    om.inventory_exits[TOKEN_A] = state
    om.reentry_cooldowns[TOKEN_A] = fake_clock.monotonic() + 30.0
    assert om.maybe_reenter_markets([_market()])[TOKEN_A] is False


def test_cooldown_end_with_entry_failure_does_not_rebuy(fake_clock):
    om, clob = _make_om(fake_clock)
    state = om._new_inventory_state("market-1", TOKEN_A, 0.60, 100.0)
    state["state"] = "FLAT"
    om.inventory_exits[TOKEN_A] = state
    om.reentry_cooldowns[TOKEN_A] = fake_clock.monotonic() - 1.0
    # stale book fails full entry
    book = _book(fake_clock)
    book["_received_at"] = fake_clock.monotonic() - 10.0
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = book
    assert om.maybe_reenter_markets([_market()])[TOKEN_A] is False
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0


def test_cooldown_end_with_entry_pass_rebuys(fake_clock):
    om, clob = _make_om(fake_clock)
    state = om._new_inventory_state("market-1", TOKEN_A, 0.60, 100.0)
    state["state"] = "FLAT"
    om.inventory_exits[TOKEN_A] = state
    om.reentry_cooldowns[TOKEN_A] = fake_clock.monotonic() - 1.0
    assert om.maybe_reenter_markets([_market()])[TOKEN_A] is True
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 1


def test_startup_reconciles_inventory_before_buy(fake_clock):
    om, clob = _make_om(
        fake_clock,
        positions=[{"asset": TOKEN_A, "size": 100.0, "avgPrice": 0.60}],
        open_orders=[
            {
                "id": "buy-old",
                "side": "BUY",
                "token_id": TOKEN_A,
                "price": 0.50,
                "size": 100.0,
                "filled": 0.0,
                "remaining": 100.0,
            },
            {
                "id": "sell-old",
                "side": "SELL",
                "token_id": TOKEN_A,
                "price": 0.60,
                "size": 100.0,
                "filled": 0.0,
                "remaining": 100.0,
            },
        ],
    )
    om.get_positions = lambda **kwargs: [
        {"asset": TOKEN_A, "size": 100.0, "avgPrice": 0.60}
    ]
    result = om.reconcile_startup()
    assert result["buys_cancelled"] == 1
    assert result["sells_imported"] == 1
    assert result["positions_imported"] == 1
    assert "buy-old" in clob.cancelled
    assert om.inventory_exits[TOKEN_A]["confirmed_filled_size"] == 100.0
    assert om.active_orders.get("market-1", {}).get(TOKEN_A, {}).get("SELL") is not None

    # 启动对账后，库存未清之前不得挂新 BUY
    results = om.maybe_reenter_markets([_market()])
    assert results[TOKEN_A] is False


def test_risk_exposure_never_negative_after_fill_flow(fake_clock):
    om, clob = _make_om(fake_clock)
    order_id = _place_buy(om, clob, price=0.50, size=100.0)
    clob.fill_order(order_id, 100.0)
    om.check_orders()
    assert om.risk_manager.get_market_exposure("market-1") >= 0.0
    # 取消/成交循环也不得产生负敞口
    om.risk_manager.remove_exposure("market-1", 9999.0)
    om.risk_manager.remove_filled_order_exposure("market-1", 0.5, 9999.0)
    assert om.risk_manager.get_market_exposure("market-1") >= 0.0
