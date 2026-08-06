"""Fake clients and deterministic time sources for offline tests."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from py_clob_client_v2.clob_types import OrderArgs


class FakeClock:
    """Deterministic wall-clock and monotonic-clock source."""

    def __init__(
        self,
        start: float = 1_000_000.0,
        wall_start: Optional[float] = None,
        mono_start: Optional[float] = None,
    ):
        self.wall = start if wall_start is None else wall_start
        self.mono = start if mono_start is None else mono_start

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


class FakeClobClient:
    """In-memory stand-in for py-clob-client-v2 ClobClient."""

    def __init__(self, clock: Optional[FakeClock] = None):
        self.clock = clock or FakeClock()
        self.open_orders: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []
        self.cancelled: List[str] = []
        self.post_order_calls: List[Dict[str, Any]] = []
        self.neg_risk = False
        self.next_order_id = 1
        self.fail_post: Optional[Exception] = None
        self.respond_success = True
        self.fail_open_orders = False
        self.fail_trades = False
        self.cancel_fail = False
        self.cancel_keep_order = False
        self.trade_pages: Optional[List[List[Dict[str, Any]]]] = None

    class _TradePage(list):
        pass

    # ------------------------------------------------------------------
    # Credentials / setup
    # ------------------------------------------------------------------
    def set_api_creds(self, creds: Any) -> None:
        pass

    def create_or_derive_api_key(self) -> str:
        return "fake-api-key"

    def get_neg_risk(self, token_id: str) -> bool:
        return self.neg_risk

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------
    def create_order(self, order_args: OrderArgs, order_options: Any = None) -> Any:
        return order_args

    def post_order(self, signed_order: Any, order_type: Any = None) -> Dict[str, Any]:
        self.post_order_calls.append({"order": signed_order, "order_type": order_type})
        if self.fail_post is not None:
            raise self.fail_post
        if not self.respond_success:
            return {"success": False, "errorMsg": "simulated failure"}
        order_id = f"order-{self.next_order_id}"
        self.next_order_id += 1
        price = float(getattr(signed_order, "price", 0))
        size = float(getattr(signed_order, "size", 0))
        side = getattr(signed_order, "side", "BUY")
        token_id = getattr(signed_order, "token_id", "token")
        self.open_orders.append(
            {
                "id": order_id,
                "token_id": token_id,
                "price": price,
                "size": size,
                "filled": 0.0,
                "remaining": size,
                "side": side,
            }
        )
        return {
            "success": True,
            "id": order_id,
            "status": "live",
            "price": price,
            "size": size,
        }

    def cancel_order(self, payload: Any) -> Dict[str, Any]:
        if self.cancel_fail:
            raise RuntimeError("simulated cancel failure")
        order_id = getattr(payload, "orderID", None) or (
            payload.get("orderID") if isinstance(payload, dict) else None
        )
        if order_id is None:
            order_id = getattr(payload, "order_id", None) or (
                payload.get("order_id") if isinstance(payload, dict) else None
            )
        if not self.cancel_keep_order:
            self.open_orders = [o for o in self.open_orders if o.get("id") != order_id]
        self.cancelled.append(order_id)
        return {"success": True}

    def get_open_orders(self, params: Any = None) -> List[Dict[str, Any]]:
        if self.fail_open_orders:
            raise RuntimeError("simulated open orders failure")
        return list(self.open_orders)

    def get_trades(self, params: Any = None, next_cursor: Optional[str] = None) -> List[Dict[str, Any]]:
        if self.fail_trades:
            raise RuntimeError("simulated trades failure")
        if self.trade_pages is not None:
            if next_cursor in (None, "MA=="):
                index = 0
            else:
                index = int(str(next_cursor).replace("page", ""))
            if index >= len(self.trade_pages):
                return []
            page = self._TradePage(self.trade_pages[index])
            page.next_cursor = (
                f"page{index + 1}" if index + 1 < len(self.trade_pages) else None
            )
            return page
        return list(self.trades)

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------
    def fill_order(self, order_id: str, size: float) -> None:
        """Simulate a fill on an existing open order."""
        for order in self.open_orders:
            if order.get("id") == order_id:
                order["filled"] += size
                order["remaining"] = max(0.0, order["size"] - order["filled"])
                self.trades.append(
                    {
                        "taker_order_id": order_id,
                        "size": size,
                        "maker_orders": [],
                    }
                )
                if order["remaining"] <= 0.0:
                    self.open_orders = [o for o in self.open_orders if o.get("id") != order_id]
                return
        # Order no longer open: still record the trade for reconciliation tests.
        self.trades.append({"taker_order_id": order_id, "size": size, "maker_orders": []})

    def remove_open_order(self, order_id: str) -> None:
        """Drop an order from open orders without marking it cancelled (fill simulation)."""
        self.open_orders = [o for o in self.open_orders if o.get("id") != order_id]


class FakeOrderbookSource:
    """Returns preloaded orderbooks keyed by token id."""

    def __init__(self, orderbooks: Optional[Dict[str, Dict[str, Any]]] = None):
        self.orderbooks: Dict[str, Dict[str, Any]] = orderbooks or {}
        self.fail = False

    def get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        if self.fail:
            return None
        book = self.orderbooks.get(token_id)
        if book is None:
            return None
        return dict(book)


class FakeAPIClient:
    """Fake Polymarket API client with no network access."""

    def __init__(
        self,
        orderbooks: Optional[Dict[str, Dict[str, Any]]] = None,
        markets: Optional[List[Dict[str, Any]]] = None,
        positions: Optional[List[Dict[str, Any]]] = None,
    ):
        self.orderbook_source = FakeOrderbookSource(orderbooks)
        self.markets = markets or []
        self.positions = positions or []
        self.positions_fail = False
        self.market_orderbooks_fail = False

    def get_orderbook(self, token_id: str) -> Optional[Dict[str, Any]]:
        return self.orderbook_source.get_orderbook(token_id)

    def get_markets_orderbooks(
        self, markets: List[Dict[str, Any]], use_cache: bool = False
    ) -> Dict[str, Dict[str, Any]]:
        if self.market_orderbooks_fail:
            return {}
        result: Dict[str, Dict[str, Any]] = {}
        for market in markets:
            for token in market.get("tokens", []):
                token_id = token.get("token_id")
                if token_id:
                    book = self.get_orderbook(token_id)
                    if book is not None:
                        result[token_id] = book
        return result

    def get_all_rewards_markets(self, **kwargs) -> List[Dict[str, Any]]:
        return list(self.markets)

    def get_markets_detail(self, market_ids: List[str]) -> List[Dict[str, Any]]:
        return []

    def get_positions(self, **kwargs) -> List[Dict[str, Any]]:
        if self.positions_fail:
            return []
        return [dict(p) for p in self.positions]
