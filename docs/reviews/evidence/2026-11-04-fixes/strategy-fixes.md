# 策略修复验收 S1–S5（2026-11-04）

本报告接续原策略修复 agent 的共享工作树，并完成独立复核发现的两处缺陷。未提交 Git、未操作生产数据库、未启动生产工厂；全部验证使用仓库 hermetic 隔离。

## 修复行为

| 编号 | 当前实现及验收 |
| --- | --- |
| S1 | `_panels_evaluation.py` 使用真实 BacktestEngine 每日执行权益计算成本后收益，读取未平仓 round-trip 数量与市值，保留日末持仓；参数族同样走执行引擎。`BacktestEngine` 的 `use_daily_execution_replay=True` 允许内置策略走每日执行分支，而非旧 JIT 汇总。buy-and-hold 最近120日收益大于12%，与真实引擎逐日权益完全对账。 |
| S2 | 面板按规范化的交易区间 `(previous_date, current_date)` 交集对齐；两端都必须一致，避免把停牌后的多日收益当一日收益。不相交日期拒绝测量，缺失单日时同时排除涉及该日的两个不一致区间。失败/跳过代码保留明细。 |
| S3 | 全期收益只保留为 `target_backtest_return`，不能自动成为 `target_layer_oos_return`。新增 `oos_replay.py` 验证调用方提供的候选/组合/执行参数哈希、冻结时间与信息截止、留出区间和排除选择声明，再执行独立区间回放；缺少契约、日期不齐、未成熟等均返回 `None + reason`。回测→候选→submission 的真实路径保留 oos_validation，准入不再用 total_return 补齐。未绑定冻结证据的历史 OOS 数值被拒绝。 |
| S4 | Gate-2 直接读取已经展平的基准结果；真实0保留，None/NaN/非法文本标为unknown，不冒充neutral。−30%/+30%基准产生不同市场状态和评分。 |
| S5 | 有限0值是有效测量；真实扰动稳定性1.0产生敏感度0并正常过门槛。显式 unavailable/available=False/measurable=False 仍保留缺失语义。 |

## 独立复核缺陷的收尾

### S1：现金分母

面板返回 `portfolio_value` 和 `cash_value`；风险消费将实际净值传给 `RiskModel.stress_test` 的可选关键字参数。现金不受股票冲击，损失除以完整净值；coverage继续表示已投资市值中有敏感度证据的占比，不让现金掩盖未知beta。

真实220日引擎案例：10.93%实际股票权重，压力损失从约−20.17%修正为约−2.20%。全现金/所有beta缺失仍不可测。旧 `stress_test(holdings)` 调用保留按持仓市值分母的兼容行为；原独立复现脚本直接这样调用时仍会得到股票子组合损失，验收应使用新的 `_run_risk_report` 消费路径，或显式传面板portfolio_value。

代码：`packages/aiask-quant-core/src/aiask_quant_core/risk_model.py`、`packages/strategy-factory/src/strategy_factory/application/_panels_evaluation.py`。

### S3：暖机交易状态

留出回放明确从现金开始。买入持有没有指标暖机需求，回放从留出起点前一根信号bar开始，于首个留出日开盘建仓，费用和交易约束仍由真实引擎执行。DSL保留完整指标暖机数据，但 `_build_signal_events` 从trade_start_index开始初始化空仓和冷却状态，避免暖机买入吞掉留出入场。

已支持的回放实现：内置buy_and_hold、无状态信号的ma_cross/momentum、会重置交易状态的DslRuleStrategy。其他可能维护内部仓位/冷却状态的legacy实现返回 `holdout_strategy_state_reset_unsupported`，不能宣称measured 0。DSL无入场机会的真实0仍可测量。验证新增买入持有、DSL暖机已买入、unsupported RSI、DSL真实不交易四种行为。

新增/收尾代码：`oos_replay.py`、`packages/aiask-quant-core/src/aiask_quant_core/backtest/dsl_strategy.py`。

## 回归证据

1. 独立复核缺陷与S1–S5定向组：**57 passed in 1.40s**。
   命令：
   ```sh
   /opt/miniconda3/envs/aiask/bin/python -m pytest -q packages/strategy-factory/tests/test_validation_evidence_truth.py packages/strategy-factory/tests/test_gate3_statistical_evidence.py packages/strategy-factory/tests/test_validation_cash_stress.py packages/aiask-quant-core/tests/test_risk_model_stress_test.py
   ```
   日志：`/tmp/strategy_sfix_review_closure.log`。
2. 最终两包及DSL相关集成组：**1473 passed, 3 skipped in 35.95s**。
   命令：
   ```sh
   /opt/miniconda3/envs/aiask/bin/python -m pytest -q packages/strategy-factory/tests packages/aiask-quant-core/tests packages/akshare-mcp/tests/test_reduce_semantics.py packages/akshare-mcp/tests/test_strategy_dsl_semantic_contract.py
   ```
   日志：`/tmp/strategy_sfix_final_packages_20261104.log`。
3. `git diff --check` 通过。

两组有重叠，不能相加。现金专项子任务46 passed被以上最终组合覆盖，详见 `/tmp/aiask_cash_stress_fixes_20261104.md`。

主要新增回归：`test_validation_evidence_truth.py`、`test_validation_cash_stress.py`；更新统计门禁和策略质量测试以修正旧错误预期。原工作区已有参数扰动/准入/观察质量相关修改保留，不能把全部git diff算成本次新增。

## 能力边界

OOS冻结契约仍由调用方提供。本轮没有建立生产自动生成/持久化冻结证据的平台，没有重算历史报告；不能声称历史策略已经拥有可信OOS、生产候选全部具有成熟留出样本或策略已经盈利。真正“排除候选选择”的可信性依赖上游冻结流程，布尔声明本身不是独立证明。无契约和不支持状态重置的策略保留不可测是本次诚实语义修复。

全仓最终集成验收由root执行；本报告的1473项只覆盖上述命令范围。
