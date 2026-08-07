# Agent guidelines

This file is binding for any agent or contributor working in this repository.

## Safety

- Never run live trading loops.
- Never read, request, or use the real `.env` contents (private keys, mnemonics, API secrets, proxy wallet addresses, account credentials).
- Never use a real Polymarket account or real funds.
- All trading tests must use mocks, fake clients, static orderbook fixtures, controllable clocks, local temporary SQLite databases, and simulated order/position responses.
- Missing private keys, network access, or real APIs are not blockers for test work.

## Architecture

- Do not change the overall architecture: keep `main.py`, `market_manager.py`, `market_making_strategy.py`, `order_manager.py`, `risk_manager.py`, `api_client.py`, `http_orderbook_client.py`, `redis_orderbook_client.py`, `orderbook_data_service.py`, and the SQLite local cache.
- Keep the market-scan mechanism, the order-management mechanism, the buy-at-second-bid / reward-boundary strategy direction, and the fill-then-SELL flow.
- Prefer minimal, conservative changes. No new production dependencies, no async/WebSocket/message-queue rewrite, no new database or runtime service, no large state-machine framework, no machine learning.

## Process

- Every phase runs, in order:
  - `python -m pytest -q`
  - `python -m compileall -q .`
  - `git diff --check`
- Core trading-logic changes must come with regression tests.
- Do not skip or delete tests to make results pass.
- Do not merge automatically.
- Do not create PRs against the upstream repository.
