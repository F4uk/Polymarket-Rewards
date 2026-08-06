"""Phase 4: time-bounded tiered inventory exit."""
from __future__ import annotations

from tests.conftest import make_order_manager
from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import TOKEN_A, TOKEN_B, market_fixture, orderbook_fixture


def _market(**kwargs):
    kwargs.setdefault("orderPriceMinTickSize", 0.01)
    return market_fixture(**kwargs)


def _book(clock, token_id=TOKEN_A, best_bid=0.60, best_ask=0.62, bid_sizes=None, ask_sizes=None):
    bid_sizes = bid_sizes or [200.0] * 4
    ask_sizes = ask_sizes or [200.0] * 2
    bids = [
        {"price": f"{best_bid - i * 0.01:.2f}", "size": str(bid_sizes[i])}
        for i in range(4)
    ]
    asks = [
        {"price": f"{best_ask + i * 0.01:.2f}", "size": str(ask_sizes[i])}
        for i in range(2)
    ]
    book = orderbook_fixture(token_id, bids=bids, asks=asks)
    book["_received_at"] = clock.monotonic()
    return book


def _make_om(clock, books, positions=None):
    api = FakeAPIClient(
        markets=[_market()],
        orderbooks=books,
        positions=positions if positions is not None else [],
    )
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = _market()
    return om, clob


def _sell_record(om, token_id=TOKEN_A):
    return om.active_orders.get("market-1", {}).get(token_id, {}).get("SELL", {})


def test_fast_exit_zero_loss(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "FAST_EXIT"
    assert state["sell_order_id"] is not None
    assert state["confirmed_filled_size"] == 100.0
    sell = clob.post_order_calls[-1]["order"]
    assert sell.side == "SELL"
    assert sell.price == 0.60
    assert sell.size == 100.0
    assert _sell_record(om)["purpose"] == "FAST_EXIT"


def test_fast_exit_one_tick_loss(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.59)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    sell = clob.post_order_calls[-1]["order"]
    assert sell.price == 0.59
    assert sell.size == 100.0


def test_bps_threshold_blocks_low_price_one_tick_fast_exit(fake_clock):
    book = _book(fake_clock, best_bid=0.04, best_ask=0.06)
    om, clob = _make_om(fake_clock, {TOKEN_A: book})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.05, 100.0)

    state = om.inventory_exits[TOKEN_A]
    # 1 tick loss at 0.05 = 2000 bps: exceeds both immediate (300) and
    # emergency (1000) bps thresholds, so fast exit must NOT be used.
    assert state["state"] == "EMERGENCY_EXIT"
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    sell = clob.post_order_calls[-1]["order"]
    assert sell.price == 0.04  # executable at best bid


def test_limited_wait_timeout_goes_emergency(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.58)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    passive = _sell_record(om)
    assert passive["purpose"] == "LIMITED_WAIT_EXIT"
    passive_id = passive["order_id"]

    fake_clock.advance(100.0)  # beyond EXIT_MAX_HOLD_SECONDS
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(fake_clock, best_bid=0.58)
    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    # 取消传播期间：保留旧订单身份，不得立即提交覆盖相同库存的新 SELL
    assert state["state"] == "EMERGENCY_EXIT"
    assert state["sell_order_status"] == "CANCEL_PENDING"
    assert state["sell_order_id"] == passive_id
    assert passive_id in clob.cancelled
    sells_after_cancel = [
        c for c in clob.post_order_calls if c["order"].side == "SELL"
    ]
    assert len(sells_after_cancel) == 1  # 只有原来的被动卖单

    # 取消确认后，只为剩余库存重新挂 SELL
    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["sell_order_status"] in ("PENDING_CONFIRMATION", "LIVE")
    emergency = _sell_record(om)
    assert emergency["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.58


def test_severe_loss_immediate_emergency(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.56)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert om.inventory_exits[TOKEN_A]["state"] == "EMERGENCY_EXIT"
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.56


def test_bid_depth_drop_triggers_emergency(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.60)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    first_sell = _sell_record(om)["order_id"]

    # Book drops more than 2 ticks below the previous best bid.
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(fake_clock, best_bid=0.57)
    om.check_inventory_exits()

    assert om.inventory_exits[TOKEN_A]["state"] == "EMERGENCY_EXIT"
    assert first_sell in clob.cancelled
    assert om.inventory_exits[TOKEN_A]["sell_order_status"] == "CANCEL_PENDING"
    assert om.inventory_exits[TOKEN_A]["sell_order_id"] == first_sell
    # 取消传播期间不得提交覆盖相同库存的新 SELL
    assert len([c for c in clob.post_order_calls if c["order"].side == "SELL"]) == 1

    om.check_inventory_exits()
    assert om.inventory_exits[TOKEN_A]["sell_order_status"] in (
        "PENDING_CONFIRMATION",
        "LIVE",
    )
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.57


def test_spread_widening_triggers_emergency(monkeypatch, fake_clock):
    from config import Config

    monkeypatch.setenv("SPREAD_RANGE_MAX", "0.05")
    monkeypatch.setattr("order_manager.config", Config())
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.60, best_ask=0.61)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert _sell_record(om)["purpose"] == "FAST_EXIT"

    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(fake_clock, best_bid=0.60, best_ask=0.68)
    om.check_inventory_exits()
    assert om.inventory_exits[TOKEN_A]["state"] == "EMERGENCY_EXIT"
    assert om.inventory_exits[TOKEN_A]["sell_order_status"] == "CANCEL_PENDING"
    om.check_inventory_exits()
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"


def test_position_api_delay_does_not_abandon_exit(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert om.inventory_exits[TOKEN_A]["sell_order_id"] is not None

    # positions API returns nothing / fails; inventory confirmed by fills must survive
    om.api_client.positions_fail = True
    results = om.check_positions_and_hedge()
    assert TOKEN_A in results
    assert om.inventory_exits[TOKEN_A]["state"] != "FLAT"
    assert om.inventory_exits[TOKEN_A]["confirmed_filled_size"] == 100.0


def test_sell_invisible_in_open_orders_no_duplicate(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    sell_id = om.inventory_exits[TOKEN_A]["sell_order_id"]
    assert len(clob.post_order_calls) == 1

    # Open orders propagation delay: order disappears without trades.
    clob.remove_open_order(sell_id)
    om.check_inventory_exits()
    om.check_inventory_exits()
    assert len(clob.post_order_calls) == 1
    assert om.inventory_exits[TOKEN_A]["sell_order_id"] == sell_id


def test_emergency_partial_fill_then_remaining_relisted(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.56)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    sell_id = om.inventory_exits[TOKEN_A]["sell_order_id"]
    clob.fill_order(sell_id, 40.0)
    # Cancel the remaining part to simulate a partial fill + cancel.
    assert om.cancel_order(sell_id)

    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["sold_size"] == 40.0
    assert state["remaining_size"] == 60.0
    # Remaining inventory must be re-listed immediately (still emergency).
    assert state["sell_order_id"] is not None
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].size == 60.0


def test_never_sells_more_than_confirmed_inventory(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.56)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 60.0)
    om.check_inventory_exits()
    total_sell_size = sum(call["order"].size for call in clob.post_order_calls if call["order"].side == "SELL")
    assert total_sell_size <= 60.0
    assert om.inventory_exits[TOKEN_A]["remaining_size"] >= 0.0


def test_same_fill_not_double_processed(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0) is True
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0) is False
    assert om.inventory_exits[TOKEN_A]["confirmed_filled_size"] == 100.0
    assert len([c for c in clob.post_order_calls if c["order"].side == "SELL"]) == 1


def test_dust_fill_is_flat_with_cooldown(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 0.05)
    assert om.inventory_exits[TOKEN_A]["state"] == "FLAT"
    assert len([c for c in clob.post_order_calls if c["order"].side == "SELL"]) == 0
    assert om.has_inventory_or_pending_exit(TOKEN_A) is True
    fake_clock.advance(31.0)
    assert om.has_inventory_or_pending_exit(TOKEN_A) is False


def test_has_inventory_or_pending_exit_blocks_while_exiting(fake_clock):
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock, best_bid=0.58)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert om.has_inventory_or_pending_exit(TOKEN_A) is True


def test_other_token_not_blocked(fake_clock):
    book_b = _book(fake_clock, token_id=TOKEN_B, best_bid=0.40, best_ask=0.42)
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock), TOKEN_B: book_b})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert om.has_inventory_or_pending_exit(TOKEN_B) is False
