# 策略可交易性验证标准 v1（A股日线 / 纸面实盘）

状态：门禁与证书读写已实现；**端到端真实 OOS / 成本压力 / 数据快照证据生产链仍未接通，当前不能据此认证任何历史候选**。实现：`packages/strategy-factory/src/strategy_factory/application/tradeability_gate.py`（gate v1，证书 v1）。

> **历史策略声明：** 旧回测、旧质量门禁、LLM 解释或 submission 结果均为历史研究材料 / 候选筛选结果，不是交易建议、可交易证明、纸面验证或实盘授权。未取得本标准所需的绑定证据前，状态必须保持 `research_only`。

## 1. 目标与边界

- 目标：建立一条可复核、扣除现实成本、统计验证充分、遵守 A 股执行规则并经隔离模拟盘验证的策略认证流程；现有旧筛选本身不满足此目标。
- 边界（v1 硬约束）：
  - 市场：A股日线；仅做多/现金；无期货、融资、盘中交易。
  - 信号在收盘后确认，**下一可交易时段**开盘执行（`next_session_open`）。
  - 不启用真实资金交易；`paper_validated` 不等于收益保证、不等于实盘授权。
  - 未通过数据完整性/可交易性的候选**拒绝并记录**（fail-closed），不允许静默降级。
- LLM 只能生成候选与解释，**不能授予认证**。唯一入口是 `build_tradeability_certificate()`，输入是实测证据。

## 2. 认证状态机

| 状态 | 含义 | 升级条件 |
|---|---|---|
| `research_only` | 缺证据，可重跑 | 初始默认；缺 profile 字段、身份哈希、数据快照哈希时停留于此 |
| `backtest_candidate` | 真实 OOS 回测证据完整且达标 | OOS ≥3 折且有效 round-trip ≥30、OOS 交易日 ≥60、后成本 Sharpe ≥0.35、MDD ≤0.25、profit_factor > 1、净收益 block-bootstrap 95% LCB > 0、多重检验（DSR ≥0.10 / PBO ≤0.35 / White & Hansen p ≤0.10）全部可用且通过、成本压力（保守 ≥2× 基准成本）下净优势仍在 |
| `paper_trial` | 已绑定模拟盘，证据积累中 | 达到 `backtest_candidate` 且存在纸面证据 |
| `paper_validated` | 纸面验证通过 | 纸面有效交易日 ≥60、已闭合 round-trip ≥30、纸面净 Sharpe ≥0.50、profit_factor ≥1.5、MDD ≤0.25、净收益 LCB > 0，且 `candidate_hash_bound=True`、`evidence_track=real` |
| `rejected` | 完成测试但证伪了冻结判据 | 任一 `complete=true` 的判据不达标 |
| `expired` | 证书失效 | 超过有效期（90 天）或 `expires_at` 已过期 |

拒绝与缺失的区分：`rejected` 是「测出来是坏的」，`research_only` 是「测不出来」。前者必须人工/新候选处理，后者可以重跑补齐证据。

## 3. 证据绑定（哈希即失效）

证书携带：
- `tested_object_hash`：策略类型 + 逻辑/DSL/因子/入场出场签名（`build_tested_object_hash`）。
- `execution_contract_hash`：执行契约（targeting、持仓、交易计划、风控、组合规格、rebalance 等，`build_execution_contract_hash`）。
- `tradeability_profile_hash`：完整 A股运营契约（执行/宇宙/持仓退出/仓位/风控/成本模型/流动性/验证设计）。
- `data_snapshot_hash` + `as_of`：数据快照。
- `cost_model_version`：成本模型版本。

任何一项变化，`validate_tradeability_certificate_freshness()` 判 `certificate_stale_<field>`，证书必须重建。`candidate_identity_incomplete`（`tested_object_backfill_incomplete` / `legacy_identity_partial`）直接锁在 `research_only`。

## 4. 缺失数据规则（fail-closed）

- 未测量字段一律保持 `None` + 稳定原因码，**不填 0、不填默认值**。
- 派生值（`derived=true`）不作为证据：OOS fold、成本情景、执行事件、纸面指标任一 `derived=true` 即不可用。
- 重复 round-trip 计数（`independent_trade_ids` 冲突）会直接拒绝该折。
- purge/embargo 违规、OOS 窗口重叠、final holdout 未密封或被用于选择，均产生阻断原因码。

## 5. 多重检验

复用 `aiask_quant_core.validation` 真实实现（不是近似派生值）：
- `deflated_sharpe_ratio`（`n_trials` 必须显式声明，≥2）
- `probability_of_backtest_overfitting`（CSCV，8 折）
- `white_reality_check` / `hansen_spa_test`（各 1000 次 bootstrap，seed=1729）

计算失败 → `multiple_testing_calculation_failed:<Exc>`，证书停在 `research_only`。

## 6. 执行语义证据

`execution.order_fill_events` 逐事件校验：
- `execution_date > signal_date`（同日/乱序执行直接拒绝——A股 T+1 与收盘信号约束）；
- 成交价必须 `next_session_open`；
- 已成交订单必须带 `fill_price` 与完整 `fee_breakdown`；拒单/部分成交必须带原因。

成本双情景：`base`（带 `evidence_ref`）与 `conservative`（`cost_multiple` ≥2.0）；保守情景净收益 ≤0 → `conservative_cost_net_advantage_disappears`。

## 7. 纸面证据（由 paper_trading_bridge 提供）

- `valid_trading_days`：去重 NAV 日期计数（非行数）。
- `closed_round_trip_count`：按 `position_id` 配对 buy/sell 实际成交的完整回合（含费后净 PnL）。
- `net_return_ci_lower`：closed round-trip 净收益的 95% 置信区间下界。
- Sharpe / profit factor / MDD / win rate 全部基于实际成交配对，**不得用 `amount>0` 之类代理**。

## 8. 运行入口

- 周期内：`cycle_runner` 在 submit 之后为每个候选构建证书，写 `results["tradeability_certification"]`（含各状态计数），证书以 `tradeability_certificate` artifact 落库。
- 读取：`factory_tradeability_certificates(run_id)`（只读）。
- 当前实现仅消费候选载荷中的 `tradeability_backtest_evidence` / `tradeability_paper_evidence` 和 snapshot 中明确提供的数据绑定；不会从旧回测报告推导原始 OOS 证据。若这些输入缺失，结果是 `research_only`。运行摘要明确报告证据输入计数及 `evidence_generation: candidate_payload_only`。
- 证书状态不改写策略业务状态、不触发自动晋级，也不能开启实盘。`paper_validated` 仅代表满足本证书门槛的纸面证据，不代表收益保证或实盘许可。
- 阈值全部冻结在 `TRADEABILITY_THRESHOLDS`（`profile_version: a_share_live_admission_seed_2026_10_v1`）；改动必须升 gate 版本。

## 9. 当前证据状态（2026-10-21）

- 纸面数据严重不足：paper_nav 3164 行、0 个账户 ≥30 天（max 13，均值 1.79）；无策略可达 60 天 + 30 round-trip 门槛。
- 因此当前全部候选的实际状态为 `research_only`；这是正确的 fail-closed 结果，不是缺陷。
- 当前 cycle runner 尚未连接由实时纸面成交自动生成认证输入的完整链路；每日纸面周期本身不会让证书自动升级。只有同一候选身份下的真实证据被明确绑定并重新评估，证书状态才可能变化。
