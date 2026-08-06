"""Shared pytest fixtures and offline guards."""
from __future__ import annotations

import socket
import threading
import time as time_module
from pathlib import Path
from typing import Any, Dict

import pytest

from tests.fakes import FakeAPIClient, FakeClobClient, FakeClock
from tests.fixtures import (
    TOKEN_A,
    market_fixture,
    orderbook_fixture,
)


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Raise on any socket creation so tests can never touch the network."""

    def deny(*args, **kwargs):
        raise RuntimeError("network access is disabled in tests")

    monkeypatch.setattr(socket, "socket", deny)
    # Keep tests fast and deterministic; FakeClock controls all elapsed time.
    monkeypatch.setattr(time_module, "sleep", lambda _seconds: None)
    yield


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def fake_clob(fake_clock: FakeClock) -> FakeClobClient:
    return FakeClobClient(clock=fake_clock)


@pytest.fixture
def fake_api() -> FakeAPIClient:
    return FakeAPIClient(
        orderbooks={
            TOKEN_A: orderbook_fixture(TOKEN_A),
        },
        markets=[market_fixture()],
    )


@pytest.fixture
def temp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "test.sqlite3")


def make_order_manager(
    *,
    api_client: Any = None,
    clob_client: Any = None,
    risk_manager: Any = None,
    strategy: Any = None,
    clock: Any = None,
) -> Any:
    """Build an OrderManager instance without requiring wallet credentials."""
    from order_manager import OrderManager
    from risk_manager import RiskManager
    from market_making_strategy import MarketMakingStrategy

    clock = clock or FakeClock()
    om = OrderManager.__new__(OrderManager)
    om.api_client = api_client or FakeAPIClient()
    om.risk_manager = risk_manager or RiskManager(max_exposure_per_market_usdc=1000.0)
    om.strategy = strategy or MarketMakingStrategy()
    om.clob_client = clob_client or FakeClobClient(clock=clock)
    om.active_orders = {}
    om.filled_buy_orders = {}
    om.market_data_cache = {}
    om.pending_reorder_tokens = {}
    om.partial_filled_tracking = {}
    om.hedge_sell_failures = {}
    om.inventory_exits = {}
    om.reentry_cooldowns = {}
    om.order_fingerprints = {}
    om.requote_confirmations = {}
    om.position_diff_confirmations = {}
    om.inventory_generations = {}
    om.last_requote_cancel = {}
    om.cancel_pending_tracking = {}
    om.startup_open_orders_blocked = False
    om.metrics = {}
    om.lock = threading.RLock()
    om._now = clock.time
    om._monotonic = clock.monotonic
    om._init_metrics()
    return om


@pytest.fixture
def order_manager(fake_clob: FakeClobClient, fake_api: FakeAPIClient, fake_clock: FakeClock):
    om = make_order_manager(api_client=fake_api, clob_client=fake_clob, clock=fake_clock)
    om.market_data_cache["market-1"] = market_fixture()
    return om


def register_fake_orders(om: Any, orders: Dict[str, Dict[str, Any]]) -> None:
    """Register internal active orders (bypassing placement)."""
    with om.lock:
        for order_id, info in orders.items():
            market_id = info["market_id"]
            token_id = info["token_id"]
            side = info["side"]
            om.active_orders.setdefault(market_id, {}).setdefault(token_id, {})[side] = {
                "order_id": order_id,
                "token_id": token_id,
                "side": side,
                "price": info.get("price", 0.50),
                "size": info.get("size", 100.0),
                "exposure": info.get("exposure", 0.0),
                "created_at": info.get("created_at", om._now()),
                "created_at_monotonic": info.get(
                    "created_at_monotonic", om._monotonic()
                ),
                "submitted_at": info.get("submitted_at", om._monotonic()),
                "purpose": info.get("purpose", "REWARD_BUY"),
                "status": info.get("status", "PENDING_CONFIRMATION"),
            }
