# 孵化工厂与 SignalTracker 审查问题修复（I1–I6）

日期：2026-11-04。工作区：`/Users/mac/Desktop/张勇/AIASK`。

本轮只修改当前工作区源码与测试。未提交 Git，未启动真实工厂，未访问或写入生产数据库。以下验证均使用仓库 hermetic 隔离、临时 SQLite 与内存替身。

## 修复结果

| 编号 | 修复与行为 | 回归依据 |
| --- | --- | --- |
| I1 | canonical 孵化流程恢复执行审计 → 流水线 → 报告 → 反馈，并调用真实 accelerator 的 evaluate_batch(db, strategies, verifications)。修正公开 Adapter 与 Legacy Provider 的真实方法签名。仅将本周期成功处理的策略送入流水线/加速器；accelerated_count 不冒充完成晋升数，pipeline.auto_promoted 进入实际晋升统计。 | canonical Adapter 周期真实调用 ForwardVerifier、HitRateReporter、FeedbackWriter；独立集成回归绑定真实 IncubationFactoryRunner._run_pipeline 并持久化真实孵化阶段快照，检查快照 ID、来源及反馈事件。 |
| I2 | SQLiteAdapter 新增显式 read_only 连接模式，使用 mode=ro + query_only，跳过目录创建、schema 迁移及维护。dry-run 使用独立只读连接，无只读 provider 时拒绝降级到 writer；跳过信号生成/结算/指标/预测验证/流水线/反馈/加速/告警/修复/清理/审计写入；保留前向证据读取与不持久化报告。runtime 构造参数及已有 runner 的 dry_run 均保留，显式 run_once(dry_run=False) 不能重新开启写入；dry daemon 不启停模拟盘引擎。 | 真 SQLite 所有表行数前后不变、报告仍含 1 个有证据策略、writer bootstrap 和所有写钩子未调用；独立只读测试验证文件哈希不变、INSERT 被拒绝、缺失数据库/父目录不会创建。 |
| I3 | 策略 timeout/exception、结算错误及返回型 failed/error/partial 均进入 phase_failures；全部策略失败时 failed，部分/阶段失败时 partial；失败策略排除出晋升 cohort。真实 MetricsRecorder 写库异常上抛，两个 Provider 与 canonical 均拒绝 None 保存结果；批量模式诚实计失败。真实报告持久化失败传回阶段错误并阻断反馈；FeedbackWriter 逐操作保留异常，事件/控制/快照只计成功写入，部分写入返回 partial，全失败返回 failed。legacy pipeline 不再把异常返回为成功空结果。daemon 对返回型 partial/failed 计失败并退避。 | 信号/结算异常、返回型错误、真实指标写库异常及 None（两个 Provider）、真实报告/反馈/快照/控制写入错误、部分写入计数、批量失败计数、daemon 退避回归。 |
| I4 | ForwardVerifier 在最近窗口与稳定性计算前，把数据库返回的证据按 signal_ts / signal_date 转成 UTC 升序。 | 真实存储 DESC 返回 60 条记录，早 40 条盈利、最新 20 条亏损：全量命中率仍 2/3，recent_primary_hit_rate 修正为 0，recent_primary_skill_lcb < 0。 |
| I5 | SignalTracker orchestrator 不再硬编码 [1,5,10,20]，省略 horizons 参数，由宿主 provider 采用配置的窗口列表；协议与 Adapter 支持 None。 | 配置 STRATEGY_FACTORY_FORWARD_DAYS=1,5,10,20,40，实际宿主补录循环访问五个窗口，包含 40。 |
| I6 | SignalTracker Adapter 统一 computed → computed_count，orchestrator 同时接受兼容字段。 | 宿主补录真实返回契约 computed=42，顶层 forward_returns_computed=42。 |

## 代码位置

- `packages/strategy-factory/src/strategy_factory/application/incubation/orchestrator.py`
- `packages/strategy-factory/src/strategy_factory/application/incubation/provider.py`
- `packages/strategy-factory/src/strategy_factory/runtime/incubation.py`
- `packages/strategy-factory/src/strategy_factory/runtime/provider_adapters.py`：只修改 LegacyIncubationProvider 区域。
- `packages/akshare-mcp/src/akshare_mcp/integration/strategy_factory_support.py`
- `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/{runner,forward_verifier,hit_rate_reporter,metrics_recorder,feedback_writer}.py`
- `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_base.py`
- `packages/strategy-factory/src/strategy_factory/application/signal_tracker/{orchestrator,provider}.py`
- `packages/akshare-mcp/src/akshare_mcp/adapters/signal_tracker/provider.py`

新增回归：
- `packages/akshare-mcp/tests/test_incubation_audit_regressions.py`（22 个用例，含参数化）
- `packages/aiask-quant-core/tests/test_sqlite_read_only.py`

同时更新已有 runtime/hitrate/cleanup/daemon/facade/SignalTracker 测试替身以匹配修复后的 provider 契约。

## 验证

最终定向回归（独立复核补修后）：**146 passed in 10.09s**。日志：`/tmp/aiask_incubation_failure_final_tests.log`。

执行命令：
```sh
/opt/miniconda3/envs/aiask/bin/python -m pytest -q \
  packages/akshare-mcp/tests/test_incubation_audit_regressions.py \
  packages/aiask-quant-core/tests/test_sqlite_read_only.py \
  packages/strategy-factory/tests/test_incubation_runtime_contract.py \
  packages/strategy-factory/tests/test_incubation_runtime_daemon.py \
  packages/strategy-factory/tests/test_incubation_hitrate_wiring.py \
  packages/strategy-factory/tests/test_incubation_orchestrator_cleanup.py \
  packages/strategy-factory/tests/test_incubation_provider_get_db.py \
  packages/strategy-factory/tests/test_signal_tracker_runtime_contract.py \
  packages/strategy-factory/tests/test_signal_tracker_session_date.py \
  packages/strategy-factory/tests/test_runtime_facades.py \
  packages/akshare-mcp/tests/test_incubation_factory_runner_adapter.py \
  packages/akshare-mcp/tests/test_incubation_session_date_and_paper_gate.py \
  packages/akshare-mcp/tests/test_incubation_factory_horizon.py \
  packages/akshare-mcp/tests/test_signal_tracker_forward_returns.py \
  packages/akshare-mcp/tests/test_factory_deep_repair_part2.py \
  packages/akshare-mcp/tests/test_hit_rate_reporter_matrix_p3_1.py \
  packages/akshare-mcp/tests/test_trade_prediction_verifier_p1_p2.py \
  packages/akshare-mcp/tests/test_diagnostic_observation_runner.py
```

相关修改 `git diff --check` 通过。此前 61/71 passed，以及补修首组 64 passed 均被本轮 146 passed 覆盖，不相加。更早的一轮广义测试含兄弟 agent 正在调整的准入策略断言失败，不作为最终验证结果。

原独立复现已原样复跑（`/tmp/aiask_incubation_independent_repro_fixed_20261104.log`）：真实指标写入失败现在为 `status=failed`、`metrics_recorded=0`、`pipeline_ids=[]`，保留 `disk write rejected`；真实报告写入失败为 `status=partial`、`phase=hit_rate_report`、`feedback={}`，保留 `domain write rejected`。另一个独立子代理只读复核此次补修，未发现新的阻断问题。

快照保存是宿主可选能力：无方法时明确返回 `snapshot_status=unsupported` 且写入数为0；宿主提供方法但实际写入失败时返回 partial/failed，不再静默吞掉。

## 边界

此结果验证修复后的编排、真实 SQLite 持久化与只读边界、数据顺序和计数契约，不代表生产行情源可达性、真实工厂长期运行表现或策略盈利能力。真实 pipeline 集成用例关闭自动应用晋升评审，避免扩展到其他工厂负责的准入/晋升规则。本轮未重写历史生产数据；修复 I4 后未来重算会读取正确的最近证据窗口。

