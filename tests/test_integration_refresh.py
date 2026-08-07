"""Integration: market rescan -> retained/removed/added -> order behavior."""
from __future__ import annotations

from tests.conftest import make_order_manager, register_fake_orders
from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import (
    TOKEN_A,
    TOKEN_B,
    TOKEN_C,
    TOKEN_D,
    market_fixture,
    orderbook_fixture,
)


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


def _book(clock, token_id, best_bid, best_ask):
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
    m2 = _market(
        "market-2",
        tokens=[
            {"token_id": TOKEN_C, "outcome": "YES"},
            {"token_id": TOKEN_D, "outcome": "NO"},
        ],
    )
    books = {
        TOKEN_A: _book(clock, TOKEN_A, 0.60, 0.62),
        TOKEN_B: _book(clock, TOKEN_B, 0.40, 0.42),
        TOKEN_C: _book(clock, TOKEN_C, 0.40, 0.42),
        TOKEN_D: _book(clock, TOKEN_D, 0.58, 0.60),
    }
    api = FakeAPIClient(markets=[m1, m2], orderbooks=books)
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = m1
    om.market_data_cache["market-2"] = m2
    return om, clob, m1, m2


def test_rescan_retained_removed_added(fake_clock):
    om, clob, m1, m2 = _make_env(fake_clock)
    register_fake_orders(
        om,
        {
            "buy-m1": {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "BUY",
                "price": 0.59,
                "size": 100.0,
                "created_at": fake_clock.monotonic(),
            }
        },
    )

    # Scan 1: m1 retained, m2 added -> only m2 gets new BUY orders
    r1 = om.refresh_market_selection({"market-1"}, {"market-1", "market-2"}, [m1, m2])
    assert r1["retained"] == ["market-1"]
    assert r1["added"] == ["market-2"]
    assert r1["cancelled_buys"] == 0
    assert "buy-m1" not in clob.cancelled
    m2_buys = [
        c
        for c in clob.post_order_calls
        if c["order"].side == "BUY" and c["order"].token_id in (TOKEN_C, TOKEN_D)
    ]
    assert len(m2_buys) >= 1

    # Inventory + SELL appear in m1 before removal
    register_fake_orders(
        om,
        {
            "sell-m1": {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "SELL",
                "price": 0.60,
                "size": 100.0,
                "created_at": fake_clock.monotonic(),
                "purpose": "LIMITED_WAIT_EXIT",
            }
        },
    )
    om.inventory_exits[TOKEN_A] = om._new_inventory_state(
        "market-1", TOKEN_A, 0.60, 100.0
    )

    # Scan 2: m1 removed -> BUY cancelled, SELL + inventory preserved
    r2 = om.refresh_market_selection({"market-1", "market-2"}, {"market-2"}, [m2])
    assert r2["removed"] == ["market-1"]
    assert r2["cancelled_buys"] == 1
    assert "buy-m1" in clob.cancelled
    assert om.active_orders["market-1"][TOKEN_A]["SELL"]["order_id"] == "sell-m1"
    assert om.inventory_exits[TOKEN_A]["state"] != "FLAT"

    # Scan 3: m2 unchanged -> zero cancels
    r3 = om.refresh_market_selection({"market-2"}, {"market-2"}, [m2])
    assert r3["retained"] == ["market-2"]
    assert r3["cancelled_buys"] == 0
