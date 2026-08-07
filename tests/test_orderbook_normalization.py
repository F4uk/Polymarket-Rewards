"""Phase 1: unified orderbook normalization."""
from __future__ import annotations

import copy
import itertools

from market_making_strategy import MarketMakingStrategy, normalize_orderbook
from redis_orderbook_client import RedisOrderbookClient
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


def _book(bids=None, asks=None):
    return {
        "asset_id": TOKEN_A,
        "bids": bids if bids is not None else bid_levels(),
        "asks": asks if asks is not None else ask_levels(),
        "_received_at": 1000.0,
    }


def test_all_ordering_combinations_are_equivalent():
    base = _book()
    expected = normalize_orderbook(base)

    bid_orders = [
        bid_levels(),
        list(reversed(bid_levels())),
        shuffled(bid_levels(), seed=3),
        shuffled(bid_levels(), seed=17),
    ]
    ask_orders = [
        ask_levels(),
        list(reversed(ask_levels())),
        shuffled(ask_levels(), seed=5),
        shuffled(ask_levels(), seed=23),
    ]

    for bids, asks in itertools.product(bid_orders, ask_orders):
        result = normalize_orderbook(_book(bids=bids, asks=asks))
        assert result.normalized_bids == expected.normalized_bids
        assert result.normalized_asks == expected.normalized_asks
        assert result.best_bid == expected.best_bid
        assert result.best_ask == expected.best_ask
        assert result.second_bid == expected.second_bid


def test_same_price_levels_are_aggregated():
    book = same_price_multiple_levels()
    result = normalize_orderbook(book)
    assert result.normalized_bids[0] == (0.60, 100.0)
    assert result.normalized_bids[1] == (0.59, 100.0)
    assert result.normalized_asks[0] == (0.62, 100.0)
    assert result.best_bid == 0.60
    assert result.best_ask == 0.62


def test_second_bid_is_a_distinct_lower_price():
    book = _book(
        bids=[
            {"price": "0.60", "size": "10"},
            {"price": "0.60", "size": "10"},
            {"price": "0.59", "size": "10"},
            {"price": "0.58", "size": "10"},
        ]
    )
    result = normalize_orderbook(book)
    assert result.second_bid == 0.59
    assert result.normalized_bids[0] == (0.60, 20.0)
    assert result.normalized_bids[1] == (0.59, 10.0)


def test_empty_book():
    result = normalize_orderbook(empty_book())
    assert result.is_empty
    assert not result.is_one_sided
    assert not result.is_crossed
    assert result.best_bid is None
    assert result.best_ask is None
    assert result.second_bid is None


def test_one_sided_book():
    bids_only = normalize_orderbook(one_sided_book(side="bids"))
    assert not bids_only.is_empty
    assert bids_only.is_one_sided
    assert bids_only.best_bid == 0.60
    assert bids_only.best_ask is None

    asks_only = normalize_orderbook(one_sided_book(side="asks"))
    assert not asks_only.is_empty
    assert asks_only.is_one_sided
    assert asks_only.best_bid is None
    assert asks_only.best_ask == 0.62


def test_crossed_book():
    result = normalize_orderbook(crossed_book())
    assert result.is_crossed
    assert result.best_bid == 0.65
    assert result.best_ask == 0.62


def test_invalid_prices_are_ignored():
    result = normalize_orderbook(invalid_price_book())
    assert result.invalid_rows >= 1
    assert result.best_bid == 0.60
    assert result.best_ask == 0.62


def test_invalid_sizes_are_ignored():
    result = normalize_orderbook(invalid_size_book())
    assert result.invalid_rows >= 2
    assert result.normalized_bids == [(0.59, 100.0)]
    assert result.normalized_asks == [(0.62, 100.0)]


def test_input_lists_are_not_mutated():
    bids = bid_levels()
    asks = ask_levels()
    book = _book(bids=bids, asks=asks)
    original_bids = copy.deepcopy(bids)
    original_asks = copy.deepcopy(asks)
    normalize_orderbook(book)
    assert bids == original_bids
    assert asks == original_asks


def test_age_grows_with_monotonic_now():
    book = _book()
    book["_received_at"] = 100.0
    assert normalize_orderbook(book, now_monotonic=100.0).age_seconds == 0.0
    assert normalize_orderbook(book, now_monotonic=103.0).age_seconds == 3.0
    assert normalize_orderbook(book, now_monotonic=98.0).age_seconds == 0.0


def test_age_none_without_received_at():
    book = _book()
    book.pop("_received_at")
    result = normalize_orderbook(book, now_monotonic=123.0)
    assert result.received_at is None
    assert result.age_seconds is None


def test_cache_read_does_not_refresh_received_at(temp_db_path):
    client = RedisOrderbookClient(db_path=temp_db_path, orderbook_ttl=3600)
    try:
        book = _book()
        book["_received_at"] = 500.0
        assert client.set_orderbook(TOKEN_A, book)
        cached = client.get_orderbook(TOKEN_A)
        assert cached["_received_at"] == 500.0
        # Even after many reads the received time stays the original value.
        for _ in range(3):
            cached = client.get_orderbook(TOKEN_A)
            assert cached["_received_at"] == 500.0
        result = normalize_orderbook(cached, now_monotonic=501.0)
        assert result.age_seconds == 1.0
    finally:
        client.close()


def test_different_tick_sizes_parse_cleanly():
    for tick in ("0.1", "0.01", "0.001", "0.0001"):
        book = _book()
        book["tick_size"] = tick
        result = normalize_orderbook(book)
        assert result.best_bid == 0.60
        assert result.best_ask == 0.62


def test_strategy_trading_decisions_are_order_independent():
    strategy = MarketMakingStrategy()
    base = _book()
    shuffled_book = _book(
        bids=shuffled(bid_levels(), seed=11),
        asks=shuffled(ask_levels(), seed=13),
    )
    prices = strategy.calculate_order_prices(base, 3.0)
    assert prices is not None
    assert strategy.calculate_mid_price(base) == strategy.calculate_mid_price(shuffled_book)
    assert strategy.calculate_actual_buy_price(base, prices["buy_price"]) == (
        strategy.calculate_actual_buy_price(shuffled_book, prices["buy_price"])
    )
    base_safe, base_info = strategy.can_place_buy_order_safely(
        base, prices["buy_price"], prices["sell_price"], 100.0, 0.59
    )
    shuffled_safe, shuffled_info = strategy.can_place_buy_order_safely(
        shuffled_book, prices["buy_price"], prices["sell_price"], 100.0, 0.59
    )
    assert base_safe == shuffled_safe
    assert base_info["best_bid"] == shuffled_info["best_bid"]
    assert base_info["second_bid"] == shuffled_info["second_bid"]
