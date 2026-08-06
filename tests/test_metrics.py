"""Phase 9: dead-code cleanup verification and lightweight metrics."""
from __future__ import annotations

from pathlib import Path

from tests.conftest import make_order_manager
from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import TOKEN_A, market_fixture, orderbook_fixture


def _market(**kwargs):
    kwargs.setdefault("orderPriceMinTickSize", 0.01)
    return market_fixture(**kwargs)


def _book(clock):
    bids = [
        {"price": "0.60", "size": "200"},
        {"price": "0.59", "size": "200"},
        {"price": "0.58", "size": "200"},
        {"price": "0.57", "size": "200"},
    ]
    asks = [
        {"price": "0.62", "size": "200"},
        {"price": "0.63", "size": "200"},
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


def test_metrics_initialized_with_all_keys():
    om, _ = _make_om(FakeClock())
    for key in (
        "buys_placed",
        "buys_cancelled",
        "safety_cancels",
        "requotes",
        "retained_markets",
        "full_fills",
        "partial_fills",
        "fast_exit_count",
        "limited_wait_count",
        "emergency_exit_count",
        "avg_hold_time_seconds",
        "exit_price_loss",
        "blocked_reentry_count",
        "stale_book_rejections",
        "insufficient_exit_depth_rejections",
        "pending_confirmation_count",
        "unknown_order_count",
        "blocked_duplicate_count",
        "exit_fills",
        "positions_flat",
    ):
        assert key in om.metrics


def test_metrics_increment_on_trading_flow(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    assert resp is not None
    assert om.metrics["buys_placed"] == 1

    order_id = resp["id"]
    clob.remove_open_order(order_id)
    om._confirm_pending_orders()
    assert om.metrics["pending_confirmation_count"] == 1
    clob.fill_order(order_id, 100.0)
    om.check_orders()

    assert om.metrics["full_fills"] >= 1
    assert om.metrics["fast_exit_count"] >= 1
    # BUY 已确认；库存退出 SELL 仍处于 PENDING_CONFIRMATION
    assert om.metrics["pending_confirmation_count"] == 1


def test_metrics_include_reentry_and_duplicates(fake_clock):
    om, clob = _make_om(fake_clock)
    om.inventory_exits[TOKEN_A] = om._new_inventory_state(
        "market-1", TOKEN_A, 0.60, 100.0
    )
    om.maybe_reenter_markets([_market()])
    assert om.metrics["blocked_reentry_count"] >= 1

    om.place_order("market-1", TOKEN_A, "SELL", 0.60, 100.0, purpose="FAST_EXIT", generation=1)
    om.place_order("market-1", TOKEN_A, "SELL", 0.60, 100.0, purpose="FAST_EXIT", generation=1)
    assert om.metrics["blocked_duplicate_count"] >= 1


def test_avg_hold_time_and_flat_metrics(fake_clock):
    om, clob = _make_om(fake_clock)
    resp = om.place_order("market-1", TOKEN_A, "BUY", 0.50, 100.0)
    order_id = resp["id"]
    clob.fill_order(order_id, 100.0)
    om.check_orders()
    sell_id = om.inventory_exits[TOKEN_A]["sell_order_id"]
    fake_clock.advance(5.0)
    clob.fill_order(sell_id, 100.0)
    om.check_inventory_exits()
    assert om.metrics["positions_flat"] >= 1
    assert om.metrics["avg_hold_time_seconds"] > 0.0
    assert om.metrics["exit_fills"] >= 100.0


def test_stats_include_metrics(fake_clock):
    om, _ = _make_om(fake_clock)
    stats = om.get_order_statistics()
    assert "metrics" in stats
    assert stats["metrics"]["buys_placed"] == 0


def test_dead_duplicate_check_orders_removed():
    source = Path("order_manager.py").read_text(encoding="utf-8")
    assert "简化版本：主要功能是清理订单记录和更新风险敞口" not in source


def test_unused_math_import_removed():
    source = Path("market_making_strategy.py").read_text(encoding="utf-8")
    assert "import math" not in source
