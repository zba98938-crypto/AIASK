# 孵化评分 85 分达成方案

| 项 | 内容 |
|---|---|
| 日期 | 2026-10-23 |
| 状态 | 已按活库逐条核验（§2 全部数字可复现） |
| 前序材料 | `docs/PREDICTION_HITRATE_85_PLAN.md`（v3，命中率 85% 方案）、`.aiask-local/reports/incubating_strategy_scores_2026-10-23.csv`（1799 策略评分快照） |
| 核验库 | 活库 `~/.aiask/data/db/akshare_mcp.sqlite3`（daemon 写入端）+ 快照 `.aiask-local/data/db/akshare_mcp.sqlite3`（评分用） |

---

## ⚠ 2026-10-25 勘误（以代码树与活库为准，优先于下文）

1. **combo 的 85 路径不成立。** 下文 §1.1 / §2.2 / §4 / §10 第 0 轮引用的 combo「L5d +45.4%」是 `primary_hit_rate_lcb`（命中率 0.54 的 Wilson 下界，几乎任何策略都为正），不是 skill LCB。combo 真实 `skill_lcb_5d` = **−0.087（10-20）/ −0.074（10-23）**，孵化自身以「主窗口5D skill LCB ≤ 0」判 halt。评分 CSV 的 `lcb_5d` 列对 combo 取了命中率下界、对其余策略取 skill LCB，口径混用。技能 20 分需 skill LCB ≥ +10%，其余维度合计上限 75，所以**没有 skill LCB ≥ +10% 的策略不可能到 85**；当前全库最大 skill LCB 为 +3.7%（lsmap ×3），**Y 的上界 = 0**。
2. **Z 按 §10 自己的定义（`skill_lcb_5d > 0`）是 lsmap ×3，不含 combo。** 第 1 轮台账「Z=3（combo + lsmap 系）」应为「lsmap ×3」。
3. **两个库已分叉。** 沙箱进程经 `scripts_local_bootstrap/sitecustomize.py` 写的是 `.aiask-local` 快照库；权威库 `~/.aiask/data/db/akshare_mcp.sqlite3`（`DATA_SOURCES.md` §10）在 10-16 之后每天只有 53 只 K 线，最新全市场会话仍是 **10-16**。WP-1「数据恢复」只在快照库完成。
4. **WP-7 的前提不成立。** lsmap 三策略各只有**一个** active 孵化账户（`inc_36f65a13` 等，20 笔成交 / 10 filled + 1 pending），不存在 warmup/paper 账户拆分；验收项「total_trades=10」应为 20。全库只有 1 个策略有 >1 个 active 账户，该路径的 SQL 占位符原写成 `IN (1)`（非 `$1`），错误又被 `except: pass` 吞掉——已修。
5. **WP-5 引用的 `check_four_factories_readiness.py` 不存在。** 复算命令改为 `scripts/check_score85_progress.py`（§11）。
6. **`calibration_models` 为 0 行不代表校准没跑**：`CalibrationService.try_fit` 只在 `status=ok` 时落库，`unmeasurable` 不写行（WP-3 预期结果）。证据要看孵化运行摘要里的 `calibration` 段。
7. **ForwardVerifier 曾把 `neutral` 方向当做多计分**（`float('neutral')` 失败后落到默认 +1；快照库 `strategy_signal_evidence` 有 72 条 neutral）。已修为排除并计数（`excluded_directions`），下一轮孵化后 effN 与命中率会变化，本节以前的 X/Z 数字均为修前口径。

本轮代码修复见 §10 第 2 轮。

---

## 0. 两个「85」口径钉死

本方案同时回应两个常被混在一起的目标，**它们的路径、时间、达成形态完全不同**：

| 口径 | 定义 | 当前实测 | 本方案判定的可达性 |
|---|---|---|---|
| **A. 孵化评分卡 85 分** | 95 分量表：证据 35（nav 天数≤30→20 + effective_n≤100→15 + 质量报告 5）+ 技能 30（5d LCB≥+10%→20 + 窗口一致性 10）+ 风险 20（MDD 分档，NAV<0.9 再 −4）+ 执行 10 | 19 个可测策略，最高 `combo_vmr_8cf25c8e` = 71.5 | **单个达成 ~2027-01-08（combo 到 ~90）**；**批量线 Y≥30（2026-10-23 用户裁决）**，前置 = 正 LCB 因子池扩到 ~30（§3 增补 §3.1） |
| **B. v3 方向命中率 85%** | 净收益口径，n≥250 交易日，Wilson LCB>0.80 + uplift LCB>0 + PF≥1.5 | 判据 6 gate 全 `unmeasurable`；600 条 outcome 卡死；4 个开关全关 | **19 个月后出第一次诚实判定**（v3 §7.1）；本方案负责把证据机器跑起来，不承诺 85% 达成 |

> **批量线裁决（2026-10-23）**：Y≥30 个 85+ 策略 = 一轮"批量达成"。诚实提示：当前正 LCB 策略只有 1 个（combo），lsmap 系 LCB +3.6% 够不到技能 20 分门槛（需 LCB≥+10%）。**30 个的前提是因子挖掘工厂把正 LCB（≥+10%）池子从 1 扩到 ≥30**——这是 alpha 研究产出，不是流水线参数。循环每轮记录三个数（可测池 X / ≥85 策略 Y / 正 LCB 因子 Z）；若连续 2 轮 Z 无增量（alpha 枯竭），按 §8 出"不可达 + 证据"终态报告，**不降线、不造分**。

**v3 认证结论（继承，不再论证）**：净收益口径 85% 当前数据不可达；宽松口径（"亏损≤8%"/止盈优先）能到 87–90% 但达标即亏钱（PF 0.01/0.83）。因此口径 B 的交付物是**判据与证据积累**，不是命中率数字本身。

---

## 1. 结论摘要

1. **[2026-10-25 作废，见勘误 1：combo skill LCB 为负，此路径不存在]** ~~口径 A：`combo_vmr_8cf25c8e` 是唯一诚实可达 85 的策略，且不需要动任何评分阈值。~~ 到 85 的机制是日历 + 风控：① paper_nav 每天 +1 行（NavEngine 已验证 15:30 出数），12 月中证据分封顶（总分先到 ~84）；② MDD 归因窗口（`get_paper_nav_rows(limit=60)`）在 nd≈69（推算 **~2027-01-08**）时把 7 月谷（07-16，−7.4%）滚出窗口，风险分 14→20，总分 ~90；③ 全部前提只有一个——**这 4 个多月里策略不再做出比 7 月更大的回撤**（§6 风控盯防项）。
2. **其余 18 个可测策略没有到 85 的路**（§4 逐一钉死原因），且 1780 个 `U（不可测）` 策略的解锁条件不是「跑分」，而是前向收益出数（WP-2/3）。
3. **`lsmap_*` 三个策略的 `decision=promote` 是坏数据**：`promotion_ready`（`overview.py:879`）判据里没有 NAV/MDD 维度，NAV 0.781 / MDD 22% 照样晋级——这是 v3 §2.7 明令拒绝的"只报命中率不报盈亏"形态。WP-6 修复后它们在下个孵化轮转 halt/deprecation。**修的是判据，不是它们的分数。**
4. **口径 B 的机器今天整体停着**（§2 卡点表）：K 线日更停 2 天、hitrate 4 开关全关、SignalTracker 停摆 5 天、strategy_factory 被 cohort 门硬拦。**第 1 周（WP-1/2/3）全部是恢复既有交付物，不写新代码**；第 2–4 周开开关、出诚实结论（WP-4/5）；第 4 周后修 2 处判据（WP-6/7）。
5. **负向承诺（§8）**：不放宽 LCB 阈值、不降 MDD 分档、不造 effN、不用宽松口径冒充 B 口径达标。

---

## 2. 事实核验（2026-10-23，全部可复现）

### 2.1 证据生产链路现状

| 链路 | 实测 | 影响 |
|---|---|---|
| K 线日更 | 活库 `kline_1d` 最新 bar = **10-20**（缺 10-21/22 两个交易日）；`trading_dates` 停在 10-16（紧凑格式） | WP-0 闸门 `stuck_pending_market_data==0` 永远红；600 条卡死 outcome 补不上 |
| NavEngine（孵化 daemon 持有） | 今天 15:30 已运行：**1902 行 nav_date=10-23**（快照库；活库 daemon 侧最后写 10-20，因其连接在 10-22 快照前） | combo nd 现 14 行（快照口径），**每交易日 +1** |
| SignalTracker（sidecar，不在 supervisor 内） | 停摆。`signal_forward_rates` 最后计算日 **10-18**（5/10d）与 **10-20**（1d）；10-20 那批 **1298 个信号 ~10-27 成熟**，无人计算 | combo `effN 88→100` 与次窗口 LCB 转正被卡 |
| 前向收益缺口 | `signal_forward_returns`：fwd5/10 最后 10-18；10-19/20/21/22/23 无行 | `forward_ic_5d` 全库 0 非零 = 信号侧证据断供 5 天 |
| strategy_factory | 10-21 后全 `skipped`：readiness **0.32**，`cohort_promotion_ready_complete_failure` 硬拦；LLM 未配置，能跑的最多 `partial_llm` | 可测池冻结在 19 个，85 之上无梯队 |
| 4 个 hitrate 开关 | `DAILY_PREDICTION / HORIZON_SCORING / CALIBRATION / SELECTIVE_GATE` 全 off（v3 §9.1 默认 false，.env 未设）；`daily_prediction_writer` 的 `as_of` 序列停在 10-21 | 交易日块 n 在 0 处 |

### 2.2 combo_vmr 证据序列（快照 14 行，nav_date→total_value）

```
03-20  99,993        06-04 100,294 ← 全期峰值
04-03  98,586        06-18  99,696
04-20  98,400        07-02  98,532
05-07  98,098        07-16  92,869 ← 谷（MDD 7.40%）
05-21  95,624        10-15  97,283 → 10-23  97,416（横盘）
```

MDD 归因 = `record_metrics` 内对最近 60 行 nav 的 running peak。模拟（未来每日 +1 行、净值横盘在 97,416）：

| nd | 日期(推算) | 窗口内 MDD | rk | combo 总分(估) |
|---|---|---|---|---|
| 14 | 10-23（今） | 7.40% | 14 | 71.5（实测） |
| 30 | ~11-30 | 7.40%（峰谷仍在窗口） | 14 | **84**（ev 封顶 35、sk 维持 20、ex 10） |
| 54 | ~12-18 | 7.40% | 14 | 84 |
| 67 | ~01-06 | 5.75%（峰值 06-04 滚出） | 14 | 84 |
| **69** | **~01-08** | **0.22%（07-16 谷滚出）** | **20** | **~90**（sk 仍 20；次窗口 LCB 转正则 95 上限内再 +10 空间） |

**85 的到达点在 01-06 → 01-08 之间（rk 14→20 的瞬间总分 84→90）。** 两个前置假设：① 未来 daily nav 不再跌破 97,416×(1−5%)≈92,545；② `get_paper_nav_rows(limit=60)` 的 60 行口径不改（改了就是移门柱，见 §8）。

### 2.3 全量评分分布（1799 active 策略，快照）

| 分层 | 数量 | 到 85 的判定 |
|---|---|---|
| `U（不可测）`：effN=0 或无 metrics 行 | 1780 | 不给数值分。解锁 = 前向收益出数（WP-2/3）+ 18:30 孵化轮重算，~10-27 起逐日 |
| 可测 | 19 | 仅 combo 可达（§4 逐条） |
| 有 paper_trades 但 metrics 错账 | lsmap ×3 | 先修判据（WP-6），分数本身 rk=0（NAV 0.781）到不了 85 |

---

## 3. 工作包（按依赖序，每项含验收与判据）

### WP-1 数据恢复（前置，第 1 周，不写新代码）

| 步骤 | 动作 | 验收 |
|---|---|---|
| 1.1 | 补 K 线 10-21/10-22：`sync_tasks` 的 `core_market` 最近一次 10-20 07:48 完成；按 `docs/DATA_SOURCES.md` 通道跑日更（东财 `datacenter-web` 通、TDX 通道按登记表核验） | 活库 `max(kline_1d.time)` ≥ 10-22；`trading_dates` 追平；`tdx_data_completeness` 有登记行 |
| 1.2 | 跑 `scripts/backfill_hitrate_stuck_outcomes.py`（WP-0.9 已交付） | 600 条 `pending_market_data` 按真实原因重评；K 线到位的转 `partial_daily_only`/`ok`，真缺的保持 pending 并计入 `data_sync_stalled`，**不写 0** |
| 1.3 | 起 SignalTracker sidecar（`scripts/factories/run_signal_tracker.py --daemon --run-time 18:30`，注意它**不在** supervisor 四运行体内） | `signal_forward_returns` 出现 `calculated_at > 10-23` 的行；fwd5/10 恢复日更 |
| 1.4 | 保持孵化 daemon + NavEngine 每交易日运行 | 每日 15:30 有 `nav_date=当日` 的行（今天已验证 1902 行） |

**风险登记**：SignalTracker 起不来时（如 `cn_stock_holidays` 缺失导致日历降级——已在 strategy_factory 日志里 swallowed），前向收益的"第 k 个交易日"判定会静默退化，1.1 验收时必须同时核验 trading_dates 与信号成熟日对得上。

### WP-2 开关 1/2 打开（第 2 周，依赖 WP-1 验收）

`STRATEGY_FACTORY_DAILY_PREDICTION_ENABLED=1` + `STRATEGY_FACTORY_HORIZON_SCORING_ENABLED=1`（写入 `.env`，supervisor `_child_env` 透传）。

| 验收 | 判据（v3 §6.1/6.3） |
|---|---|
| 每日预测 | `strategy_trade_predictions` 出现 `as_of = 最新已收盘 bar 日`（由数据推导，禁止墙钟——10-18 周日的 1497 条是反例，不再产生） |
| 可判分 | 可判分率 ≥70%；600 条卡死清零（WP-1.2）；重跑幂等（行数不变） |
| 告警 | `check_hitrate_evidence_gates.py --json` 入每日监控；`stuck>0` 连 2 天 / `scorable<0.7` 连 3 天 触发（v3 §9.2） |

### WP-3 开关 3 打开（第 3 周）

`STRATEGY_FACTORY_CALIBRATION_ENABLED=1`。

**预期结果按 v3 §6.2 B1/B2/B3 三门：大概率判「不可测」**（confidence 是 (horizon,direction) 查表，0 行 ≥0.85，桶数不足 30）。这是**正确的交付**：`calibration_models` 不产生 active 模型，`calibrated_confidence` 保持 NULL，WP-C 全 abstain——报告里写 `unmeasurable + 原因码`，不写"校准失败"。B 口径 85% 在出现单调正向的规则信号之前**没有前置条件可达成**，本工作包交付的是"证明没有"的证据。

### WP-4 开关 4 与 WP-C（第 4 周）

`STRATEGY_FACTORY_SELECTIVE_GATE_ENABLED=1`：gate 匹配率 ≥0.9 且观察 2 周后才允许转 `ordered` 下单；当前无 active 校准模型 → 全部 `abstained/calibration_unavailable`（v3 §13.5 已验证实测行为：7 条全 abstain、执行器零调用）。**开关打开的价值是把"不交易"变成有记录的 abstain 决策**，不是放行交易。

### WP-5 cohort 门解除 + LLM 配置（第 4 周起）

strategy_factory `skipped` 的根因：`factory_readiness_score=0.32`、`cohort_promotion_ready_complete_failure`（P0-3 maturity-aware cohort gate 的降级判定）。动作：

1. 配 LLM（`STRATEGY_LLM_*` / `AIASK_AGENT_MODEL_*` 组，supervisor `LLM_ENV_KEYS` 白名单内）→ 运行脱离 `partial_llm`；
2. 定位 cohort 门的具体失败分母（原文引用的 `check_four_factories_readiness.py` 不存在于本树）（是孵化池整体无 promote_ready，还是成熟度样本不足），修复分母而不是绕过门；
3. 验收：`strategy_factory_runs` 出现新 `success*`，新候选进孵化池，可测池 >19。

**不做**：把 `cohort_promotion_ready` 判据整体关掉——那等于把 85 分池子的唯一补给线换成劣质水。

### WP-6 `promotion_ready` 判据修复（第 2 周即可做，独立于 WP-1）

`packages/akshare-mcp/src/akshare_mcp/services/strategy_lifecycle_shared/overview.py:879`：

```python
promotion_ready = (
    primary_effective_n >= 60
    and secondary_effective_n >= 30
    and (primary_skill_lcb or 0.0) > 0.0
    and (secondary_skill_lcb or 0.0) > 0.0
    and (recent_primary_skill_lcb or 0.0) > 0.0
    and coverage_ratio >= 0.75
    and (stability_gap is None or stability_gap <= 0.05)
    and execution_hard_gate_passed
    and risk_hard_gate_status == "passed"      # ← 有 risk gate，但无 NAV 维度
    and not blockers
)
```

实测：lsmap 三策略 NAV **0.781 / MDD 22.1%**（`DEPRECATION_THRESHOLDS["mdd_critical"]` 已对 deprecation_risk 生效，但对 promotion **不设防**），`promotion_ready=True` → `decision=promote`，同时 10-19 的 metrics 陈账里 `total_trades=0`（错账，见 WP-7）。

修复内容（最小、行为可测）：

1. `promotion_ready` 增加 `and nav >= 0.90 and mdd <= 0.10`（阈值取既有分档：NAV<0.9 已是我方评分的风险降档线，MDD 10% 是 risk 分 14/20 的档线——不是新发明，是复用既有口径）；
2. 单测（行为断言）：注入 NAV 0.78 / MDD 0.22 / LCB 全正的 fixture → `promotion_ready` 必须 False 且 `blockers` 含稳定原因码 `nav_below_floor`/`mdd_above_ceiling`（**不写 None 冒充**）；
3. 回归：19 个可测策略重算 decision，只有 lsmap 三个从 promote → halt；combo（NAV 0.976）不受影响。

**验收**：lsmap 三个在下个 18:30 孵化轮转 `halt`/`needs_deprecation`，`strategy_promotion_reviews` 不再为其产 `watch/promote` 行。

### WP-7 metrics 错账修复（随 WP-6 同批）

`record_metrics`（`incubation.py`）按 `ensure_account` 返回的**单账户**取 `order_summary`，而 `ensure_account` 只升不降 stage（`warmup`→`paper` 方向锁死）：lsmap 的账户停在 warmup（`stage='paper'` 行早已不存在），metrics 却按 paper 语义生成 → `total_trades=0`，同时同策略 warmup 账户里躺着 7-16 一次性回填的 10 笔成交（`created_at=2026-07-16 04:32`，非日更自然产出）。

修复内容：

1. `record_metrics` 的 `total_orders/total_trades` 口径改为**该策略名下全部 active 孵化账户聚合**（account 维度 join 用 `strategy_incubation_accounts` 而非单一 binding），`metadata` 记 `account_aggregation=[...]` 署名；
2. 10-19 那批陈账不手改（证据链可追溯）：下个孵化轮新 metrics 行覆盖后，评分一律取最新行（我方评分器已按 `MAX(metric_date)` 取最新）；
3. 不追溯改 `paper_trades`（那批回填成交是 7 月真实操作，删改会破坏 `execution_audit` 的 3f 验收口径）。

**验收**：~~lsmap 三策略最新 metrics 行 `total_trades=10`~~（10-25 更正：lsmap 只有一个账户、20 笔成交，见勘误 4）；`metadata.execution_accounts` 列出实际计入的账户、`execution_aggregation_errors` 为空；combo 新行 14 笔成交不变。

### WP-8 判据 B 的证据积累里程碑（被动，第 13 周起）

- n_trading_days 按 WP-2 后节奏积累（~22 天/月）；
- **~2028-04**（n=250）记分卡首次可出 `pass/fail`（v3 §7.1：即使真实 85%，n=200 判据通过概率也只有 39.1%，方案必须写明，避免再次误判"85% 却没通过"）；
- 中间里程碑：每 6 周跑一次 `check_hitrate_evidence_gates.py`，快照归档 `docs/archive/`。

### WP-9 监控（第 1 周起持续）

| 对象 | 频率 | 触发 |
|---|---|---|
| combo 每日 nav 行 | 每日 15:30 后 | `nav_date` 未当日 +1 → 查 NavEngine 日志（`swallowed` 模式排查） |
| combo 回撤 | 每日 | 净值跌破 92,545（97,416×0.95，7 月谷线）→ 风险执行器 `reduce_position` 是否生效；**若发生新的 >7.4% 谷，85 路径时间线整体顺延并重新模拟（§2.2 表格）** |
| 前向收益 | 每日 18:30 后 | `signal_forward_returns.max(calculated_at)` 落后 >2 交易日 → SignalTracker 排查 |
| 交易日块 n | 每周 | 记分卡 `n_trading_days` 增幅 |
| cohort 门 | WP-5 完成后每日 | `strategy_factory_runs` 再出 `skipped` 即复测 readiness |

---

## 4. 19 个可测策略逐一判定（诚实口径）

| 策略 | 今分 | 到 85 的判定 | 卡死的具体数字 |
|---|---|---|---|
| **combo_vmr_8cf25c8e** | ~~71.5~~ | **不可达**（10-25 更正；原判「~2027-01-08 到 ~90」基于取错的字段） | skill LCB **−0.074**（10-23），技能 20 分拿不到；原表把命中率下界 0.454 当成 skill LCB |
| mapfix_5b385868d2 | 63.7 | 不可达 | 5d LCB **−0.073**（负），主窗口 20 分拿 0；LCB 转正需要信号 skill 真实 > 均值，时间补不上 |
| mapfix_76891f598a | 57.0 | 不可达 | LCB −0.103 |
| strat_rule_value/quality/mean_rev ×3 | 55.9 | 不可达（共享同一证据组） | LCB −0.085，三策略同账户 14 笔成交共享 |
| factory_1784164814/166715 | 55.7 | 不可达 | LCB −0.086，hit 48.7% ≈ 掷硬币 |
| strat_baseline_001（两账户） | 55.7 / 45.7 | 不可达 | 基准策略本身 LCB −0.086 |
| **lsmap_×3** | **54.0** | **不可达，且现有 promote 应撤** | rk=0：NAV 0.781 / MDD 22.1%；修复判据（WP-6/7）后转 halt；到 85 需要 NAV≥0.9 且 MDD≤5%，等价于要求策略停止亏 22%——不是运营旋钮 |
| combo_v3_b4eb022c | 52.5 | 不可达 | effN 39、LCB −0.1245 |
| stable_q_4 / stable_q_7_1969c3 | 52.2 | 不可达 | 同上（共享证据组，LCB −0.1196） |
| stable_q_7_4c04d3 / stable_q_8_04383f | 50.1 | 不可达 | effN 38–39、LCB −0.12 |
| stable_q_8_5009d6 | 44.5 | 不可达 | hit 34.2%（全库最低），LCB −0.111 |

> 「给每个策略评分」已完成（CSV 落盘 `.aiask-local/reports/incubating_strategy_scores_2026-10-23.csv`）；本表是**判定**：能到 85 的是 1 个，不能到的是 18 个，且每个"不能到"都挂着一个具体的、时间补不上的数字——这不是评分器太严，是信号 skill 不显著（v3 §2.5：全库最强因子 uplift +2.7pp，没有一个能把 top 0.1% 拉过 47%）。

---

## 5. 时间线

```
第 1 周 (10-24~10-31):  WP-1 数据恢复 + WP-6/WP-7 判据修复(可并行) + WP-9 监控上线
第 2 周 (11-01~11-07):  WP-2 开关 1/2 打开, 每日预测+判分跑通
第 3 周 (11-08~11-14):  WP-3 校准三门出结论(预期: 不可测+原因码)
第 4 周 (11-15~11-21):  WP-4 gate 打开(abstain 入库) + WP-5 cohort/LLM
第 13 周起:              WP-8 证据积累(~22 交易日/月), 每 6 周闸门快照
2026-12-18:             combo nd=54, 总分 ~84(证据分封顶)
2027-01-08:             combo nd=69, 7 月谷滚出 60 行窗口, 总分 ~90 → 口径 A 达成
2028-04 (n=250):        口径 B 首次可判定(pass/fail/unmeasurable, 三态不布尔化)
```

**提前量**：若 combo 在 12 月内净值创出 >100,294 的新峰，MDD 计算基础重置，rk 提前到 14+ 之上的条件变化——每 6 周用 §2.2 的模拟重算一次，不猜。

---

## 6. 风控盯防（85 路径的唯一威胁面）

combo 的 85 依赖"横盘不破谷"。已观测的自保护：MatchingEngine 每 30s 扫描，09:30 日志显示 `risk_executor reduce_position` 已对持仓占比 29.9%>18% 超限生效（inc_f5dd5f27 等账户）。盯防判据（§WP-9）：**任何单一策略账户在 7 月谷线（−7.4% 自峰值）之下新创谷，85 路径顺延，且该顺延要在监控里写明"是策略行为导致的"，不是日历自然到达。**

---

## 7. 与项目规则的对应（CLAUDE.md 逐条）

| 规则 | 本方案落点 |
|---|---|
| 诚实优先于永不报错 | 1780 个 `U` 不给 0 分；WP-3 判"不可测"不是"失败"；§4 的 18 个"不可达"逐条挂数字 |
| 省略必须被计数 | WP-7 的 `account_aggregation` 署名；metrics 取最新行而非聚合历史 |
| 数据标签不用墙钟 | WP-2 验收项：`as_of` 由已收盘 bar 推导（10-18 周日的 1497 条为反例，不再产生） |
| 降级要署名 | WP-1.3 的日历降级（`cn_stock_holidays` 缺失）必须核验后才算前向收益有效 |
| 闸门拦下 ≠ 噪音 | WP-5 只修 cohort 门分母，不开门 |
| 指标复核先独立复算 | §2.2 的 MDD 窗口模拟为全序列独立复算，非读入库值 |

---

## 8. 负向承诺（不做清单）

1. **不动评分阈值**：LCB +10%、effN cap 100、nav cap 30、MDD 分档——任何一个为凑 85 而调，都是移门柱，且把"不可测"包装成"可测"。§2.2 的 85 路径一个阈值都不依赖。
2. **不改 MDD 归因窗口 60 行口径**：改成 30/20 行可把 85 提前到 1 月中/底，但窗口变短 = 风险敏感度被静默降低，与 v3 "只报命中率不报盈亏"是同一类错误。
3. **不手工补 lsmap 的分数**：它们的正确处理是 WP-6/7 转 halt，不是把错账修成"看起来能晋级"。
4. **不用 B 口径宽松判据冒充 85%**：v3 §3.3 三种拒绝口径保持拒绝；PF/净收益 LCB 与命中率并行报告，缺一项的记分卡按无效处理。
5. **不在活库上做回溯改写**：10-19 陈账保留，新行覆盖；`paper_trades` 的 7 月回填成交保留（audit 3f 口径依赖它）。
6. **不把 SignalTracker 塞进 supervisor 四运行体**：它是 sidecar，`03-四工厂运行规范` 的边界不动。

---

## 9. 交付物清单

| 交付物 | 位置 | 完成标志 |
|---|---|---|
| 本方案 | `SCORE_85_SOLUTION_PLAN.md`（根目录） | 本文 |
| 评分快照 | `.aiask-local/reports/incubating_strategy_scores_2026-10-23.csv` | 1799 行 |
| WP-6/WP-7 代码 + 行为测试 | `overview.py` / `incubation.py` + 对应 test | lsmap 转 halt、combo 不变 |
| 每日闸门快照 | `docs/archive/hitrate_gates_YYYY-MM-DD.json` | 每 6 周一份 |
| 85 达成判定记录 | 本文件 §2.2 表格追加实测行 | 2027-01 前后一次 |

## 10. 循环台账（每轮记录 X / Y / Z）

| 轮 | 日期 | X 可测池 | Y ≥85 | Z 正 LCB | 本轮动作 | 遗留/阻断 |
|---|---|---|---|---|---|---|
| 0（基线） | 10-23 上午 | 19 | 0 | 1（combo，L5d +45.4%） | 全量评分 1799 → CSV | — |
| 1 | 10-23 晚 | 19 | 0 | 3（combo + lsmap 系 LCB +3.6%） | ① 修 `provider.py` 三个 sync 方法接 `as_of`（SignalTracker 18:30 卡 `TypeError` 的根因，20:00 全 Phase 跑通、errors=0）；② 修 orchestrator Phase G `transitions` list/int 归一；③ WP-6：`promotion_ready` 增 paper NAV/MDD 两道门（overview.py，原因码 `paper_nav_*`）；④ WP-7：`record_metrics` 执行证据聚合该策略全部 active 账户（署名 `execution_accounts`）；⑤ 孵化 18:30 轮出 661 行新 metrics；⑥ SignalTracker 20:00 跑通全 Phase | **K 线停在 10-20（无网络通道）** → 前向收益成熟批次（10-16 的 1137 信号 ~10-27 到期）与 600 条卡死 outcome 全部等 K 线；cohort 门 readiness 0.32 仍硬拦 strategy_factory（`skipped`），需 LLM + 真实 promotion_ready 解除 |

| 2 | 10-25（周日） | 权威库 18 / 快照库 36（口径：最新 metrics 行 decision 非 rejected/deprecated/archived 且 effN>0；修前 neutral 计入） | **0（上界）** | **3（lsmap ×3，+3.7%；无一 ≥+10%）** | 代码修复 ① 包分层：hitrate 钩子从 strategy-factory 移回 akshare-mcp 适配器（strategy-factory 4 个边界测试由红转绿）；② WP-6 去掉 `except: pass`，读失败按 `paper_evidence_unavailable` 拒绝晋级，抽出 `apply_paper_promotion_gate` 并加行为测试；③ WP-7 占位符 `IN (1)`→`IN ($1)`，聚合失败写 `execution_aggregation_errors`（变异测试验证：改回旧代码后测试失败 2≠5）；④ 新增 `aiask_quant_core.market_session`：SignalGenerator / MetricsRecorder / 结算 / SignalTracker / DataCollector 不再用墙钟日期，取最新全市场同步会话；⑤ ForwardVerifier 排除 neutral 方向并计数；⑥ 新增 `scripts/check_score85_progress.py` 替代未提交的评分 SQL | 权威库 K 线未追平（10-16 后每天 53 只，需联网日更，沙箱无写权限）；权威库已有周末日期行（10-18、10-24）待处置；supervisor/sidecar 进程均不在；LLM 配置尚未经一次 factory run 验证 |

> 每轮重算命令：`python scripts/check_score85_progress.py [--db PATH] [--json]`。X 取最新 metrics 行 decision 非 rejected/deprecated/archived 且 `effective_n_5d>0`；Y 只报**上界**（`skill_lcb_5d ≥ +10%` 的策略数，到 85 的必要条件，不是得分）；Z 取 `skill_lcb_5d > 0`，**不用 `hit_rate_lcb_5d`**。

## 11. 复现附录

```bash
# X / Y 上界 / Z、流水线状态、非会话日期标签审计（只读；默认权威库）
/opt/miniconda3/envs/aiask/bin/python scripts/check_score85_progress.py
/opt/miniconda3/envs/aiask/bin/python scripts/check_score85_progress.py --db .aiask-local/data/db/akshare_mcp.sqlite3
# 评分重算(快照库只读)
/opt/miniconda3/envs/aiask/bin/python - <<'EOF'
import sqlite3
con = sqlite3.connect("file:/Users/mac/Desktop/张勇/AIASK/.aiask-local/data/db/akshare_mcp.sqlite3?mode=ro", uri=True)
print(con.execute("SELECT MAX(nav_date) FROM paper_nav").fetchone())
print(con.execute("SELECT MAX(time) FROM kline_1d").fetchone())
EOF
# 闸门
cd /Users/mac/Desktop/张勇/AIASK && /opt/miniconda3/envs/aiask/bin/python scripts/check_hitrate_evidence_gates.py
# combo MDD 窗口模拟(§2.2) 输入: paper_nav WHERE account_id='inc_92143ebd'
```
