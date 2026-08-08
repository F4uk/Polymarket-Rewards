"""Official-metadata-only market type filtering regressions."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

import redis_orderbook_client
from api_client import PolymarketAPIClient
from config import Config
from market_manager import MarketManager
from tests.fakes import FakeAPIClient
from tests.fixtures import TOKEN_A, TOKEN_B, market_fixture, orderbook_fixture


FILTER_KWARGS = {
    "min_reward_ratio": 0.0,
    "max_markets": 10,
    "spread_range": {"min": None, "max": 1.0},
    "volume_24hr_range": {"min": None, "max": None},
    "rewards_min_size_range": {"min": None, "max": None},
}


def _set_exclusions(monkeypatch, *values):
    import market_manager

    monkeypatch.setitem(
        market_manager.config.config, "excluded_market_types", set(values)
    )


def _pipeline(monkeypatch, market):
    api = FakeAPIClient(
        markets=[market],
        orderbooks={
            TOKEN_A: orderbook_fixture(TOKEN_A),
            TOKEN_B: orderbook_fixture(TOKEN_B),
        },
    )
    orderbook_calls = []
    original_get_orderbooks = api.get_markets_orderbooks

    def get_orderbooks(markets, use_cache=False):
        orderbook_calls.append([m.get("market_id") for m in markets])
        return original_get_orderbooks(markets, use_cache=use_cache)

    monkeypatch.setattr(api, "get_markets_orderbooks", get_orderbooks)
    manager = MarketManager(api)
    reward_spy = Mock(return_value=1.0)
    monkeypatch.setattr(manager, "calculate_reward_ratio", reward_spy)
    monkeypatch.setattr(
        manager,
        "_check_market_can_place_orders",
        lambda market, orderbooks, strategy: (True, "", {}),
    )
    selected = manager.filter_markets([market], **FILTER_KWARGS)
    return selected, manager, api, reward_spy, orderbook_calls


def _empty_cache(monkeypatch):
    class EmptyCache:
        def __init__(self, **kwargs):
            pass

        def get_markets_detail_batch(self, market_ids):
            return {}

        def close(self):
            pass

    monkeypatch.setattr(redis_orderbook_client, "RedisOrderbookClient", EmptyCache)


def test_config_parses_excluded_market_types(monkeypatch):
    monkeypatch.setenv("EXCLUDED_MARKET_TYPES", "sports,esports,weather")
    assert Config().excluded_market_types == {"sports", "esports", "weather"}


def test_config_normalizes_and_deduplicates_market_types(monkeypatch):
    monkeypatch.setenv(
        "EXCLUDED_MARKET_TYPES", " Sports,ESPORTS, weather, sports ,, "
    )
    assert Config().excluded_market_types == {"sports", "esports", "weather"}


def test_empty_config_and_env_example(monkeypatch):
    monkeypatch.delenv("EXCLUDED_MARKET_TYPES", raising=False)
    assert Config().excluded_market_types == set()
    assert "EXCLUDED_MARKET_TYPES=sports,esports,weather" in Path(
        ".env.example"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "metadata, expected",
    [
        ({"category": " Sports "}, "sports"),
        ({"tags": [{"slug": "weather"}]}, "weather"),
        ({"tags": [{"label": "Esports"}]}, "esports"),
        ({"categories": [{"slug": "weather"}]}, "weather"),
        ({"event": {"category": "Sports"}}, "sports"),
        ({"events": [{"tags": [{"label": "Esports"}]}]}, "esports"),
    ],
)
def test_official_market_and_event_metadata_is_normalized(metadata, expected):
    assert expected in MarketManager._market_type_values(metadata)


def test_non_excluded_metadata_and_question_words_do_not_classify_market():
    market = {
        "question": "Will weather affect League of Legends football?",
        "category": "Politics",
        "tags": [{"slug": "crypto"}],
    }
    values = MarketManager._market_type_values(market)
    assert values == {"politics", "crypto"}
    assert not values & {"sports", "esports", "weather"}


def test_excluded_market_never_reaches_orderbooks_or_reward_scoring(monkeypatch):
    _set_exclusions(monkeypatch, "sports", "esports", "weather")
    market = market_fixture(category="Sports")
    selected, manager, _, reward_spy, orderbook_calls = _pipeline(monkeypatch, market)
    assert selected == []
    assert manager.selected_markets == []
    assert orderbook_calls == []
    reward_spy.assert_not_called()


def test_non_excluded_good_market_remains_selectable(monkeypatch):
    _set_exclusions(monkeypatch, "sports", "esports", "weather")
    market = market_fixture(
        category="Politics",
        question="weather League of Legends football without matching metadata",
    )
    selected, manager, _, reward_spy, orderbook_calls = _pipeline(monkeypatch, market)
    assert [m["market_id"] for m in selected] == ["market-1"]
    assert [m["market_id"] for m in manager.selected_markets] == ["market-1"]
    assert orderbook_calls == [["market-1"]]
    reward_spy.assert_called_once()


def test_empty_config_preserves_pipeline_without_metadata_lookup(monkeypatch):
    _set_exclusions(monkeypatch)
    detail_lookup = Mock(side_effect=AssertionError("metadata lookup must stay disabled"))
    monkeypatch.setattr(MarketManager, "_load_market_type_details", detail_lookup)
    market = market_fixture(question="weather League of Legends football")
    selected, _, _, reward_spy, orderbook_calls = _pipeline(monkeypatch, market)
    assert [m["market_id"] for m in selected] == ["market-1"]
    assert orderbook_calls == [["market-1"]]
    reward_spy.assert_called_once()
    detail_lookup.assert_not_called()


def test_missing_local_metadata_uses_gamma_batch_and_excludes(monkeypatch):
    _set_exclusions(monkeypatch, "weather")
    _empty_cache(monkeypatch)
    markets = [market_fixture(), market_fixture(market_id="market-2")]
    api = FakeAPIClient()
    gamma = Mock(return_value=[
        {"id": "market-1", "tags": [{"slug": "weather"}]},
        {"id": "market-2", "tags": [{"slug": "weather"}]},
    ])
    monkeypatch.setattr(api, "get_markets_detail", gamma)
    monkeypatch.setattr(
        api,
        "get_markets_orderbooks",
        Mock(side_effect=AssertionError("excluded market reached orderbooks")),
    )
    manager = MarketManager(api)
    reward_spy = Mock(return_value=1.0)
    monkeypatch.setattr(manager, "calculate_reward_ratio", reward_spy)
    assert manager.filter_markets(markets, **FILTER_KWARGS) == []
    gamma.assert_called_once_with(["market-1", "market-2"])
    reward_spy.assert_not_called()


def test_cached_metadata_precedes_gamma(monkeypatch):
    _set_exclusions(monkeypatch, "sports")

    class SportsCache:
        def __init__(self, **kwargs):
            pass

        def get_markets_detail_batch(self, market_ids):
            return {"market-1": {"id": "market-1", "category": "Sports"}}

        def close(self):
            pass

    monkeypatch.setattr(redis_orderbook_client, "RedisOrderbookClient", SportsCache)
    api = FakeAPIClient()
    gamma = Mock(side_effect=AssertionError("Gamma should not run on a cache hit"))
    monkeypatch.setattr(api, "get_markets_detail", gamma)
    assert MarketManager(api).filter_markets(
        [market_fixture()], **FILTER_KWARGS
    ) == []
    gamma.assert_not_called()


def test_incomplete_cached_metadata_falls_back_to_gamma(monkeypatch):
    _set_exclusions(monkeypatch, "weather")

    class IncompleteCache:
        def __init__(self, **kwargs):
            pass

        def get_markets_detail_batch(self, market_ids):
            return {"market-1": {"id": "market-1"}}

        def close(self):
            pass

    monkeypatch.setattr(redis_orderbook_client, "RedisOrderbookClient", IncompleteCache)
    api = FakeAPIClient()
    gamma = Mock(return_value=[
        {"id": "market-1", "tags": [{"slug": "weather"}]},
    ])
    monkeypatch.setattr(api, "get_markets_detail", gamma)
    assert MarketManager(api).filter_markets(
        [market_fixture()], **FILTER_KWARGS
    ) == []
    gamma.assert_called_once_with(["market-1"])


def test_gamma_failure_skips_unknown_market_without_crashing(monkeypatch):
    _set_exclusions(monkeypatch, "sports")
    _empty_cache(monkeypatch)
    api = FakeAPIClient()
    monkeypatch.setattr(
        api, "get_markets_detail", Mock(side_effect=RuntimeError("Gamma unavailable"))
    )
    orderbooks = Mock(side_effect=AssertionError("unknown market reached orderbooks"))
    monkeypatch.setattr(api, "get_markets_orderbooks", orderbooks)
    assert MarketManager(api).filter_markets(
        [market_fixture()], **FILTER_KWARGS
    ) == []
    orderbooks.assert_not_called()


def test_gamma_market_detail_requests_tags_in_batch(monkeypatch):
    client = PolymarketAPIClient()
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": "1"}, {"id": "2"}]

    def get(url, params, timeout):
        captured.update(url=url, params=params, timeout=timeout)
        return Response()

    monkeypatch.setattr(client.session, "get", get)
    assert client.get_markets_detail(["1", "2"]) == [{"id": "1"}, {"id": "2"}]
    assert captured["params"] == {"id": ["1", "2"], "include_tag": "true"}
