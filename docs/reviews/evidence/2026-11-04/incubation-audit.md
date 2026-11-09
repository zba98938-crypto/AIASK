# 孵化工厂与 SignalTracker 审查 — 2026-11-04

范围：当前工作区代码中的 IncubationFactoryRunner → 宿主公开 Adapter → Strategy Factory canonical runtime/orchestrator，SignalTracker canonical runtime，前向收益与晋级相关服务。只读业务审查；未启动工厂、未访问或修改生产数据库、未修改项目文件。复现只使用内存替身或 hermetic 临时 SQLite。

## 结论

孵化不是空壳：有 intake、策略信号与 paper order、前向收益、多窗口指标、paper 结算、持仓清理、执行审计、预测 outcome/horizon/calibration 的真实实现；SignalTracker 有独立 execution universe、forward-return 回填、NAV/metrics、pipeline、风险和生命周期扫描。但迁移后的 canonical 孵化入口并不执行其文档声称的完整循环，且 dry-run、错误状态和统计顺序存在可复现缺陷。现有测试通过不能证明完整运行闭环。

## 确认的问题

### I1 / P1 — canonical 孵化周期丢失报告、反馈、pipeline 与加速处理

定位：
- `/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/incubation/orchestrator.py:535`：完成 verification/horizon 后直接进入 accelerator、alerts、audit；整个 run_cycle 没有调用 pipeline、generate_hit_rate_report 或 write_feedback。
- `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/integration/strategy_factory_support.py:198`：真实宿主 accelerator 有 evaluate_batch，因此恒定返回 skipped / evaluate_batch_requires_strategies_and_verifications。
- 同文件 `:86` 调用不存在的 `HitRateReporter.generate_report`；`:97` 向只接受 `(db, report)` 的 `FeedbackWriter.write` 传 `(db, strategy, verification)`。
- `/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/runtime/incubation.py:106` 只补上空 `pipeline={}`，并非执行业务。
- 可对比仍保留但不由 public run_once 执行的旧实现 `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py:497` 至 `:538`。

触发：标准脚本 `scripts/factories/run_incubation_factory.py` 的 once/daemon 默认路径。
影响：孵化自身不产出报告/feedback_written、family cooldown/freeze 等报告反馈；自动加速恒为零；pipeline 只能依赖另行运行的 SignalTracker 等调用者，四工厂进程启动并不表示这些闭环阶段已执行。MetricsRecorder 仍可写每日指标，不能夸大为“所有反馈都不存在”。

验证：真实公开 Adapter + canonical runtime，内存追踪 pipeline/report/feedback/accelerator 的 await_count 全为0，周期仍 completed。直接调用真实 Adapter 两个报告/反馈 hook，分别得到 AttributeError 和 TypeError（没有 DB 操作）。修复时需同时恢复阶段编排和纠正 adapter 参数契约。

### I2 / P1 — --dry-run 仍会创建信号/订单，且默认 run_once 可能进行 paper 结算

定位：
- `/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/runtime/incubation.py:83`：run_once 默认 dry_run=False，并不继承 build_incubation_runtime(dry_run=True)。
- 同文件 `:165` daemon 调用 run_once(trigger="daemon") 不传 dry_run。
- `/Users/mac/Desktop/张勇/AIASK/scripts/factories/run_incubation_factory.py:166` CLI 同样不传。
- `/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/incubation/orchestrator.py:190` 和 `:274`：即使显式 dry_run=True 仍执行 intake 和 generate_signals。
- `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/signal_generator.py:72` 实际调用 sync_signals_to_orders，既写 strategy_signals 又写 paper_orders。
- `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py:727` settle_orders 自身没有 dry_run 保护。

触发：CLI --dry-run 或 build_incubation_runtime(dry_run=True).run_once()；显式 run_once(dry_run=True) 仍有部分写入。
影响：原本用于诊断的试跑改变 paper 数据/证据，污染后续性能与晋级判断。无需真实资金订单就已是严重副作用。

验证：设置构建 dry_run=True 后，真实 Adapter + runtime 内存探针记录 intake、generate、settlement、metrics、trade_prediction_verifier 均被调用一次。显式 run_once(dry_run=True) 只阻止 settlement/metrics；intake、generate、verify_pending 仍各调用一次。真实 signal generator 的写入链已静态确认。未对生产 DB 试跑。

### I3 / P2 — 逐策略失败不进入周期错误，全部策略失败仍 completed

定位：
- `/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/incubation/orchestrator.py:385` settlement 异常只日志。
- 同文件 `:408` 到 `:420` 的超时/异常只日志，没有加入 phase_failures。
- 同文件 `:580` 只根据 phase_failures 判 completed/partial。
- `/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/runtime/incubation.py:165` daemon 也只统计抛异常，未检查返回的 failed/partial。

触发：一条或全部策略生成、验证、指标写入失败，其他顶层阶段成功。
影响：supervisor/诊断把没有成功处理策略的周期当成功；外层 retry/告警无法通过状态识别故障。

验证：唯一策略 generate_signals 抛 RuntimeError，结果 status=completed、phase_failures=[]、strategies_verified=0。SignalTracker 另一路已将类似异常计入 errors，孵化入口没有等价保护。

### I4 / P2 — ForwardVerifier 把最旧一段样本当作近期，性能恶化可显示近期全胜

定位：
- `/Users/mac/Desktop/张勇/AIASK/packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_incubation_repos/reads.py:413` list_strategy_signal_evidence 按 signal_ts/signal_date DESC 返回。
- `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/forward_verifier.py:253` 直接使用该顺序。
- 同文件 `:196` 使用 primary_hits[-20:] 作为最近20条；`:461` 附近 stability 同样取尾部。

触发：evidence payload 含可用 forward_returns，且有效样本大于20。全无收益时走 signal_stats fallback，不一定受这个分支影响。
影响：recent_primary_hit_rate、recent_primary_skill_lcb 和 stability_gap 口径反向；劣化/恢复识别和采用这些指标的观测决策不可靠。

验证：真实 hermetic 临时 SQLite 存60条日期递增证据：早40条命中、最新20条均亏损。数据库返回日期 2026-08-29 → 2026-07-01；ForwardVerifier 输出 recent_primary_hit_rate=1.0、recent_primary_skill_lcb=+0.3389，而真实最近20条命中率0。该测试完整经过存储读取端。

### I5 / P2 — SignalTracker 忽略配置的 40 日等前向收益窗口

定位：
- `/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/signal_tracker/orchestrator.py:317` 写死 forward_days_list=[1,5,10,20]。
- `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:29` 起实现了 STRATEGY_FACTORY_FORWARD_DAYS；`:1100` 仅当调用者不显式传值才使用 FORWARD_DAYS。
- `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/forward_verifier.py:88` low_frequency 的 secondary_horizon=40。

触发：通过环境变量配置40日或其他自定义窗口，再运行 canonical SignalTracker。
影响：配置看似生效但生产回填缺少对应窗口；长期策略所需40日验证无法靠该循环生成。不要笼统声称所有低频策略永不能晋级，其他入口/统计fallback可能使用不同窗口；确认的是配置被运行路径覆盖。

验证：设 env=1,5,10,20,40，实际宿主 FORWARD_DAYS 确认包含40，但 canonical orchestrator 调用参数仍只有1,5,10,20。

### I6 / P2 — SignalTracker forward-return 完成数被字段名不一致归零

定位：
- `/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/signal_tracker/orchestrator.py:322` 读 computed_count。
- `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/signal_tracker.py:1151` 返回 computed。
- `/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/adapters/signal_tracker/provider.py:123` 透传未归一化。

影响：主计数和任务运行记录总报0，容易被误诊为没有生产前向收益，监控产出率失真；具体 phase payload 尚可看到真实 computed。
验证：宿主返回 computed=42，canonical 主结果 forward_returns_computed=0，errors=[]。

## 能力判断

| 能力 | 当前证据 | 判断 |
|---|---|---|
| 候选接纳、paper账户、信号/订单 | 有真实 intake/IncubationService 数据库实现 | 已实现，dry-run有缺陷 |
| paper结算、持有期/拒绝策略清理、执行审计 | 真实公开 Adapter 接入 canonical orchestrator | 已接线；不能据此断言行情足够新、样本真实 |
| 多窗口 forward return | tracker按日线交易日索引计算、幂等存储、支持成熟窗口 | 已实现，自定义窗口在canonical被覆盖 |
| 前向质量、recent skill、stability | ForwardVerifier和signal_stats两条路径 | 有实现，evidence路径排序错误 |
| 日预测、horizon净收益、calibration、selective gate | canonical有独立开关和hook | 可选能力；selective adapter executor=None，拒绝真实执行是预期能力边界 |
| 自动晋级与闭环反馈 | sidecar有pipeline，孵化旧实现有report/feedback | canonical孵化本身不完整，不能视为独立完成闭环 |
| DSR选择偏差校正 | domain PromotionGate可计算，promotion_gate_enabled恒False，pipeline记 diagnostic_only | 仅诊断，不能宣称正在强制执行DSR晋级门禁；这是当前明确策略选择，不按偶发bug报告 |
| 健康与错误传播 | 顶层phase支持partial，sidecar支持错误台账 | 孵化逐策略异常遗漏；未达到可靠无人值守条件 |

## 验证

使用 `/opt/miniconda3/envs/aiask/bin/python`，先核对根/包 conftest hermetic 隔离。

75 passed, 2 warnings, 5.93s，测试文件：
- strategy-factory: test_incubation_runtime_contract, test_incubation_runtime_daemon, test_incubation_hitrate_wiring, test_incubation_orchestrator_cleanup, test_signal_tracker_runtime_contract, test_signal_tracker_session_date, test_promotion_gate_domain。
- akshare-mcp: test_incubation_factory_runner_adapter, test_paper_fill_execution_price, test_paper_evidence_independence, test_promotion_gate_wiring, test_signal_tracker_forward_returns。

警告为 DSR 测试中几乎常数序列计算 skew/kurtosis precision loss。

独立复现脚本：
- `/Users/mac/Desktop/张勇/AIASK/docs/reviews/evidence/2026-11-04/incubation_repro.py`
- `/Users/mac/Desktop/张勇/AIASK/docs/reviews/evidence/2026-11-04/forward_order_repro.py`
- `/Users/mac/Desktop/张勇/AIASK/docs/reviews/evidence/2026-11-04/signal_horizon_repro.py`

覆盖缺口：当前合同测试大量只断言结果键存在/零策略桩完成，未断言完整周期应调用 pipeline/report/feedback，未断言 dry-run 没有任何写操作，未覆盖真实 DB DESC 读取后近期统计的方向。
