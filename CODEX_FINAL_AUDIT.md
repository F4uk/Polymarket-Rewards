# CODEX_FINAL_AUDIT

Baseline: `e46eef240b4b8c17f98a219b3d75ac20c85a8143`
Final head: see commit list in the PR / progress file.

## 1. Final call-chain audit (25 questions)

Status values: `PASS`, `FAIL`, `PARTIAL`, `NOT_APPLICABLE`.

### 1. 是否仍有交易决策依赖订单簿固定数组顺序？ - PASS

- File/class/method: `market_making_strategy.py` -> `normalize_orderbook()` (single entry), `MarketMakingStrategy.calculate_mid_price`, `calculate_actual_buy_price`, `can_place_buy_order_safely`, `evaluate_token_entry`; `market_manager.py` spread filter; `order_manager.py` requote/inventory paths.
- Tests: `tests/test_orderbook_normalization.py::test_all_ordering_combinations_are_equivalent`, `test_strategy_trading_decisions_are_order_independent`.
- Evidence: all trading decisions consume `normalized_bids`/`normalized_asks` (sorted, aggregated); the only index accesses are on the sorted normalized lists.
- Residual risk: none for array ordering; raw API ordering remains irrelevant by construction.

### 2. 是否仍有浮点配置被整数化？ - PASS

- File/method: `config.py` -> `orderbook_update_interval_seconds`, `order_check_interval_seconds`, `price_deviation_threshold_bps` now return floats.
- Tests: `tests/test_config.py::test_float_configs_are_not_truncated`.
- Evidence: `0.5` and `0.0001` survive parsing.
- Residual risk: none.

### 3. 是否仍有 SELL 使用向下取整？ - PASS

- File/method: `market_making_strategy.py::round_price_to_tick(..., side="SELL")` uses Decimal ceil; immediate-exit SELL uses `immediate_exit_price()` which intentionally floors to the best executable price (documented and tested).
- Tests: `tests/test_rounding.py::test_sell_rounds_up`, `test_immediate_exit_price_never_above_best_bid`.
- Residual risk: exchange-side rounding rules beyond documented ticks are not unit-provable.

### 4. 是否仍有无法解释的固定一美分奖励缩进？ - PASS

- File/method: `market_making_strategy.py::reward_spread_decimal`; `reward_calculator.calculate_q_one_q_two`.
- Tests: `tests/test_rounding.py::test_reward_boundary_has_no_fixed_one_cent_inset`, `tests/test_reward_calculator.py`.
- Evidence: the only inset is `REWARD_BOUNDARY_INSET_TICKS * tick_size` (default 0).
- Residual risk: none.

### 5. 是否仍有固定 0.05 作为唯一退出规则？ - PASS

- File/method: `order_manager.py` tiered exit (`FAST_EXIT`/`LIMITED_WAIT`/`EMERGENCY_EXIT`) uses config thresholds; `HEDGE_SELL_MAX_BID_GAP` survives only in the legacy public `place_hedge_sell()` helper, which is not part of the main flow.
- Tests: `tests/test_inventory_exit.py`.
- Residual risk: legacy public helper remains for interface compatibility.

### 6. 是否仍有完全成交后无条件补 BUY？ - PASS

- File/method: `main.py` no longer calls `replace_filled_order`; `OrderManager.maybe_reenter_markets` requires flat + cooldown + full entry.
- Tests: `tests/test_reentry_block.py::test_full_fill_does_not_rebuy`, `tests/test_integration_flow.py`.

### 7. 是否仍有部分成交后补完整 BUY？ - PASS

- File/method: `order_manager.py::check_orders` partial-fill path cancels the remainder and never re-submits the full size.
- Tests: `tests/test_reentry_block.py::test_partial_fill_cancels_remaining_and_does_not_rebuy`.

### 8. 是否仍有库存未清而恢复 BUY？ - PASS

- File/method: `OrderManager.has_inventory_or_pending_exit`, `maybe_reenter_markets`, `place_market_orders` guard.
- Tests: `tests/test_reentry_block.py`, `tests/test_market_refresh.py::test_readded_market_with_inventory_blocks_buy`.

### 9. 是否仍有先撤旧安全订单再验证新订单？ - PASS

- File/method: `order_manager.py::adjust_orders_to_reward_boundaries` preflight-then-cancel.
- Tests: `tests/test_requote.py::test_old_safe_new_unsafe_keeps_old_order`, `test_old_danger_new_unsafe_cancel_only`.

### 10. 是否仍有市场重扫全撤全挂？ - PASS

- File/method: `main.py` set-diff refresh; `OrderManager.refresh_market_selection` / `remove_market`.
- Tests: `tests/test_market_refresh.py::test_unchanged_set_zero_cancels`, `tests/test_integration_refresh.py`.

### 11. 是否仍用过期订单簿挂新 BUY？ - PASS

- File/method: `MarketMakingStrategy.evaluate_token_entry` (age check), `OrderManager.maybe_reenter_markets` / `place_market_orders` / pending reorder.
- Tests: `tests/test_market_entry.py::test_stale_book_rejects`, `tests/test_requote.py::test_transient_missing_book_no_blind_cancel`.

### 12. 是否仍用 0.01/0.99 虚构保守订单簿？ - PASS

- File/method: `use_conservative_price` removed from `calculate_order_prices`; no fabricated books.
- Tests: `tests/test_market_entry.py::test_no_conservative_price_parameter`, `test_place_market_orders_skips_without_fresh_book`.

### 13. 是否仍把 SELL 传播延迟误判为失败并重复提交？ - PASS

- File/method: `place_order` no longer blocks on open-orders verification; `_confirm_pending_orders`, fingerprints, `_submit_inventory_sell` blocking statuses.
- Tests: `tests/test_inventory_exit.py::test_sell_invisible_in_open_orders_no_duplicate`, `tests/test_order_reconciliation.py::test_confirmation_window_blocks_same_fingerprint`.

### 14. 是否仍因持仓 API 暂时返回零而放弃库存退出？ - PASS

- File/method: `OrderManager._reconcile_positions_to_inventory` keeps trade-confirmed state; `check_inventory_exits` uses trade history.
- Tests: `tests/test_inventory_exit.py::test_position_api_delay_does_not_abandon_exit`.

### 15. 是否仍在严重亏损时无限等待保本？ - PASS

- File/method: `_process_inventory_exit` emergency thresholds and `EXIT_MAX_HOLD_SECONDS`.
- Tests: `tests/test_inventory_exit.py::test_severe_loss_immediate_emergency`, `test_limited_wait_timeout_goes_emergency`.

### 16. 是否仍可能卖出超过实际持仓？ - PASS

- File/method: `_submit_inventory_sell` uses remaining - pending; `_confirm_inventory_sells` clamps sold.
- Tests: `tests/test_inventory_exit.py::test_never_sells_more_than_confirmed_inventory`.

### 17. 是否仍可能重复处理同一成交？ - PASS

- File/method: `_handle_buy_fill` cumulative `processed_fill_size` deltas; `_confirm_inventory_sells` `processed_sell_size`.
- Tests: `tests/test_inventory_exit.py::test_same_fill_not_double_processed`.

### 18. 是否仍可能因持仓微小抖动频繁重挂 SELL？ - PASS

- File/method: `POSITION_CHANGE_CONFIRMATIONS` in `_reconcile_positions_to_inventory`.
- Tests: `tests/test_order_reconciliation.py::test_micro_position_jitter_requires_confirmations`.

### 19. 启动时是否先处理已有库存？ - PASS

- File/method: `OrderManager.reconcile_startup` runs before scanning/placing in `main.py`.
- Tests: `tests/test_reentry_block.py::test_startup_reconciles_inventory_before_buy`.

### 20. retained 市场是否真的不会因重扫撤挂？ - PASS

- File/method: `refresh_market_selection` retained set performs no cancels; `remove_market` only on removed.
- Tests: `tests/test_market_refresh.py::test_unchanged_set_zero_cancels`, `test_empty_active_orders_does_not_mean_all_added`.

### 21. 是否引入了新的生产依赖？ - PASS

- Evidence: `requirements.txt` unchanged; pytest/pytest-cov are test-only environment tools.

### 22. 是否引入了不必要的状态或抽象？ - PASS

- Evidence: two small dataclasses (`NormalizedOrderbook`, `EntryDecision`), plain-dict inventory state, no framework. See over-optimization audit.

### 23. 是否存在未测试的重要资金路径？ - PARTIAL

- Evidence: `main.py`, `orderbook_data_service.py`, `start_orderbook_service.py`, and the real `api_client`/CLOB HTTP paths are not covered because tests are offline by design (network guard). Order lifecycle logic itself is covered via fakes.
- Residual risk: real API request/response differences and full main-loop orchestration are only covered by the components, not by an end-to-end offline harness.

### 24. 是否存在无法证明安全的行为变化？ - PARTIAL

- Evidence: order/position final consistency and exchange-side matching behavior cannot be proven with mocks; initial parameters are conservative defaults, not calibrated.
- Residual risk: gray-scale trading and simulation are required before live use.

### 25. 是否适合直接实盘，还是必须先做模拟和极小资金灰度？ - NOT_APPLICABLE

- Explanation: this PR is a draft for external review and explicitly does not authorize live trading. The correct deployment path remains simulation + tiny gray-scale; that is a process requirement, not a defect in this change set.

## 2. Over-optimization audit

1. 是否存在可以删除的抽象？ - 否。`NormalizedOrderbook`/`EntryDecision` 均被多个模块实际使用。
2. 是否可以用更少状态完成同样行为？ - 否。库存状态字段均有对应使用点（卖出确认、剩余、冷却、代数）。
3. 是否为了测试而扭曲生产接口？ - 否。测试通过 `__new__` + 实例属性注入时钟/持仓提供者，未改变公开构造函数。
4. 是否重复实现订单簿解析？ - 否。单一 `normalize_orderbook` 入口；`extract_prices` 仅为展示用途。
5. 是否重复实现订单状态？ - 否。订单状态集中在订单记录中；`place_hedge_sell` 保留为公开兼容接口但不在主流程。
6. 是否出现单个辅助类只被调用一次且没有必要？ - 否。
7. 是否出现超过任务范围的重构？ - 否。改动集中在交易决策与资金风险路径。
8. 是否修改了与资金风险无关的代码？ - 否（仅注释/展示层清理与配置暴露）。
9. 是否保留了向后兼容性？ - 是。方法名与 `normalize_price` 默认 BUY 行为保留；配置为增量新增。
10. 是否符合“现有架构内最小改动”？ - 是。未引入异步/WebSocket/消息队列/新数据库/新运行时服务。

## 3. Complexity summary

```text
生产代码新增行数: 2328
生产代码删除行数: 1193
测试代码新增行数: 3179
新增生产类数量: 2 (NormalizedOrderbook, EntryDecision)
新增生产依赖数量: 0
新增配置数量: 18
删除不可达代码行数: 332（check_orders 重复实现）+ 已替换的旧 check_positions_and_hedge 主体
```

## 4. Residual risks

- 单元测试无法证明的实盘 API 差异（真实 open orders/trades/positions 的字段、延迟与最终一致性）。
- 订单与持仓接口最终一致性风险（UNKNOWN 对账依赖可用交易历史）。
- 市场极端跳价风险（快照之间价格可能大幅变动）。
- 初始参数为保守默认值，需要模拟与极小资金灰度校准。
- `main.py`/订单簿数据服务等网络入口模块未纳入离线单元测试。
