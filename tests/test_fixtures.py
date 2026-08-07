"""Phase 0: fixture completeness checks for the deterministic test baseline."""
from __future__ import annotations

from py_clob_client_v2.clob_types import OrderArgs

from tests.fakes import FakeClobClient
from tests.fixtures import (
    TOKEN_A,
    ask_levels,
    bid_levels,
    crossed_book,
    empty_book,
    invalid_price_book,
    invalid_size_book,
    one_sided_book,
    same_price_multiple_levels,
    shuffled,
)


def test_orderbook_scenario_fixtures_exist():
    assert bid_levels()
    assert ask_levels()
    assert crossed_book()["bids"]
    assert one_sided_book(side="bids")["bids"] and not one_sided_book(side="bids")["asks"]
    assert not empty_book()["bids"] and not empty_book()["asks"]
    assert invalid_price_book()["bids"]
    assert invalid_size_book()["bids"]
    assert same_price_multiple_levels()["bids"]


def test_shuffle_is_deterministic():
    a = shuffled(bid_levels(), seed=7)
    b = shuffled(bid_levels(), seed=7)
    assert a == b
    assert set(p["price"] for p in a) == set(p["price"] for p in b)


def test_fake_clob_fill_scenarios():
    clob = FakeClobClient()
    resp = clob.post_order(
        OrderArgs(token_id=TOKEN_A, price=0.50, size=100.0, side="BUY")
    )
    order_id = resp["id"]
    assert clob.get_open_orders()[0]["remaining"] == 100.0

    # Partial fill
    clob.fill_order(order_id, 30.0)
    open_order = clob.get_open_orders()[0]
    assert open_order["filled"] == 30.0
    assert open_order["remaining"] == 70.0

    # Full fill removes from open orders via simulated fill
    clob.fill_order(order_id, 70.0)
    assert clob.get_open_orders() == []
    assert len(clob.trades) == 2

    # SELL partial/full uses same mechanics
    resp2 = clob.post_order(
        OrderArgs(token_id=TOKEN_A, price=0.50, size=50.0, side="SELL")
    )
    clob.fill_order(resp2["id"], 20.0)
    assert clob.get_open_orders()[0]["filled"] == 20.0
