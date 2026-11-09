# 四工厂策略质量审查记录 — 2026-11-04

> 由 `monitor_factory_quality.py` 持续生成；最后更新：2026-11-04 15:01:25 +0800。
> 数据源：四工厂 supervisor heartbeat、`strategy_factory_runs`、`strategy_quality_reports`。观察器只读数据库，不触发工厂运行。
> 观察会话：`continuous-20261104`；轮次开始时间下界：`2026-11-04T14:37:42+08:00`。

## 当前运行健康

- supervisor：**healthy**；heartbeat：`2026-11-04 15:00:42 +0800`。
- 工厂状态：`{"strategy_factory": "running", "factor_mining_factory": "running", "incubation_factory": "running", "market_event_ingest": "running"}`；重启次数：`{"strategy_factory": 0, "factor_mining_factory": 0, "incubation_factory": 0, "market_event_ingest": 0}`。
- 当前策略库存：`{"archived_draft": 17, "deprecated": 263, "draft": 1005, "incubating": 258, "rejected": 6175, "submitted": 8381}`。
- 最近侧链摘要：`{"market_event_ingest": {"round": 1, "timestamp": "2026-11-04 14:40:34", "saved_docs": 110, "normalized_total": 67, "normalized_verified": 30, "errors": 0, "signals": 158, "elapsed_sec": 169}, "factor_mining": {"cycle": 1, "timestamp": "2026-11-04 14:41:40", "raw_candidates": 4, "evolved": 4, "validated": 0, "admitted": 0, "pool_size": 12}, "incubation": {"timestamp": "2026-11-04 14:41:24", "elapsed_sec": 218.9, "status": "completed", "verified": 1700, "promoted": 0, "execution_audit": {"status": "pending_evidence", "evaluated": 80, "saved_signal_evidence": 94, "available_signal_evidence": 160, "hard_gate_passed": 0}}}`。

## 当日汇总

- 完成策略工厂轮次：**4**
- 质量报告：**48**；孵化评估通过：**0**；准入率：**0.0%**
- 计数口径 v3：仅精确关联 factory_run_id、admission_stage=incubation/live 且 incubation 评估明确通过、无硬拒绝的报告计为孵化评估通过；不使用历史 passed 标记。
- 以上仅为孵化评估通过报告数，不代表实际获配孵化预算/持久化准入，不等于独立高质量策略数或可实盘交易；持续高质量目标仍需去重、收益/风险、样本外与执行证据独立审查。
- 带候选冻结契约的样本外回放报告：**0**；全部提交候选及最新前向/执行证据见 [candidates.json](candidates.json)，每轮持久化原始产物见 `runs/`。上游压缩省略的候选仍属于记录缺口。

## 每轮记录

| # | run_id | 完成时间 | 运行 | 质量报告 | 通过率 | 负 Sharpe | 平均 Sharpe | 最大回撤 | 审查结论 |
|---:|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | `factory_run_1793774266_c4443295` | 2026-11-04 14:39:21 +0800 | success_observe_only / 94.6s | 0/13 | 0.0% | 10 | -0.8119 | 0.0621 | 运行状态 success_observe_only，需复核 |
| 2 | `factory_run_1793774663_5d44e906` | 2026-11-04 14:45:12 +0800 | success_observe_only / 48.6s | 0/13 | 0.0% | 10 | -0.8353 | 0.0515 | 运行状态 success_observe_only，需复核 |
| 3 | `factory_run_1793775013_17126cf6` | 2026-11-04 14:50:53 +0800 | success_observe_only / 39.8s | 0/12 | 0.0% | 10 | -0.9005 | 0.0515 | 运行状态 success_observe_only，需复核 |
| 4 | `factory_run_1793775354_f679abe9` | 2026-11-04 14:56:32 +0800 | success_observe_only / 38.0s | 0/10 | 0.0% | 7 | -1.0161 | 0.0517 | 运行状态 success_observe_only，需复核 |

## 轮次审查明细

### `factory_run_1793774266_c4443295`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-04 14:39:21 +0800`。
- 市场快照：`{"date": "2026-11-04", "fear_greed": 51, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 258, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.8119; 最大回撤 0.0621；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), weak_pkf_ic (5), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_bootstrap_ci_lower (4)。
- 代表样本：
  - `factory_1793774324_22146553` 002415+300308·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=0.8500812775565545，回撤=0.06211982439422676。
  - `factory_1793774311_409de721` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1793774311_5debd62e` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1793774663_5d44e906`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-04 14:45:12 +0800`。
- 市场快照：`{"date": "2026-11-04", "fear_greed": 51, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 258, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.8353; 最大回撤 0.0515；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), missing_target_layer_oos_return (7), weak_wf_ic_ir (4), weak_pkf_ic (4)。
- 代表样本：
  - `factory_1793774689_c3bf8ac1` 300059+601398·动量突破·20日：gate=fail，grade=D，Sharpe=0.22708043715416226，回撤=0.02277442153566239。
  - `factory_1793774684_1dfb7f16` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1793774684_6f461c21` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1793775013_17126cf6`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-04 14:50:53 +0800`。
- 市场快照：`{"date": "2026-11-04", "fear_greed": 51, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 258, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 12；通过 0；负 Sharpe 10；平均 Sharpe -0.9005; 最大回撤 0.0515；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), missing_target_layer_oos_return (7), weak_bootstrap_ci_lower (4), weak_wf_ic_ir (3)。
- 代表样本：
  - `factory_1793775039_0037c2ef` 300059+601398·动量突破·20日：gate=fail，grade=D，Sharpe=0.22708043715416226，回撤=0.02277442153566239。
  - `factory_1793775031_d712835e` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1793775047_c708095c` 601398+601939·波动突破·20日：gate=fail，grade=B，Sharpe=-0.018485305259614178，回撤=0.01931378987405515。

### `factory_run_1793775354_f679abe9`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-04 14:56:32 +0800`。
- 市场快照：`{"date": "2026-11-04", "fear_greed": 51, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 258, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 10；通过 0；负 Sharpe 7；平均 Sharpe -1.0161; 最大回撤 0.0517；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), weak_wf_ic_ir (2)。
- 代表样本：
  - `factory_1793775379_287c9b3d` 002594+300308·均线交叉·快6慢20：gate=fail，grade=B，Sharpe=0.4674005111594438，回撤=0.02695546957594621。
  - `factory_1793775382_18ae612a` 601398+601939·多因子：gate=fail，grade=D，Sharpe=0.40349985659058807，回撤=0.01219093107421284。
  - `factory_1793775375_5def5d96` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

## 口径与边界

- 该文档记录观察到的运行和质量证据，不把 `success` 自动解释为策略可交易。
- 监督器的四个运行时为 Market Event Ingest、Factor Mining、Strategy Factory、Incubation；SignalTracker 作为单独 sidecar 状态保留在运行环境中。
- 质量报告缺失或无法按 `factory_run_id` 关联时会保留时间窗口回退结果，并在 JSON 状态中体现报告数量。
