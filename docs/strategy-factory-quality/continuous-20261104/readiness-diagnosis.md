# 持续运行 readiness 阻断只读诊断（2026-11-04）

## 结论

**暂未证实当前工作树仍有 `cohort_promotion_ready_complete_failure` 的永久阻断缺陷。不要关闭 readiness 硬门禁。** 最新已完成生产运行仍是 2026-11-02 的历史记录；其输入缺少成熟度计数。当前代码已经有成熟度汇总和 repository adapter 转发修复，当前库中相同选择口径的 cohort 有 130 个明确尚未成熟、0 个已到期不就绪、5 个无孵化指标。将这些真实成熟度输入交给当前纯 ReadinessService 后，门禁从 score=0.38 / blocked 变为 score=0.68 / can_proceed=True，保留 forward/晋级缺口警告。

该结论是对已确认 blocker 的输入因果复核，不是对新一轮完整运行的成功保证。新 factor 池、数据新鲜度、事件源等输入仍可能形成其他合理阻断；应检查 root 启动后新 run_id 的实际 readiness。

## 实测数据

生产只读查询时间：2026-11-04 14:37 +08:00。连接严格使用 sqlite URI `mode=ro` 和 `PRAGMA query_only=ON`，未调用任何生产 initialize / migration / writer。

最新历史策略轮次：id=1485，run_id=`factory_run_1793592309_4d38af76`，2026-11-02 12:05 +08:00，status=skipped。

其 readiness：
- score 0.38，threshold 0.55；唯一 blocker/critical blocker 为 `cohort_promotion_ready_complete_failure`。
- cohort=135，promotion_ready=0，forward_window_coverage=0，zero_signal=0，promotion_review=0，evidence_debt_ratio=0.55。
- factor active candidates=10、families=7，event tasks ready=4，snapshot completion=1.0，均不是当轮阻断原因。

按当前 `list_feedback_source_strategies` 的真实选择口径重新查询：incubating 最新135条、listed最新18条、submitted最新27条且仅接受3种活跃track（按 updated_at DESC）：
- 最终 cohort 仍为135，全部 incubating。
- min_days_remaining=45 的112条、=19 的18条、未知5条；past_due=0。
- 全部135条都有信号，且仅有1日 forward return；5/10/20日均未覆盖。
- 信号示例最早2026-10-30、最新2026-11-03。到审查时只有少数交易日，缺5/10/20日数据不能解释为已成熟策略质量全失败。
- 最新孵化指标仍为 metric_date=2026-10-30；5个最新策略连指标行都没有。因此补跑孵化与 SignalTracker 有真实价值，但不能让未来20/40日证据提前成熟。

## 代码链路

1. `application/research/_feedback_metrics.py:318` 按状态和 updated_at 选择反馈 cohort；默认180配额，incubating占135。
2. `_feedback_routes.py:460` 读取每策略最新孵化metric，`:607` 将 metadata.min_days_remaining 放入反馈指标。
3. `_feedback_metrics.py:655` 根据剩余样本/天数计 pending_maturity 与 past_due；`_feedback_routes.py:693` 汇总；`_budget_feedback.py:602` 保留字段；`research/_artifact_summary.py:332` 输出到 readiness 的 `budget_feedback_*` 字段。
4. `infrastructure/mcp_adapters.py:204` 当前已经显式转发 `list_strategy_incubation_metrics`。`tests/test_adapter_feedback_parity.py` 记录了旧适配器丢成熟度证据会永久硬阻断的已知缺陷，并覆盖 raw/adapter parity。
5. `services/readiness_service.py:719–745`：cohort>=50且promotion_ready=0时，已知 pending>0、past_due=0会改为 `cohort_promotion_ready_pending_maturity` 警告（扣0.10）；否则 `complete_failure` 扣0.40并形成硬阻断。

## 隔离复核

只读生产查询先抽取 JSON；随后单独进程先运行根 conftest 安装 hermetic，再调用当前 ReadinessService 的纯 evaluate。

- 保留历史快照/因子输入，未补成熟度：`can_proceed=False`、score=0.38、唯一blocker=complete_failure。
- 仅加入当前只读取得的 pending=130/past_due=0：`can_proceed=True`、score=0.68、blockers=[]；仍保留前向覆盖、晋级缺口、review缺口和证据债务警告。

证据：
- `/tmp/aiask_readiness_selected_cohort_20261104.json`：精确135个策略ID、指标日、成熟度、信号日期、实际forward horizons。
- `/tmp/aiask_readiness_inputs_20261104.json`：用于纯函数复核的历史输入。
- `/tmp/aiask_readiness_current_evidence_replay_20261104.json`：前后评估结果。

## 建议 root 下一步

启动后的第一轮先核对新 run_id；若仍出现同一 blocker，直接抓完整 factor_research.summary 的两个成熟度字段及 adapter 原始读取，而不要沿用压缩 summary 或旧run来判断。当前 readiness 返回值本身尚未把 pending/past_due 两字段放入诊断输出，日志可观测性可小范围补齐，便于之后不必反查生产库。

若新一轮变成其他缺口，按实际 blocker 修复；无需调整此次已能正确区分未成熟 cohort 的门槛。旧报告和旧运行记录应保留原样。

本次只读诊断未编辑业务代码、未启动工厂、未写生产业务数据。
