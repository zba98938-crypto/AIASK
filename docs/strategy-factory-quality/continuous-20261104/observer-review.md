# 24小时四工厂质量观察接线审查与改进

日期：2026-11-04。只修改观察器及其隔离测试；没有修改工厂业务/门禁，没有启动工厂或长期 observer，没有写生产数据库。生产库读取仅标准库 SQLite mode=ro/query_only，另外 CLI 单次冒烟只向 /tmp 写观察结果。

## 已落实

修改文件：
- `scripts/factories/monitor_factory_quality.py`
- `packages/strategy-factory/tests/test_quality_observer.py`

1. `review_version=3`。保留既有严谨口径：精确 factory_run_id、stage=incubation/live、明确 incubation passed、无 hard_fail/diagnostic 才记孵化评估通过。增加 summary.diagnostic_only 检查，真实0指标仍是数值、布尔值不当数值。
2. 新增 CLI：`--db`、`--log-dir`、`--state`、`--report-dir`、`--session-id`、`--since`。since必须有UTC偏移，筛选轮次 started_at；不同会话不能复用已有state，避免历史结果冒充本次24小时产出。自定义state同时使用独立PID文件。
3. 修复并行派发轮次漏记：旧逻辑只查询 id > last_run_rowid，较早但较慢结束的轮次会永久丢失；现在按本会话已审查run ID去重，完整收集乱序结束轮次。
4. 每轮保留所有提交报告候选（原先只保留5个代表样本），记录 report_id、strategy_id、tested_object_hash、run关联、准入stage、阻塞、成本后收益/Sharpe、成交数、回撤、扰动稳定性、OOS契约与缺失原因。无真实冻结回放证据的旧 target_layer_oos_return 数字不会算OOS测量。
5. 输出 `candidates.json`，包含全部关联策略的实际数据库 params、factor_weights、状态；每次poll刷新最新孵化5日证据和执行审计快照。新前向证据不会被提交当时的历史记录冻结。运行就绪、孵化通过均不能代替 execution_hard_gate_passed；无审计保留None。该ledger始终goal_completion_verified=false，需要主代理实际判断持续质量。
6. 输出 `runs/<run_id>.json`，归档精确run summary和现有strategy_factory_run_artifacts（候选/研究/证据/提交等）。上游候选明细截断显式记为recording_gaps。
7. 数据库连接使用mode=ro/query_only；poll遇到数据库/输出失败保存last_error并保留原游标，下次可重试。`--once`缺库或本轮失败退出1，不创建缺失数据库。暂时失败不会提前标记轮次已处理而丢档。
8. 日报显示本次session/since、真实OOS报告数、全部候选账本链接；去掉旧目标特有的“首批30”措辞。

## 根代理启动建议

请在四工厂启动时间确定后使用新state/report目录。示例（具体session/start/log_dir以root实际启动为准）：

```sh
/opt/miniconda3/envs/aiask/bin/python scripts/factories/monitor_factory_quality.py \
  --daemon --interval 60 \
  --session-id four-factories-20261104 \
  --since '2026-11-04T14:40:00+08:00' \
  --state /Users/mac/.aiask/run/four-factories-20261104-observer.state.json \
  --log-dir /Users/mac/.aiask/logs/four_runtime_supervisor \
  --report-dir /Users/mac/Desktop/张勇/AIASK/docs/strategy-factory-quality/four-factories-20261104
```

`--since`应为真正启动时刻，示例时间不能直接当证据。supervisor若使用legacy或专属log目录，必须传实际目录。observer并不启动四工厂，也不证明持续运行；仍需真实PID、心跳及每个工厂完成周期。

## 发现的剩余生产记录缺口

生产只读测量：历史最近一个有候选的 `factory_run_1793572508_22c18106`，candidate_artifact storage_mode=inline_compact_json，candidate_count=20，但candidate_briefs只有12条。observer能归档的是上游已经持久化的内容，无法恢复被压缩省略的8个候选或省略的完整参数。

因此，用户要求的“实际生成策略全部记录”仍需要在工厂持久化/逐轮输出路径保存完整候选，或确认新的生产run已经记录完整。新observer会把此类缺口显式列在每轮JSON的recording_gaps，不能声称这一步已由观察器完全解决。提交到strategies表的候选已可通过candidates.json完整记录params。

## 验证

- 最终观察器 + 研究初筛回归：27 passed in 0.35s，日志 `/tmp/aiask_continuous_quality_observer_final_tests.log`。
- 新增行为覆盖：并行乱序完成、带偏移since范围、7报告保留全部候选、OOS证据绑定、后续执行审计刷新、原始产物归档、上游20/12截断告警、会话state不混用、缺库不创建、失败渲染不推进游标且可重试、query_only拒绝写入。
- py_compile及git diff --check通过。
- 真实只读CLI单次冒烟（临时scope）：`/tmp/aiask-quality-observer-smoke/state.json`及`reports/`；返回0，review_version3，无last_error。since=2026-11-04T00:00:00+08:00当时无已完成新轮次，候选账本0条，没有借用11-02历史结果。未启动长期observer。

## 质量验收标准建议

维持既有严格准入规则并记录各层差异：run完成、报告passed、孵化评估、实际分配/策略状态、独立tested_object、成本后交易指标、真实OOS、成熟前向证据、执行硬门槛。新的24小时目标没有给独立策略数量阈值，不应继承旧文档30个作为用户新要求。24小时到达也不自动证明高质量持续性；必须检查多轮独立产出及真实证据完整性。现在没有新增自动“高质量”布尔值来替代这项判断。
