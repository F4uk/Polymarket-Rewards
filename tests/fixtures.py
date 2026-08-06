"""Static markets and orderbook fixtures for deterministic tests."""
from __future__ import annotations

from typing import Any, Dict, List


TOKEN_A = "token-a"
TOKEN_B = "token-b"
TOKEN_C = "token-c"


def market_fixture(
    market_id: str = "market-1",
    rewards_max_spread: float = 3.0,
    rewards_min_size: int = 50,
    tokens: Optional[List[Dict[str, Any]]] = None,
    neg_risk: bool = False,
    **extra,
) -> Dict[str, Any]:
    if tokens is None:
        tokens = [
            {"token_id": TOKEN_A, "outcome": "YES"},
            {"token_id": TOKEN_B, "outcome": "NO"},
        ]
    return {
        "market_id": market_id,
        "question": f"Test market {market_id}",
        "rewards_max_spread": rewards_max_spread,
        "rewards_min_size": rewards_min_size,
        "tokens": tokens,
        "neg_risk": neg_risk,
        "rewards_config": [{"rate_per_day": 100.0}],
        **extra,
    }


def _level(price: float, size: float) -> Dict[str, str]:
    return {"price": str(price), "size": str(size)}


def orderbook_fixture(
    token_id: str = TOKEN_A,
    bids: Optional[List[Dict[str, Any]]] = None,
    asks: Optional[List[Dict[str, Any]]] = None,
    tick_size: str = "0.01",
    received_at: Optional[float] = None,
) -> Dict[str, Any]:
    return {
        "asset_id": token_id,
        "bids": bids if bids is not None else [_level(0.60, 100), _level(0.59, 100)],
        "asks": asks if asks is not None else [_level(0.62, 100), _level(0.63, 100)],
        "tick_size": tick_size,
        "_received_at": received_at,
    }


def book_with_prices(
    token_id: str,
    best_bid: float = 0.60,
    best_ask: float = 0.62,
    second_bid: Optional[float] = None,
    bid_sizes: Optional[List[float]] = None,
    ask_sizes: Optional[List[float]] = None,
    bid_levels: int = 4,
    ask_levels: int = 4,
    tick: float = 0.01,
) -> Dict[str, Any]:
    """Build a deterministic book with price levels spaced one tick apart."""
    if second_bid is None:
        second_bid = best_bid - tick
    bid_prices = [best_bid - i * tick for i in range(bid_levels)]
    ask_prices = [best_ask + i * tick for i in range(ask_levels)]
    sizes = bid_sizes or [100.0] * bid_levels
    ask_sizes_list = ask_sizes or [100.0] * ask_levels
    bids = [_level(p, sizes[i]) for i, p in enumerate(bid_prices)]
    asks = [_level(p, ask_sizes_list[i]) for i, p in enumerate(ask_prices)]
    # Keep second_bid explicit even when it equals the computed level.
    if second_bid < best_bid:
        bids = [_level(best_bid, sizes[0]), _level(second_bid, sizes[1])] + bids[2:]
    return orderbook_fixture(token_id, bids=bids, asks=asks, tick_size=f"{tick:g}")


def shuffled(levels: List[Dict[str, Any]], seed: int = 1) -> List[Dict[str, Any]]:
    """Deterministic pseudo-random shuffle for order-robustness tests."""
    result = list(levels)
    rng_state = seed
    for i in range(len(result) - 1, 0, -1):
        rng_state = (rng_state * 1103515245 + 12345) & 0x7FFFFFFF
        j = rng_state % (i + 1)
        result[i], result[j] = result[j], result[i]
    return result


# ----------------------------------------------------------------------
# Orderbook scenarios required by the task spec
# ----------------------------------------------------------------------
def bid_levels() -> List[Dict[str, Any]]:
    return [_level(0.60, 100), _level(0.59, 100), _level(0.58, 100)]


def ask_levels() -> List[Dict[str, Any]]:
    return [_level(0.62, 100), _level(0.63, 100), _level(0.64, 100)]


def crossed_book(token_id: str = TOKEN_A) -> Dict[str, Any]:
    return orderbook_fixture(
        token_id,
        bids=[_level(0.65, 100), _level(0.64, 100)],
        asks=[_level(0.62, 100), _level(0.63, 100)],
    )


def one_sided_book(token_id: str = TOKEN_A, side: str = "bids") -> Dict[str, Any]:
    return orderbook_fixture(
        token_id,
        bids=bid_levels() if side == "bids" else [],
        asks=[] if side == "bids" else ask_levels(),
    )


def empty_book(token_id: str = TOKEN_A) -> Dict[str, Any]:
    return orderbook_fixture(token_id, bids=[], asks=[])


def invalid_price_book(token_id: str = TOKEN_A) -> Dict[str, Any]:
    return orderbook_fixture(
        token_id,
        bids=[{"price": "abc", "size": "100"}, _level(0.60, 100)],
        asks=[_level(0.62, 100)],
    )


def invalid_size_book(token_id: str = TOKEN_A) -> Dict[str, Any]:
    return orderbook_fixture(
        token_id,
        bids=[{"price": "0.60", "size": "-5"}, _level(0.59, 100)],
        asks=[_level(0.62, 100), {"price": "0.63", "size": "0"}],
    )


def same_price_multiple_levels(token_id: str = TOKEN_A) -> Dict[str, Any]:
    return orderbook_fixture(
        token_id,
        bids=[
            _level(0.60, 40),
            _level(0.60, 60),
            _level(0.59, 100),
        ],
        asks=[
            _level(0.62, 30),
            _level(0.62, 70),
            _level(0.63, 100),
        ],
    )


def stale_book(token_id: str = TOKEN_A, received_at: float = 0.0) -> Dict[str, Any]:
    book = orderbook_fixture(token_id)
    book["_received_at"] = received_at
    return book
