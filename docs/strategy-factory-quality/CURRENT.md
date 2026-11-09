# 四工厂策略质量审查记录 — 2026-11-02

> 由 `monitor_factory_quality.py` 持续生成；最后更新：2026-11-02 12:05:45 +0800。
> 数据源：四工厂 supervisor heartbeat、`strategy_factory_runs`、`strategy_quality_reports`。观察器只读数据库，不触发工厂运行。

## 当前运行健康

- supervisor：**healthy**；heartbeat：`2026-11-02 12:05:01 +0800`。
- 工厂状态：`{"strategy_factory": "running", "factor_mining_factory": "running", "incubation_factory": "running", "market_event_ingest": "running"}`；重启次数：`{"strategy_factory": 1, "factor_mining_factory": 0, "incubation_factory": 0, "market_event_ingest": 0}`。
- 当前策略库存：`{"archived_draft": 17, "deprecated": 124, "draft": 1001, "incubating": 397, "rejected": 6175, "submitted": 8333}`。
- 最近侧链摘要：`{"market_event_ingest": {"round": 14, "timestamp": "2026-11-02 11:17:59", "saved_docs": 110, "normalized_total": 57, "normalized_verified": 26, "errors": 0, "signals": 151, "elapsed_sec": 44}, "factor_mining": {"cycle": 2, "timestamp": "2026-11-01 18:34:18", "raw_candidates": 4, "evolved": 4, "validated": 1, "admitted": 0, "pool_size": 12}, "incubation": {"timestamp": "2026-11-01 18:30:43", "elapsed_sec": 43.4, "status": "partial", "verified": 1700, "promoted": 0, "execution_audit": {"status": "pending_evidence", "evaluated": 80, "saved_signal_evidence": 156, "available_signal_evidence": 240, "hard_gate_passed": 0}}}`。

## 当日汇总

- 完成策略工厂轮次：**146**
- 质量报告：**1513**；孵化评估通过：**115**；准入率：**7.6%**
- 计数口径 v2：仅精确关联 factory_run_id、admission_stage=incubation/live 且 incubation 评估明确通过、无硬拒绝的报告计为孵化评估通过；不使用历史 passed 标记。
- 以上仅为孵化评估通过报告数，不代表实际获配孵化预算/持久化准入，不等于独立高质量策略数或可实盘交易；首批30个及持续性目标仍需去重、收益/风险、样本外与执行证据独立审查。

## 每轮记录

| # | run_id | 完成时间 | 运行 | 质量报告 | 通过率 | 负 Sharpe | 平均 Sharpe | 最大回撤 | 审查结论 |
|---:|---|---|---|---:|---:|---:|---:|---:|---|
| 1 | `factory_run_1793548808_885e1c84` | 2026-11-02 00:01:28 +0800 | success / 80.8s | 1/19 | 5.3% | 13 | -0.607 | 0.124 | 保留观察：存在 gate/风险证据 |
| 2 | `factory_run_1793549108_d338e2d3` | 2026-11-02 00:06:11 +0800 | success_observe_only / 63.2s | 0/15 | 0.0% | 14 | -0.7131 | 0.1243 | 运行状态 success_observe_only，需复核 |
| 3 | `factory_run_1793549408_2d7edfad` | 2026-11-02 00:11:07 +0800 | success / 58.8s | 1/14 | 7.1% | 8 | -1.0817 | 0.1271 | 保留观察：存在 gate/风险证据 |
| 4 | `factory_run_1793549708_b8cfc11b` | 2026-11-02 00:16:22 +0800 | success / 74.5s | 2/23 | 8.7% | 16 | -1.4211 | 0.08 | 保留观察：存在 gate/风险证据 |
| 5 | `factory_run_1793550008_0e1404b5` | 2026-11-02 00:22:20 +0800 | success / 132.6s | 3/23 | 13.0% | 15 | -0.9907 | 0.287 | 保留观察：存在 gate/风险证据 |
| 6 | `factory_run_1793550308_b2513dd6` | 2026-11-02 00:27:12 +0800 | success / 124.4s | 1/23 | 4.3% | 15 | -0.8607 | 0.3262 | 保留观察：存在 gate/风险证据 |
| 7 | `factory_run_1793550608_6a3c69ea` | 2026-11-02 00:32:29 +0800 | success / 141.0s | 4/24 | 16.7% | 15 | -1.2953 | 0.1394 | 保留观察：存在 gate/风险证据 |
| 8 | `factory_run_1793550908_7e03a997` | 2026-11-02 00:36:21 +0800 | success / 73.2s | 2/20 | 10.0% | 10 | -0.8682 | 0.114 | 保留观察：存在 gate/风险证据 |
| 9 | `factory_run_1793551208_a62d5032` | 2026-11-02 00:41:22 +0800 | success / 73.9s | 2/20 | 10.0% | 12 | -0.884 | 0.1109 | 保留观察：存在 gate/风险证据 |
| 10 | `factory_run_1793551508_f7c9e9e5` | 2026-11-02 00:46:22 +0800 | success / 74.7s | 2/20 | 10.0% | 12 | -0.9017 | 0.1088 | 保留观察：存在 gate/风险证据 |
| 11 | `factory_run_1793551808_64301b59` | 2026-11-02 00:51:19 +0800 | success / 71.6s | 2/18 | 11.1% | 11 | -1.0492 | 0.1087 | 保留观察：存在 gate/风险证据 |
| 12 | `factory_run_1793552108_cc2cd8ad` | 2026-11-02 00:56:26 +0800 | success / 78.2s | 2/21 | 9.5% | 12 | -0.8482 | 0.1244 | 保留观察：存在 gate/风险证据 |
| 13 | `factory_run_1793552408_da45229a` | 2026-11-02 01:01:19 +0800 | success / 71.6s | 2/18 | 11.1% | 11 | -0.9439 | 0.1008 | 保留观察：存在 gate/风险证据 |
| 14 | `factory_run_1793552708_952cb15f` | 2026-11-02 01:06:23 +0800 | success / 75.5s | 2/20 | 10.0% | 13 | -0.9051 | 0.1095 | 保留观察：存在 gate/风险证据 |
| 15 | `factory_run_1793553008_53b52702` | 2026-11-02 01:11:22 +0800 | success / 74.3s | 1/19 | 5.3% | 12 | -0.9447 | 0.1007 | 保留观察：存在 gate/风险证据 |
| 16 | `factory_run_1793553308_a4f418c2` | 2026-11-02 01:16:02 +0800 | success_observe_only / 54.2s | 0/14 | 0.0% | 12 | -1.2357 | 0.133 | 运行状态 success_observe_only，需复核 |
| 17 | `factory_run_1793553608_2dcbc1c4` | 2026-11-02 01:21:07 +0800 | success_observe_only / 59.6s | 0/14 | 0.0% | 10 | -1.2294 | 0.1242 | 运行状态 success_observe_only，需复核 |
| 18 | `factory_run_1793553908_6d8e4a8f` | 2026-11-02 01:26:10 +0800 | success / 62.2s | 1/15 | 6.7% | 11 | -1.1098 | 0.1244 | 保留观察：存在 gate/风险证据 |
| 19 | `factory_run_1793554208_531e35ca` | 2026-11-02 01:31:23 +0800 | success / 74.6s | 1/20 | 5.0% | 10 | -0.789 | 0.1271 | 保留观察：存在 gate/风险证据 |
| 20 | `factory_run_1793554508_5aeb5626` | 2026-11-02 01:36:09 +0800 | success / 61.5s | 1/14 | 7.1% | 10 | -1.0947 | 0.1259 | 保留观察：存在 gate/风险证据 |
| 21 | `factory_run_1793554808_a8503300` | 2026-11-02 01:41:22 +0800 | success / 73.8s | 1/20 | 5.0% | 11 | -0.8625 | 0.1008 | 保留观察：存在 gate/风险证据 |
| 22 | `factory_run_1793555108_78f9f719` | 2026-11-02 01:46:09 +0800 | success / 61.5s | 1/14 | 7.1% | 12 | -1.277 | 0.1088 | 保留观察：存在 gate/风险证据 |
| 23 | `factory_run_1793555408_dc19a860` | 2026-11-02 01:51:23 +0800 | success / 75.6s | 2/20 | 10.0% | 9 | -0.8242 | 0.1144 | 保留观察：存在 gate/风险证据 |
| 24 | `factory_run_1793555708_291721b2` | 2026-11-02 01:56:33 +0800 | success / 85.3s | 1/23 | 4.3% | 16 | -1.458 | 0.08 | 保留观察：存在 gate/风险证据 |
| 25 | `factory_run_1793556008_73412126` | 2026-11-02 02:01:09 +0800 | success_observe_only / 60.9s | 0/16 | 0.0% | 12 | -1.3719 | 0.08 | 运行状态 success_observe_only，需复核 |
| 26 | `factory_run_1793556308_dd5481fc` | 2026-11-02 02:06:10 +0800 | success / 62.1s | 1/14 | 7.1% | 10 | -1.1033 | 0.1261 | 保留观察：存在 gate/风险证据 |
| 27 | `factory_run_1793556608_6e681c26` | 2026-11-02 02:11:30 +0800 | success_observe_only / 82.6s | 0/23 | 0.0% | 14 | -2.6346 | 0.08 | 运行状态 success_observe_only，需复核 |
| 28 | `factory_run_1793556908_3dc3fb4e` | 2026-11-02 02:16:26 +0800 | success / 78.0s | 1/17 | 5.9% | 15 | -3.8344 | 0.1109 | 保留观察：存在 gate/风险证据 |
| 29 | `factory_run_1793557208_9fa66d1a` | 2026-11-02 02:21:31 +0800 | success_observe_only / 82.7s | 0/21 | 0.0% | 13 | -1.2725 | 0.133 | 运行状态 success_observe_only，需复核 |
| 30 | `factory_run_1793557508_7dda8396` | 2026-11-02 02:26:03 +0800 | success_observe_only / 54.8s | 0/15 | 0.0% | 12 | -1.3199 | 0.1008 | 运行状态 success_observe_only，需复核 |
| 31 | `factory_run_1793557808_e92fbfc8` | 2026-11-02 02:31:44 +0800 | success / 96.0s | 1/15 | 6.7% | 9 | -1.9123 | 0.1141 | 保留观察：存在 gate/风险证据 |
| 32 | `factory_run_1793558108_db48031a` | 2026-11-02 02:36:23 +0800 | success / 75.2s | 2/19 | 10.5% | 12 | -1.0067 | 0.1087 | 保留观察：存在 gate/风险证据 |
| 33 | `factory_run_1793558408_8a8a531e` | 2026-11-02 02:41:24 +0800 | success / 76.0s | 2/20 | 10.0% | 13 | -1.0172 | 0.1011 | 保留观察：存在 gate/风险证据 |
| 34 | `factory_run_1793558708_4db9a686` | 2026-11-02 02:46:39 +0800 | success / 90.8s | 2/21 | 9.5% | 11 | -1.0371 | 0.1237 | 保留观察：存在 gate/风险证据 |
| 35 | `factory_run_1793559008_11e21610` | 2026-11-02 02:51:23 +0800 | success / 74.8s | 1/19 | 5.3% | 10 | -0.8668 | 0.1261 | 保留观察：存在 gate/风险证据 |
| 36 | `factory_run_1793559308_839942e3` | 2026-11-02 02:56:10 +0800 | success / 61.9s | 1/15 | 6.7% | 10 | -1.0513 | 0.1255 | 保留观察：存在 gate/风险证据 |
| 37 | `factory_run_1793559608_27dafa1a` | 2026-11-02 03:01:40 +0800 | success / 92.0s | 2/21 | 9.5% | 13 | -2.8804 | 0.1007 | 保留观察：存在 gate/风险证据 |
| 38 | `factory_run_1793559908_c1b11592` | 2026-11-02 03:06:39 +0800 | success / 90.8s | 2/21 | 9.5% | 12 | -7.359 | 0.1242 | 保留观察：存在 gate/风险证据 |
| 39 | `factory_run_1793560208_4d65ddad` | 2026-11-02 03:11:41 +0800 | success / 92.6s | 2/20 | 10.0% | 10 | -1.1187 | 0.1141 | 保留观察：存在 gate/风险证据 |
| 40 | `factory_run_1793560508_bb70ccb7` | 2026-11-02 03:16:17 +0800 | success_observe_only / 69.3s | 0/20 | 0.0% | 13 | -0.9081 | 0.1234 | 运行状态 success_observe_only，需复核 |
| 41 | `factory_run_1793560808_860c8245` | 2026-11-02 03:21:46 +0800 | success / 98.4s | 1/15 | 6.7% | 10 | -11.7653 | 0.1247 | 保留观察：存在 gate/风险证据 |
| 42 | `factory_run_1793561108_547ac0d1` | 2026-11-02 03:26:36 +0800 | success / 88.0s | 1/21 | 4.8% | 14 | -2.8451 | 0.1008 | 保留观察：存在 gate/风险证据 |
| 43 | `factory_run_1793561408_e39d22b5` | 2026-11-02 03:31:50 +0800 | success / 101.7s | 1/16 | 6.2% | 9 | -1.771 | 0.1142 | 保留观察：存在 gate/风险证据 |
| 44 | `factory_run_1793561708_cc98454d` | 2026-11-02 03:36:33 +0800 | success / 85.0s | 1/22 | 4.5% | 15 | -3.0762 | 0.0964 | 保留观察：存在 gate/风险证据 |
| 45 | `factory_run_1793562008_8d1ad2ad` | 2026-11-02 03:42:30 +0800 | success / 142.3s | 3/24 | 12.5% | 15 | -6.6643 | 0.3614 | 保留观察：存在 gate/风险证据 |
| 46 | `factory_run_1793562308_d8b7e317` | 2026-11-02 03:46:40 +0800 | success / 91.6s | 2/21 | 9.5% | 14 | -1.4486 | 0.1109 | 保留观察：存在 gate/风险证据 |
| 47 | `factory_run_1793562608_6b040fb7` | 2026-11-02 03:51:23 +0800 | success / 74.5s | 2/21 | 9.5% | 13 | -1.1096 | 0.0996 | 保留观察：存在 gate/风险证据 |
| 48 | `factory_run_1793562908_30276b88` | 2026-11-02 03:57:31 +0800 | success / 143.3s | 3/24 | 12.5% | 17 | -1.4379 | 0.3614 | 保留观察：存在 gate/风险证据 |
| 49 | `factory_run_1793563208_9518497e` | 2026-11-02 04:01:23 +0800 | success / 75.2s | 1/19 | 5.3% | 10 | -0.8238 | 0.1261 | 保留观察：存在 gate/风险证据 |
| 50 | `factory_run_1793563508_be9e2d96` | 2026-11-02 04:06:13 +0800 | success / 64.5s | 1/16 | 6.2% | 14 | -1.2299 | 0.1109 | 保留观察：存在 gate/风险证据 |
| 51 | `factory_run_1793563808_c9dcca3b` | 2026-11-02 04:11:39 +0800 | success / 91.1s | 1/21 | 4.8% | 10 | -1.029 | 0.1136 | 保留观察：存在 gate/风险证据 |
| 52 | `factory_run_1793564108_858bbfd2` | 2026-11-02 04:16:03 +0800 | success_observe_only / 54.6s | 0/14 | 0.0% | 12 | -1.2802 | 0.1008 | 运行状态 success_observe_only，需复核 |
| 53 | `factory_run_1793564408_79253afc` | 2026-11-02 04:21:18 +0800 | success_observe_only / 69.7s | 0/15 | 0.0% | 11 | -1.284 | 0.1008 | 运行状态 success_observe_only，需复核 |
| 54 | `factory_run_1793564708_93cce4b6` | 2026-11-02 04:26:11 +0800 | success / 62.4s | 1/15 | 6.7% | 10 | -1.0498 | 0.1259 | 保留观察：存在 gate/风险证据 |
| 55 | `factory_run_1793565008_515a9890` | 2026-11-02 04:31:21 +0800 | success / 72.7s | 2/20 | 10.0% | 12 | -0.9094 | 0.1008 | 保留观察：存在 gate/风险证据 |
| 56 | `factory_run_1793565308_0d8ef8a4` | 2026-11-02 04:36:24 +0800 | success / 75.7s | 1/20 | 5.0% | 13 | -0.9544 | 0.1008 | 保留观察：存在 gate/风险证据 |
| 57 | `factory_run_1793565608_03f1002c` | 2026-11-02 04:41:18 +0800 | success_observe_only / 69.8s | 0/14 | 0.0% | 10 | -1.2309 | 0.1244 | 运行状态 success_observe_only，需复核 |
| 58 | `factory_run_1793565908_2e1c3814` | 2026-11-02 04:46:10 +0800 | success / 61.6s | 2/14 | 14.3% | 9 | -1.1176 | 0.1142 | 保留观察：存在 gate/风险证据 |
| 59 | `factory_run_1793566208_c6ad4808` | 2026-11-02 04:51:17 +0800 | success_observe_only / 68.6s | 0/19 | 0.0% | 12 | -0.9601 | 0.1009 | 运行状态 success_observe_only，需复核 |
| 60 | `factory_run_1793566508_8871e1fb` | 2026-11-02 04:56:11 +0800 | success / 62.5s | 1/14 | 7.1% | 11 | -1.2632 | 0.1109 | 保留观察：存在 gate/风险证据 |
| 61 | `factory_run_1793566808_53452a43` | 2026-11-02 05:01:27 +0800 | success / 79.1s | 2/20 | 10.0% | 9 | -0.7428 | 0.1244 | 保留观察：存在 gate/风险证据 |
| 62 | `factory_run_1793567108_0caba2d7` | 2026-11-02 05:06:19 +0800 | success_observe_only / 70.7s | 0/20 | 0.0% | 13 | -0.9703 | 0.124 | 运行状态 success_observe_only，需复核 |
| 63 | `factory_run_1793567408_a52384bc` | 2026-11-02 05:11:25 +0800 | success / 76.8s | 1/13 | 7.7% | 8 | -1.1837 | 0.1257 | 保留观察：存在 gate/风险证据 |
| 64 | `factory_run_1793567708_6d7978ed` | 2026-11-02 05:16:37 +0800 | success / 88.6s | 1/23 | 4.3% | 14 | -6.9321 | 0.0864 | 保留观察：存在 gate/风险证据 |
| 65 | `factory_run_1793568008_c9afcf10` | 2026-11-02 05:21:47 +0800 | success_observe_only / 98.9s | 0/18 | 0.0% | 13 | -4.0663 | 0.0832 | 运行状态 success_observe_only，需复核 |
| 66 | `factory_run_1793568308_4297d47a` | 2026-11-02 05:26:26 +0800 | success / 78.1s | 2/19 | 10.5% | 13 | -1.3318 | 0.1242 | 保留观察：存在 gate/风险证据 |
| 67 | `factory_run_1793568608_c0e0211a` | 2026-11-02 05:31:31 +0800 | success / 82.5s | 2/21 | 9.5% | 12 | -0.8165 | 0.1261 | 保留观察：存在 gate/风险证据 |
| 68 | `factory_run_1793568908_ca02750e` | 2026-11-02 05:38:07 +0800 | success / 178.6s | 3/25 | 12.0% | 18 | -2.5843 | 0.341 | 保留观察：存在 gate/风险证据 |
| 69 | `factory_run_1793569208_fae42711` | 2026-11-02 05:41:32 +0800 | success_observe_only / 83.4s | 0/20 | 0.0% | 13 | -2.9543 | 0.1019 | 运行状态 success_observe_only，需复核 |
| 70 | `factory_run_1793569508_3d105537` | 2026-11-02 05:46:26 +0800 | success / 77.7s | 1/17 | 5.9% | 11 | -9.0904 | 0.114 | 保留观察：存在 gate/风险证据 |
| 71 | `factory_run_1793569808_3d2a16a1` | 2026-11-02 05:51:43 +0800 | success / 94.2s | 2/22 | 9.1% | 12 | -1.1106 | 0.1259 | 保留观察：存在 gate/风险证据 |
| 72 | `factory_run_1793570108_d9ed656e` | 2026-11-02 05:56:25 +0800 | success / 76.7s | 2/19 | 10.5% | 12 | -0.9936 | 0.1087 | 保留观察：存在 gate/风险证据 |
| 73 | `factory_run_1793570408_91a5d923` | 2026-11-02 06:01:41 +0800 | success / 92.1s | 1/21 | 4.8% | 14 | -2.8428 | 0.1242 | 保留观察：存在 gate/风险证据 |
| 74 | `factory_run_1793570708_8e65db70` | 2026-11-02 06:06:25 +0800 | success / 76.9s | 1/16 | 6.2% | 14 | -3.8055 | 0.1008 | 保留观察：存在 gate/风险证据 |
| 75 | `factory_run_1793571008_e495c672` | 2026-11-02 06:11:39 +0800 | success / 90.4s | 1/21 | 4.8% | 13 | -2.7632 | 0.1008 | 保留观察：存在 gate/风险证据 |
| 76 | `factory_run_1793571308_03e4f576` | 2026-11-02 06:16:18 +0800 | success_observe_only / 69.2s | 0/16 | 0.0% | 14 | -3.6772 | 0.1008 | 运行状态 success_observe_only，需复核 |
| 77 | `factory_run_1793571608_ab596057` | 2026-11-02 06:21:56 +0800 | success / 107.8s | 1/16 | 6.2% | 12 | -2.2374 | 0.1087 | 保留观察：存在 gate/风险证据 |
| 78 | `factory_run_1793571908_02998bd8` | 2026-11-02 06:26:19 +0800 | success_observe_only / 70.9s | 0/20 | 0.0% | 13 | -0.9496 | 0.1008 | 运行状态 success_observe_only，需复核 |
| 79 | `factory_run_1793572208_a48f29eb` | 2026-11-02 06:31:25 +0800 | success / 76.6s | 1/14 | 7.1% | 7 | -1.0602 | 0.1141 | 保留观察：存在 gate/风险证据 |
| 80 | `factory_run_1793572508_22c18106` | 2026-11-02 06:39:41 +0800 | success / 272.7s | 18/39 | 46.2% | 31 | -1.879 | 0.345 | 保留观察：存在 gate/风险证据 |
| 81 | `factory_run_1793572808_508dcddc` | 2026-11-02 06:40:13 +0800 | skipped / 4.2s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 82 | `factory_run_1793573109_a31da1a1` | 2026-11-02 06:45:10 +0800 | skipped / 1.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 83 | `factory_run_1793573408_a8ad8ff2` | 2026-11-02 06:50:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 84 | `factory_run_1793573708_f2422f96` | 2026-11-02 06:55:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 85 | `factory_run_1793574008_c8873923` | 2026-11-02 07:00:10 +0800 | skipped / 1.6s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 86 | `factory_run_1793574309_17279028` | 2026-11-02 07:05:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 87 | `factory_run_1793574608_af028383` | 2026-11-02 07:10:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 88 | `factory_run_1793574909_91cdd8d1` | 2026-11-02 07:15:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 89 | `factory_run_1793575209_88a6ac85` | 2026-11-02 07:20:10 +0800 | skipped / 1.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 90 | `factory_run_1793575509_c6fd6232` | 2026-11-02 07:25:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 91 | `factory_run_1793575809_7e24fde3` | 2026-11-02 07:30:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 92 | `factory_run_1793576109_b53c9bf3` | 2026-11-02 07:35:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 93 | `factory_run_1793576409_a4be5611` | 2026-11-02 07:40:10 +0800 | skipped / 1.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 94 | `factory_run_1793576709_d446d4a2` | 2026-11-02 07:45:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 95 | `factory_run_1793577009_58863300` | 2026-11-02 07:50:10 +0800 | skipped / 1.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 96 | `factory_run_1793577309_c5f89e0e` | 2026-11-02 07:55:11 +0800 | skipped / 1.9s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 97 | `factory_run_1793577609_3d9409e7` | 2026-11-02 08:00:11 +0800 | skipped / 2.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 98 | `factory_run_1793577909_b76eaee7` | 2026-11-02 08:05:11 +0800 | skipped / 2.3s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 99 | `factory_run_1793578209_beb3d12d` | 2026-11-02 08:10:12 +0800 | skipped / 3.7s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 100 | `factory_run_1793578509_a8b9a799` | 2026-11-02 08:15:11 +0800 | skipped / 2.1s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 101 | `factory_run_1793578809_d8b918a6` | 2026-11-02 08:20:11 +0800 | skipped / 2.3s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 102 | `factory_run_1793579109_bd96c4b2` | 2026-11-02 08:25:11 +0800 | skipped / 2.6s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 103 | `factory_run_1793579409_5015021d` | 2026-11-02 08:30:11 +0800 | skipped / 2.6s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 104 | `factory_run_1793579709_bd9ee059` | 2026-11-02 08:35:11 +0800 | skipped / 2.6s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 105 | `factory_run_1793580009_6a20f7cc` | 2026-11-02 08:40:11 +0800 | skipped / 2.1s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 106 | `factory_run_1793580309_22de5799` | 2026-11-02 08:45:11 +0800 | skipped / 2.3s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 107 | `factory_run_1793580609_469860ab` | 2026-11-02 08:50:11 +0800 | skipped / 2.8s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 108 | `factory_run_1793580909_8f21ee98` | 2026-11-02 08:55:10 +0800 | skipped / 1.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 109 | `factory_run_1793581209_cb051af3` | 2026-11-02 09:00:10 +0800 | skipped / 1.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 110 | `factory_run_1793581509_65f26f25` | 2026-11-02 09:05:11 +0800 | skipped / 2.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 111 | `factory_run_1793581809_7781ae64` | 2026-11-02 09:10:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 112 | `factory_run_1793582109_d1e27cb3` | 2026-11-02 09:15:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 113 | `factory_run_1793582409_39471f4a` | 2026-11-02 09:20:11 +0800 | skipped / 1.8s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 114 | `factory_run_1793582709_e9f327bb` | 2026-11-02 09:25:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 115 | `factory_run_1793583009_d7c5a70c` | 2026-11-02 09:30:11 +0800 | skipped / 1.9s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 116 | `factory_run_1793583309_ec2907f1` | 2026-11-02 09:35:11 +0800 | skipped / 2.3s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 117 | `factory_run_1793583609_e2c14425` | 2026-11-02 09:40:11 +0800 | skipped / 2.3s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 118 | `factory_run_1793583909_225af888` | 2026-11-02 09:45:11 +0800 | skipped / 2.2s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 119 | `factory_run_1793584209_70d77c2c` | 2026-11-02 09:50:11 +0800 | skipped / 2.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 120 | `factory_run_1793584509_e68e17e6` | 2026-11-02 09:55:10 +0800 | skipped / 1.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 121 | `factory_run_1793584809_98a622a0` | 2026-11-02 10:00:11 +0800 | skipped / 2.6s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 122 | `factory_run_1793585109_8bcf7353` | 2026-11-02 10:05:11 +0800 | skipped / 2.1s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 123 | `factory_run_1793585409_4b532f59` | 2026-11-02 10:10:11 +0800 | skipped / 2.7s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 124 | `factory_run_1793585709_dc41da3f` | 2026-11-02 10:15:11 +0800 | skipped / 2.4s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 125 | `factory_run_1793586009_752ebe25` | 2026-11-02 10:20:11 +0800 | skipped / 2.3s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 126 | `factory_run_1793586309_1e03824f` | 2026-11-02 10:25:11 +0800 | skipped / 1.8s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 127 | `factory_run_1793586609_4075f630` | 2026-11-02 10:30:11 +0800 | skipped / 1.9s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 128 | `factory_run_1793586909_fb2aae4b` | 2026-11-02 10:35:11 +0800 | skipped / 1.9s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 129 | `factory_run_1793587209_0e2c3cb3` | 2026-11-02 10:40:11 +0800 | skipped / 1.8s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 130 | `factory_run_1793587509_06f9ac7e` | 2026-11-02 10:45:11 +0800 | skipped / 1.9s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 131 | `factory_run_1793587809_f760549f` | 2026-11-02 10:50:11 +0800 | skipped / 1.8s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 132 | `factory_run_1793588109_4d75d611` | 2026-11-02 10:55:11 +0800 | skipped / 1.9s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 133 | `factory_run_1793588409_2f4fa114` | 2026-11-02 11:00:11 +0800 | skipped / 1.8s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 134 | `factory_run_1793588709_b3c2745c` | 2026-11-02 11:05:11 +0800 | skipped / 1.7s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 135 | `factory_run_1793589009_a74fb373` | 2026-11-02 11:10:11 +0800 | skipped / 1.8s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 136 | `factory_run_1793589309_1de2acd3` | 2026-11-02 11:15:11 +0800 | skipped / 1.9s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 137 | `factory_run_1793589609_4fa0aa2f` | 2026-11-02 11:20:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 138 | `factory_run_1793589909_2ba5cee7` | 2026-11-02 11:25:10 +0800 | skipped / 1.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 139 | `factory_run_1793590209_4ae9c9d0` | 2026-11-02 11:30:10 +0800 | skipped / 1.6s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 140 | `factory_run_1793590509_ead1a93a` | 2026-11-02 11:35:11 +0800 | skipped / 1.9s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 141 | `factory_run_1793590809_309c8a5b` | 2026-11-02 11:40:11 +0800 | skipped / 2.0s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 142 | `factory_run_1793591109_e52afa0b` | 2026-11-02 11:45:11 +0800 | skipped / 2.5s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 143 | `factory_run_1793591409_0748e2e5` | 2026-11-02 11:50:11 +0800 | skipped / 2.6s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 144 | `factory_run_1793591709_8419aba0` | 2026-11-02 11:55:17 +0800 | skipped / 7.6s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 145 | `factory_run_1793592009_543657cb` | 2026-11-02 12:00:14 +0800 | skipped / 5.2s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |
| 146 | `factory_run_1793592309_4d38af76` | 2026-11-02 12:05:14 +0800 | skipped / 5.2s | 0/0 | — | 0 | — | — | 运行状态 skipped，需复核 |

## 轮次审查明细

### `factory_run_1793548808_885e1c84`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:01:28 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 282, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 19；通过 1；负 Sharpe 13；平均 Sharpe -0.607; 最大回撤 0.124；关联方式 `factory_run_id`。
- 主要阻塞：insufficient_statistical_evidence (6), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6), missing_wf_ic_ir (6), missing_pkf_ic (6), missing_bootstrap_ci_lower (6)。
- 代表样本：
  - `factory_1793548857_5ffb4d92` 000002+000006·多因子成长主导(65日)：gate=pass，grade=S，Sharpe=-1.51458874478015，回撤=0.08332328586050188。
  - `factory_1793548868_1c4bfb2b` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793548874_c37992f1` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793549108_d338e2d3`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:06:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 283, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 0；负 Sharpe 14；平均 Sharpe -0.7131; 最大回撤 0.1243；关联方式 `factory_run_id`。
- 主要阻塞：weak_wf_ic_ir (4), validation_grade_d_not_allowed_for_incubation (3), validation_grade_d_below_minimum_c_for_incubation (3), win_rate 0.379 < 0.400 (2), default_profile_not_allowed_for_single_name_runtime (2)。
- 代表样本：
  - `factory_1793549141_765d5b07` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793549162_80516118` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。
  - `factory_1793549156_ca728f61` 000002+000006·均线交叉·快5慢20：gate=fail，grade=A，Sharpe=-0.5069877735278279，回撤=0.06652619039823914。

### `factory_run_1793549408_2d7edfad`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:11:07 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 283, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 1；负 Sharpe 8；平均 Sharpe -1.0817; 最大回撤 0.1271；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), insufficient_statistical_evidence (3), missing_param_sensitivity (3), post_cost_sharpe 0.000 < 0.100 (2)。
- 代表样本：
  - `factory_1793549446_ee80aec7` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793549429_74cd5511` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793549454_38c8a5d8` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793549708_b8cfc11b`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:16:22 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 284, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 23；通过 2；负 Sharpe 16；平均 Sharpe -1.4211; 最大回撤 0.08；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793549759_1c9ad011` 000001+000007·多因子价值主导(59日)：gate=pass，grade=SSS，Sharpe=-0.7023308164115148，回撤=0.04459807419167165。
  - `factory_1793549753_53df321f` 000002+000006·多因子均衡(65日)：gate=pass，grade=SS，Sharpe=-0.8825730529339842，回撤=0.04349712569906334。
  - `factory_1793549765_a988a90c` 000001+000007·价值精选·20日轮动：gate=fail，grade=SS，Sharpe=-0.06264643570400383，回撤=0.05829715417906662。

### `factory_run_1793550008_0e1404b5`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:22:20 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 286, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 23；通过 3；负 Sharpe 15；平均 Sharpe -0.9907; 最大回撤 0.287；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793550128_e6527428` 中集集团·多因子价值主导(56日)：gate=pass，grade=SSS，Sharpe=0.0165448449249748，回撤=0.278498134757326。
  - `factory_1793550095_563348ca` 000002+000006·多因子价值主导(60日)：gate=pass，grade=SS，Sharpe=-0.6503344132303648，回撤=0.03319869423022529。
  - `factory_1793550119_38685642` 万 科Ａ·多因子价值主导(56日)：gate=pass，grade=SSS，Sharpe=-0.7184542185074777，回撤=0.28704546579716733。

### `factory_run_1793550308_b2513dd6`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:27:12 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 289, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 23；通过 1；负 Sharpe 15；平均 Sharpe -0.8607; 最大回撤 0.3262；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793550391_bc152a3a` 000002+000006·多因子均衡(62日)：gate=pass，grade=SSS，Sharpe=-0.6223945362822185，回撤=0.03039009495133131。
  - `factory_1793550397_e4468e75` 000001+000007·价值精选·20日轮动：gate=fail，grade=SSS，Sharpe=0.03660729038560906，回撤=0.05862471817752904。
  - `factory_1793550405_eb6960d1` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。

### `factory_run_1793550608_6a3c69ea`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:32:29 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 290, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 24；通过 4；负 Sharpe 15；平均 Sharpe -1.2953; 最大回撤 0.1394；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793550737_70292035` 中集集团·多因子均衡(59日)：gate=pass，grade=SSS，Sharpe=0.3034259579337959，回撤=0.11055627987068972。
  - `factory_1793550731_3461a699` 万 科Ａ·多因子均衡(59日)：gate=pass，grade=SS，Sharpe=0.2698765210928573，回撤=0.13938214155040649。
  - `factory_1793550702_49868b38` 000002+000006·多因子价值主导(61日)：gate=pass，grade=SSS，Sharpe=-0.3014171890652508，回撤=0.03257405409928222。

### `factory_run_1793550908_7e03a997`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:36:21 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 294, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 10；平均 Sharpe -0.8682; 最大回撤 0.114；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793550948_4812bc11` 000006+000007·多因子成长主导(62日)：gate=pass，grade=SSS，Sharpe=-1.3633674822264188，回撤=0.058464940529945525。
  - `factory_1793550962_ca0738c0` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793550954_a5b7bb8a` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793551208_a62d5032`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:41:22 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 296, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 12；平均 Sharpe -0.884; 最大回撤 0.1109；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793551247_bab91e8e` 000002+000006·多因子价值主导(57日)：gate=pass，grade=SSS，Sharpe=-1.7798913572570976，回撤=0.07614144446701167。
  - `factory_1793551263_417cb23d` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793551247_5dc58928` 000001+000021·多因子成长主导(64日)：gate=fail，grade=B，Sharpe=0.10469456813905048，回撤=0.024356729742975567。

### `factory_run_1793551508_f7c9e9e5`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:46:22 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 298, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 12；平均 Sharpe -0.9017; 最大回撤 0.1088；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793551549_d5ea8c4e` 000002+000006·多因子价值主导(71日)：gate=pass，grade=SSS，Sharpe=-2.1831261733857903，回撤=0.07345098800529479。
  - `factory_1793551564_b5b07ff4` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793551549_e8ce79ee` 000001+000021·多因子均衡(48日)：gate=fail，grade=C，Sharpe=0.8576426058622172，回撤=0.022557426013708724。

### `factory_run_1793551808_64301b59`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:51:19 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 300, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 18；通过 2；负 Sharpe 11；平均 Sharpe -1.0492; 最大回撤 0.1087；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793551862_50d21610` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793551848_b296e710` 000002+000006·多因子价值主导(69日)：gate=pass，grade=SSS，Sharpe=-2.5714430074649752，回撤=0.1047153044866644。
  - `factory_1793551842_27a6f18e` 000001+000021·动量突破·20日：gate=fail，grade=A，Sharpe=-0.1718526990491373，回撤=0.07175397866123459。

### `factory_run_1793552108_cc2cd8ad`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 00:56:26 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 302, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 2；负 Sharpe 12；平均 Sharpe -0.8482; 最大回撤 0.1244；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793552151_98301062` 000006+000007·多因子质量主导(65日)：gate=pass，grade=SSS，Sharpe=-1.0927139447322924，回撤=0.06364140541442516。
  - `factory_1793552166_f1348c86` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793552164_6dfc3239` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793552408_da45229a`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:01:19 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 304, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 18；通过 2；负 Sharpe 11；平均 Sharpe -0.9439; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793552449_4cef88dc` 000002+000006·多因子均衡(53日)：gate=pass，grade=SSS，Sharpe=-1.3231213200305447，回撤=0.07452718650848915。
  - `factory_1793552462_584fea4c` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793552449_00fcfd83` 000001+000021·宏观择时·恐贪(31/62)：gate=fail，grade=SS，Sharpe=-0.12676706970354565，回撤=0.10079842411353165。

### `factory_run_1793552708_952cb15f`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:06:23 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 306, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 13；平均 Sharpe -0.9051; 最大回撤 0.1095；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793552750_c3dd2324` 000002+000006·多因子均衡(69日)：gate=pass，grade=SSS，Sharpe=-1.7605086473738378，回撤=0.07709214201604597。
  - `factory_1793552764_da541f4b` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793552750_1a555192` 000001+000021·多因子成长主导(59日)：gate=fail，grade=S，Sharpe=-0.03806106687964227，回撤=0.03721902368958865。

### `factory_run_1793553008_53b52702`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:11:22 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 308, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 19；通过 1；负 Sharpe 12；平均 Sharpe -0.9447; 最大回撤 0.1007；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793553063_aa72ee45` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793553050_65548707` 000001+000021·宏观择时·恐贪(26/73)：gate=fail，grade=SS，Sharpe=-0.10962492020392353，回撤=0.10068877599714733。
  - `factory_1793553060_83a58d71` 000001+000021·动量突破·20日：gate=fail，grade=A，Sharpe=-0.1718526990491373，回撤=0.07175397866123459。

### `factory_run_1793553308_a4f418c2`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:16:02 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 309, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 12；平均 Sharpe -1.2357; 最大回撤 0.133；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (5), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793553335_45b83316` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793553341_8e569cab` 000001+000007·动量突破·21日：gate=fail，grade=SSS，Sharpe=-0.29177458463087197，回撤=0.057721455185395856。
  - `factory_1793553355_4669a35e` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793553608_2dcbc1c4`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:21:07 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 309, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 10；平均 Sharpe -1.2294; 最大回撤 0.1242；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), insufficient_statistical_evidence (3), missing_param_sensitivity (3), weak_wf_ic_ir (3)。
- 代表样本：
  - `factory_1793553637_d2ffebcb` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793553661_b24324f2` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。
  - `factory_1793553654_485f8b1e` 000002+000006·均线交叉·快5慢20：gate=fail，grade=A，Sharpe=-0.5069877735278279，回撤=0.06652619039823914。

### `factory_run_1793553908_6d8e4a8f`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:26:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 309, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 1；负 Sharpe 11；平均 Sharpe -1.1098; 最大回撤 0.1244；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793553949_98ac3b92` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793553936_ebdd75de` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793553955_98022fb9` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793554208_531e35ca`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:31:23 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 310, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 1；负 Sharpe 10；平均 Sharpe -0.789; 最大回撤 0.1271；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793554262_34207130` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793554260_4fa91efb` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793554270_388184c1` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793554508_5aeb5626`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:36:09 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 311, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 1；负 Sharpe 10；平均 Sharpe -1.0947; 最大回撤 0.1259；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793554549_e0be698d` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793554536_b0bc2f80` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793554557_2d4a4b0d` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793554808_a8503300`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:41:22 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 312, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 1；负 Sharpe 11；平均 Sharpe -0.8625; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793554850_725a2e8e` 000001+000007·多因子均衡(60日)：gate=pass，grade=SSS，Sharpe=-1.9368618778505091，回撤=0.06656980637233462。
  - `factory_1793554850_9036087d` 000002+000006·多因子均衡(50日)：gate=fail，grade=D，Sharpe=0.3604609013640097，回撤=0.030284795480870142。
  - `factory_1793554861_5feba4e0` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。

### `factory_run_1793555108_78f9f719`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:46:09 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 313, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 1；负 Sharpe 12；平均 Sharpe -1.277; 最大回撤 0.1088；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793555149_129b0fd9` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793555142_8c163328` 000001+000021·动量突破·18日：gate=fail，grade=D，Sharpe=-0.10154192218572022，回撤=0.06114213104244277。
  - `factory_1793555157_0797dec4` 000001+000021·波动突破·20日：gate=fail，grade=C，Sharpe=-0.35062210998312415，回撤=0.04131068645756102。

### `factory_run_1793555408_dc19a860`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:51:23 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 314, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 9；平均 Sharpe -0.8242; 最大回撤 0.1144；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793555465_d1da276d` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793555450_ff70f1cc` 000006+000007·多因子均衡(60日)：gate=pass，grade=S，Sharpe=-1.598669399866054，回撤=0.06953687740961587。
  - `factory_1793555461_8f5ea0a9` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793555708_291721b2`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 01:56:33 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 316, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 23；通过 1；负 Sharpe 16；平均 Sharpe -1.458; 最大回撤 0.08；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793555765_12e727be` 000001+000007·多因子价值主导(61日)：gate=pass，grade=SSS，Sharpe=-0.7584012767112903，回撤=0.042921300906967656。
  - `factory_1793555765_062d5a4f` 000001+000007·价值精选·20日轮动：gate=fail，grade=SS，Sharpe=-0.04080311419742925，回撤=0.05951462113703758。
  - `factory_1793555772_f32f534c` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。

### `factory_run_1793556008_73412126`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:01:09 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 317, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 16；通过 0；负 Sharpe 12；平均 Sharpe -1.3719; 最大回撤 0.08；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), insufficient_statistical_evidence (3), missing_param_sensitivity (3), weak_wf_ic_ir (3)。
- 代表样本：
  - `factory_1793556054_93ae85bf` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793556065_9623e255` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。
  - `factory_1793556057_99589bfd` 000001+000007·波动突破·20日：gate=fail，grade=B，Sharpe=-0.5457672987375531，回撤=0.039161837016394904。

### `factory_run_1793556308_dd5481fc`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:06:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 317, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 1；负 Sharpe 10；平均 Sharpe -1.1033; 最大回撤 0.1261；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793556349_a74f7c40` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793556336_aafb7165` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793556357_6ed1ec05` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793556608_6e681c26`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:11:30 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 318, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 23；通过 0；负 Sharpe 14；平均 Sharpe -2.6346; 最大回撤 0.08；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (11), validation_grade_d_below_minimum_c_for_incubation (11), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793556675_a4eb6685` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793556684_2cf8e581` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。
  - `factory_1793556684_a3274596` 000001+000007·波动突破·20日：gate=fail，grade=B，Sharpe=-0.5457672987375531，回撤=0.039161837016394904。

### `factory_run_1793556908_3dc3fb4e`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:16:26 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 318, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 17；通过 1；负 Sharpe 15；平均 Sharpe -3.8344; 最大回撤 0.1109；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793556965_4500a526` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793556957_681b938d` 000001+000021·动量突破·20日：gate=fail，grade=A，Sharpe=-0.1718526990491373，回撤=0.07175397866123459。
  - `factory_1793556972_2abec108` 000001+000021·波动突破·20日：gate=fail，grade=C，Sharpe=-0.35062210998312415，回撤=0.04131068645756102。

### `factory_run_1793557208_9fa66d1a`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:21:31 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 319, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 0；负 Sharpe 13；平均 Sharpe -1.2725; 最大回撤 0.133；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793557269_73565199` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793557262_17609ca7` 000001+000007·多因子质量主导(49日)：gate=fail，grade=C，Sharpe=-0.1510875557867441，回撤=0.02902245879529006。
  - `factory_1793557262_dcc56972` 000002+000006·多因子均衡(53日)：gate=fail，grade=S，Sharpe=-0.3190584897482656，回撤=0.03361630996856849。

### `factory_run_1793557508_7dda8396`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:26:03 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 319, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 0；负 Sharpe 12；平均 Sharpe -1.3199; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (5), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793557542_8fb61285` 000001+000007·动量突破·17日：gate=fail，grade=SS，Sharpe=0.07195545039300844，回撤=0.045511513008245345。
  - `factory_1793557536_dae23b98` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793557542_7870e5f1` 000001+000007·宏观择时·恐贪(29/55)：gate=fail，grade=SS，Sharpe=-0.20311246217025392，回撤=0.10078553183130295。

### `factory_run_1793557808_e92fbfc8`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:31:44 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 319, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 1；负 Sharpe 9；平均 Sharpe -1.9123; 最大回撤 0.1141；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), missing_stress_loss_percent (3), post_cost_sharpe 0.000 < 0.100 (2), trade_count 0 < 4 (2)。
- 代表样本：
  - `factory_1793557887_5b960859` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793557862_49e9813c` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793557887_f4b9f2c0` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793558108_db48031a`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:36:23 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 320, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 19；通过 2；负 Sharpe 12；平均 Sharpe -1.0067; 最大回撤 0.1087；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793558166_31019d55` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793558151_ace70a1e` 000002+000006·多因子成长主导(65日)：gate=pass，grade=SSS，Sharpe=-2.555244084524868，回撤=0.0986097403308267。
  - `factory_1793558151_5c36e6d8` 000001+000021·多因子均衡(72日)：gate=fail，grade=A，Sharpe=-0.09257695509091181，回撤=0.027872162455903193。

### `factory_run_1793558408_8a8a531e`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:41:24 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 322, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 13；平均 Sharpe -1.0172; 最大回撤 0.1011；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793558450_5e85fb89` 000002+000006·多因子价值主导(48日)：gate=pass，grade=SSS，Sharpe=-1.2600975888409538，回撤=0.051145563555646194。
  - `factory_1793558465_9aabca5a` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793558457_e6cc89ff` 000001+000021·宏观择时·恐贪(32/56)：gate=fail，grade=SS，Sharpe=-0.15060044734626388，回撤=0.10106623333884573。

### `factory_run_1793558708_4db9a686`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:46:39 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 324, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 2；负 Sharpe 11；平均 Sharpe -1.0371; 最大回撤 0.1237；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793558768_beb22689` 000006+000007·多因子均衡(54日)：gate=pass，grade=SSS，Sharpe=-1.3124883012957562，回撤=0.09011191871316737。
  - `factory_1793558780_4bcbf3f4` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793558756_99c5078a` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793559008_11e21610`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:51:23 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 326, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 19；通过 1；负 Sharpe 10；平均 Sharpe -0.8668; 最大回撤 0.1261；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793559064_9c438224` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793559061_853ee03c` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793559064_3a54da3a` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793559308_839942e3`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 02:56:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 327, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 1；负 Sharpe 10；平均 Sharpe -1.0513; 最大回撤 0.1255；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793559349_e9b1dfa7` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793559336_9cc0b4ca` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793559355_1dc770a4` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793559608_27dafa1a`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:01:40 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 328, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 2；负 Sharpe 13；平均 Sharpe -2.8804; 最大回撤 0.1007；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793559683_3a4b9a0a` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793559663_1d460e3b` 000002+000006·多因子均衡(64日)：gate=pass，grade=SSS，Sharpe=-2.4934667287223897，回撤=0.09304652378502458。
  - `factory_1793559671_1adec930` 000001+000021·宏观择时·恐贪(35/65)：gate=fail，grade=SS，Sharpe=-0.11598418008295898，回撤=0.10067660279417981。

### `factory_run_1793559908_c1b11592`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:06:39 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 330, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 2；负 Sharpe 12；平均 Sharpe -7.359; 最大回撤 0.1242；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (11), validation_grade_d_below_minimum_c_for_incubation (11), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793559968_455cdd1d` 000001+000002·多因子价值主导(60日)：gate=pass，grade=SS，Sharpe=-0.4431970478513347，回撤=0.03163590459608933。
  - `factory_1793559980_3e170847` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793559968_f09bb7e3` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793560208_4d65ddad`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:11:41 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 332, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 10；平均 Sharpe -1.1187; 最大回撤 0.1141；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793560281_88d06756` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793560262_6f30c1df` 000006+000007·多因子均衡(62日)：gate=pass，grade=SSS，Sharpe=-1.5306865606209286，回撤=0.0701148761268818。
  - `factory_1793560272_edafb4d9` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793560508_bb70ccb7`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:16:17 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 334, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 0；负 Sharpe 13；平均 Sharpe -0.9081; 最大回撤 0.1234；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793560551_17b8e444` 000002+000006·多因子均衡(48日)：gate=fail，grade=A，Sharpe=-0.048707646453156166，回撤=0.04125121333615058。
  - `factory_1793560545_fd391a37` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793560570_5af2ddad` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793560808_860c8245`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:21:46 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 334, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 1；负 Sharpe 10；平均 Sharpe -11.7653; 最大回撤 0.1247；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), profit_factor 0.000 < 1.500 (4), missing_stress_loss_percent (3), win_rate 0.000 < 0.450 (3)。
- 代表样本：
  - `factory_1793560884_601dc8d8` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793560876_a4ff1d5f` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793560891_429fc187` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793561108_547ac0d1`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:26:36 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 335, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 1；负 Sharpe 14；平均 Sharpe -2.8451; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (11), validation_grade_d_below_minimum_c_for_incubation (11), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793561163_0fd0c657` 000001+000007·多因子价值主导(61日)：gate=pass，grade=SSS，Sharpe=-1.7062568444792339，回撤=0.06361853834450082。
  - `factory_1793561156_dfe643be` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793561169_1d160f8b` 000001+000007·宏观择时·恐贪(30/72)：gate=fail，grade=SS，Sharpe=-0.22589661270756398，回撤=0.10081319515147141。

### `factory_run_1793561408_e39d22b5`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:31:50 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 336, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 16；通过 1；负 Sharpe 9；平均 Sharpe -1.771; 最大回撤 0.1142；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), missing_stress_loss_percent (3), insufficient_statistical_evidence (3), missing_param_sensitivity (3)。
- 代表样本：
  - `factory_1793561490_ff5a8ad2` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793561487_8837c077` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793561500_167f730c` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793561708_cc98454d`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:36:33 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 337, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 22；通过 1；负 Sharpe 15；平均 Sharpe -3.0762; 最大回撤 0.0964；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793561776_4d311647` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793561773_350d2997` 000001+000021·动量突破·20日：gate=fail，grade=A，Sharpe=-0.1718526990491373，回撤=0.07175397866123459。
  - `factory_1793561776_49789d7a` 000001+000021·波动突破·20日：gate=fail，grade=C，Sharpe=-0.35062210998312415，回撤=0.04131068645756102。

### `factory_run_1793562008_8d1ad2ad`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:42:30 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 338, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 24；通过 3；负 Sharpe 15；平均 Sharpe -6.6643; 最大回撤 0.3614；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793562118_5d3f90ad` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793562138_826faaa3` 中集集团·多因子：gate=pass，grade=SSS，Sharpe=-1.4977025824060335，回撤=0.34095544367286523。
  - `factory_1793562128_a99f464f` 万 科Ａ·多因子：gate=pass，grade=SSS，Sharpe=-1.7701357781274325，回撤=0.36138459862346467。

### `factory_run_1793562308_d8b7e317`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:46:40 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 341, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 2；负 Sharpe 14；平均 Sharpe -1.4486; 最大回撤 0.1109；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793562379_14a839a3` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793562364_3501eeac` 000002+000006·多因子均衡(66日)：gate=pass，grade=SS，Sharpe=-2.4333138715517935，回撤=0.07926967757542437。
  - `factory_1793562357_83e704da` 000001+000021·动量突破·20日：gate=fail，grade=A，Sharpe=-0.1718526990491373，回撤=0.07175397866123459。

### `factory_run_1793562608_6b040fb7`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:51:23 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 343, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 2；负 Sharpe 13；平均 Sharpe -1.1096; 最大回撤 0.0996；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793562666_97e91ab1` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793562650_1c5bb291` 000002+000006·多因子均衡(51日)：gate=pass，grade=SSS，Sharpe=-2.5129942466546784，回撤=0.09961548942431293。
  - `factory_1793562650_06159423` 000001+000021·多因子成长主导(53日)：gate=fail，grade=D，Sharpe=0.2556767183999292，回撤=0.021361362104710624。

### `factory_run_1793562908_30276b88`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 03:57:31 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 345, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 24；通过 3；负 Sharpe 17；平均 Sharpe -1.4379; 最大回撤 0.3614；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (11), validation_grade_d_below_minimum_c_for_incubation (11), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793563039_899caa31` 中集集团·多因子：gate=pass，grade=SSS，Sharpe=-1.4977025824060335，回撤=0.34095544367286523。
  - `factory_1793563030_3a5cd8ef` 万 科Ａ·多因子：gate=pass，grade=SSS，Sharpe=-1.7701357781274325，回撤=0.36138459862346467。
  - `factory_1793563020_c48637aa` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。

### `factory_run_1793563208_9518497e`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:01:23 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 348, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 19；通过 1；负 Sharpe 10；平均 Sharpe -0.8238; 最大回撤 0.1261；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793563264_28017ee4` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793563261_1acac382` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793563264_3ac89dec` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793563508_be9e2d96`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:06:13 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 349, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 16；通过 1；负 Sharpe 14；平均 Sharpe -1.2299; 最大回撤 0.1109；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793563553_078670ce` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793563537_6edcae80` 000001+000021·动量突破·20日：gate=fail，grade=A，Sharpe=-0.1718526990491373，回撤=0.07175397866123459。
  - `factory_1793563563_86f86789` 000001+000021·波动突破·20日：gate=fail，grade=C，Sharpe=-0.35062210998312415，回撤=0.04131068645756102。

### `factory_run_1793563808_c9dcca3b`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:11:39 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 350, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 1；负 Sharpe 10；平均 Sharpe -1.029; 最大回撤 0.1136；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793563883_4045306d` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793563871_763feaee` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793563883_6a80736c` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793564108_858bbfd2`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:16:03 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 351, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 12；平均 Sharpe -1.2802; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (5), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793564142_3048e5ea` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.19441513826620022，回撤=0.09018145596487405。
  - `factory_1793564142_97762a35` 000001+000007·宏观择时·恐贪(31/70)：gate=fail，grade=SS，Sharpe=-0.20495882675947033，回撤=0.10080472759703597。
  - `factory_1793564156_57762d98` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793564408_79253afc`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:21:18 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 351, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 0；负 Sharpe 11；平均 Sharpe -1.284; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), profit_factor 0.000 < 1.500 (3), insufficient_statistical_evidence (3), missing_param_sensitivity (3)。
- 代表样本：
  - `factory_1793564446_c2e8f400` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793564456_a17c4aba` 000001+000007·宏观择时·恐贪(36/76)：gate=fail，grade=SS，Sharpe=-0.20495882675947033，回撤=0.10080472759703597。
  - `factory_1793564468_266785bb` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793564708_93cce4b6`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:26:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 351, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 15；通过 1；负 Sharpe 10；平均 Sharpe -1.0498; 最大回撤 0.1259；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793564749_1a65a416` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793564736_e74ba47d` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793564755_6a7a3efc` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793565008_515a9890`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:31:21 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 352, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 12；平均 Sharpe -0.9094; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793565052_414053f2` 000002+000006·多因子均衡(59日)：gate=pass，grade=SS，Sharpe=-0.9783235614756981，回撤=0.05057890261884922。
  - `factory_1793565052_e3311d77` 000001+000007·多因子均衡(69日)：gate=pass，grade=SS，Sharpe=-1.4881307808738726，回撤=0.0656766704700827。
  - `factory_1793565065_d895bbb1` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。

### `factory_run_1793565308_0d8ef8a4`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:36:24 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 354, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 1；负 Sharpe 13；平均 Sharpe -0.9544; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793565353_9d50694e` 000001+000007·多因子价值主导(69日)：gate=pass，grade=SSS，Sharpe=-1.5386698261604659，回撤=0.06264127357730598。
  - `factory_1793565359_8c7a33eb` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793565359_f9a1470d` 000001+000007·宏观择时·恐贪(37/62)：gate=fail，grade=SS，Sharpe=-0.20495882675947033，回撤=0.10080472759703597。

### `factory_run_1793565608_03f1002c`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:41:18 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 355, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 0；负 Sharpe 10；平均 Sharpe -1.2309; 最大回撤 0.1244；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), insufficient_statistical_evidence (3), missing_param_sensitivity (3), weak_wf_ic_ir (3)。
- 代表样本：
  - `factory_1793565648_15ac75bf` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793565671_15c684f2` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。
  - `factory_1793565665_ccc9f55c` 000002+000006·均线交叉·快5慢20：gate=fail，grade=A，Sharpe=-0.5069877735278279，回撤=0.06652619039823914。

### `factory_run_1793565908_2e1c3814`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:46:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 355, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 2；负 Sharpe 9；平均 Sharpe -1.1176; 最大回撤 0.1142；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793565943_78b577f7` 000006+000007·动量突破·17日：gate=pass，grade=SSS，Sharpe=0.33637197777933375，回撤=0.05009423254860749。
  - `factory_1793565953_0a647332` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793565961_b64ffee7` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793566208_c6ad4808`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:51:17 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 357, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 19；通过 0；负 Sharpe 12；平均 Sharpe -0.9601; 最大回撤 0.1009；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793566245_8f3c8af1` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793566252_e08aa2e7` 000001+000007·宏观择时·恐贪(37/55)：gate=fail，grade=SS，Sharpe=-0.2302269019555119，回撤=0.10085662362457837。
  - `factory_1793566273_f6d16f96` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793566508_8871e1fb`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 04:56:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 357, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 1；负 Sharpe 11；平均 Sharpe -1.2632; 最大回撤 0.1109；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (5), validation_grade_d_below_minimum_c_for_incubation (5), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793566550_5fe9ee6b` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793566543_15640a35` 000001+000021·动量突破·22日：gate=fail，grade=B，Sharpe=0.021968626962837665，回撤=0.06468573933178409。
  - `factory_1793566558_79c65463` 000001+000021·波动突破·20日：gate=fail，grade=C，Sharpe=-0.35062210998312415，回撤=0.04131068645756102。

### `factory_run_1793566808_53452a43`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:01:27 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 358, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 2；负 Sharpe 9；平均 Sharpe -0.7428; 最大回撤 0.1244；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793566853_083ace6b` 000006+000007·多因子质量主导(61日)：gate=pass，grade=A，Sharpe=-1.1285438988288898，回撤=0.053890619103856965。
  - `factory_1793566868_41659a85` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793566865_ce8bb3f0` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793567108_0caba2d7`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:06:19 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 360, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 0；负 Sharpe 13；平均 Sharpe -0.9703; 最大回撤 0.124；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793567147_198ddc67` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793567172_7f42a6d2` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。
  - `factory_1793567166_276200b1` 000002+000006·均线交叉·快5慢20：gate=fail，grade=A，Sharpe=-0.5069877735278279，回撤=0.06652619039823914。

### `factory_run_1793567408_a52384bc`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:11:25 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 360, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 13；通过 1；负 Sharpe 8；平均 Sharpe -1.1837; 最大回撤 0.1257；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (3), validation_grade_d_below_minimum_c_for_incubation (3), post_cost_sharpe 0.000 < 0.100 (2), trade_count 0 < 4 (2), missing_parameter_perturbation_trade_stability (2)。
- 代表样本：
  - `factory_1793567466_05f6c6c8` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793567463_b87eb97a` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793567466_161e800d` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793567708_6d7978ed`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:16:37 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 361, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 23；通过 1；负 Sharpe 14；平均 Sharpe -6.9321; 最大回撤 0.0864；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793567777_ad00cee7` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793567774_2899c38d` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793567785_bf2e09df` 000006+000007·波动突破·20日：gate=fail，grade=S，Sharpe=0.19224567799804937，回撤=0.041044866259776984。

### `factory_run_1793568008_c9afcf10`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:21:47 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 362, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 18；通过 0；负 Sharpe 13；平均 Sharpe -4.0663; 最大回撤 0.0832；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), missing_stress_loss_percent (4), profit_factor 0.000 < 1.500 (4), post_cost_sharpe 0.000 < 0.100 (3)。
- 代表样本：
  - `factory_1793568088_498b1dc3` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793568098_51af4685` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。
  - `factory_1793568098_42b43e96` 000001+000007·波动突破·20日：gate=fail，grade=B，Sharpe=-0.5457672987375531，回撤=0.039161837016394904。

### `factory_run_1793568308_4297d47a`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:26:26 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 362, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 19；通过 2；负 Sharpe 13；平均 Sharpe -1.3318; 最大回撤 0.1242；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (7), validation_grade_d_below_minimum_c_for_incubation (7), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793568354_65d9b131` 000006+000007·动量突破·18日：gate=pass，grade=C，Sharpe=0.3707595255847153，回撤=0.06338448297300045。
  - `factory_1793568367_6f5d6fd7` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793568348_af6a9d42` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793568608_c0e0211a`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:31:31 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 364, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 2；负 Sharpe 12；平均 Sharpe -0.8165; 最大回撤 0.1261；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (8), validation_grade_d_below_minimum_c_for_incubation (8), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793568653_d757b7c7` 000006+000007·多因子均衡(51日)：gate=pass，grade=SSS，Sharpe=-1.0951402764003741，回撤=0.08886772749882513。
  - `factory_1793568670_c68baa6b` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793568669_cf5a5ebc` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793568908_ca02750e`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:38:07 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 366, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 25；通过 3；负 Sharpe 18；平均 Sharpe -2.5843; 最大回撤 0.341；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793569074_1205026d` 中集集团·多因子：gate=pass，grade=SSS，Sharpe=-0.9721608209655512，回撤=0.33602657727838553。
  - `factory_1793569065_6e375a56` 万 科Ａ·多因子：gate=pass，grade=SSS，Sharpe=-1.4977025824060335，回撤=0.34095544367286523。
  - `factory_1793569037_e1c50d0d` 000001+000007·多因子价值主导(67日)：gate=pass，grade=SSS，Sharpe=-1.5825155701355795，回撤=0.06611071925099894。

### `factory_run_1793569208_fae42711`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:41:32 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 369, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 0；负 Sharpe 13；平均 Sharpe -2.9543; 最大回撤 0.1019；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793569271_fef399df` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793569271_acde2793` 000001+000007·宏观择时·恐贪(30/75)：gate=fail，grade=SS，Sharpe=-0.17774064391103778，回撤=0.10193176475405426。
  - `factory_1793569285_25e32207` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793569508_3d105537`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:46:26 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 369, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 17；通过 1；负 Sharpe 11；平均 Sharpe -9.0904; 最大回撤 0.114；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (7), validation_grade_d_below_minimum_c_for_incubation (7), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793569566_ad9dcdc6` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793569555_6d2d0887` 000006+000007·动量突破·11日：gate=fail，grade=D，Sharpe=0.9193057857595482，回撤=0.04126028186968056。
  - `factory_1793569559_b3051061` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793569808_3d2a16a1`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:51:43 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 370, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 22；通过 2；负 Sharpe 12；平均 Sharpe -1.1106; 最大回撤 0.1259；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793569866_3279016b` 000006+000007·多因子质量主导(63日)：gate=pass，grade=SSS，Sharpe=-1.1438060003280264，回撤=0.061262933833607536。
  - `factory_1793569884_2774740d` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793569880_15215956` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793570108_d9ed656e`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 05:56:25 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 372, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 19；通过 2；负 Sharpe 12；平均 Sharpe -0.9936; 最大回撤 0.1087；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793570154_6f25bd0d` 000002+000006·多因子均衡(61日)：gate=pass，grade=SSS，Sharpe=-2.3699113260126703，回撤=0.08307760293472521。
  - `factory_1793570169_40163e80` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793570154_82bb166d` 000001+000021·多因子均衡(72日)：gate=fail，grade=A，Sharpe=-0.09865804280762522，回撤=0.023772825495548437。

### `factory_run_1793570408_91a5d923`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:01:41 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 374, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 1；负 Sharpe 14；平均 Sharpe -2.8428; 最大回撤 0.1242；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (7), missing_param_sensitivity (7), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793570465_c6fa14c8` 000002+000006·多因子均衡(72日)：gate=pass，grade=SS，Sharpe=-1.9127500855434276，回撤=0.07098043510505576。
  - `factory_1793570480_f93f45f2` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793570473_41de3946` 000001+000007·价值精选·20日轮动：gate=fail，grade=SS，Sharpe=-0.2071607613084567，回撤=0.05776700660106283。

### `factory_run_1793570708_8e65db70`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:06:25 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 375, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 16；通过 1；负 Sharpe 14；平均 Sharpe -3.8055; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (6), validation_grade_d_below_minimum_c_for_incubation (6), weak_wf_ic_ir (4), weak_pkf_ic (3), weak_bootstrap_ci_lower (3)。
- 代表样本：
  - `factory_1793570766_edff60f5` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793570755_a892831f` 000001+000021·宏观择时·恐贪(29/59)：gate=fail，grade=SS，Sharpe=-0.12676706970354565，回撤=0.10079842411353165。
  - `factory_1793570763_a6df64a2` 000001+000021·动量突破·20日：gate=fail，grade=A，Sharpe=-0.1718526990491373，回撤=0.07175397866123459。

### `factory_run_1793571008_e495c672`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:11:39 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 376, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 21；通过 1；负 Sharpe 13；平均 Sharpe -2.7632; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (10), validation_grade_d_below_minimum_c_for_incubation (10), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (7)。
- 代表样本：
  - `factory_1793571066_8ca29aa2` 000001+000007·多因子价值主导(56日)：gate=pass，grade=SSS，Sharpe=-0.9371339093811748，回撤=0.059643165820428744。
  - `factory_1793571074_4a008a28` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793571074_b201d36c` 000001+000007·宏观择时·恐贪(36/67)：gate=fail，grade=SS，Sharpe=-0.20311246217025392，回撤=0.10078553183130295。

### `factory_run_1793571308_03e4f576`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:16:18 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 377, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 16；通过 0；负 Sharpe 14；平均 Sharpe -3.6772; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (7), validation_grade_d_below_minimum_c_for_incubation (7), weak_wf_ic_ir (5), profit_factor 0.000 < 1.500 (3), weak_pkf_ic (3)。
- 代表样本：
  - `factory_1793571348_7b2f6b4e` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793571354_a1715ac5` 000001+000007·宏观择时·恐贪(33/55)：gate=fail，grade=SS，Sharpe=-0.20311246217025392，回撤=0.10078553183130295。
  - `factory_1793571374_5082c79a` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793571608_ab596057`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:21:56 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 377, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 16；通过 1；负 Sharpe 12；平均 Sharpe -2.2374; 最大回撤 0.1087；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (7), validation_grade_d_below_minimum_c_for_incubation (7), missing_stress_loss_percent (3), insufficient_statistical_evidence (3), missing_param_sensitivity (3)。
- 代表样本：
  - `factory_1793571697_b9d43bd4` 000002+000006·多因子：gate=pass，grade=SSS，Sharpe=-2.3794349307274762，回撤=0.08601706409716955。
  - `factory_1793571694_d0ca6c5e` 000001+000021·动量突破·20日：gate=fail，grade=A，Sharpe=-0.1718526990491373，回撤=0.07175397866123459。
  - `factory_1793571707_f3500c5d` 000001+000021·波动突破·20日：gate=fail，grade=C，Sharpe=-0.35062210998312415，回撤=0.04131068645756102。

### `factory_run_1793571908_02998bd8`

- 工厂状态：`success_observe_only`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:26:19 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 378, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 20；通过 0；负 Sharpe 13；平均 Sharpe -0.9496; 最大回撤 0.1008；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (9), validation_grade_d_below_minimum_c_for_incubation (9), insufficient_statistical_evidence (8), missing_param_sensitivity (8), missing_statistical_metrics:wf_ic_ir,pkf_ic,bootstrap_ci_lower,param_sensitivity (6)。
- 代表样本：
  - `factory_1793571959_4ebb223c` 000002+000006·动量突破·20日：gate=fail，grade=A，Sharpe=-0.07265266966261044，回撤=0.0800270221673199。
  - `factory_1793571959_1951e7f8` 000001+000007·宏观择时·恐贪(27/55)：gate=fail，grade=SS，Sharpe=-0.20495882675947033，回撤=0.10080472759703597。
  - `factory_1793571973_06b334b8` 000002+000006·波动突破·20日：gate=fail，grade=C，Sharpe=-0.33231659224135796，回撤=0.06493799961834339。

### `factory_run_1793572208_a48f29eb`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:31:25 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 378, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 14；通过 1；负 Sharpe 7；平均 Sharpe -1.0602; 最大回撤 0.1141；关联方式 `factory_run_id`。
- 主要阻塞：validation_grade_d_not_allowed_for_incubation (4), validation_grade_d_below_minimum_c_for_incubation (4), profit_factor 1.639 < 1.800 (2), post_cost_sharpe 0.000 < 0.100 (2), trade_count 0 < 4 (2)。
- 代表样本：
  - `factory_1793572265_f63c2336` 000006+000007·多因子：gate=pass，grade=SSS，Sharpe=-1.380393653829088，回撤=0.07649066925875261。
  - `factory_1793572248_1ba86da1` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。
  - `factory_1793572263_65fb39ad` 000006+000007·动量突破·20日：gate=fail，grade=A，Sharpe=0.2018292639841968，回撤=0.05853665712501524。

### `factory_run_1793572508_22c18106`

- 工厂状态：`success`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:39:41 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 379, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 39；通过 18；负 Sharpe 31；平均 Sharpe -1.879; 最大回撤 0.345；关联方式 `factory_run_id`。
- 主要阻塞：formal_multiple_testing_mode_required_for_live_admission (18), deflated_sharpe_missing_for_live_admission (18), pbo_missing_for_live_admission (18), white_reality_check_missing_for_live_admission (18), hansen_spa_missing_for_live_admission (18)。
- 代表样本：
  - `factory_1793572716_322d72fa` 000020·多因子：gate=pass，grade=SSS，Sharpe=-0.37556840070732284，回撤=0.3205730932392129。
  - `factory_1793572692_d8236c9f` 000017·多因子：gate=pass，grade=SSS，Sharpe=-0.37876904309845216，回撤=0.2964159782633682。
  - `factory_1793572710_3e8a240c` 000019·多因子：gate=pass，grade=SS，Sharpe=-0.49751557710933036，回撤=0.235135229392096。

### `factory_run_1793572808_508dcddc`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:40:13 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793573109_a31da1a1`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:45:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793573408_a8ad8ff2`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:50:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793573708_f2422f96`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 06:55:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793574008_c8873923`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:00:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793574309_17279028`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:05:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793574608_af028383`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:10:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793574909_91cdd8d1`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:15:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793575209_88a6ac85`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:20:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793575509_c6fd6232`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:25:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793575809_7e24fde3`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:30:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793576109_b53c9bf3`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:35:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793576409_a4be5611`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:40:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793576709_d446d4a2`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:45:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793577009_58863300`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:50:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793577309_c5f89e0e`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 07:55:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793577609_3d9409e7`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:00:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793577909_b76eaee7`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:05:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793578209_beb3d12d`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:10:12 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793578509_a8b9a799`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:15:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793578809_d8b918a6`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:20:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793579109_bd96c4b2`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:25:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793579409_5015021d`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:30:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793579709_bd9ee059`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:35:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793580009_6a20f7cc`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:40:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793580309_22de5799`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:45:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793580609_469860ab`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:50:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793580909_8f21ee98`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 08:55:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793581209_cb051af3`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:00:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793581509_65f26f25`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:05:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793581809_7781ae64`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:10:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793582109_d1e27cb3`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:15:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793582409_39471f4a`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:20:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793582709_e9f327bb`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:25:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793583009_d7c5a70c`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:30:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793583309_ec2907f1`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:35:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793583609_e2c14425`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:40:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793583909_225af888`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:45:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793584209_70d77c2c`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:50:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793584509_e68e17e6`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 09:55:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793584809_98a622a0`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:00:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793585109_8bcf7353`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:05:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793585409_4b532f59`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:10:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793585709_dc41da3f`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:15:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793586009_752ebe25`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:20:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793586309_1e03824f`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:25:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793586609_4075f630`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:30:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793586909_fb2aae4b`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:35:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793587209_0e2c3cb3`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:40:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793587509_06f9ac7e`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:45:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793587809_f760549f`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:50:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793588109_4d75d611`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 10:55:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793588409_2f4fa114`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:00:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793588709_b3c2745c`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:05:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793589009_a74fb373`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:10:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793589309_1de2acd3`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:15:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793589609_4fa0aa2f`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:20:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793589909_2ba5cee7`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:25:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793590209_4ae9c9d0`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:30:10 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793590509_ead1a93a`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:35:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793590809_309c8a5b`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:40:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793591109_e52afa0b`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:45:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793591409_0748e2e5`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:50:11 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793591709_8419aba0`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 11:55:17 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793592009_543657cb`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 12:00:14 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

### `factory_run_1793592309_4d38af76`

- 工厂状态：`skipped`；execution mode：`stock_first_observe_primary`；完成：`2026-11-02 12:05:14 +0800`。
- 市场快照：`{"date": "2026-11-02", "fear_greed": 46, "fg_level": "neutral", "completion_ratio": 1.0, "failure_reason_count": 0, "incubating_count": 397, "optional_unavailable_sources": ["north_fund"]}`。
- 质量：报告 0；通过 0；负 Sharpe 0；平均 Sharpe None; 最大回撤 None；关联方式 `none`。
- 主要阻塞：未提取到显式阻塞原因。

## 口径与边界

- 该文档记录观察到的运行和质量证据，不把 `success` 自动解释为策略可交易。
- 监督器的四个运行时为 Market Event Ingest、Factor Mining、Strategy Factory、Incubation；SignalTracker 作为单独 sidecar 状态保留在运行环境中。
- 质量报告缺失或无法按 `factory_run_id` 关联时会保留时间窗口回退结果，并在 JSON 状态中体现报告数量。
