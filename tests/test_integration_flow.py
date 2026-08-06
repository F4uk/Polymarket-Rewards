"""Integration: BUY -> fill -> inventory -> SELL -> confirm -> flat -> cooldown -> reentry."""
from __future__ import annotations

from tests.conftest import make_order_manager
from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import TOKEN_A, market_fixture, orderbook_fixture


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


def _make_om(clock):
    api = FakeAPIClient(
        markets=[_market()],
        orderbooks={TOKEN_A: _book(clock)},
    )
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = _market()
    return om, clob


def test_full_fill_flow_to_reentry(fake_clock):
    om, clob = _make_om(fake_clock)

    # 1. BUY submit
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    assert resp is not None
    order_id = resp["id"]

    # 2. Full fill -> inventory exit -> FAST_EXIT SELL
    clob.fill_order(order_id, 100.0)
    om.check_orders()
    state = om.inventory_exits[TOKEN_A]
    assert state["state"] == "FAST_EXIT"
    sell_id = state["sell_order_id"]
    assert sell_id is not None

    # 3. SELL confirm -> flat + cooldown
    clob.fill_order(sell_id, 100.0)
    om.check_inventory_exits()
    assert om.inventory_exits[TOKEN_A]["state"] == "FLAT"
    assert om.has_inventory_or_pending_exit(TOKEN_A) is True  # cooldown

    # 4. Cooldown blocks reentry
    assert om.maybe_reenter_markets([_market()])[TOKEN_A] is False

    # 5. After cooldown + fresh book -> full entry -> BUY again
    fake_clock.advance(40.0)
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(fake_clock)
    assert om.maybe_reenter_markets([_market()])[TOKEN_A] is True
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["status"] == "PENDING_CONFIRMATION"


def test_partial_fill_flow_reentry_fails_on_bad_book(fake_clock):
    om, clob = _make_om(fake_clock)

    # 1. BUY submit
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]

    # 2. Partial fill -> remaining cancelled -> inventory 30
    clob.fill_order(order_id, 30.0)
    om.check_orders()
    assert order_id in clob.cancelled
    state = om.inventory_exits[TOKEN_A]
    assert state["confirmed_filled_size"] == 30.0
    sell_id = state["sell_order_id"]

    # 3. SELL fills -> flat
    clob.fill_order(sell_id, 30.0)
    om.check_inventory_exits()
    assert om.inventory_exits[TOKEN_A]["state"] == "FLAT"

    # 4. Cooldown ends but entry gate fails (stale book) -> no BUY
    fake_clock.advance(40.0)
    stale = _book(fake_clock)
    stale["_received_at"] = fake_clock.monotonic() - 10.0
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = stale
    assert om.maybe_reenter_markets([_market()])[TOKEN_A] is False
    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})

    # 5. Fresh book -> reentry passes
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = _book(fake_clock)
    assert om.maybe_reenter_markets([_market()])[TOKEN_A] is True
