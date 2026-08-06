"""Reward calculator boundary consistency (no fixed one-cent inset)."""
from __future__ import annotations

from reward_calculator import calculate_q_one_q_two, calculate_spread_cents


def _book(bids=None, asks=None):
    return {
        "bids": bids or [],
        "asks": asks or [],
    }


def test_q_one_includes_bid_at_new_boundary_without_one_cent_inset():
    # mid 0.50, rewards_max_spread=3.0 cents -> half-width 0.03 (new boundary 0.47).
    # The old (rewards_max_spread - 1)/100 formula used 0.02 and would have
    # excluded a bid at 0.475 (inside the new boundary, outside the old one).
    book = _book(bids=[{"price": "0.475", "size": "100"}])
    q_one, q_two = calculate_q_one_q_two(
        book, None, 0.50, v=3.0, b=1.0, rewards_max_spread=3.0
    )
    assert q_one > 0.0
    assert q_two == 0.0


def test_q_one_excludes_bid_outside_boundary():
    book = _book(bids=[{"price": "0.46", "size": "100"}])
    q_one, _ = calculate_q_one_q_two(
        book, None, 0.50, v=3.0, b=1.0, rewards_max_spread=3.0
    )
    assert q_one == 0.0


def test_spread_cents():
    import pytest

    assert calculate_spread_cents(0.47, 0.50) == pytest.approx(3.0)
    assert calculate_spread_cents(0.53, 0.50) == pytest.approx(3.0)
