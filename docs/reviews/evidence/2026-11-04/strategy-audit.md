# 策略工厂专项审查（2026-11-04）

范围：当前工作树 `packages/strategy-factory`，策略生成、回测证据、验证/风险报告、准入与提交。只读审查；没有修改业务代码或现有未提交改动，没有启动工厂，没有读取/写入生产数据库。对当前工作树（包含用户未提交质量修复）审查。

## 结论

策略工厂已形成完整的研究编排和候选管理能力，但**当前验证证据尚不能整体作为可靠的交易业绩/样本外能力证明**。本次确认 5 个高置信问题，其中前 3 个直接影响统计和风险证据口径。当前生产流程明确使用 observe-first：回测失败候选仍可进入零资本观察，这是现行设计，不能把观察数量/提交数/运行 success 当成优质策略数量。

## 已证实问题

### S1 [P1] 把“买卖事件”当成每日持仓，验证收益与风险报告描述了另一个策略

主要位置：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/_panels_evaluation.py:170`、`:174`–`:177`；同类 family_returns 计算在 `:88`。接口证据：`/Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/backtest/strategy_base.py:15` 明确 `1=买入,-1=卖出,0=持有`。

`aligned_signals * aligned_returns` 将持仓期间的 0=持有当成空仓，把卖出事件 -1 当成做空；`latest_signal != 0` 则把最近发生卖出也当成当前持仓，并漏掉持续持仓。得到的 strategy_returns 被 `_run_validation_report` 传入 FactorValidationPipeline（用于策略 Sharpe/多重检验/评分），被 `_run_risk_report` 用于 VaR；holdings 被用于压力测试。

内存复现：使用真实 `StrategyRegistry` 的 `buy_and_hold`，3 只股票各 220 根每天上涨 0.1% 的 K 线，最近 120 日实际持有收益 +12.7429%，返回 panel 的 120 个 strategy_returns 全 0，holdings=[]。这不是“策略无仓位/无风险”，而是事件与状态混淆。

建议：风险/策略收益复用真实回测逐日权益与持仓（包含交易成本、T+1、现金和仓位），或建立与引擎一致的持仓状态机；保留原信号仅用于信号预测能力 IC，严格区分 signal IC 与 strategy returns。

### S2 [P1] 多标的验证面板按数组尾部拼接，未按交易日期对齐

位置：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/_panels_evaluation.py:186`–`:189`；相关 beta 计算 `:205`–`:209`。

各股独立取最近 220 根，仅裁剪到相同长度后 column_stack，日期信息在读取 close/volume 后完全丢失。停牌、缺 bar、同步时间不一致、IPO 数据较短时，不同日期被当作同一天计算横截面 IC、组合收益、协方差与 beta，后续 WF/purged-kfold 再规范也无法修复这个输入。

内存复现：3 个股票分别使用 2021、2023、2025 年互不相交的 220 日数据（共同日期数 0），函数仍输出 `return_panel.shape=(120,3)`、used=3，无日期/错位错误。

建议：以收益归属交易日做索引并显式对齐；缺失/停牌按交易规则处理，并传递 actual dates、coverage、skipped/failed 到报告；共同样本不足时返回不可测。

### S3 [P1] target_layer_oos_return 直接别名全期回测收益，交易准入中的 OOS 证据没有独立测量

主要位置：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/backtest_filter.py:2556`；另一路赋值 `:1084`。消费位置：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/submission_gate/runner.py:2424`、`:2465`。

Gate-2 `_test_one` 从 DB 取最多 500 根 K 线后整段交给 engine（`:2853`、`:2877`–`:2887`）；`_derive_trade_validation_metrics` 把 target_metrics.total_return 直接写成 target_layer_oos_return，不携带 holdout 起止/训练截止日期，也没有时间切分。随后交易准入以它检查 `target_layer_oos_return_min`。

内存复现：仅提供 target 全期 total_return=0.42（没有任何 OOS 样本），得到 target_layer_oos_return=0.42。这不意味着系统完全没有 walk-forward/purged-kfold：另有因子/信号验证流程，但**该交易收益字段**并非独立 OOS 交易收益，不能以它证明交易策略泛化。

建议：单独定义并回放未参与选股/调参/排名的 holdout，保存 split/训练截止/候选冻结时间；没有 OOS 交易回放时指标留空，或将现字段改名 target_backtest_return 并停止作为 OOS 门槛。

### S4 [P2] Gate-2 基准提取沿用旧嵌套结构，牛熊状态始终退化 neutral

位置：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/backtest_filter.py:3305`–`:3313`。上游展平发生在 `:2959`–`:2962`，primary_results 直接是引擎 data payload。

这里又读 `pr.get('data')` 得到空字典，即使引擎有真实 benchmark_return/buy_hold_return，也被当作缺失再补为 0。导致 regime 恒 neutral、超额收益变绝对收益、相对 Sharpe/综合评分失真。

内存复现：真实 `_test_one` 配内存 DB/固定返回的回测引擎，分别输入基准 -30%、+30%，两次均 `regime='neutral'`、同一 multi_score=93.0；基准本应分别触发 bear/bull。当前 observe-first 中此结果不直接淘汰候选，影响证据、评分、报告/下游消费者；不要写成它目前直接放行实盘。

建议：按展平 payload 读取；缺基准时明确 unavailable，不可补0；覆盖不同基准且相同策略收益的行为测试。

### S5 [P2] 将所有数值0当“缺测”，误拒绝真实稳定的参数扰动结果

位置：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/submission_gate/runner.py:2734`–`:2735`；真实0的生成 `:3814`–`:3823`。

分类函数无视 metric source，把所有 `abs(value)<1e-12` 标 missing。但 perturbation_trade_stability=1.0 是合法真实结果，换算 param_sensitivity=1-stability=0.0，恰代表最稳定。

内存复现：WF IC IR=.35、PKF IC=.03、bootstrap CI下界=.02、真实稳定性=1.0，`_run_statistical_gate` 得到 passed=False，唯一缺项 `missing_param_sensitivity`，来源仍正确写为 `backtest_metrics.parameter_perturbation_trade_stability_inverse`。因此最稳定的参数被归为无证据。

建议：缺测用 None/reason/available/来源标记，0应按其指标含义评估。当前现有测试将所有0视为missing写成期望，测试通过并未覆盖经济语义正确性。

## 能力与边界

| 环节 | 当前已有能力 | 实际边界 |
|---|---|---|
| 研究输入 | 快照、因子池、市场事件、研究任务、theme/factor研究 | 数据就绪、覆盖和外部LLM成功仍是前提；不等于每条family都有真实原生数据 |
| 候选生成 | 多策略family、参数分布、自适应/历史反馈、个股矩阵、LLM/DSL、去重/重试/预算 | 候选结构/语义可执行与经济有效性是两种证据；不可用“生成成功”代替alpha |
| 回测 | 单标的与共享资金组合、成本/T+1/交易约束、真实交易闭环统计、参数扰动 | 本次S3/S4使OOS/相对市场证据口径不可靠；扰动仅在首个成功标的上跑有限变体 |
| 验证与风控 | WF、purged-kfold、bootstrap、多重检验、VaR/stress、报告来源字段 | S1/S2先污染了输入；这套“算法齐全”不等于验证真实持仓收益 |
| 准入 | 单一AdmissionAuthority、结构hard reject、正式/观察/延后lane、状态和审计 | 当前CandidatePipeline把全部候选标记observe-first后提交，回测pass只作为证据；运行success/提交数非质量通过数 |
| 工程 | 运行契约、schema ownership、API boundary、独立MCP、任务板、repository、质量记录 | 约束/兼容层很多，关键文件非常大（backtest_filter约3400行、submission_gate runner约4500行），口径修复需端到端行为验证 |

当前观察模式证据：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/services/candidate_pipeline.py:109`–`:120`。这是代码明确设计，不作为本次缺陷。

## 已修复项（当前工作树，不能再次报成未修缺陷）

- `_submitter_actions/runner.py` 不再用因子池验证统计覆盖策略自身验证，因子证据放 advisory `factor_pool_evidence`。
- admission_authority 新候选结构hard reject优先于所有新入场lane（refresh保留原状态是明确语义）。
- 参数扰动修复了baseline扁平data解析、实际运行参数/defaults、元数据占用变体、交易数口径。

这些修复有未提交改动，本审查保留原状，不代表已部署到运行进程/历史数据已重算。

## 验证

- `/opt/miniconda3/envs/aiask/bin/python -m pytest -q`，cwd `packages/strategy-factory`：**1228 passed, 3 skipped in 27.51s**。
- 未提交质量修复相关7文件定向：**102 passed in 2.91s**（为上述全套的子集，不相加）。
- 包 conftest 明确安装 hermetic helper；复现脚本同样先安装 hermetic，隔离 HOME/.env/runtime/SQLite。
- `/Users/mac/Desktop/张勇/AIASK/docs/reviews/evidence/2026-11-04/strategy_repro.py`：S1/S2/S3/S5行为复现。
- `/Users/mac/Desktop/张勇/AIASK/docs/reviews/evidence/2026-11-04/strategy_benchmark_repro.py`：S4真实 `_test_one` 行为复现。
- 没有生产数据库质量分布测量，没有外部数据源/LLM网络验证，没有工厂长期运行验证；这些由主审查其他分工补足。
