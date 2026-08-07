"""Phase 4: time-bounded tiered inventory exit (loss-bounded)."""
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


def _sell_calls(clob):
    return [c for c in clob.post_order_calls if c["order"].side == "SELL"]


def _use_runtime_profile(monkeypatch):
    from config import Config

    monkeypatch.setenv("HEDGE_SELL_MAX_BID_GAP", "0.05")
    monkeypatch.setenv("EXIT_IMMEDIATE_MAX_LOSS_TICKS", "1")
    monkeypatch.setenv("EXIT_IMMEDIATE_MAX_LOSS_BPS", "300")
    monkeypatch.setenv("EXIT_PASSIVE_WAIT_SECONDS", "10")
    monkeypatch.setenv("EXIT_EMERGENCY_LOSS_TICKS", "3")
    monkeypatch.setenv("EXIT_EMERGENCY_LOSS_BPS", "800")
    monkeypatch.setenv("EXIT_MAX_HOLD_SECONDS", "30")
    monkeypatch.setenv("SPREAD_RANGE_MAX", "0.05")
    monkeypatch.setattr("order_manager.config", Config())


def test_fast_exit_zero_loss(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
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


def test_fast_exit_one_tick_small_loss_044_043(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.43, best_ask=0.45)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.44, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "FAST_EXIT"
    assert _sell_record(om)["purpose"] == "FAST_EXIT"
    sell = clob.post_order_calls[-1]["order"]
    assert sell.price == 0.43
    assert sell.size == 100.0


def test_two_tick_limited_wait_then_bounded_emergency_044_042(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.42, best_ask=0.44)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.44, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "LIMITED_WAIT"
    passive = _sell_record(om)
    assert passive["purpose"] == "LIMITED_WAIT_EXIT"
    assert passive["price"] == 0.44
    passive_id = passive["order_id"]

    fake_clock.advance(10.0)  # EXIT_PASSIVE_WAIT_SECONDS
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(
        fake_clock, best_bid=0.42, best_ask=0.44
    )
    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert state["sell_order_status"] == "CANCEL_PENDING"
    assert state["sell_order_id"] == passive_id
    assert passive_id in clob.cancelled
    assert len(_sell_calls(clob)) == 1

    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["sell_order_status"] in ("PENDING_CONFIRMATION", "LIVE")
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.42


def test_three_tick_bounded_emergency_044_041(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.41, best_ask=0.43)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.44, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.41


def test_profitable_best_bid_uses_best_bid(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.50, best_ask=0.52)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.44, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "FAST_EXIT"
    assert _sell_record(om)["purpose"] == "FAST_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.50


def test_four_tick_extreme_loss_044_040_protected_no_churn(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.40, best_ask=0.42)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.44, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    sell = clob.post_order_calls[-1]["order"]
    assert sell.price == 0.44
    assert sell.price != 0.40
    sell_id = state["sell_order_id"]

    om.check_inventory_exits()
    assert state["sell_order_id"] == sell_id
    assert sell_id not in clob.cancelled
    assert len(_sell_calls(clob)) == 1


def test_real_regression_044_033_protected_no_unlimited_dump(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.33, best_ask=0.35)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.44, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    sell = clob.post_order_calls[-1]["order"]
    assert sell.price == 0.44
    assert sell.price != 0.33
    sell_id = state["sell_order_id"]

    om.check_inventory_exits()
    assert state["sell_order_id"] == sell_id
    assert sell_id not in clob.cancelled
    assert len(_sell_calls(clob)) == 1


def test_low_price_005_004_bps_protection_with_gap_005(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.04, best_ask=0.06)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.05, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    sell = clob.post_order_calls[-1]["order"]
    assert sell.price == 0.05
    assert sell.price != 0.04


def test_passive_wait_timeout_bounded_emergency_060_058(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.58, best_ask=0.60)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "LIMITED_WAIT"
    passive = _sell_record(om)
    assert passive["purpose"] == "LIMITED_WAIT_EXIT"
    assert passive["price"] == 0.60
    passive_id = passive["order_id"]

    fake_clock.advance(10.0)
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(
        fake_clock, best_bid=0.58, best_ask=0.60
    )
    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert state["sell_order_status"] == "CANCEL_PENDING"
    assert passive_id in clob.cancelled

    om.check_inventory_exits()
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.58


def test_extreme_loss_timeout_060_050_protected_no_cross(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.58, best_ask=0.60)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)

    state = om.inventory_exits[TOKEN_A]
    passive_id = state["sell_order_id"]
    assert state["state"] == "LIMITED_WAIT"
    assert _sell_record(om)["price"] == 0.60

    fake_clock.advance(10.0)
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(
        fake_clock, best_bid=0.50, best_ask=0.52
    )
    om.check_inventory_exits()

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert state["sell_order_id"] == passive_id
    assert passive_id not in clob.cancelled
    assert len(_sell_calls(clob)) == 1
    assert all(c["order"].price != 0.50 for c in _sell_calls(clob))
    assert all(c["order"].price >= 0.60 for c in _sell_calls(clob))


def test_force_does_not_bypass_extreme_loss_protection(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.33, best_ask=0.35)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.44, 100.0)
    sell_id = om.inventory_exits[TOKEN_A]["sell_order_id"]

    om._process_inventory_exit(TOKEN_A, force=True)

    assert om.inventory_exits[TOKEN_A]["state"] == "EMERGENCY_EXIT"
    assert om.inventory_exits[TOKEN_A]["sell_order_id"] == sell_id
    assert sell_id not in clob.cancelled
    assert len(_sell_calls(clob)) == 1
    assert clob.post_order_calls[-1]["order"].price == 0.44


def test_bid_depth_drop_bounded_emergency_reprices(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.60, best_ask=0.62)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    first_sell = _sell_record(om)["order_id"]

    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(
        fake_clock, best_bid=0.57, best_ask=0.59
    )
    om.check_inventory_exits()

    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert state["sell_order_status"] == "CANCEL_PENDING"
    assert state["sell_order_id"] == first_sell
    assert first_sell in clob.cancelled
    assert len(_sell_calls(clob)) == 1

    om.check_inventory_exits()
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.57


def test_spread_widening_bounded_emergency_reprices(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.58, best_ask=0.60)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert _sell_record(om)["purpose"] == "LIMITED_WAIT_EXIT"

    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(
        fake_clock, best_bid=0.58, best_ask=0.68
    )
    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "EMERGENCY_EXIT"
    assert state["sell_order_status"] == "CANCEL_PENDING"

    om.check_inventory_exits()
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].price == 0.58


def test_position_api_delay_does_not_abandon_exit(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert om.inventory_exits[TOKEN_A]["sell_order_id"] is not None

    om.api_client.positions_fail = True
    results = om.check_positions_and_hedge()
    assert TOKEN_A in results
    assert om.inventory_exits[TOKEN_A]["state"] != "FLAT"
    assert om.inventory_exits[TOKEN_A]["confirmed_filled_size"] == 100.0


def test_sell_invisible_in_open_orders_no_duplicate(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    sell_id = om.inventory_exits[TOKEN_A]["sell_order_id"]
    assert len(clob.post_order_calls) == 1

    clob.remove_open_order(sell_id)
    om.check_inventory_exits()
    om.check_inventory_exits()
    assert len(clob.post_order_calls) == 1
    assert om.inventory_exits[TOKEN_A]["sell_order_id"] == sell_id


def test_emergency_partial_fill_then_remaining_relisted(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.57, best_ask=0.59)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    sell_id = om.inventory_exits[TOKEN_A]["sell_order_id"]
    clob.fill_order(sell_id, 40.0)
    assert om.cancel_order(sell_id)

    om.check_inventory_exits()
    state = om.inventory_exits[TOKEN_A]
    assert state["sold_size"] == 40.0
    assert state["remaining_size"] == 60.0
    assert state["sell_order_id"] is not None
    assert _sell_record(om)["purpose"] == "EMERGENCY_EXIT"
    assert clob.post_order_calls[-1]["order"].size == 60.0


def test_never_sells_more_than_confirmed_inventory(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.57, best_ask=0.59)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 60.0)
    om.check_inventory_exits()
    total_sell_size = sum(call["order"].size for call in _sell_calls(clob))
    assert total_sell_size <= 60.0
    assert om.inventory_exits[TOKEN_A]["remaining_size"] >= 0.0


def test_same_fill_not_double_processed(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0) is True
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0) is False
    assert om.inventory_exits[TOKEN_A]["confirmed_filled_size"] == 100.0
    assert len(_sell_calls(clob)) == 1


def test_dust_fill_is_flat_with_cooldown(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(fake_clock, {TOKEN_A: _book(fake_clock)})
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 0.05)
    assert om.inventory_exits[TOKEN_A]["state"] == "FLAT"
    assert len(_sell_calls(clob)) == 0
    assert om.has_inventory_or_pending_exit(TOKEN_A) is True
    fake_clock.advance(31.0)
    assert om.has_inventory_or_pending_exit(TOKEN_A) is False


def test_has_inventory_or_pending_exit_blocks_while_exiting(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock, best_bid=0.58, best_ask=0.60)},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert om.has_inventory_or_pending_exit(TOKEN_A) is True


def test_other_token_not_blocked(monkeypatch, fake_clock):
    _use_runtime_profile(monkeypatch)
    book_b = _book(fake_clock, token_id=TOKEN_B, best_bid=0.40, best_ask=0.42)
    om, clob = _make_om(
        fake_clock,
        {TOKEN_A: _book(fake_clock), TOKEN_B: book_b},
    )
    assert om._handle_buy_fill("market-1", TOKEN_A, 0.60, 100.0)
    assert om.has_inventory_or_pending_exit(TOKEN_B) is False
