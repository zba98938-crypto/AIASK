# 因子、事件与孵化修复独立验收

日期：2026-11-04。只读检查当前工作区修复；未修改业务文件，未启动生产，未访问生产数据库。独立复现安装根 conftest 的 hermetic 隔离，使用临时 SQLite。没有重复根测试。

## 结论

F1–F4 / E1、I1 接线、I2 只读边界、I4–I6 的改动与生产调用链一致，未发现新的高置信阻断问题；但 **I3 尚未闭合**。真实存储失败有两个仍被当成成功的路径，现有新增测试只覆盖返回显式 failed 的替身，漏掉真实模块吞错/None 语义。

### 1. [P1] 真实指标保存失败仍进入流水线

- `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/metrics_recorder.py:169–183` 捕获数据库写异常并返回 `None`（该返回语义在其 docstring 也有明确说明）。
- `packages/strategy-factory/src/strategy_factory/application/incubation/orchestrator.py:424–431` 的 require_success 只检查 dict；None 被认作成功，metrics_recorded 递增，策略加入 processed_ids。
- 复现用真实 MetricsRecorder、真实 Adapter/canonical runtime，仅令临时 DB 的 save_strategy_incubation_metric 抛 `RuntimeError('disk write rejected')`。
- 实际输出：`status=completed`、`phase_failures=[]`、`metrics_recorded=1`、流水线仍收到 `s1`。
- 建议：明确检查 MetricsRecorder 的 None 失败契约（最好在 Adapter/Legacy Provider 及编排一致处理），并用真实记录器的失败写库补回归。失败策略不得计成功或进入后续晋升 cohort。

### 2. [P2] 新接入的报告/反馈持久化失败仍报成功

- `hit_rate_reporter.py:498–513` `_persist_report` 捕获写异常并静默返回；generate 仍返回正常报告。
- `feedback_writer.py:58–64` 无条件增加 written_events，而底层 `_write_feedback_event` / `_write_prediction_feedback_event` 捕获写错误；汇总快照也吞错。
- 复现使用真实 HitRateReporter 与 FeedbackWriter，仅令临时 DB 的 save_strategy_domain_event 抛 `RuntimeError('domain write rejected')`。
- 实际输出：`status=completed`、`phase_failures=[]`、`feedback.written_events=2`，实际事件均未写入。
- 建议：必要写入返回明确失败状态或抛异常，写入数量只累计真实成功；canonical 应返回 partial/failed 并保留原因。补真实 reporter/writer 失败回归，不只用外层替身 `status=failed`。

## 证据

- 独立复现：`/tmp/aiask_incubation_independent_repro_20261104.py`
- 结果：`/tmp/aiask_incubation_independent_repro_20261104.log`
- 命令：`/opt/miniconda3/envs/aiask/bin/python /tmp/aiask_incubation_independent_repro_20261104.py`

其余已复核：因子 context 实际安装数值 evaluator，普通准入及两条 quarantine 晋级均检查实际值；同批晋级集合更新；quality_summary 持久明细与恢复读取字段匹配；scheduler 下一轮反馈接线存在；只读孵化使用 mode=ro/query_only 且写 phase 跳过；近期排序先升序再取末尾；SignalTracker 配置窗口与 computed 字段契约吻合。
