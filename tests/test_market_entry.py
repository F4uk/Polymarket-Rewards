"""Phase 3: strict market entry (worst-side spread, freshness, exit liquidity)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from market_manager import MarketManager
from market_making_strategy import (
    ENTRY_ACCEPTED,
    ENTRY_CROSSED_BOOK,
    ENTRY_EXIT_VWAP_TOO_LOSSY,
    ENTRY_INSUFFICIENT_EXIT_DEPTH,
    ENTRY_NO_SECOND_BID,
    ENTRY_ONE_SIDED_BOOK,
    ENTRY_PRICE_CLIFF,
    ENTRY_SPREAD_TOO_WIDE,
    ENTRY_STALE_BOOK,
    MarketMakingStrategy,
)
from tests.fakes import FakeAPIClient, FakeClock
from tests.fixtures import (
    TOKEN_A,
    TOKEN_B,
    market_fixture,
    orderbook_fixture,
)


def _override(monkeypatch, **values):
    defaults = dict(
        max_orderbook_age_seconds=3.0,
        spread_range={"min": None, "max": 0.05},
        min_exit_depth_multiplier=1.2,
        price_cliff_threshold=0.05,
        min_protection_size_multiplier=1.0,
        exit_immediate_max_loss_bps=300.0,
        reward_boundary_inset_ticks=0,
        order_size_multiplier=2.0,
    )
    defaults.update(values)
    monkeypatch.setattr(
        "market_making_strategy.config",
        SimpleNamespace(**defaults),
    )


def _fresh_book(clock: FakeClock, token_id: str, **kwargs):
    book = orderbook_fixture(token_id, **kwargs)
    book["_received_at"] = clock.monotonic()
    return book


def _market(**kwargs):
    return market_fixture(**kwargs)


def test_accepted_market(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
        {"price": "0.58", "size": "200"},
        {"price": "0.57", "size": "200"},
    ], asks=[
        {"price": "0.62", "size": "200"},
        {"price": "0.63", "size": "200"},
    ])
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
        {"price": "0.38", "size": "200"},
        {"price": "0.37", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
        {"price": "0.43", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert decision.accepted
    assert decision.reason == ENTRY_ACCEPTED


def test_worst_side_spread_rejects_market(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    good = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
        {"price": "0.58", "size": "200"},
        {"price": "0.57", "size": "200"},
    ], asks=[
        {"price": "0.61", "size": "200"},
    ])
    wide = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
    ], asks=[
        {"price": "0.49", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: good, TOKEN_B: wide},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_SPREAD_TOO_WIDE
    assert decision.token_id == TOKEN_B


def test_average_ok_but_worst_fails(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    tight = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
        {"price": "0.58", "size": "200"},
        {"price": "0.57", "size": "200"},
    ], asks=[
        {"price": "0.61", "size": "200"},
    ])
    wide = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
    ], asks=[
        {"price": "0.49", "size": "200"},
    ])
    # average = (0.01 + 0.09)/2 = 0.05 <= max; worst = 0.09 > max
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: tight, TOKEN_B: wide},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_SPREAD_TOO_WIDE


def test_exit_depth_exactly_enough(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "100"},
        {"price": "0.59", "size": "60"},
        {"price": "0.58", "size": "60"},
        {"price": "0.57", "size": "60"},
    ], asks=[
        {"price": "0.62", "size": "200"},
    ])
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
        {"price": "0.38", "size": "200"},
        {"price": "0.37", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert decision.accepted
    assert decision.details["tokens"][TOKEN_A]["details"]["exit_simulation"]["cumulative_size"] >= 120.0


def test_exit_depth_slightly_insufficient(monkeypatch, fake_clock):
    _override(monkeypatch, min_protection_size_multiplier=0.8)
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "80"},
        {"price": "0.59", "size": "20"},
        {"price": "0.58", "size": "5"},
        {"price": "0.57", "size": "5"},
    ], asks=[
        {"price": "0.62", "size": "200"},
    ])
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
        {"price": "0.38", "size": "200"},
        {"price": "0.37", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_INSUFFICIENT_EXIT_DEPTH


def test_exit_vwap_too_lossy(monkeypatch, fake_clock):
    _override(
        monkeypatch,
        exit_immediate_max_loss_bps=1.0,
        min_exit_depth_multiplier=4.0,
    )
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "100"},
        {"price": "0.59", "size": "1"},
        {"price": "0.57", "size": "300"},
        {"price": "0.56", "size": "300"},
    ], asks=[
        {"price": "0.62", "size": "200"},
    ])
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
        {"price": "0.38", "size": "200"},
        {"price": "0.37", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_EXIT_VWAP_TOO_LOSSY


def test_price_cliff_rejects(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
        {"price": "0.50", "size": "200"},
        {"price": "0.49", "size": "200"},
    ], asks=[
        {"price": "0.62", "size": "200"},
    ])
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
        {"price": "0.38", "size": "200"},
        {"price": "0.37", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_PRICE_CLIFF


def test_stale_book_rejects(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A)
    book_a["_received_at"] = fake_clock.monotonic() - 10.0
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_STALE_BOOK


def test_one_sided_book_rejects(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "200"},
    ], asks=[])
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_ONE_SIDED_BOOK


def test_crossed_book_rejects(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.65", "size": "200"},
    ], asks=[
        {"price": "0.62", "size": "200"},
    ])
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_CROSSED_BOOK


def test_no_second_bid_rejects(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "200"},
    ], asks=[
        {"price": "0.62", "size": "200"},
    ])
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    decision = strategy.evaluate_market_entry(
        _market(),
        {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100,
        now_monotonic=fake_clock.monotonic(),
    )
    assert not decision.accepted
    assert decision.reason == ENTRY_NO_SECOND_BID


def test_entry_is_order_independent(monkeypatch, fake_clock):
    _override(monkeypatch)
    strategy = MarketMakingStrategy()
    from tests.fixtures import shuffled

    base_bids = [
        {"price": "0.60", "size": "100"},
        {"price": "0.59", "size": "100"},
        {"price": "0.58", "size": "100"},
        {"price": "0.57", "size": "100"},
    ]
    base_asks = [
        {"price": "0.62", "size": "100"},
        {"price": "0.63", "size": "100"},
    ]
    book_a = _fresh_book(fake_clock, TOKEN_A, bids=base_bids, asks=base_asks)
    shuffled_a = _fresh_book(
        fake_clock,
        TOKEN_A,
        bids=shuffled(base_bids, seed=9),
        asks=shuffled(base_asks, seed=19),
    )
    book_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    d1 = strategy.evaluate_market_entry(
        _market(), {TOKEN_A: book_a, TOKEN_B: book_b},
        order_size=100, now_monotonic=fake_clock.monotonic(),
    )
    d2 = strategy.evaluate_market_entry(
        _market(), {TOKEN_A: shuffled_a, TOKEN_B: book_b},
        order_size=100, now_monotonic=fake_clock.monotonic(),
    )
    assert d1.accepted == d2.accepted
    assert d1.reason == d2.reason


def test_market_manager_uses_worst_spread(monkeypatch, fake_clock):
    _override(monkeypatch)
    good = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
        {"price": "0.58", "size": "200"},
        {"price": "0.57", "size": "200"},
    ], asks=[
        {"price": "0.61", "size": "200"},
    ])
    wide = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
        {"price": "0.38", "size": "200"},
        {"price": "0.37", "size": "200"},
    ], asks=[
        {"price": "0.49", "size": "200"},
    ])
    api = FakeAPIClient(
        markets=[_market()],
        orderbooks={TOKEN_A: good, TOKEN_B: wide},
    )
    mm = MarketManager(api)
    mm._monotonic = fake_clock.monotonic
    selected = mm.filter_markets(
        markets=[_market()],
        min_reward_ratio=0.0,
        max_markets=10,
        spread_range={"min": None, "max": 0.05},
        volume_24hr_range={"min": 0.0, "max": None},
        rewards_min_size_range={"min": None, "max": 1000},
    )
    assert selected == []


def test_place_market_orders_skips_without_fresh_book(fake_clock):
    from tests.conftest import make_order_manager

    api = FakeAPIClient(
        markets=[_market()],
        orderbooks={},
    )
    om = make_order_manager(api_client=api, clock=fake_clock)
    results = om.place_market_orders(_market(), {})
    assert results
    assert all(v is False for v in results.values())
    assert om.get_active_orders() == {}


def test_place_market_orders_skips_stale_book(monkeypatch, fake_clock):
    _override(monkeypatch)
    from tests.conftest import make_order_manager
    from tests.fakes import FakeClobClient

    stale_a = _fresh_book(fake_clock, TOKEN_A)
    stale_a["_received_at"] = fake_clock.monotonic() - 10.0
    fresh_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
    ])
    api = FakeAPIClient(
        markets=[_market()],
        orderbooks={TOKEN_A: stale_a, TOKEN_B: fresh_b},
    )
    clob = FakeClobClient(clock=fake_clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=fake_clock)
    om.market_data_cache["market-1"] = _market()
    results = om.place_market_orders(_market(), {})
    assert results.get(TOKEN_A) is False
    assert om.get_active_orders() == {}
    assert clob.post_order_calls == []


def test_full_market_preflight_rejects_before_first_post(monkeypatch, fake_clock):
    _override(monkeypatch)
    from tests.conftest import make_order_manager
    from tests.fakes import FakeClobClient

    safe_a = _fresh_book(fake_clock, TOKEN_A, bids=[
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
        {"price": "0.58", "size": "200"},
        {"price": "0.57", "size": "200"},
    ], asks=[
        {"price": "0.62", "size": "200"},
        {"price": "0.63", "size": "200"},
    ])
    stale_b = _fresh_book(fake_clock, TOKEN_B, bids=[
        {"price": "0.40", "size": "200"},
        {"price": "0.39", "size": "200"},
        {"price": "0.38", "size": "200"},
        {"price": "0.37", "size": "200"},
    ], asks=[
        {"price": "0.42", "size": "200"},
        {"price": "0.43", "size": "200"},
    ])
    stale_b["_received_at"] = fake_clock.monotonic() - 10.0
    api = FakeAPIClient(
        markets=[_market()],
        orderbooks={TOKEN_A: safe_a, TOKEN_B: stale_b},
    )
    clob = FakeClobClient(clock=fake_clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=fake_clock)

    results = om.place_market_orders(_market(), {})

    assert results[TOKEN_A] is False
    assert results[TOKEN_B] is False
    assert clob.post_order_calls == []
    assert om.get_active_orders() == {}


def test_no_conservative_price_parameter():
    import inspect
    from market_making_strategy import MarketMakingStrategy

    signature = inspect.signature(MarketMakingStrategy.calculate_order_prices)
    assert "use_conservative_price" not in signature.parameters
