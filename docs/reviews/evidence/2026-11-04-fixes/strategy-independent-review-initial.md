# S1–S5 独立复核（2026-11-04）

只读复核当前共享工作树，未修改业务代码或测试。策略修复 agent 同时仍在收尾，行号/选项名可能继续变化。

## 已通过

独立运行 `python -m pytest -q packages/strategy-factory/tests/test_validation_evidence_truth.py packages/strategy-factory/tests/test_gate3_statistical_evidence.py`：**27 passed**（仓库 hermetic）。S2 的双端点日期交集、S3 缺失 OOS 不用全期补齐、S4 展平基准/真实零/不可测区别、S5 零敏感度行为均得到回归证明。

## 仍需处理的实际问题

### 1. S1 风险报告未把真实现金纳入压力损失分母

`_panels_evaluation.py:193–205` 用实际持仓 value 和总权益计算 weight，但 `_run_risk_report` 直接将 holdings 送入 `RiskModel.stress_test`；后者按 sum(value) 作为组合价值，完全不读取 weight。因此只投 10% 仓位的组合仍按满仓损失报告。

hermetic 真实引擎复现：buy_and_hold、max_position_pct=.1，3 只同源波动价格 220 日。实际股票权益权重和 **0.10931798**，现压力损失 **−20.168067%**，同一 beta/冲击对真实组合净值应为 **−2.204732%**。这不是估计 beta 差异，而是现金分母丢失。

建议在面板/风险消费端传入真实组合权益/现金并保持全现金不可测政策（不要假造市场敞口）；测试低仓位与满仓压力损失应按真实敞口缩放。原始 S1 审查验收要求包括现金与仓位，故不宜把 S1 整体结项。

### 2. 新 holdout 回放丢失事件型策略的入场状态，却报告 measured 0

`oos_replay.py:95–109` 将完整暖机历史传给策略生成信号，然后仅跳过 trade_start_index 以前的成交。buy_and_hold 仅在整个数组第0根发 buy，真实 holdout 开始时已经没有 buy；因此 40 日 holdout 被标为 measured、total_return=0.0、trades_count=0，实际上完全没入场。状态型信号也存在“信号生成已模拟暖机持仓、成交引擎却从现金开始”的一致性风险。

同一 hermetic 复现：候选/参数 seal 有效，180 日暖机+40 日 holdout，buy_and_hold 返回 measured 0 且0成交。建议明确暖机只初始化指标、在holdout起点初始化交易状态；不支持正确初始化的策略返回 unavailable/reason，不能把没回放到策略持仓当作实测0。

## 复现材料

`/tmp/aiask_strategy_independent_repro_20261104.py`：先执行根 conftest 安装 hermetic，然后调用真实 `_build_strategy_panels`、`RiskModel.stress_test`、`replay_frozen_holdout`、BacktestEngine。运行输出已核实上述两组数字；无真实DB访问。

## OOS能力措辞边界

当前 seal 是调用方提供的契约，没有看到生产生成/保存冻结证据的完整链路。没有契约返回 unavailable 属于诚实修复；不要在交付中宣称历史策略已经拥有可信 OOS，或新系统已经自动生成独立 holdout。另完整回测/参数扰动仍使用全历史，因此真正“未参与候选选择”的证据还取决于调用方冻结流程，不能仅由 bool 自证。
