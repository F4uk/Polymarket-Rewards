"""Phase 6: requote hysteresis and preflight checks."""
from __future__ import annotations

from tests.conftest import make_order_manager
from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import TOKEN_A, market_fixture, orderbook_fixture


def _market(**kwargs):
    kwargs.setdefault("orderPriceMinTickSize", 0.01)
    return market_fixture(**kwargs)


def _book(clock, bids=None, asks=None):
    book = orderbook_fixture(
        TOKEN_A,
        bids=bids
        if bids is not None
        else [
            {"price": "0.60", "size": "200"},
            {"price": "0.59", "size": "200"},
            {"price": "0.58", "size": "200"},
            {"price": "0.57", "size": "200"},
        ],
        asks=asks
        if asks is not None
        else [
            {"price": "0.62", "size": "200"},
            {"price": "0.63", "size": "200"},
        ],
    )
    book["_received_at"] = clock.monotonic()
    return book


def _make_om(clock, book, current_price=0.58, created_at=None, size=100.0):
    api = FakeAPIClient(
        markets=[_market()],
        orderbooks={TOKEN_A: book},
    )
    clob = FakeClobClient(clock=clock)
    om = make_order_manager(api_client=api, clob_client=clob, clock=clock)
    om.market_data_cache["market-1"] = _market()
    om.active_orders.setdefault("market-1", {}).setdefault(TOKEN_A, {})["BUY"] = {
        "order_id": "buy-1",
        "token_id": TOKEN_A,
        "side": "BUY",
        "price": current_price,
        "size": size,
        "exposure": current_price * size,
        # 默认让订单足够“老”，以便普通重新报价门槛只由测试意图控制
        "created_at": created_at if created_at is not None else clock.monotonic() - 60.0,
        "submitted_at": clock.monotonic(),
        "status": "LIVE",
        "purpose": "REWARD_BUY",
        "generation": 0,
    }
    return om, clob


def _adjust(om, markets=None):
    return om.adjust_orders_to_reward_boundaries(
        markets if markets is not None else [_market()]
    )


def test_stable_book_no_cancel(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.59)
    _adjust(om)
    assert clob.cancelled == []
    assert "BUY" in om.active_orders["market-1"][TOKEN_A]


def test_one_tick_blip_no_cancel(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.58)
    # target actual = 0.59, diff = 1 tick < REQUOTE_MIN_TICKS
    _adjust(om)
    assert clob.cancelled == []


def test_threshold_met_once_no_cancel(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.57)
    _adjust(om)
    assert clob.cancelled == []
    assert om.requote_confirmations[TOKEN_A]["count"] == 1


def test_consecutive_confirmations_then_adjust(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.57)
    _adjust(om)
    _adjust(om)
    assert clob.cancelled == ["buy-1"]
    assert om.metrics["requotes"] == 1
    new_buy = om.active_orders["market-1"][TOKEN_A]["BUY"]
    assert new_buy["price"] == 0.59


def test_target_change_resets_confirmation_count(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.57)
    _adjust(om)  # count=1 toward target 0.59
    # Book moves so the target changes to 0.61; the previous confirmation must reset.
    shifted = _book(fake_clock, bids=[
        {"price": "0.62", "size": "200"},
        {"price": "0.61", "size": "200"},
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
    ], asks=[
        {"price": "0.64", "size": "200"},
        {"price": "0.65", "size": "200"},
    ])
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = shifted
    _adjust(om)
    assert om.requote_confirmations[TOKEN_A]["target"] == 0.61
    assert om.requote_confirmations[TOKEN_A]["count"] == 1
    assert clob.cancelled == []


def test_requote_cooldown(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.57)
    _adjust(om)
    _adjust(om)
    assert clob.cancelled == ["buy-1"]
    # Immediately ask for another move; cooldown must block it.
    om.active_orders["market-1"][TOKEN_A]["BUY"]["created_at"] = fake_clock.monotonic()
    moved = _book(fake_clock, bids=[
        {"price": "0.62", "size": "200"},
        {"price": "0.61", "size": "200"},
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
    ], asks=[
        {"price": "0.64", "size": "200"},
        {"price": "0.65", "size": "200"},
    ])
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = moved
    _adjust(om)
    assert len(clob.cancelled) == 1  # no second cancel during cooldown


def test_new_order_lifetime(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.57)
    _adjust(om)
    _adjust(om)
    assert clob.cancelled == ["buy-1"]
    # Replace with a fresh order and try again before MIN_QUOTE_LIFETIME_SECONDS.
    om.active_orders["market-1"][TOKEN_A]["BUY"] = {
        "order_id": "buy-2",
        "token_id": TOKEN_A,
        "side": "BUY",
        "price": 0.59,
        "size": 100.0,
        "exposure": 59.0,
        "created_at": fake_clock.monotonic(),
        "submitted_at": fake_clock.monotonic(),
        "status": "LIVE",
        "purpose": "REWARD_BUY",
        "generation": 0,
    }
    moved = _book(fake_clock, bids=[
        {"price": "0.62", "size": "200"},
        {"price": "0.61", "size": "200"},
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
    ], asks=[
        {"price": "0.64", "size": "200"},
        {"price": "0.65", "size": "200"},
    ])
    om.api_client.orderbook_source.orderbooks[TOKEN_A] = moved
    _adjust(om)
    assert "buy-2" not in clob.cancelled


def test_best_bid_danger_immediate_cancel_and_replace(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.60, created_at=fake_clock.monotonic())
    _adjust(om)
    assert clob.cancelled == ["buy-1"]
    assert om.metrics["safety_cancels"] == 1
    assert om.active_orders["market-1"][TOKEN_A]["BUY"]["price"] == 0.59


def test_crossed_book_danger_cancel_only(fake_clock):
    crossed = _book(fake_clock, bids=[
        {"price": "0.65", "size": "200"},
        {"price": "0.64", "size": "200"},
    ], asks=[
        {"price": "0.62", "size": "200"},
        {"price": "0.63", "size": "200"},
    ])
    om, clob = _make_om(fake_clock, crossed, current_price=0.65)
    _adjust(om)
    assert clob.cancelled == ["buy-1"]
    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})


def test_old_safe_new_unsafe_keeps_old_order(fake_clock):
    single_bid = _book(fake_clock, bids=[
        {"price": "0.60", "size": "200"},
    ], asks=[
        {"price": "0.62", "size": "200"},
    ])
    om, clob = _make_om(fake_clock, single_bid, current_price=0.59)
    _adjust(om)
    assert clob.cancelled == []
    assert "BUY" in om.active_orders["market-1"][TOKEN_A]


def test_old_danger_new_unsafe_cancel_only(fake_clock):
    single_bid = _book(fake_clock, bids=[
        {"price": "0.60", "size": "200"},
    ], asks=[
        {"price": "0.62", "size": "200"},
    ])
    om, clob = _make_om(fake_clock, single_bid, current_price=0.60)
    _adjust(om)
    assert clob.cancelled == ["buy-1"]
    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})
    assert len([c for c in clob.post_order_calls if c["order"].side == "BUY"]) == 0


def test_protection_gone_is_danger(fake_clock):
    thin = _book(fake_clock, bids=[
        {"price": "0.60", "size": "1"},
        {"price": "0.59", "size": "1"},
        {"price": "0.58", "size": "1"},
        {"price": "0.57", "size": "1"},
    ])
    om, clob = _make_om(fake_clock, thin, current_price=0.59, size=100.0)
    _adjust(om)
    assert clob.cancelled == ["buy-1"]
    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})


def test_pending_reorder_cooldown_then_retry(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.57)
    om.active_orders["market-1"][TOKEN_A].pop("BUY")
    om.pending_reorder_tokens[TOKEN_A] = {
        "market_id": "market-1",
        "side": "BUY",
        "last_attempt_time": fake_clock.monotonic() - 1.0,
        "target_price": 0.59,
        "order_size": 100,
        "safety_info": {"reason": "test"},
    }
    _adjust(om)
    # Cooldown (5s) not elapsed yet.
    assert "BUY" not in om.active_orders.get("market-1", {}).get(TOKEN_A, {})
    fake_clock.advance(6.0)
    _adjust(om)
    assert "BUY" in om.active_orders["market-1"][TOKEN_A]
    assert TOKEN_A not in om.pending_reorder_tokens


def test_transient_missing_book_no_blind_cancel(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.57)
    om.api_client.orderbook_source.orderbooks = {}
    _adjust(om)
    assert clob.cancelled == []


def test_inventory_blocks_requote(fake_clock):
    om, clob = _make_om(fake_clock, _book(fake_clock), current_price=0.57)
    om.inventory_exits[TOKEN_A] = om._new_inventory_state(
        "market-1", TOKEN_A, 0.60, 100.0
    )
    _adjust(om)
    assert clob.cancelled == []
    assert om.metrics["blocked_reentry_count"] >= 1
