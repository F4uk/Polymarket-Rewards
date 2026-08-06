# Polymarket Rewards unattended overhaul - task spec

## Business objective

Reduce unnecessary fills, inventory wear, requote churn, and holding drift caused by the liquidity-reward market-making bot, so reward income is converted into real net income as much as possible. This is **not** a principal-protection or profit guarantee.

Principles:

1. Strict entry, timely exit.
2. Process inventory first after a fill; do not immediately restore BUY.
3. Small controlled losses for fast inventory release are acceptable.
4. Never let a small loss expand while waiting for breakeven.
5. Severe loss, holding timeout, or book deterioration must trigger exit.
6. Cancel immediately only on dangerous changes; normal short-term noise must not cause churn.
7. Never place a new BUY without a reliable orderbook.
8. Never rely on implicit array ordering of bids/asks.
9. Order submission and fill processing must be idempotent.
10. Keep the liquidity-reward market-making direction.

## Hard boundaries

- Target repository: `F4uk/Polymarket-Rewards`, base branch `main`, baseline commit `e46eef240b4b8c17f98a219b3d75ac20c85a8143`.
- Upstream `crazygirl437/Polymarket-Rewards` is reference only. No pushes, PRs, sync, merge, rebase, or pull from upstream.
- No force push, no direct main modifications, no automatic merge.
- No live trading, no real API orders/cancels/positions, no real keys or `.env`.
- No new production dependencies (target: 0).

## Phases and acceptance criteria

### Phase 0 - deterministic test baseline

- `tests/` with fake CLOB/API/orderbook clients, injectable clock, temp SQLite, network guard, static market and orderbook fixtures.
- Orderbook fixtures: bids asc/desc/random, asks asc/desc/random, same-price levels, empty, one-sided, crossed, invalid price/size, different tick sizes, stale data.
- Order fixtures: BUY unfilled/partial/full, SELL partial/full, submission invisible in open orders, positions API returning zero, delayed position updates, repeated polling of the same fill.
- Gate: `compileall`, `pytest`, `git diff --check`; commit `test: establish deterministic trading regression baseline`.

### Phase 1 - unified orderbook parsing

- Single normalization entry with `normalized_bids`, `normalized_asks`, `best_bid`, `best_ask`, `second_bid`, `is_empty`, `is_one_sided`, `is_crossed`, `received_at`, `age_seconds`.
- Rules: numeric coercion, ignore unparseable rows and non-positive sizes, aggregate same price, bids desc / asks asc, detect crossed book, do not mutate input, order-independent.
- Age must use monotonic clock; cache reads must not refresh `received_at`.
- Replace all fixed-index assumptions (`bids[0]`, `bids[-1]`, `asks[0]`, `asks[-1]`) in trading decisions.
- Commit `fix: normalize orderbook price-level parsing`.

### Phase 2 - rounding, reward boundary, config types

- BUY rounds down to tick, SELL rounds up to tick, with `Decimal`; support 0.1/0.01/0.001/0.0001; clamp to legal range; unknown side rejected.
- Remove the unexplained fixed one-cent inset `(rewards_max_spread - 1)/100`; use `REWARD_BOUNDARY_INSET_TICKS * tick_size` (default 0).
- Fix float config truncation (`ORDERBOOK_UPDATE_INTERVAL_SECONDS`, `PRICE_DEVIATION_THRESHOLD_BPS`); every config gets type/default/range validation/warning/safe fallback; invalid values never silently become zero.
- Add the new conservative configs listed in the task; validate relations (time >= 0, confirmations >= 1, non-negative tick counts, depth multiplier >= 1, emergency thresholds >= immediate thresholds, max hold >= passive wait, retry interval > 0, timeout >= retry interval, dust >= 0).
- Commit `fix: preserve float config and side-aware tick rounding`.

### Phase 3 - strict entry and exit-liquidity gating

- Worst-side spread (`max(token_spreads)`), not average. Any required token failing (wide spread, empty/one-sided/crossed/stale book, no second bid, weak protection, insufficient exit depth, lossy exit VWAP, price cliff) rejects the whole market.
- Books older than `MAX_ORDERBOOK_AGE_SECONDS` cannot be used for new BUY or requote. No fabricated `0.01`/`0.99` conservative books.
- Simulate exit: cumulative bid coverage vs `order_size * MIN_EXIT_DEPTH_MULTIPLIER`, exit VWAP, worst fill, tick/bps loss vs planned buy, price cliff, depth multiplier.
- Entry result carries reason codes (`ACCEPTED`, `STALE_BOOK`, `EMPTY_BOOK`, `ONE_SIDED_BOOK`, `CROSSED_BOOK`, `SPREAD_TOO_WIDE`, `NO_SECOND_BID`, `INSUFFICIENT_PROTECTION`, `INSUFFICIENT_EXIT_DEPTH`, `EXIT_VWAP_TOO_LOSSY`, `PRICE_CLIFF`, `INVALID_BOOK`).
- Commit `feat: gate markets by worst-side spread and exit liquidity`.

### Phase 4 - tiered inventory exit

- Simple per-token inventory state: market_id, token_id, entry_price, confirmed_filled_size, remaining_size, first_fill_at, last_action_at, state, sell_order_id, sell_order_status, last_known_best_bid, processed_fill_size.
- States `FAST_EXIT`, `LIMITED_WAIT`, `EMERGENCY_EXIT`, `FLAT`.
- FAST_EXIT: immediate executable SELL at best bid when loss within `EXIT_IMMEDIATE_MAX_LOSS_TICKS` and `EXIT_IMMEDIATE_MAX_LOSS_BPS`; immediate SELL must not round above best bid.
- LIMITED_WAIT: passive near-cost SELL bounded by `EXIT_PASSIVE_WAIT_SECONDS`, no tick-level churn, no duplicate submits.
- EMERGENCY_EXIT: cancel passive SELL, immediate SELL at current best executable bid, process partial fills and continue, never wait for breakeven, never oversell confirmed inventory, short confirmation cooldown.
- Position API returning zero must not abandon trade-history-confirmed inventory; use cumulative fill deltas; same fill never double-counted.
- Commit `feat: add time-bounded tiered inventory exit`.

### Phase 5 - no BUY until flat

- Unified `has_inventory_or_pending_exit(token_id)`: confirmed position > dust, active SELL, PENDING_CONFIRMATION SELL, UNKNOWN SELL before reconciliation, unfinished inventory state, position not yet confirmed flat, reentry cooldown, unprocessed fill delta.
- Full fill: remove BUY, process delta, start inventory exit, never unconditional re-buy.
- Partial fill: cancel remaining BUY (reconcile if cancel fails), process only new delta, never re-submit the full BUY.
- Startup order: reconcile orders/positions/inventory BEFORE scanning and placing new BUY.
- RiskManager exposure updates must be idempotent and never negative.
- Commit `fix: block buy replenishment until inventory is flat`.

### Phase 6 - requote hysteresis and preflight

- Normal requote requires fresh book, complete price calculation, market entry pass, BUY safety check, `MIN_QUOTE_LIFETIME_SECONDS`, change >= `REQUOTE_MIN_TICKS`, `REQUOTE_CONFIRMATIONS` consecutive confirmations of the same target, `REQUOTE_COOLDOWN_SECONDS`, no inventory/pending SELL, different target price.
- Preflight before cancel: read book -> normalize -> freshness -> new price -> entry -> safety -> thresholds -> cancel -> cancel reconciliation -> place.
- Danger cases cancel immediately: real best-bid position, protection depth gone, safety violation, price cliff, spread over limit, crossed, market removed. If new target unsafe: cancel only.
- Pending reorder has cooldown, re-runs full entry, is cleared when market removed or inventory exists.
- Commit `fix: add hysteresis and preflight checks to requoting`.

### Phase 7 - set-diff market refresh

- Maintain `previous_selected_market_ids` and `new_selected_market_ids`; compute retained/removed/added.
- Retained: keep safe orders, normal safety adjustment only, continue inventory exits.
- Removed: cancel that market's BUY, clear its pending reorders, keep SELL/inventory/reconciliation state.
- Added: full fresh entry before BUY; re-added markets must re-enter from scratch.
- Market selection must not be inferred from the active-order set.
- Commit `fix: refresh selected markets with set-diff updates`.

### Phase 8 - order confirmation and idempotent reconciliation

- Order statuses: `SUBMITTED`, `PENDING_CONFIRMATION`, `LIVE`, `PARTIALLY_FILLED`, `FILLED`, `CANCEL_PENDING`, `CANCELLED`, `FAILED`, `UNKNOWN`.
- Successful response with order id is recorded immediately as PENDING_CONFIRMATION; temporary invisibility is not failure; bounded confirmation via open orders/trades/positions at `ORDER_CONFIRMATION_RETRY_SECONDS` until `ORDER_CONFIRMATION_TIMEOUT_SECONDS`; timeout -> UNKNOWN; UNKNOWN SELL must reconcile before any retry; retry has cooldown.
- Business fingerprint: token_id, side, normalized price, normalized size, purpose (`REWARD_BUY`, `FAST_EXIT`, `LIMITED_WAIT_EXIT`, `EMERGENCY_EXIT`), inventory generation.
- Cancel success -> CANCEL_PENDING until confirmed; continue processing fill deltas; no duplicate same-price/size BUY; no infinite cancel spam.
- Position micro-jitter requires `POSITION_CHANGE_CONFIRMATIONS` consecutive confirmations before adjusting SELL (exceptions: emergency, confirmed oversell, new fills, order ended).
- Commit `fix: reconcile delayed orders without duplicate execution`.

### Phase 9 - cleanup and metrics

- Remove unreachable code after `return`, duplicate order-check implementations, superseded helpers, wrong comments, unused locals.
- Lightweight in-memory/log metrics: new BUY count, BUY cancels, safety cancels, normal requotes, retained markets, full/partial fills, FAST_EXIT/LIMITED_WAIT/EMERGENCY_EXIT counts, average hold time, exit price loss, blocked reentry count, stale-book rejections, exit-depth rejections, PENDING_CONFIRMATION count, UNKNOWN count, blocked duplicates.
- Commit `refactor: remove dead order paths and add execution metrics`.

## Final verification

```text
python -m compileall -q .
python -m pytest -q
python -m pytest --cov=. --cov-report=term-missing
git diff --check
git status --short
git log --oneline --decorate -15
```

Final audit must answer all 25 audit questions and all 10 over-optimization questions with evidence, and the repository must end in a Draft PR to `F4uk/Polymarket-Rewards:main` titled `Reduce unnecessary fills, inventory loss, and requote churn`, with `READY_FOR_EXTERNAL_REVIEW` status.
