# 四工厂策略质量审查记录 — 2026-11-09

> 由 `monitor_factory_quality.py` 持续生成；最后更新：2026-11-09 18:08:29 +0800。
> 数据源：四工厂 supervisor heartbeat、`strategy_factory_runs`、`strategy_quality_reports`。观察器只读数据库，不触发工厂运行。
> 观察会话：`continuous-20261104`；轮次开始时间下界：`2026-11-04T14:37:42+08:00`。

## 当前运行健康

- supervisor：**healthy**；heartbeat：`2026-11-09 18:08:29 +0800`。
- 工厂状态：`{"strategy_factory": "running", "factor_mining_factory": "running", "incubation_factory": "running", "market_event_ingest": "running"}`；重启次数：`{"strategy_factory": 0, "factor_mining_factory": 0, "incubation_factory": 0, "market_event_ingest": 0}`。
- 当前策略库存：`{"archived_draft": 17, "deprecated": 284, "draft": 1334, "incubating": 237, "rejected": 6175, "submitted": 13011}`。
- 最近侧链摘要：`{"market_event_ingest": {"round": 8, "timestamp": "2026-11-09 17:57:17", "saved_docs": 110, "normalized_total": 67, "normalized_verified": 30, "errors": 0, "signals": 216, "elapsed_sec": 331}, "factor_mining": {"cycle": 3, "timestamp": "2026-11-09 16:52:11", "raw_candidates": 17, "evolved": 17, "validated": 0, "admitted": 0, "pool_size": 4}, "incubation": {"timestamp": "2026-11-09 17:40:11", "elapsed_sec": 389.6, "status": "partial", "verified": 1700, "promoted": 0, "execution_audit": {"status": "pending_evidence", "evaluated": 80, "saved_signal_evidence": 0, "available_signal_evidence": 160, "hard_gate_passed": 0}}}`。

## 当日汇总

- 完成策略工厂轮次：**56**
- 质量报告：**743**；孵化评估通过：**0**；准入率：**0.0%**
- 计数口径 v3：仅精确关联 factory_run_id、admission_stage=incubation/live 且 incubation 评估明确通过、无硬拒绝的报告计为孵化评估通过；不使用历史 passed 标记。
- 以上仅为孵化评估通过报告数，不代表实际获配孵化预算/持久化准入，不等于独立高质量策略数或可实盘交易；持续高质量目标仍需去重、收益/风险、样本外与执行证据独立审查。
- 带候选冻结契约的样本外回放报告：**0**；全部提交候选及最新前向/执行证据见 [candidates.json](candidates.json)，每轮持久化原始产物见 `runs/`。上游压缩省略的候选仍属于记录缺口。

## 每轮记录

| # | run_id | 完成时间 | 运行 | 质量报告 | 通过率 | 负 Sharpe | 平均 Sharpe | 最大回撤 | 审查结论 |
|---:|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | `factory_run_1794186715_c84de2d8` | 2026-11-09 09:13:07 +0800 | success_observe_only / 72.0s | 0/12 | 0.0% | 7 | -0.9394 | 0.0577 | 运行状态 success_observe_only，需复核 |
| 2 | `factory_run_1794187088_00f4d802` | 2026-11-09 09:18:53 +0800 | success_observe_only / 44.6s | 0/12 | 0.0% | 8 | -1.0559 | 0.0577 | 运行状态 success_observe_only，需复核 |
| 3 | `factory_run_1794187435_3b6f545f` | 2026-11-09 09:24:36 +0800 | success_observe_only / 41.8s | 0/11 | 0.0% | 7 | -0.5487 | 0.052 | 运行状态 success_observe_only，需复核 |
| 4 | `factory_run_1794187778_f50ea359` | 2026-11-09 09:30:26 +0800 | success_observe_only / 47.8s | 0/15 | 0.0% | 7 | -0.5898 | 0.052 | 运行状态 success_observe_only，需复核 |
| 5 | `factory_run_1794188127_f6c3f0e9` | 2026-11-09 09:36:10 +0800 | success_observe_only / 43.3s | 0/12 | 0.0% | 7 | -0.5964 | 0.0574 | 运行状态 success_observe_only，需复核 |
| 6 | `factory_run_1794188472_347607b5` | 2026-11-09 09:41:56 +0800 | success_observe_only / 44.0s | 0/12 | 0.0% | 6 | -0.5304 | 0.0574 | 运行状态 success_observe_only，需复核 |
| 7 | `factory_run_1794188817_48426775` | 2026-11-09 09:47:52 +0800 | success_observe_only / 54.7s | 0/11 | 0.0% | 6 | -1.015 | 0.052 | 运行状态 success_observe_only，需复核 |
| 8 | `factory_run_1794189174_c4f628a3` | 2026-11-09 09:53:49 +0800 | success_observe_only / 55.7s | 0/12 | 0.0% | 8 | -0.5197 | 0.052 | 运行状态 success_observe_only，需复核 |
| 9 | `factory_run_1794189312_78e77b65` | 2026-11-09 10:01:03 +0800 | success_observe_only / 350.8s | 0/14 | 0.0% | 11 | -1.082 | 0.052 | 运行状态 success_observe_only，需复核 |
| 10 | `factory_run_1794189970_7144367c` | 2026-11-09 10:11:15 +0800 | success_observe_only / 305.8s | 0/14 | 0.0% | 12 | -1.0217 | 0.052 | 运行状态 success_observe_only，需复核 |
| 11 | `factory_run_1794190492_d3b2aae8` | 2026-11-09 10:19:34 +0800 | success_observe_only / 281.6s | 0/13 | 0.0% | 9 | -1.0459 | 0.052 | 运行状态 success_observe_only，需复核 |
| 12 | `factory_run_1794191083_70188680` | 2026-11-09 10:30:03 +0800 | success_observe_only / 320.4s | 0/14 | 0.0% | 12 | -1.1562 | 0.052 | 运行状态 success_observe_only，需复核 |
| 13 | `factory_run_1794191710_4750b002` | 2026-11-09 10:39:20 +0800 | success_observe_only / 249.8s | 0/13 | 0.0% | 11 | -1.1652 | 0.052 | 运行状态 success_observe_only，需复核 |
| 14 | `factory_run_1794192266_2a6561b5` | 2026-11-09 10:48:56 +0800 | success_observe_only / 270.1s | 0/14 | 0.0% | 11 | -1.1255 | 0.052 | 运行状态 success_observe_only，需复核 |
| 15 | `factory_run_1794192844_b1e36071` | 2026-11-09 11:01:17 +0800 | success_observe_only / 433.0s | 0/13 | 0.0% | 11 | -1.108 | 0.052 | 运行状态 success_observe_only，需复核 |
| 16 | `factory_run_1794193584_49beb36f` | 2026-11-09 11:15:21 +0800 | success_observe_only / 537.3s | 0/14 | 0.0% | 11 | -0.9677 | 0.052 | 运行状态 success_observe_only，需复核 |
| 17 | `factory_run_1794194425_bebb68e5` | 2026-11-09 11:24:03 +0800 | success_observe_only / 217.5s | 0/13 | 0.0% | 11 | -1.1652 | 0.052 | 运行状态 success_observe_only，需复核 |
| 18 | `factory_run_1794194949_26e6c627` | 2026-11-09 11:33:11 +0800 | success_observe_only / 241.4s | 0/13 | 0.0% | 10 | -0.9485 | 0.052 | 运行状态 success_observe_only，需复核 |
| 19 | `factory_run_1794195497_3f8ee6d1` | 2026-11-09 11:43:58 +0800 | success_observe_only / 340.8s | 0/13 | 0.0% | 11 | -1.1652 | 0.052 | 运行状态 success_observe_only，需复核 |
| 20 | `factory_run_1794196145_4751098d` | 2026-11-09 11:53:41 +0800 | success_observe_only / 276.1s | 0/13 | 0.0% | 10 | -0.9485 | 0.052 | 运行状态 success_observe_only，需复核 |
| 21 | `factory_run_1794196729_a62e2fd2` | 2026-11-09 12:04:09 +0800 | success_observe_only / 320.6s | 0/13 | 0.0% | 10 | -0.9485 | 0.052 | 运行状态 success_observe_only，需复核 |
| 22 | `factory_run_1794197356_95b23978` | 2026-11-09 12:14:28 +0800 | success_observe_only / 312.0s | 0/14 | 0.0% | 10 | -1.0661 | 0.052 | 运行状态 success_observe_only，需复核 |
| 23 | `factory_run_1794197973_25d3f481` | 2026-11-09 12:21:53 +0800 | success_observe_only / 140.2s | 0/13 | 0.0% | 10 | -1.2238 | 0.0654 | 运行状态 success_observe_only，需复核 |
| 24 | `factory_run_1794198418_cfdb40dc` | 2026-11-09 12:29:14 +0800 | success_observe_only / 136.2s | 0/14 | 0.0% | 12 | -1.0558 | 0.053 | 运行状态 success_observe_only，需复核 |
| 25 | `factory_run_1794198859_d45343e5` | 2026-11-09 12:43:23 +0800 | success_observe_only / 543.8s | 0/13 | 0.0% | 10 | -1.0501 | 0.0486 | 运行状态 success_observe_only，需复核 |
| 26 | `factory_run_1794199710_cc4b9e3d` | 2026-11-09 12:53:53 +0800 | success_observe_only / 322.5s | 0/14 | 0.0% | 12 | -1.233 | 0.0463 | 运行状态 success_observe_only，需复核 |
| 27 | `factory_run_1794200341_4f200f92` | 2026-11-09 13:03:02 +0800 | success_observe_only / 241.2s | 0/13 | 0.0% | 11 | -1.2001 | 0.0439 | 运行状态 success_observe_only，需复核 |
| 28 | `factory_run_1794200887_18cdaeda` | 2026-11-09 13:13:26 +0800 | success_observe_only / 318.8s | 0/14 | 0.0% | 11 | -1.156 | 0.0472 | 运行状态 success_observe_only，需复核 |
| 29 | `factory_run_1794201513_c8c157e3` | 2026-11-09 13:22:45 +0800 | success_observe_only / 252.6s | 0/13 | 0.0% | 11 | -1.0059 | 0.053 | 运行状态 success_observe_only，需复核 |
| 30 | `factory_run_1794202071_9172b3f6` | 2026-11-09 13:31:41 +0800 | success_observe_only / 229.9s | 0/13 | 0.0% | 11 | -1.0059 | 0.053 | 运行状态 success_observe_only，需复核 |
| 31 | `factory_run_1794202397_1eee5e28` | 2026-11-09 13:42:53 +0800 | success_observe_only / 575.8s | 0/13 | 0.0% | 12 | -1.3583 | 0.0463 | 运行状态 success_observe_only，需复核 |
| 32 | `factory_run_1794203281_4e0810e1` | 2026-11-09 13:55:15 +0800 | success_observe_only / 434.2s | 0/13 | 0.0% | 11 | -1.1781 | 0.0463 | 运行状态 success_observe_only，需复核 |
| 33 | `factory_run_1794204020_125fa581` | 2026-11-09 14:02:48 +0800 | success_observe_only / 148.0s | 0/13 | 0.0% | 10 | -0.8324 | 0.0523 | 运行状态 success_observe_only，需复核 |
| 34 | `factory_run_1794204472_92ceeb69` | 2026-11-09 14:12:39 +0800 | success_observe_only / 286.6s | 0/13 | 0.0% | 11 | -0.9378 | 0.0495 | 运行状态 success_observe_only，需复核 |
| 35 | `factory_run_1794205067_e311b7f5` | 2026-11-09 14:22:41 +0800 | success_observe_only / 294.0s | 0/13 | 0.0% | 9 | -1.0587 | 0.0472 | 运行状态 success_observe_only，需复核 |
| 36 | `factory_run_1794205669_27bd28d3` | 2026-11-09 14:33:17 +0800 | success_observe_only / 328.4s | 0/14 | 0.0% | 12 | -1.2597 | 0.0463 | 运行状态 success_observe_only，需复核 |
| 37 | `factory_run_1794206305_74992311` | 2026-11-09 14:43:17 +0800 | success_observe_only / 292.5s | 0/13 | 0.0% | 11 | -1.1781 | 0.0463 | 运行状态 success_observe_only，需复核 |
| 38 | `factory_run_1794206903_ab9caa7c` | 2026-11-09 14:53:49 +0800 | success_observe_only / 326.6s | 0/14 | 0.0% | 11 | -0.9083 | 0.0523 | 运行状态 success_observe_only，需复核 |
| 39 | `factory_run_1794207537_daf1e9e8` | 2026-11-09 15:05:01 +0800 | success_observe_only / 364.1s | 0/13 | 0.0% | 10 | -0.9613 | 0.05 | 运行状态 success_observe_only，需复核 |
| 40 | `factory_run_1794208218_d9787662` | 2026-11-09 15:25:21 +0800 | success_observe_only / 903.3s | 0/13 | 0.0% | 11 | -1.0059 | 0.053 | 运行状态 success_observe_only，需复核 |
| 41 | `factory_run_1794209429_c5068dae` | 2026-11-09 15:35:34 +0800 | success_observe_only / 305.4s | 0/13 | 0.0% | 10 | -1.0501 | 0.0486 | 运行状态 success_observe_only，需复核 |
| 42 | `factory_run_1794210040_f6adf73a` | 2026-11-09 15:44:34 +0800 | success_observe_only / 234.3s | 0/14 | 0.0% | 11 | -1.0676 | 0.0501 | 运行状态 success_observe_only，需复核 |
| 43 | `factory_run_1794210579_ab77018f` | 2026-11-09 15:53:16 +0800 | success_observe_only / 217.0s | 0/13 | 0.0% | 9 | -0.8401 | 0.0463 | 运行状态 success_observe_only，需复核 |
| 44 | `factory_run_1794211104_45fb5fb4` | 2026-11-09 16:02:28 +0800 | success_observe_only / 243.9s | 0/13 | 0.0% | 10 | -0.9613 | 0.05 | 运行状态 success_observe_only，需复核 |
| 45 | `factory_run_1794211655_d0333e3a` | 2026-11-09 16:12:58 +0800 | success_observe_only / 323.2s | 0/13 | 0.0% | 10 | -1.3643 | 0.0654 | 运行状态 success_observe_only，需复核 |
| 46 | `factory_run_1794212290_d5c3149c` | 2026-11-09 16:23:02 +0800 | success_observe_only / 292.1s | 0/13 | 0.0% | 11 | -0.946 | 0.0495 | 运行状态 success_observe_only，需复核 |
| 47 | `factory_run_1794212889_21c927e1` | 2026-11-09 16:32:54 +0800 | success_observe_only / 284.3s | 0/13 | 0.0% | 10 | -0.9719 | 0.05 | 运行状态 success_observe_only，需复核 |
| 48 | `factory_run_1794213488_854bec02` | 2026-11-09 16:44:51 +0800 | success_observe_only / 402.8s | 0/13 | 0.0% | 9 | -1.0886 | 0.0475 | 运行状态 success_observe_only，需复核 |
| 49 | `factory_run_1794214199_52cc80c4` | 2026-11-09 16:55:06 +0800 | success_observe_only / 306.3s | 0/13 | 0.0% | 12 | -1.1196 | 0.0663 | 运行状态 success_observe_only，需复核 |
| 50 | `factory_run_1794214813_3c15bc71` | 2026-11-09 17:04:38 +0800 | success_observe_only / 264.8s | 0/14 | 0.0% | 13 | -1.4007 | 0.0663 | 运行状态 success_observe_only，需复核 |
| 51 | `factory_run_1794215385_91ae667a` | 2026-11-09 17:14:10 +0800 | success_observe_only / 264.7s | 0/13 | 0.0% | 12 | -1.3636 | 0.0663 | 运行状态 success_observe_only，需复核 |
| 52 | `factory_run_1794215956_5eaff3fd` | 2026-11-09 17:23:57 +0800 | success_observe_only / 281.0s | 0/13 | 0.0% | 11 | -1.2178 | 0.0758 | 运行状态 success_observe_only，需复核 |
| 53 | `factory_run_1794216544_fd3ff4ba` | 2026-11-09 17:33:30 +0800 | success_observe_only / 265.7s | 0/13 | 0.0% | 11 | -1.3698 | 0.0758 | 运行状态 success_observe_only，需复核 |
| 54 | `factory_run_1794217117_3bbad398` | 2026-11-09 17:43:26 +0800 | success_observe_only / 289.1s | 0/17 | 0.0% | 15 | -1.2153 | 0.0758 | 运行状态 success_observe_only，需复核 |
| 55 | `factory_run_1794217714_0e579136` | 2026-11-09 17:54:04 +0800 | success_observe_only / 330.4s | 0/14 | 0.0% | 13 | -1.2962 | 0.1755 | 运行状态 success_observe_only，需复核 |
| 56 | `factory_run_1794218352_a41f63d2` | 2026-11-09 18:03:59 +0800 | success_observe_only / 286.7s | 0/17 | 0.0% | 15 | -1.3553 | 0.0934 | 运行状态 success_observe_only，需复核 |

## 轮次审查明细

### `factory_run_1794186715_c84de2d8`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 09:13:07 +0800`。
- 市场快照：`{"completion_ratio": 1.0, "date": "2026-11-09", "failure_reason_count": 0, "fear_greed": 60, "fg_level": "greed", "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 12；通过 0；负 Sharpe 7；平均 Sharpe -0.9394; 最大回撤 0.0577；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), default_profile_not_allowed_for_single_name_runtime (2)。
- 代表样本：
  - `factory_1794186769_041df090` 002415+300308·动量突破·20日：gate=fail，grade=C，Sharpe=0.8949406237498805，回撤=0.047026914656337473。
  - `factory_1794186778_9db70bee` 002594+300308·波动突破·20日：gate=fail，grade=B，Sharpe=0.6000631419099212，回撤=0.020324509911356013。
  - `factory_1794186773_1fdfd436` 300059+601398·均线交叉·快5慢20：gate=fail，grade=B，Sharpe=0.33799941206019585，回撤=0.018302614192417948。

### `factory_run_1794187088_00f4d802`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 09:18:53 +0800`。
- 市场快照：`{"completion_ratio": 1.0, "date": "2026-11-09", "failure_reason_count": 0, "fear_greed": 60, "fg_level": "greed", "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 12；通过 0；负 Sharpe 8；平均 Sharpe -1.0559; 最大回撤 0.0577；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), default_profile_not_allowed_for_single_name_runtime (2)。
- 代表样本：
  - `factory_1794187123_497a3b58` 601398+601939·多因子：gate=fail，grade=D，Sharpe=0.5019601747781753，回撤=0.012143590600982477。
  - `factory_1794187121_678da50a` 002594+300308·均线交叉·快6慢20：gate=fail，grade=B，Sharpe=0.4735539469022318，回撤=0.02695028454551547。
  - `factory_1794187113_33480430` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。

### `factory_run_1794187435_3b6f545f`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 09:24:36 +0800`。
- 市场快照：`{"completion_ratio": 1.0, "date": "2026-11-09", "failure_reason_count": 0, "fear_greed": 60, "fg_level": "greed", "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 11；通过 0；负 Sharpe 7；平均 Sharpe -0.5487; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_pkf_ic (3), weak_bootstrap_ci_lower (2)。
- 代表样本：
  - `factory_1794187458_52e9b990` 002594+300308·动量突破·20日：gate=fail，grade=C，Sharpe=0.9557536970187115，回撤=0.04070824681418451。
  - `factory_1794187472_6bb7bf75` 002415+300308·波动突破·20日：gate=fail，grade=D，Sharpe=0.30624334341259224，回撤=0.0313284908411153。
  - `factory_1794187458_9842404c` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。

### `factory_run_1794187778_f50ea359`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 09:30:26 +0800`。
- 市场快照：`{"completion_ratio": 1.0, "date": "2026-11-09", "failure_reason_count": 0, "fear_greed": 60, "fg_level": "greed", "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 0；负 Sharpe 7；平均 Sharpe -0.5898; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), weak_pkf_ic (7), missing_target_layer_oos_return (7), walk_forward_ic_ir 0.000 < 0.300 (5)。
- 代表样本：
  - `factory_1794187814_b1e34a4f` 002415+300308·动量突破·20日：gate=fail，grade=C，Sharpe=0.8949406237498805，回撤=0.047026914656337473。
  - `factory_1794187814_dd64280d` 300059+601398·均线交叉·快6慢20：gate=fail，grade=B，Sharpe=0.45256534129780285，回撤=0.023321874774396085。
  - `factory_1794187807_12b37e6b` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。

### `factory_run_1794188127_f6c3f0e9`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 09:36:10 +0800`。
- 市场快照：`{"completion_ratio": 1.0, "date": "2026-11-09", "failure_reason_count": 0, "fear_greed": 60, "fg_level": "greed", "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 12；通过 0；负 Sharpe 7；平均 Sharpe -0.5964; 最大回撤 0.0574；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794188158_2f583a44` 002594+300308·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=0.918172904688288，回撤=0.05744897029024041。
  - `factory_1794188155_24de81c8` 601398+601939·动量突破·20日：gate=fail，grade=C，Sharpe=0.48016906559228095，回撤=0.017827443849510478。
  - `factory_1794188149_f9f2be92` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。

### `factory_run_1794188472_347607b5`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 09:41:56 +0800`。
- 市场快照：`{"completion_ratio": 1.0, "date": "2026-11-09", "failure_reason_count": 0, "fear_greed": 60, "fg_level": "greed", "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 12；通过 0；负 Sharpe 6；平均 Sharpe -0.5304; 最大回撤 0.0574；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1794188506_acb6f36a` 002594+300308·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=0.918172904688288，回撤=0.05744897029024041。
  - `factory_1794188501_7e1dadb0` 601398+601939·动量突破·20日：gate=fail，grade=C，Sharpe=0.48016906559228095，回撤=0.017827443849510478。
  - `factory_1794188496_218adc1d` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。

### `factory_run_1794188817_48426775`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 09:47:52 +0800`。
- 市场快照：`{"completion_ratio": 1.0, "date": "2026-11-09", "failure_reason_count": 0, "fear_greed": 60, "fg_level": "greed", "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 11；通过 0；负 Sharpe 6；平均 Sharpe -1.015; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), weak_pkf_ic (3), validation_grade_d_not_allowed_for_incubation (3), validation_grade_d_below_minimum_c_for_incubation (3), weak_wf_ic_ir (2)。
- 代表样本：
  - `factory_1794188865_20ac236b` 002594+300308·波动突破·20日：gate=fail，grade=B，Sharpe=0.6000631419099212，回撤=0.020324509911356013。
  - `factory_1794188856_1cd95abd` 002415+300308·均线交叉·快6慢20：gate=fail，grade=B，Sharpe=0.3107674103173406，回撤=0.048873133234996945。
  - `factory_1794188848_36094308` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。

### `factory_run_1794189174_c4f628a3`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 09:53:49 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 12；通过 0；负 Sharpe 8；平均 Sharpe -0.5197; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794189209_e6f66c70` 002594+300308·动量突破·20日：gate=fail，grade=C，Sharpe=0.9557536970187115，回撤=0.04070824681418451。
  - `factory_1794189220_1ddbeb33` 002415+300308·波动突破·20日：gate=fail，grade=D，Sharpe=0.30624334341259224，回撤=0.0313284908411153。
  - `factory_1794189199_28079fa5` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。

### `factory_run_1794189312_78e77b65`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 10:01:03 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 11；平均 Sharpe -1.082; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), missing_target_layer_oos_return (7), weak_pkf_ic (4), weak_wf_ic_ir (3)。
- 代表样本：
  - `factory_1794189516_10a2acab` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794189516_8111916b` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794189516_bc8dd024` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794189970_7144367c`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 10:11:15 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 12；平均 Sharpe -1.0217; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794190133_ad780c29` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794190133_c8f5fd7b` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794190254_3230cf59` 600690+603259·价值精选·10日轮动：gate=fail，grade=B，Sharpe=-0.3702557791519448，回撤=0.04303872781603799。

### `factory_run_1794190492_d3b2aae8`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 10:19:34 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 9；平均 Sharpe -1.0459; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (2)。
- 代表样本：
  - `factory_1794190667_7e6cc9a5` 600690+603259·动量突破·20日：gate=fail，grade=C，Sharpe=0.3759807702531632，回撤=0.021934555158760322。
  - `factory_1794190641_49f65693` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794190731_ca8e98a6` 300760+603259·波动突破·20日：gate=fail，grade=B，Sharpe=0.029287353648709154，回撤=0.02744613641760008。

### `factory_run_1794191083_70188680`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 10:30:03 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 12；平均 Sharpe -1.1562; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794191253_817303b7` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794191253_70b77c0c` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794191283_f46c319f` 300760+603259·均线交叉·快6慢20：gate=fail，grade=B，Sharpe=-0.016475106711532878，回撤=0.0370168056410316。

### `factory_run_1794191710_4750b002`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 10:39:20 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.1652; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (7), validation_grade_d_below_minimum_c_for_incubation (7), missing_target_layer_oos_return (7), weak_pkf_ic (3), weak_wf_ic_ir (2)。
- 代表样本：
  - `factory_1794191836_4e7efd85` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794191836_910238c0` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794191882_fdda0a63` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.10078061583788536，回撤=0.0238918260277448。

### `factory_run_1794192266_2a6561b5`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 10:48:56 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 11；平均 Sharpe -1.1255; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794192401_af23b3fd` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794192461_8159e29b` 300760+603259·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=0.036632229467697744，回撤=0.04000556584334507。
  - `factory_1794192401_8aa4d451` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794192844_b1e36071`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 11:01:17 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.108; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), weak_pkf_ic (3), weak_bootstrap_ci_lower (2)。
- 代表样本：
  - `factory_1794193098_1534c6c3` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794193098_f8aa3643` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794193132_15a3ce58` 300760+603259·均线交叉·快6慢20：gate=fail，grade=B，Sharpe=-0.016475106711532878，回撤=0.0370168056410316。

### `factory_run_1794193584_49beb36f`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 11:15:21 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 11；平均 Sharpe -0.9677; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794193943_e908c2e5` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794193869_73865e34` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794193869_f778b90c` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794194425_bebb68e5`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 11:24:03 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.1652; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (7), validation_grade_d_below_minimum_c_for_incubation (7), missing_target_layer_oos_return (7), weak_pkf_ic (3), weak_wf_ic_ir (2)。
- 代表样本：
  - `factory_1794194517_b21c1cfa` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794194517_51197947` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794194566_4e2886a7` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.10078061583788536，回撤=0.0238918260277448。

### `factory_run_1794194949_26e6c627`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 11:33:11 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.9485; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (2)。
- 代表样本：
  - `factory_1794195099_35ba1217` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794195077_e5d7866e` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794195077_5ee09c1f` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794195497_3f8ee6d1`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 11:43:58 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.1652; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (7), validation_grade_d_below_minimum_c_for_incubation (7), missing_target_layer_oos_return (7), weak_pkf_ic (3), weak_wf_ic_ir (2)。
- 代表样本：
  - `factory_1794195703_901f4747` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794195703_8412cb21` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794195758_7508669c` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.10078061583788536，回撤=0.0238918260277448。

### `factory_run_1794196145_4751098d`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 11:53:41 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.9485; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (2)。
- 代表样本：
  - `factory_1794196302_af42c668` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794196275_c533073b` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794196275_42b395d9` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794196729_a62e2fd2`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 12:04:09 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.9485; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (2)。
- 代表样本：
  - `factory_1794196918_dd2bcd7f` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794196893_326bb019` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794196893_3650d226` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794197356_95b23978`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 12:14:28 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 10；平均 Sharpe -1.0661; 最大回撤 0.052；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794197551_aa5fa3ea` 600690+603259·动量突破·20日：gate=fail，grade=C，Sharpe=0.3759807702531632，回撤=0.021934555158760322。
  - `factory_1794197529_5d6e5590` 300063·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.16724175076410883，回撤=0.05201046812959805。
  - `factory_1794197616_c77aa803` 300760+603259·波动突破·20日：gate=fail，grade=B，Sharpe=0.029287353648709154，回撤=0.02744613641760008。

### `factory_run_1794197973_25d3f481`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 12:21:53 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -1.2238; 最大回撤 0.0654；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), weak_pkf_ic (3), weak_wf_ic_ir (2)。
- 代表样本：
  - `factory_1794198065_549ef094` 300760+603259·多因子：gate=fail，grade=C，Sharpe=0.05936233799035221，回撤=0.03258204169065075。
  - `factory_1794198035_5c823886` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794198036_60d22eae` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794198418_cfdb40dc`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 12:29:14 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 12；平均 Sharpe -1.0558; 最大回撤 0.053；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794198482_b9635dfb` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794198482_42a7d7f0` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794198524_97108a5a` 600690+603259·波动突破·20日：gate=fail，grade=D，Sharpe=-0.010590797946611049，回撤=0.028643539568589994。

### `factory_run_1794198859_d45343e5`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 12:43:23 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -1.0501; 最大回撤 0.0486；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_pkf_ic (3), post_cost_sharpe -0.613 < 0.100 (2)。
- 代表样本：
  - `factory_1794199087_c86d809c` 300760+603259·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=0.036632229467697744，回撤=0.04000556584334507。
  - `factory_1794199017_bcd3a551` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794199017_b37e8702` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794199710_cc4b9e3d`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 12:53:53 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 12；平均 Sharpe -1.233; 最大回撤 0.0463；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (7), validation_grade_d_below_minimum_c_for_incubation (7), missing_target_layer_oos_return (7), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794199870_5650e5df` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794199870_c20123ec` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794199926_d746a36e` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.10078061583788536，回撤=0.0238918260277448。

### `factory_run_1794200341_4f200f92`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 13:03:02 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.2001; 最大回撤 0.0439；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), weak_pkf_ic (3), win_rate 0.368 < 0.400 (2)。
- 代表样本：
  - `factory_1794200426_9caefb3b` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794200426_e854f6f1` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794200492_6c688db7` 600690+603259·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=-0.09852275819761588，回撤=0.04247734048746603。

### `factory_run_1794200887_18cdaeda`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 13:13:26 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 11；平均 Sharpe -1.156; 最大回撤 0.0472；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), weak_bootstrap_ci_lower (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1794201185_199885e8` 300760+603259·价值精选·20日轮动：gate=fail，grade=B，Sharpe=0.05176179429481176，回撤=0.02562955258663851。
  - `factory_1794201060_77ce9069` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794201060_329759e2` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794201513_c8c157e3`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 13:22:45 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.0059; 最大回撤 0.053；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_pkf_ic (3), weak_bootstrap_ci_lower (2)。
- 代表样本：
  - `factory_1794201627_3e408c2c` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794201627_a1fad1e6` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794201711_b7d86702` 600690+603259·波动突破·20日：gate=fail，grade=D，Sharpe=-0.010590797946611049，回撤=0.028643539568589994。

### `factory_run_1794202071_9172b3f6`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 13:31:41 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.0059; 最大回撤 0.053；关联方式 `factory_run_id`。
- 主要阻塞：missing_target_layer_oos_return (7), validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_pkf_ic (3), weak_bootstrap_ci_lower (2)。
- 代表样本：
  - `factory_1794202180_30aa6f71` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794202180_f46e808f` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794202252_29c8e553` 600690+603259·波动突破·20日：gate=fail，grade=D，Sharpe=-0.010590797946611049，回撤=0.028643539568589994。

### `factory_run_1794202397_1eee5e28`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 13:42:53 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 12；平均 Sharpe -1.3583; 最大回撤 0.0463；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794202705_c864f2bc` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794202817_eeb3053a` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.10078061583788536，回撤=0.0238918260277448。
  - `factory_1794202751_495baffa` 601857+601888·均线交叉·快6慢20：gate=fail，grade=D，Sharpe=-0.583758037770181，回撤=0.031623207455749924。

### `factory_run_1794203281_4e0810e1`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 13:55:15 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.1781; 最大回撤 0.0463；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794203523_9e97c935` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794203523_a6e07833` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794203602_609f5b11` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.10078061583788536，回撤=0.0238918260277448。

### `factory_run_1794204020_125fa581`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 14:02:48 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.8324; 最大回撤 0.0523；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794204091_73c72f63` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794204091_39d55cd9` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794204091_860329ed` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794204472_92ceeb69`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 14:12:39 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -0.9378; 最大回撤 0.0495；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794204614_7af81b61` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794204614_eae7ce44` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794204746_fb93f2b6` 600690+603259·价值精选·10日轮动：gate=fail，grade=B，Sharpe=-0.3702557791519448，回撤=0.04303872781603799。

### `factory_run_1794205067_e311b7f5`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 14:22:41 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 9；平均 Sharpe -1.0587; 最大回撤 0.0472；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794205250_17d083ac` 600690+603259·动量突破·20日：gate=fail，grade=C，Sharpe=0.3759807702531632，回撤=0.021934555158760322。
  - `factory_1794205316_ef955d25` 300760+603259·波动突破·20日：gate=fail，grade=B，Sharpe=0.029287353648709154，回撤=0.02744613641760008。
  - `factory_1794205223_2e90a6ca` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794205669_27bd28d3`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 14:33:17 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 12；平均 Sharpe -1.2597; 最大回撤 0.0463；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (11), target_risk_execution_assumptions_mismatch (11), missing_stress_loss_percent (11), missing_var_percent (11), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794205853_15cf9b4b` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794205853_151addb4` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794205905_7a62fbcb` 000651+600690·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=-0.6286915624622069，回撤=0.03448260537910358。

### `factory_run_1794206305_74992311`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 14:43:17 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.1781; 最大回撤 0.0463；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794206444_47bf05bb` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794206444_33f38c4a` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794206507_78888219` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.10078061583788536，回撤=0.0238918260277448。

### `factory_run_1794206903_ab9caa7c`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 14:53:49 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 11；平均 Sharpe -0.9083; 最大回撤 0.0523；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (11), target_risk_execution_assumptions_mismatch (11), missing_stress_loss_percent (11), missing_var_percent (11), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794207089_b055aca0` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794207049_039ac1f4` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794207049_5c04cd6f` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794207537_daf1e9e8`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 15:05:01 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.9613; 最大回撤 0.05；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794207740_c21ebf36` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794207697_294f39a4` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794207697_2fd33c4d` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794208218_d9787662`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 15:25:21 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.0059; 最大回撤 0.053；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794208958_d4451da0` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794208958_9b138213` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794209069_f696918c` 600690+603259·波动突破·20日：gate=fail，grade=D，Sharpe=-0.010590797946611049，回撤=0.028643539568589994。

### `factory_run_1794209429_c5068dae`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 15:35:34 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -1.0501; 最大回撤 0.0486；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794209657_589a4934` 300760+603259·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=0.036632229467697744，回撤=0.04000556584334507。
  - `factory_1794209605_70712cbd` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794209605_98cf2c8a` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794210040_f6adf73a`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 15:44:34 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 11；平均 Sharpe -1.0676; 最大回撤 0.0501；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (11), target_risk_execution_assumptions_mismatch (11), missing_stress_loss_percent (11), missing_var_percent (11), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794210210_d8f6a26f` 300760+603259·均线交叉·快5慢20：gate=fail，grade=C，Sharpe=0.04292406601179829，回撤=0.03162687193803298。
  - `factory_1794210169_d6c164c7` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794210169_0f11352b` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794210579_ab77018f`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 15:53:16 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 9；平均 Sharpe -0.8401; 最大回撤 0.0463；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794210681_60a0a683` 600690+603259·动量突破·20日：gate=fail，grade=C，Sharpe=0.3759807702531632，回撤=0.021934555158760322。
  - `factory_1794210757_7c0c7e22` 300760+603259·波动突破·20日：gate=fail，grade=B，Sharpe=0.029287353648709154，回撤=0.02744613641760008。
  - `factory_1794210681_c8e3182a` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794211104_45fb5fb4`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 16:02:28 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.9613; 最大回撤 0.05；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794211242_3657e011` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794211217_e6ec3562` 300593·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794211217_b44e05f8` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794211655_d0333e3a`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 16:12:58 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -1.3643; 最大回撤 0.0654；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794211887_69d8dd5d` 300760+603259·多因子：gate=fail，grade=C，Sharpe=0.05936233799035221，回撤=0.03258204169065075。
  - `factory_1794211845_b6eb71df` 301526·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794211845_55873155` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794212290_d5c3149c`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 16:23:02 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 59, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -0.946; 最大回撤 0.0495；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794212442_b72a0956` 301526·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794212442_fcc55757` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794212565_bf281fa2` 600690+603259·价值精选·10日轮动：gate=fail，grade=B，Sharpe=-0.3702557791519448，回撤=0.04303872781603799。

### `factory_run_1794212889_21c927e1`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 16:32:54 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 10；平均 Sharpe -0.9719; 最大回撤 0.05；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794213064_ce8f44b1` 300760+603259·动量突破·20日：gate=fail，grade=D，Sharpe=0.38615357240906745，回撤=0.021766501146577185。
  - `factory_1794213034_ee96ebf9` 301526·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794213034_8f771b82` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794213488_854bec02`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 16:44:51 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 240, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 9；平均 Sharpe -1.0886; 最大回撤 0.0475；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794213723_a6fc6363` 600690+603259·动量突破·20日：gate=fail，grade=C，Sharpe=0.3759807702531632，回撤=0.021934555158760322。
  - `factory_1794213815_a43c4a88` 300760+603259·波动突破·20日：gate=fail，grade=B，Sharpe=0.029287353648709154，回撤=0.02744613641760008。
  - `factory_1794213680_c74e9b6a` 301526·事件结构突破·20日：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。

### `factory_run_1794214199_52cc80c4`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 16:55:06 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 237, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 12；平均 Sharpe -1.1196; 最大回撤 0.0663；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794214356_f6fadc21` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794214460_07e69ecb` 600690+603259·价值精选·20日轮动：gate=fail，grade=B，Sharpe=-0.3702557791519448，回撤=0.04303872781603799。
  - `factory_1794214356_de996d2f` 600028+601138·动量突破·20日：gate=fail，grade=D，Sharpe=-0.40731788786033274，回撤=0.043594494291313936。

### `factory_run_1794214813_3c15bc71`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 17:04:38 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 237, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 13；平均 Sharpe -1.4007; 最大回撤 0.0663；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (11), target_risk_execution_assumptions_mismatch (11), missing_stress_loss_percent (11), missing_var_percent (11), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794214935_3af64fd2` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794214986_a5ab6b7b` 600690+603259·均线交叉·快6慢30：gate=fail，grade=C，Sharpe=-0.09852275819761588，回撤=0.04247734048746603。
  - `factory_1794214961_57037ccc` 600028+601857·动量突破·20日：gate=fail，grade=D，Sharpe=-0.5858555670835144，回撤=0.03330970009058022。

### `factory_run_1794215385_91ae667a`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 17:14:10 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 237, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 12；平均 Sharpe -1.3636; 最大回撤 0.0663；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794215508_5002856a` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794215566_e5fc6040` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.09901513421028511，回撤=0.02369541545709264。
  - `factory_1794215536_5bddac04` 601857+601888·均线交叉·快6慢20：gate=fail，grade=D，Sharpe=-0.583758037770181，回撤=0.031623207455749924。

### `factory_run_1794215956_5eaff3fd`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 17:23:57 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 237, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.2178; 最大回撤 0.0758；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794216184_caf56ea7` 300760+603259·价值精选·20日轮动：gate=fail，grade=B，Sharpe=0.05220617258537874，回撤=0.024413433099971543。
  - `factory_1794216098_b1ef1371` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794216125_889b36bd` 600690+603259·均线交叉·快6慢20：gate=fail，grade=C，Sharpe=-0.31474533382623415，回撤=0.04676586815264867。

### `factory_run_1794216544_fd3ff4ba`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 17:33:30 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 237, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 0；负 Sharpe 11；平均 Sharpe -1.3698; 最大回撤 0.0758；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794216726_cf566f87` 600690+603259·多因子：gate=fail，grade=C，Sharpe=0.48638006033321823，回撤=0.02173578657936427。
  - `factory_1794216666_050e7477` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794216692_660592e0` 600028+601857·均线交叉·快6慢20：gate=fail，grade=B，Sharpe=-0.537422630875456，回撤=0.039112993855592575。

### `factory_run_1794217117_3bbad398`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 17:43:26 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 237, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 17；通过 0；负 Sharpe 15；平均 Sharpe -1.2153; 最大回撤 0.0758；关联方式 `factory_run_id`。
- 主要阻塞：missing_stress_loss_percent (14), target_risk_target_mismatch (14), target_risk_execution_assumptions_mismatch (14), missing_var_percent (14), target_risk_execution_assumptions_missing (12)。
- 代表样本：
  - `factory_1794217342_c1e316e6` 600690+603259·波动突破·20日：gate=fail，grade=D，Sharpe=0.01389334562980379，回撤=0.028723203377011043。
  - `factory_1794217274_a5c1bbd5` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794217314_dc5d190a` 600941+601166·均线交叉·快5慢20：gate=fail，grade=C，Sharpe=-0.34439292620113554，回撤=0.026439604221214663。

### `factory_run_1794217714_0e579136`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 17:54:04 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 237, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 13；平均 Sharpe -1.2962; 最大回撤 0.1755；关联方式 `factory_run_id`。
- 主要阻塞：target_risk_target_mismatch (10), target_risk_execution_assumptions_mismatch (10), missing_stress_loss_percent (10), missing_var_percent (10), target_risk_execution_assumptions_missing (9)。
- 代表样本：
  - `factory_1794217850_e33e079d` 多因子gp_classic_momentum_slope_60_20_3_mut主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794217915_e73a8e14` 000651+600690·多因子：gate=fail，grade=D，Sharpe=-0.06955030932490741，回撤=0.0238918260277448。
  - `factory_1794217896_5181afb6` 601857+601888·均线交叉·快6慢20：gate=fail，grade=D，Sharpe=-0.6077404109126509，回撤=0.031623207455749924。

### `factory_run_1794218352_a41f63d2`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-09 18:03:59 +0800`。
- 市场快照：`{"date": "2026-11-09", "fear_greed": 60, "fg_level": "greed", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 237, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 17；通过 0；负 Sharpe 15；平均 Sharpe -1.3553; 最大回撤 0.0934；关联方式 `factory_run_id`。
- 主要阻塞：missing_stress_loss_percent (14), target_risk_target_mismatch (14), target_risk_execution_assumptions_mismatch (14), missing_var_percent (14), target_risk_execution_assumptions_missing (12)。
- 代表样本：
  - `factory_1794218570_e2011139` 600690+603259·多因子：gate=fail，grade=C，Sharpe=0.48638006033321823，回撤=0.02173578657936427。
  - `factory_1794218505_d11e3ab1` 多因子seed_volume_ratio主导(60日)：gate=fail，grade=D，Sharpe=0.0，回撤=0.0。
  - `factory_1794218620_91542dfb` 600000+601857·价值精选·20日轮动：gate=fail，grade=B，Sharpe=-0.18732746777234283，回撤=0.026687582035728075。

## 口径与边界

- 该文档记录观察到的运行和质量证据，不把 `success` 自动解释为策略可交易。
- 监督器的四个运行时为 Market Event Ingest、Factor Mining、Strategy Factory、Incubation；SignalTracker 作为单独 sidecar 状态保留在运行环境中。
- 质量报告缺失或无法按 `factory_run_id` 关联时会保留时间窗口回退结果，并在 JSON 状态中体现报告数量。
