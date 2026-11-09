# 四工厂产出策略质量审查 · 2026-11-01

审查范围：`strategy_quality_reports` 中 2026-10-30 起的 3,931 份 submission 报告，以及历史上所有 `passed=1` 的报告；数据库 `~/.aiask/data/db/akshare_mcp.sqlite3`，只读查询。

## 0. 运行状态

- 四运行体 supervisor 在 2026-11-01 19:24:45 收到 SIGTERM 停止（来源未知，`execution_records.jsonl` 只记录了信号）。
- 19:39:39 按上次参数重启（`--strategy-dispatch-run --strategy-dispatch-default-universe --strategy-dispatch-default-universe-limit 200 --strategy-interval 300 --no-incubation-catchup`），日志 `~/.aiask/logs/four_runtime_supervisor/supervisor-20261101.log`。
- 重启后四个运行体均有心跳，Strategy Factory 已完成 2 轮 run（status=success）。SignalTracker、FactorScheduler sidecar 和质量观察进程一直在运行。

## 1. 结论

**目前没有一个产出策略具备进入孵化的证据。** 进入孵化的 378 份报告全部来自同一个因子，回测 Sharpe 全部 ≤ 0，模拟盘零成交。表现最好的交易规则候选被一个永远测不出来的门禁挡住。

## 2. 发现（按严重程度）

### F1 `passed` 字段不表示通过
`_submitter_policy.py:284-285` 把 `passed` / `passed_strict` 无条件写成 `True`（注释："no policy outcome here may reject a candidate"）。结果是历史 `passed=1` 的 1,230 行中有 852 行 `admission_stage=rejected`。任何读 `passed` 的消费方都会把被拒策略当成通过。真实裁决只在 `quality_gate.admission_stage` 和 `admission_evaluations.*.passed` 里。

### F2 孵化准入全部依赖同一份因子池统计，不是逐策略验证
- 378 份 `admission_stage=incubation` 报告，`factor_name` 全部是 `gp_classic_momentum_slope_60_20_3_mut`（`ts_rank(momentum_60d,60) - zscore(momentum_20d,20)`）。
- 378 份的 `wf_ic_ir=0.5941`、`pkf_ic=0.1324`、`bootstrap_ci_lower=0.076`、`param_sensitivity=0.0464` 完全相同。它们由 `runner.py:2960-3034` 从 `factor_pool_active.validation_summary` 拷贝而来。
- 标签与内容不符：`walk_forward.oos_rank_ic_mean` 和 `purged_kfold.oos_rank_ic_mean` 都是同一个全样本 `rank_ic_mean`。名为 purged k-fold 的指标从未做过 k 折，也没有 walk-forward。
- 证据过期：该因子的 `factor_ic_history` 只有 60 个日期（2026-07-02 至 2026-09-23），`kline_1d` 已到 2026-10-30，之后 5 周的数据没有参与验证。因子自身记录了 `lookahead_risk_detected` 和 `multiple_testing:white_reality_check_weak / hansen_spa_weak`（white p=0.695，SPA p=0.54）。

### F3 进入孵化的策略回测为负或无交易
在 378 份孵化准入中，回测指标如下：
- Sharpe：350 份有值，最大 0.0，中位数 −0.73
- 总收益：中位数 −0.97%
- 成交：162 份 0 笔，28 份无成交字段，188 份约 80 笔

报告里同时写着 `Factory policy: backtest trade_count 0 < 4`，但仍然被准入（见 F1）。

### F4 孵化池的模拟盘没有产生成交
- 367 个已准入策略有孵化指标，最新 `metric_date=2026-10-30`。NAV 介于 0.99991 和 1.0 之间，`total_trades` 合计为 0。
- `strategy_trade_positions` 共有 77 笔已平仓，平均收益 −4.06%，胜率 42.9%。另有 648 笔 `pending_entry`、118 笔 `orphaned_exit`、150 笔 `pending_exit` 堆积。
- `strategy_promotion_reviews` 共 276 条：124 条 deprecate，152 条 observe，0 条晋升。

### F5 重复生产
3,931 份报告只对应 368 个不同的（名称, Sharpe, 收益, 成交数）组合。同一个 `002815·事件结构突破·20日`（Sharpe −1.41、1 笔成交）提交了 270 次，`002815·…·60日` 提交了 267 次，`多因子seed_volume_ratio主导(60日)`（0 成交）提交了 143 次。dedup 判定为 `spawn_revision_from_existing`（`tested_object_changed`）后照常重测，同一结果被反复写入。约 90% 的算力花在重复样本上。

### F6 交易规则通道的唯一出口被一个测不出来的门禁关闭
- `trade_rule_validation` 共 2,497 份，其中 238 份满足 post-cost Sharpe ≥ 0.1 且成交 ≥ 4。
- 这 238 份的孵化门禁全部列出了 `missing_parameter_perturbation_trade_stability`（240/240）。另外有 217 份报 profit_factor 不达标。
- 参数扰动研究的记录是 `variants_run=0, reason=baseline_trades_unavailable`。这个指标从未被测出，所以这条通道不可能有策略通过。按项目规则，"缺失"在孵化级硬失败是正确的。需要修的是扰动研究本身没有运行，不是门禁阈值。
- 最好的候选是 `000006+000007·动量突破·11日`：post-cost Sharpe 0.92、31 笔成交、收益 8.6%、回撤 4.1%。同一结果有 4 份重复提交，只覆盖 2 只股票，样本很窄，停在 `research` 级。

### F7 未测量写成 0（违反「诚实优先」规则）
- 317 份 0 成交回测写了 `sharpe_ratio=0.0, total_return=0.0, max_drawdown=0.0`，应写 `None` 加原因码。
- 280 份 `bootstrap_ci.ci_upper=0.0 < ci_lower`（例如 lower 0.076、upper 0.0），说明 upper 未测量却写了 0。
- 140 份 `oos_rank_ic_std=0.0`、`oos_ic_mean=0.0`，同样是没有测量的字段被 0 填充（来自 F2 的拷贝路径）。

## 3. 建议（未改动代码，等待确认）

1. `passed` 应与 `admission_stage` 一致，或者让下游改读 `admission_stage`。F1 是有意设计（注释写明 A 类门禁已移除），改动需要你决定。
2. 因子池拷贝路径应标注 `measurable=False`，或者对每个策略真正跑 walk-forward / purged k-fold。同时在 IC 历史过期时（最新 IC 日期落后 K 线超过 N 个交易日）拒绝使用。
3. 修复参数扰动研究在 `baseline_trades_unavailable` 时不运行的问题。这是交易规则通道唯一的出口。
4. dedup：同一 `tested_object_hash` 且指标完全相同的结果不应再次写报告。
5. 未测量字段写 `None`，不写 `0.0`。
6. 查明孵化模拟盘为什么零成交，以及 648 笔 `pending_entry` 为什么堆积（SignalTracker 18:30 运行后可复核）。

## 4. 已修复（2026-11-01 20:13 上线）

### 第 5 条：参数扰动研究没跑（F6）
- 根因：`_test_one` 往 `primary_results` 里放的是引擎的 `data` 本体，扰动研究建基线索引时却又取了一层 `data`，每只股票都拿到 `{}`，所以 3,943/3,943 份报告都是 `baseline_trades_unavailable`。
- 修复：`BacktestFilter._per_code_baseline_metrics` 直接读平铺的 payload。变体改为扰动 baseline 实际使用的参数（经 `_ensure_strategy_params` 补默认值后的参数），执行假设（佣金、ADV 限制等）原样透传，不参与扰动。
- 线上验证：修复后的 47 份报告全部 `variants_run=8`、`reason=None`，`parameter_perturbation_trade_stability` 47/47 有值（范围 0.0–1.0）。

### 第 2 条：因子池证据冒充逐策略验证（F2）
- 修复：`_factor_pool_validation_report_from_params` 不再覆盖策略自己的 `walk_forward` / `purged_kfold` / `bootstrap_ci` / `statistical_metrics`。因子级 IC 挂在 `factor_pool_evidence`（`scope=factor_level_not_strategy_level`，`binding=advisory`）下，仅供审计。策略自身验证失败（`None`）时保持缺失，不再用因子 IC 补位。
- 线上验证：修复后 `wf_ic_ir` 按策略各不相同（−1.26 到 0.88），不再全是 0.5941。修复窗口内 0 份进入孵化（此前同类策略靠拷贝来的统计量进入孵化）。

测试：`packages/strategy-factory` 1175 passed, 3 skipped。
