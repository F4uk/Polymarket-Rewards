"""Phase 7: set-diff market refresh (retained / removed / added)."""
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


def _book(clock, token_id, best_bid=0.60, best_ask=0.62, fresh=True):
    bids = [
        {"price": f"{best_bid - i * 0.01:.2f}", "size": "200"}
        for i in range(4)
    ]
    asks = [
        {"price": f"{best_ask + i * 0.01:.2f}", "size": "200"}
        for i in range(2)
    ]
    book = orderbook_fixture(token_id, bids=bids, asks=asks)
    book["_received_at"] = (
        clock.monotonic() if fresh else clock.monotonic() - 100.0
    )
    return book


def _m2(clock, fresh=True):
    return _market(
        "market-2",
        tokens=[
            {"token_id": TOKEN_C, "outcome": "YES"},
            {"token_id": TOKEN_D, "outcome": "NO"},
        ],
    ), {
        TOKEN_C: _book(clock, TOKEN_C, best_bid=0.40, best_ask=0.42, fresh=fresh),
        TOKEN_D: _book(clock, TOKEN_D, best_bid=0.58, best_ask=0.60, fresh=fresh),
    }


def _make_om(clock, books=None):
    m1 = _market()
    api = FakeAPIClient(
        markets=[m1],
        orderbooks=books if books is not None else {},
    )
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = m1
    return om, clob


def test_unchanged_set_zero_cancels(fake_clock):
    om, clob = _make_om(fake_clock)
    register_fake_orders(
        om,
        {
            "buy-1": {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "BUY",
                "price": 0.59,
                "size": 100.0,
                "created_at": fake_clock.monotonic(),
            }
        },
    )
    result = om.refresh_market_selection({"market-1"}, {"market-1"}, [_market()])
    assert result["retained"] == ["market-1"]
    assert result["removed"] == []
    assert result["added"] == []
    assert result["cancelled_buys"] == 0
    assert clob.cancelled == []
    assert "BUY" in om.active_orders["market-1"][TOKEN_A]


def test_added_market_only_processed(fake_clock):
    m2, books2 = _m2(fake_clock)
    om, clob = _make_om(fake_clock, books=books2)
    register_fake_orders(
        om,
        {
            "buy-1": {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "BUY",
                "price": 0.59,
                "size": 100.0,
                "created_at": fake_clock.monotonic(),
            }
        },
    )
    result = om.refresh_market_selection({"market-1"}, {"market-1", "market-2"}, [m2])
    assert result["added"] == ["market-2"]
    assert result["cancelled_buys"] == 0
    assert clob.cancelled == []
    # m2 tokens got BUY orders
    m2_buys = [
        c
        for c in clob.post_order_calls
        if c["order"].side == "BUY" and c["order"].token_id in (TOKEN_C, TOKEN_D)
    ]
    assert len(m2_buys) >= 1


def test_removed_market_cancels_only_its_buy(fake_clock):
    om, clob = _make_om(fake_clock)
    register_fake_orders(
        om,
        {
            "buy-1": {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "BUY",
                "price": 0.59,
                "size": 100.0,
                "created_at": fake_clock.monotonic(),
            },
            "buy-2": {
                "market_id": "market-2",
                "token_id": TOKEN_C,
                "side": "BUY",
                "price": 0.39,
                "size": 100.0,
                "created_at": fake_clock.monotonic(),
            },
        },
    )
    om.market_data_cache["market-2"] = _market("market-2")
    result = om.refresh_market_selection({"market-1", "market-2"}, {"market-2"}, [_market("market-2")])
    assert result["removed"] == ["market-1"]
    assert "buy-1" in clob.cancelled
    assert "buy-2" not in clob.cancelled
    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})
    assert "BUY" in om.active_orders["market-2"][TOKEN_C]


def test_removed_market_keeps_sell_and_inventory(fake_clock):
    om, clob = _make_om(fake_clock)
    register_fake_orders(
        om,
        {
            "buy-1": {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "BUY",
                "price": 0.59,
                "size": 100.0,
                "created_at": fake_clock.monotonic(),
            },
            "sell-1": {
                "market_id": "market-1",
                "token_id": TOKEN_A,
                "side": "SELL",
                "price": 0.60,
                "size": 50.0,
                "created_at": fake_clock.monotonic(),
                "purpose": "LIMITED_WAIT_EXIT",
            },
        },
    )
    om.inventory_exits[TOKEN_A] = om._new_inventory_state(
        "market-1", TOKEN_A, 0.60, 100.0
    )
    result = om.refresh_market_selection({"market-1"}, set(), [])
    assert result["removed"] == ["market-1"]
    assert "SELL" in om.active_orders.get("market-1", {}).get(TOKEN_A, {})
    assert om.inventory_exits[TOKEN_A]["state"] != "FLAT"


def test_pending_reorder_cleared_on_removed_market(fake_clock):
    om, clob = _make_om(fake_clock)
    om.pending_reorder_tokens[TOKEN_A] = {
        "market_id": "market-1",
        "side": "BUY",
        "last_attempt_time": fake_clock.monotonic(),
        "target_price": 0.59,
        "order_size": 100,
        "safety_info": {"reason": "test"},
    }
    om.refresh_market_selection({"market-1"}, set(), [])
    assert TOKEN_A not in om.pending_reorder_tokens


def test_readded_market_requires_fresh_entry(fake_clock):
    m2, books2 = _m2(fake_clock, fresh=False)  # stale books
    om, clob = _make_om(fake_clock, books=books2)
    result = om.refresh_market_selection(set(), {"market-2"}, [m2])
    assert result["added"] == ["market-2"]
    m2_buys = [
        c
        for c in clob.post_order_calls
        if c["order"].side == "BUY" and c["order"].token_id in (TOKEN_C, TOKEN_D)
    ]
    assert m2_buys == []


def test_readded_market_with_inventory_blocks_buy(fake_clock):
    m2, books2 = _m2(fake_clock)
    om, clob = _make_om(fake_clock, books=books2)
    om.inventory_exits[TOKEN_C] = om._new_inventory_state(
        "market-2", TOKEN_C, 0.40, 100.0
    )
    om.refresh_market_selection(set(), {"market-2"}, [m2])
    m2_buys = [
        c
        for c in clob.post_order_calls
        if c["order"].side == "BUY" and c["order"].token_id in (TOKEN_C, TOKEN_D)
    ]
    assert len(m2_buys) == 0 or all(c["order"].token_id != TOKEN_C for c in m2_buys)


def test_empty_active_orders_does_not_mean_all_added(fake_clock):
    om, clob = _make_om(fake_clock)
    # No active orders at all, but selection set unchanged -> retained, not added.
    result = om.refresh_market_selection({"market-1"}, {"market-1"}, [_market()])
    assert result["retained"] == ["market-1"]
    assert result["added"] == []
    assert clob.post_order_calls == []


def test_removed_market_inventory_state_not_deleted(fake_clock):
    om, clob = _make_om(fake_clock)
    om.inventory_exits[TOKEN_A] = om._new_inventory_state(
        "market-1", TOKEN_A, 0.60, 100.0
    )
    om.refresh_market_selection({"market-1"}, set(), [])
    assert TOKEN_A in om.inventory_exits
    assert om.inventory_exits[TOKEN_A]["market_id"] == "market-1"
