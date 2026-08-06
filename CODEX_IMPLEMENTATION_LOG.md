# CODEX_IMPLEMENTATION_LOG

## Initial audit findings (before Phase 0)

### Call chain (as read from source)

```text
main.py
  -> cancel_all_buy_orders() on startup
  -> MarketManager.scan_rewards_markets() -> PolymarketAPIClient.get_all_rewards_markets()
  -> MarketManager.filter_markets() -> orderbooks -> reward ratio -> spread filter -> safety checks
  -> OrderManager.place_market_orders() -> strategy prices -> place_order(BUY)
  -> main loop:
       check_positions_and_hedge()
       check_orders() -> fill detection -> place_hedge_sell() -> replace_filled_order() (re-buy)
       adjust_orders_to_reward_boundaries() (cancel + re-place)
       periodic full rescan: clear pending, cancel ALL active market orders, rescan, treat all as new
```

### Confirmed problems

1. **Fixed orderbook index assumptions**: `market_making_strategy.calculate_mid_price`, `calculate_actual_buy_price`, `can_place_buy_order_safely`, `order_manager.check_positions_and_hedge`, `place_hedge_sell`, `adjust_orders_to_reward_boundaries`, and `market_manager.filter_markets` treat `bids[-1]` / `asks[-1]` as best prices. The actual API ordering is not guaranteed, and cached/test data cannot be assumed sorted.
2. **One-sided rounding**: `normalize_price` always floors. SELL prices are floored too, which can lower a passive sell below the planned price.
3. **Magic one-cent inset**: `(rewards_max_spread - 1) / 100` appears in `market_making_strategy.calculate_reward_range` and `reward_calculator.calculate_q_one_q_two`.
4. **Float config truncation**: `ORDERBOOK_UPDATE_INTERVAL_SECONDS` (0.5) and `PRICE_DEVIATION_THRESHOLD_BPS` (0.0001) are read as float but exposed through `get_int`, truncating to 0.
5. **Fabricated conservative books**: `calculate_order_prices(use_conservative_price=True)` produces 0.01/0.99 prices when data is stale/missing.
6. **Average-spread market gating**: `filter_markets` averages token spreads, letting a good token mask a bad one.
7. **Unconditional re-buy after fill**: `main.py` calls `replace_filled_order` for every filled order; `check_orders` also re-submits the full BUY after a partial fill.
8. **Full cancel/rebuild on rescan**: `main.py` clears pending reorder tokens and cancels all active-market orders on every periodic rescan, then treats the reselected set as new.
9. **Fixed 0.05 exit rule**: `HEDGE_SELL_MAX_BID_GAP` is used as the primary sell-at-best-bid rule; there is no tiered/time-bounded exit.
10. **Unreachable duplicate implementation**: `check_orders` contains a second full implementation after `return`.
11. **Invisible SELL treated as failure**: `place_hedge_sell` returns `None` when the order is not visible in open orders after 0.3 s.
12. **No order status / fingerprint / confirmation window**: successful submissions are recorded without a confirmation state or duplicate-order fingerprint.
13. **No market selection set**: selected markets are only inferred from the active order set.

## Design decisions

- Single orderbook normalization entry will live in `market_making_strategy.py` (existing module) so all callers share one parser.
- Received time is stamped on fetched orderbooks as `_received_at` (monotonic) and stored inside the cached JSON so cache reads cannot refresh it.
- Immediate-exit SELL uses a floor-to-tick price capped at best bid; passive SELL uses ceil-to-tick. Both are documented and tested.
- Entry gating is centralized in `MarketMakingStrategy.evaluate_market_entry` (small result object) and reused by `MarketManager` and `OrderManager`.
- Inventory exit state is a small dict/dataclass inside `OrderManager`; no new state-machine framework.
- OrderManager gets injectable `_now()` / `_monotonic()` time hooks so tests use a fake clock without changing public interfaces.

## Conservative assumptions

- The maximum simulated exit loss for entry gating reuses `EXIT_IMMEDIATE_MAX_LOSS_BPS` (300 bps) as the "VWAP too lossy" threshold; no new loss-limit config was invented.
- A "tiny unstable top level" is treated as best-bid size below 10% of the planned order size and is rejected under `INSUFFICIENT_PROTECTION`.
- `ORDERBOOK_UPDATE_INTERVAL_SECONDS` and `ORDER_CHECK_INTERVAL_SECONDS` are treated as floats (not integers) because they are time intervals.
- `PRICE_DEVIATION_THRESHOLD_BPS` is treated as a float bps value; existing `.env.example` already documents `0.0001`.

## Phase log

### Phase 0 - deterministic test baseline

Created `tests/` with fake CLOB/API clients, fake orderbook source, deterministic clock, temp SQLite path support, network guard, and static fixtures for every required orderbook scenario. Added baseline regression tests for config defaults, price normalization, order size, risk exposure, order place/cancel, and statistics. Added fixture-completeness and fake-fill behavior tests.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 12 passed; `git diff --check` clean. Commit `237ba25`.

### Phase 1 - unified orderbook parsing

Added `NormalizedOrderbook` and `normalize_orderbook()` in `market_making_strategy.py` as the single standardization entry point. It converts prices/sizes with `Decimal`, ignores unparseable/out-of-range rows and non-positive sizes, aggregates same-price levels, sorts bids high-to-low and asks low-to-high, computes best/second prices, book-health flags, and monotonic age. It never mutates the input.

`_received_at` is stamped with `time.monotonic()` in `HTTPOrderbookClient.get_orderbooks()` and preserved through the SQLite cache, so reading a cache entry never refreshes its received time.

Replaced fixed-index price reads in:

- `MarketMakingStrategy.calculate_mid_price`, `calculate_actual_buy_price`, `can_place_buy_order_safely`, `infer_tick_size_from_orderbook`
- `MarketManager.filter_markets` spread calculation
- `OrderManager.check_positions_and_hedge`, `place_hedge_sell`, `adjust_orders_to_reward_boundaries`

`http_orderbook_client.extract_prices` was left as max/min because it is display-only and already order-independent.

Tests: `tests/test_orderbook_normalization.py` covers all sorting combinations, aggregation, distinct second bid, empty/one-sided/crossed books, invalid price/size, input immutability, age growth, cache reads not refreshing received time, tick sizes, and order-independent trading decisions.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 26 passed; `git diff --check` clean.

### Phase 2 - rounding, reward boundary, config types

Implemented `round_price_to_tick(price, tick_size, side)` with `Decimal` and explicit side: BUY floors, SELL ceils, unknown side raises `ValueError`. `normalize_price` keeps its old signature as the backward-compatible BUY default. Added `immediate_exit_price(best_bid, tick_size)` which floors to the tick and stays at or below best bid, so fill-triggered SELL orders never round above the executable price.

Replaced `(rewards_max_spread - 1) / 100` with `reward_spread_decimal(rewards_max_spread, inset_ticks, tick_size)`; the only inset is `REWARD_BOUNDARY_INSET_TICKS * tick_size` (default 0). Both `market_making_strategy.calculate_reward_range` and `reward_calculator.calculate_q_one_q_two` use the helper.

Fixed config types: `orderbook_update_interval_seconds`, `order_check_interval_seconds`, and `price_deviation_threshold_bps` are now exposed as floats. Added 18 new validated configs with explicit defaults, range checks, warnings, and safe fallback (never silently zero). `_validate_cross_field_constraints` enforces confirmations >= 1, depth multiplier >= 1, emergency >= immediate thresholds, max hold >= passive wait, retry interval > 0, and timeout >= retry interval.

`OrderManager.place_order` now rounds both BUY and SELL through the side-aware entry and rejects unknown sides.

Tests: `tests/test_config.py`, `tests/test_rounding.py` (float preservation, invalid fallback, cross-field constraints, BUY/SELL rounding across tick sizes, clamps, unknown side, reward boundary without the fixed one-cent inset, inset behavior, immediate-exit price).

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 71 passed; `git diff --check` clean.

### Phase 3 - strict entry and exit-liquidity gating

Added `EntryDecision` and reason codes (`ACCEPTED`, `STALE_BOOK`, `EMPTY_BOOK`, `ONE_SIDED_BOOK`, `CROSSED_BOOK`, `SPREAD_TOO_WIDE`, `NO_SECOND_BID`, `INSUFFICIENT_PROTECTION`, `INSUFFICIENT_EXIT_DEPTH`, `EXIT_VWAP_TOO_LOSSY`, `PRICE_CLIFF`, `INVALID_BOOK`).

`MarketMakingStrategy.evaluate_token_entry` / `evaluate_market_entry` implement the full gate: fresh book (age <= `MAX_ORDERBOOK_AGE_SECONDS`, missing receive time rejected), worst-side spread (every required token must pass), second-bid condition, protection check, and exit-liquidity simulation (cumulative bid coverage >= `order_size * MIN_EXIT_DEPTH_MULTIPLIER`, exit VWAP loss <= `EXIT_IMMEDIATE_MAX_LOSS_BPS`, price-cliff detection within needed levels, unstable tiny top level rejected).

`MarketManager.filter_markets` now uses the worst (max) token spread instead of the average, and `_check_market_can_place_orders` runs the full market entry evaluation.

`OrderManager.place_market_orders` now requires a fresh realtime book per token; the old fallback to stale cache with fabricated `0.01`/`0.99` conservative prices was removed (`calculate_order_prices` no longer has `use_conservative_price`). Rejected tokens are recorded in `pending_reorder_tokens` with the entry reason.

Conservative assumption: every token with a token_id in the market is required to pass entry (a good token cannot mask a bad one).

Tests: `tests/test_market_entry.py` covers accepted markets, worst-side spread, average-ok/worst-fail, exact and insufficient exit depth, lossy VWAP, price cliff, stale/one-sided/crossed/no-second-bid books, order independence, MarketManager worst-spread filtering, skip-without-fresh-book, skip-stale-book, and absence of the conservative-price parameter.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 87 passed; `git diff --check` clean.

### Phase 4 - tiered inventory exit

Added per-token inventory exit state inside `OrderManager` (plain dict, no state-machine framework): `market_id`, `token_id`, `entry_price`, `confirmed_filled_size`, `remaining_size`, `sold_size`, `processed_fill_size`, `processed_sell_size`, `pending_sell_size`, `first_fill_at`, `last_action_at`, `state` (`FAST_EXIT` / `LIMITED_WAIT` / `EMERGENCY_EXIT` / `FLAT`), `sell_order_id`, `sell_order_status`, `last_known_best_bid`, `generation`.

Fill handling is now idempotent: `_handle_buy_fill` processes only the cumulative-fill delta and starts the exit flow. `_process_inventory_exit` computes tick/bps loss against the latest fresh book and decides:

- FAST_EXIT: immediate SELL at `immediate_exit_price(best_bid)` when loss is within `EXIT_IMMEDIATE_MAX_LOSS_TICKS` and `EXIT_IMMEDIATE_MAX_LOSS_BPS`. The immediate-exit price is floor-aligned and capped at best bid; `place_order` skips SELL-up rounding for FAST/EMERGENCY purposes so the order cannot round above the executable price.
- LIMITED_WAIT: passive near-cost SELL bounded by `EXIT_PASSIVE_WAIT_SECONDS`; no duplicate submits while one is pending.
- EMERGENCY_EXIT: triggered by loss ticks/bps thresholds, `EXIT_MAX_HOLD_SECONDS` timeout, bid-depth drop beyond 2 ticks, spread over the entry limit, or crossed book. Cancels the passive SELL and immediately re-lists at the best executable bid; partial fills plus cancelled remainder are re-listed until flat.

`check_inventory_exits()` confirms SELL fills via trade history (positions API may lag or return zero without abandoning the state), marks FLAT below `POSITION_DUST_SIZE`, and arms the reentry cooldown. `check_positions_and_hedge()` now reconciles positions into inventory state and delegates to the tiered flow (the old fixed-0.05 body is unreachable and will be deleted in Phase 9).

Order fingerprints and `purpose` (`REWARD_BUY`, `FAST_EXIT`, `LIMITED_WAIT_EXIT`, `EMERGENCY_EXIT`) were introduced so the confirmation window can block duplicate SELL submissions (fully formalized in Phase 8).

Tests: `tests/test_inventory_exit.py` covers zero/one-tick fast exit, bps threshold blocking fast exit for low-price one-tick loss, limited-wait timeout -> emergency, severe-loss immediate emergency, depth-drop and spread-widening emergencies, position-API delay, invisible SELL not duplicated, partial fill + cancel -> remaining re-listed, never overselling, same-fill idempotency, dust flat, and reentry blocking.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 102 passed; `git diff --check` clean.

### Phase 5 - block BUY until inventory is flat

Removed the unconditional re-buy after fills from `main.py` (`replace_filled_order` is no longer called in the main loop). Partial fills now process the new fill delta and start inventory exit **before** attempting to cancel the remaining BUY; if cancellation fails, the order record is kept so later fill deltas are still processed (no assumption that cancellation succeeded).

Added `OrderManager.reconcile_startup()`: queries open orders, cancels legacy BUY orders, imports existing SELL orders into active tracking (purpose `RECONCILED_EXIT`), and imports existing positions into inventory exit management. `main.py` now runs this before scanning/placing any new BUY.

Added `OrderManager.maybe_reenter_markets()`: only re-enters tokens with no active BUY, no inventory/pending exit, no SELL in `PENDING_CONFIRMATION`/`LIVE`/`CANCEL_PENDING`/`UNKNOWN`, and cooldown expired; every reentry runs the full entry gate on a fresh book and increments `blocked_reentry_count` otherwise.

Tests: `tests/test_reentry_block.py` covers no-rebuy after full fill, partial-fill cancel + no full re-submit, delta-only processing, SELL active/pending/UNKNOWN blocking, inventory state blocking, cooldown blocking, cooldown-end entry failure/pass, startup reconciliation order, and non-negative risk exposure.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 114 passed; `git diff --check` clean.

### Phase 6 - requote hysteresis and preflight

`adjust_orders_to_reward_boundaries` was reworked so normal price adjustment is a preflight-then-cancel flow with hysteresis:

- Fresh book required (`MAX_ORDERBOOK_AGE_SECONDS`); stale books never trigger normal requotes.
- Normal requote gates: `MIN_QUOTE_LIFETIME_SECONDS`, price change >= `REQUOTE_MIN_TICKS`, `REQUOTE_CONFIRMATIONS` consecutive confirmations of the same target (different targets reset the counter), `REQUOTE_COOLDOWN_SECONDS` since the last normal cancel, no inventory/pending SELL, and a safety check on the new target before any cancel.
- Danger cases bypass the gates and cancel immediately: current order became best bid, protection depth gone (best+second size below `min_protection_size_multiplier`), spread over the entry limit, or crossed book. If the new target is unsafe, the order is cancelled only (no re-place, pending reorder recorded).
- If the old order is safe and the new target is unsafe, the old order is kept.
- Pending reorders respect the cooldown, re-check freshness/safety, and are cleared when inventory appears.
- Removed the old `price_deviation_bps`-only trigger from the decision path (config remains for compatibility).

Tests: `tests/test_requote.py` covers stable books, one-tick blips, single confirmations, consecutive confirmations, target-change reset, cooldown, order lifetime, best-bid/protection/crossed danger cancels, old-safe/new-unsafe retention, old-danger/new-unsafe cancel-only, pending reorder cooldown/retry, transient missing books, and inventory blocking.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 129 passed; `git diff --check` clean.

### Phase 7 - set-diff market refresh

`MarketManager` now keeps an explicit `previous_selected_market_ids` selection record plus `get_selected_market_ids()` / `update_selected_market_ids()`, so market selection is never inferred from the active-order set.

`main.py`'s periodic rescan was replaced with a set-diff flow: record previous selection -> rescan -> filter -> compute retained/removed/added -> `OrderManager.refresh_market_selection()`.

`OrderManager.refresh_market_selection()`:

- retained: no cancels, no re-quotes from the refresh itself; existing orders and inventory exits continue.
- removed: `remove_market()` cancels only that market's BUY orders, clears its pending BUY reorders and requote confirmation state, and keeps SELL orders, inventory exit state, and reconciliation state.
- added: `place_market_orders()` runs the full fresh-book entry gate (and skips tokens with inventory or pending exit). A re-added market must pass entry again from scratch.

Tests: `tests/test_market_refresh.py` covers unchanged sets (zero cancels), added-only processing, removed-only BUY cancellation, SELL/inventory retention on removal, pending reorder cleanup, re-added markets with stale books or inventory, empty active orders not counting as all-added, and inventory state survival.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 138 passed; `git diff --check` clean.

### Phase 8 - order confirmation and idempotent reconciliation

- Successful submissions are recorded as `PENDING_CONFIRMATION`; the blocking open-orders verification loop in `place_order` was removed (temporary invisibility is not failure).
- `_confirm_pending_orders()` (run at the top of `check_orders`) confirms pending orders via open orders or trade history within `ORDER_CONFIRMATION_TIMEOUT_SECONDS`; fills found during the window are processed and the order moves to `FILLED`/`PARTIALLY_FILLED`; after timeout with no evidence the order moves to `UNKNOWN`.
- `_reconcile_unknown_orders()` reconciles UNKNOWN orders via trades, open orders, and positions: fills are processed, no-evidence orders become `FAILED`; SELL retries stay blocked until this reconciliation runs.
- Order business fingerprints (token/side/price/size/purpose/generation) block duplicate submissions inside the confirmation window; `REWARD_BUY` vs exit SELL purposes are distinct.
- `CANCEL_PENDING` tracking: successful cancel requests move the order into `cancel_pending_tracking`; fills arriving while the cancel propagates are still processed, and no duplicate BUY is placed at the same price/size.
- Position micro-jitter: `_reconcile_positions_to_inventory` requires `POSITION_CHANGE_CONFIRMATIONS` consecutive confirmations before adjusting a SELL (only for differences < 1 share and > dust); emergency exits and clear growth (> 1 share) bypass the confirmation counter.

Tests: `tests/test_order_reconciliation.py` covers invisible BUY/SELL non-duplication, fingerprint blocking, timeout -> UNKNOWN -> reconciliation, UNKNOWN SELL blocking before reconciliation, UNKNOWN BUY fill reconciliation, cancel-pending fill processing, micro-jitter confirmation, emergency bypass, and purpose-distinct fingerprints.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 147 passed; `git diff --check` clean.

### Phase 9 - dead-code cleanup and lightweight metrics

- Removed the unreachable second implementation of `check_orders` (332 lines of duplicate, divergent order-check logic that sat after the live `return`).
- Removed the unused `math` import from `market_making_strategy.py`.
- Updated stale comments claiming BUY prices are "not normalized" (they are now normalized with side-aware rounding inside `place_order`).
- Completed lightweight metrics: `buys_placed`, `buys_cancelled`, `safety_cancels`, `requotes`, `retained_markets`, `full_fills`, `partial_fills`, `fast_exit_count`, `limited_wait_count`, `emergency_exit_count`, `avg_hold_time_seconds` (running average on flat), `exit_price_loss`, `blocked_reentry_count`, `stale_book_rejections`, `insufficient_exit_depth_rejections`, `pending_confirmation_count`, `unknown_order_count`, `blocked_duplicate_count`, `exit_fills`, `positions_flat`. `get_order_statistics()` now exposes `metrics`.

Tests: `tests/test_metrics.py` verifies metric initialization, increments through a real BUY->fill->FAST_EXIT->SELL flow, reentry/duplicate counters, average hold time, statistics exposure, and the removal of the dead duplicate implementation and unused import.

Commands and results: `python -m compileall -q .` exit 0; `python -m pytest -q` -> 154 passed; `git diff --check` clean.

### Final regression matrix

Added integration-level tests to satisfy the final matrix:

- `tests/test_integration_flow.py`: BUY submit -> full/partial fill -> inventory state -> SELL submit -> SELL confirm -> flat -> cooldown -> reentry (pass/fail on fresh/stale book).
- `tests/test_integration_refresh.py`: market rescan -> retained/removed/added -> corresponding order behavior (zero cancels on unchanged, BUY-only cancel on removed, SELL/inventory preserved, no cancels on retained).
- `tests/test_reward_calculator.py`: reward boundary no longer uses the fixed one-cent inset in the scoring filter.

Full suite: 160 passed; `python -m pytest --cov=. --cov-report=term-missing` -> 58% total coverage (core strategy 82%, config 75%, order manager 59%, risk manager 81%; network entry points intentionally not covered offline).

## Alternatives not implemented (and why)

- New standalone `orderbook_normalizer.py` module: rejected because the task says to establish the entry point in an existing suitable module and to prefer minimal change.
- General state-machine framework for inventory exit: rejected (explicitly forbidden).
- Async/WebSocket/message-queue rewrite: explicitly forbidden.
- New monitoring system (Prometheus): explicitly forbidden; metrics are in-memory counters and logs.

## Risks that unit tests cannot prove

- Real CLOB API ordering, latency, and eventual consistency of open orders/trades/positions.
- Exchange-side rounding rules and minimum order size behavior beyond the documented tick sizes.
- Extreme market gap risk between book snapshots.
- Whether initial conservative parameters are well-calibrated for real reward markets; gray-scale testing is required.
