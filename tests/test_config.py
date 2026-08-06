"""Phase 2: float config preservation, validation, and safe fallbacks."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from config import Config


def _fresh_config(monkeypatch, env: dict) -> Config:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Config()


def test_float_configs_are_not_truncated(monkeypatch):
    cfg = _fresh_config(
        monkeypatch,
        {
            "ORDERBOOK_UPDATE_INTERVAL_SECONDS": "0.5",
            "PRICE_DEVIATION_THRESHOLD_BPS": "0.0001",
            "ORDER_CHECK_INTERVAL_SECONDS": "0.25",
        },
    )
    assert cfg.orderbook_update_interval_seconds == 0.5
    assert cfg.price_deviation_threshold_bps == 0.0001
    assert cfg.order_check_interval_seconds == 0.25


def test_new_config_defaults_present():
    cfg = Config()
    assert cfg.max_orderbook_age_seconds == 3.0
    assert cfg.min_quote_lifetime_seconds == 10.0
    assert cfg.requote_min_ticks == 2
    assert cfg.requote_confirmations == 2
    assert cfg.requote_cooldown_seconds == 5.0
    assert cfg.post_fill_reentry_cooldown_seconds == 30.0
    assert cfg.reward_boundary_inset_ticks == 0
    assert cfg.exit_immediate_max_loss_ticks == 1
    assert cfg.exit_immediate_max_loss_bps == 300.0
    assert cfg.exit_passive_wait_seconds == 15.0
    assert cfg.exit_emergency_loss_ticks == 3
    assert cfg.exit_emergency_loss_bps == 1000.0
    assert cfg.exit_max_hold_seconds == 90.0
    assert cfg.min_exit_depth_multiplier == 1.2
    assert cfg.position_dust_size == 0.1
    assert cfg.position_change_confirmations == 2
    assert cfg.order_confirmation_timeout_seconds == 5.0
    assert cfg.order_confirmation_retry_seconds == 0.5


@pytest.mark.parametrize(
    "key,value",
    [
        ("MAX_ORDERBOOK_AGE_SECONDS", "-1"),
        ("MIN_QUOTE_LIFETIME_SECONDS", "-5"),
        ("REQUOTE_MIN_TICKS", "-2"),
        ("REQUOTE_CONFIRMATIONS", "0"),
        ("REQUOTE_COOLDOWN_SECONDS", "-1"),
        ("POST_FILL_REENTRY_COOLDOWN_SECONDS", "-1"),
        ("REWARD_BOUNDARY_INSET_TICKS", "-1"),
        ("EXIT_IMMEDIATE_MAX_LOSS_TICKS", "-1"),
        ("EXIT_IMMEDIATE_MAX_LOSS_BPS", "-10"),
        ("EXIT_PASSIVE_WAIT_SECONDS", "-1"),
        ("EXIT_EMERGENCY_LOSS_TICKS", "-1"),
        ("EXIT_EMERGENCY_LOSS_BPS", "-10"),
        ("EXIT_MAX_HOLD_SECONDS", "-1"),
        ("MIN_EXIT_DEPTH_MULTIPLIER", "0.5"),
        ("POSITION_DUST_SIZE", "-0.5"),
        ("POSITION_CHANGE_CONFIRMATIONS", "0"),
        ("ORDER_CONFIRMATION_TIMEOUT_SECONDS", "-1"),
        ("ORDER_CONFIRMATION_RETRY_SECONDS", "-1"),
    ],
)
def test_invalid_values_fall_back_to_safe_defaults(monkeypatch, key, value):
    cfg = _fresh_config(monkeypatch, {key: value})
    prop = {
        "MAX_ORDERBOOK_AGE_SECONDS": "max_orderbook_age_seconds",
        "MIN_QUOTE_LIFETIME_SECONDS": "min_quote_lifetime_seconds",
        "REQUOTE_MIN_TICKS": "requote_min_ticks",
        "REQUOTE_CONFIRMATIONS": "requote_confirmations",
        "REQUOTE_COOLDOWN_SECONDS": "requote_cooldown_seconds",
        "POST_FILL_REENTRY_COOLDOWN_SECONDS": "post_fill_reentry_cooldown_seconds",
        "REWARD_BOUNDARY_INSET_TICKS": "reward_boundary_inset_ticks",
        "EXIT_IMMEDIATE_MAX_LOSS_TICKS": "exit_immediate_max_loss_ticks",
        "EXIT_IMMEDIATE_MAX_LOSS_BPS": "exit_immediate_max_loss_bps",
        "EXIT_PASSIVE_WAIT_SECONDS": "exit_passive_wait_seconds",
        "EXIT_EMERGENCY_LOSS_TICKS": "exit_emergency_loss_ticks",
        "EXIT_EMERGENCY_LOSS_BPS": "exit_emergency_loss_bps",
        "EXIT_MAX_HOLD_SECONDS": "exit_max_hold_seconds",
        "MIN_EXIT_DEPTH_MULTIPLIER": "min_exit_depth_multiplier",
        "POSITION_DUST_SIZE": "position_dust_size",
        "POSITION_CHANGE_CONFIRMATIONS": "position_change_confirmations",
        "ORDER_CONFIRMATION_TIMEOUT_SECONDS": "order_confirmation_timeout_seconds",
        "ORDER_CONFIRMATION_RETRY_SECONDS": "order_confirmation_retry_seconds",
    }[key]
    default = {
        "max_orderbook_age_seconds": 3.0,
        "min_quote_lifetime_seconds": 10.0,
        "requote_min_ticks": 2,
        "requote_confirmations": 2,
        "requote_cooldown_seconds": 5.0,
        "post_fill_reentry_cooldown_seconds": 30.0,
        "reward_boundary_inset_ticks": 0,
        "exit_immediate_max_loss_ticks": 1,
        "exit_immediate_max_loss_bps": 300.0,
        "exit_passive_wait_seconds": 15.0,
        "exit_emergency_loss_ticks": 3,
        "exit_emergency_loss_bps": 1000.0,
        "exit_max_hold_seconds": 90.0,
        "min_exit_depth_multiplier": 1.2,
        "position_dust_size": 0.1,
        "position_change_confirmations": 2,
        "order_confirmation_timeout_seconds": 5.0,
        "order_confirmation_retry_seconds": 0.5,
    }[prop]
    assert getattr(cfg, prop) == default


def test_invalid_text_does_not_become_zero(monkeypatch):
    cfg = _fresh_config(
        monkeypatch,
        {"ORDERBOOK_UPDATE_INTERVAL_SECONDS": "not-a-number", "PRICE_DEVIATION_THRESHOLD_BPS": "abc"},
    )
    assert cfg.orderbook_update_interval_seconds == 5.0
    assert cfg.price_deviation_threshold_bps == 1.0


def test_cross_field_constraints_are_enforced(monkeypatch):
    cfg = _fresh_config(
        monkeypatch,
        {
            "EXIT_EMERGENCY_LOSS_TICKS": "1",
            "EXIT_IMMEDIATE_MAX_LOSS_TICKS": "5",
            "EXIT_EMERGENCY_LOSS_BPS": "100",
            "EXIT_IMMEDIATE_MAX_LOSS_BPS": "500",
            "EXIT_MAX_HOLD_SECONDS": "5",
            "EXIT_PASSIVE_WAIT_SECONDS": "15",
            "ORDER_CONFIRMATION_TIMEOUT_SECONDS": "0.2",
            "ORDER_CONFIRMATION_RETRY_SECONDS": "0.5",
        },
    )
    assert cfg.exit_emergency_loss_ticks >= cfg.exit_immediate_max_loss_ticks
    assert cfg.exit_emergency_loss_bps >= cfg.exit_immediate_max_loss_bps
    assert cfg.exit_max_hold_seconds >= cfg.exit_passive_wait_seconds
    assert cfg.order_confirmation_timeout_seconds >= cfg.order_confirmation_retry_seconds
    assert cfg.order_confirmation_retry_seconds > 0


def test_env_example_contains_new_configs():
    text = Path(".env.example").read_text(encoding="utf-8")
    for key in (
        "MAX_ORDERBOOK_AGE_SECONDS",
        "MIN_QUOTE_LIFETIME_SECONDS",
        "REQUOTE_MIN_TICKS",
        "REQUOTE_CONFIRMATIONS",
        "REQUOTE_COOLDOWN_SECONDS",
        "POST_FILL_REENTRY_COOLDOWN_SECONDS",
        "REWARD_BOUNDARY_INSET_TICKS",
        "EXIT_IMMEDIATE_MAX_LOSS_TICKS",
        "EXIT_IMMEDIATE_MAX_LOSS_BPS",
        "EXIT_PASSIVE_WAIT_SECONDS",
        "EXIT_EMERGENCY_LOSS_TICKS",
        "EXIT_EMERGENCY_LOSS_BPS",
        "EXIT_MAX_HOLD_SECONDS",
        "MIN_EXIT_DEPTH_MULTIPLIER",
        "POSITION_DUST_SIZE",
        "POSITION_CHANGE_CONFIRMATIONS",
        "ORDER_CONFIRMATION_TIMEOUT_SECONDS",
        "ORDER_CONFIRMATION_RETRY_SECONDS",
    ):
        assert key in text
