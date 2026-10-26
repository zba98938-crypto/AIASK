# 策略研究结论（24h 工厂长跑 · 2026-10-21）

本文件夹是「四工厂 24 小时长跑 + 证据库提炼」的交付物。

## 可交易性状态（2026-10-21 起生效）

本目录全部策略为**历史研究假设，未经可交易性认证**。
依据 `docs/STRATEGY_TRADEABILITY_STANDARD.md`：认证状态机为
`research_only / backtest_candidate / paper_trial / paper_validated / rejected / expired`，
LLM 生成的候选不能授予认证。当前纸面证据（paper_nav 无账户 ≥30 天）远低于
`paper_validated` 门槛（60 有效交易日 + 30 已闭合 round-trip），因此本目录
所有策略的实际认证等级为 **`research_only`**，不构成交易建议或实盘依据。
随 07:30 每日纸面周期积累证据后，由 `build_tradeability_certificate()` 自动判定升级。

## 工厂长跑状态（启动时证据）

- 四工厂 supervisor（`run_three_factories.py`）启动于 2026-10-20 09:23（本地），
  4 个子运行时：strategy_factory（dispatch 模式，全市场 top-200 默认池）、
  factor_mining_factory、incubation_factory（daemon，18:30 批跑 + catchup）、
  market_event_ingest。supervisor PID 16571，`--no-restart` 未启用（崩溃自动拉起）。
- 启动时已运行 14h02m（本次会话确认时点 23:28），续跑至 24h 无需干预；
  strategy_factory.log 最近活动 23:28:58（本会话内仍在写）。
- 近 24h 入库：`strategy_factory_runs` 193 条、dispatch 累计 671 条、
  前向收益记录 27,699 条（`signal_forward_returns`，最新计算 2026-10-20 01:12）。
- 健康诊断（`diagnose_factory_health.py`）：9 项 6 OK / 2 WARN / 0 FAIL /
  1 BLOCK。BLOCK 项 = execution audit gate（8175 个策略审计结论「证据不足」），
  属**诚实的拒发状态**，不拦截工厂生产；paper 订单 523 笔、成交 502 笔。

## 交付文件

| 文件 | 内容 |
|---|---|
| `short_term_strategy.md` | 短线（≤10 日持有）交易策略：价值/质量因子 60/72 日回看、周频再平衡 |
| `long_term_strategy.md` | 长线（20–48 日持有）交易策略：缺口回补 + 动量突破双轨、止损止盈规则 |

## 证据口径（两份策略共用，必须读）

- 前向收益 = `signal_forward_returns`（signal 发出后 N 日的实际涨跌幅，
  按 strategy × signal 聚合，样本 2026-01-15 ~ 2026-10-20）。
- **样本外 t 统计量按策略级均值/标准差 ÷ √n 计算**；n 为信号数。
- 20 日前向：全部策略平均 **+0.179%/20d**（6,822 信号），
  5 日 **+0.034%**，30 日 **-2.667%**（n=402，样本极少且含 7-9 月段）。
- 显著正向（t≥+2，n≥100）集中在 **价值/质量类 60–72 日回看规则策略**：
  20d 均值 +2.39%（t=+4.48, n=335），5d +0.36%（t=+0.90）。
- 显著负向（t≤-3.6）集中在 **短期动量策略**（`stable_q_4/7_*` 等，
  20d 均值 -4.5%，t=-6.11, n=132）—— 该族已被门禁标记 rejected，勿手工复活。

## 重要限制（诚实披露）

1. 上述策略 DB 状态均为 `rejected`（工厂门禁拒发，因证据不足 / 参数扰动
   缺失等原因），**唯一 `submitted` 且前向证据为正的是 `combo_vmr_8cf25c8e`**
   （20d +1.31%, t=+2.38, n=270）。rejected 策略的前向收益是「若当时执行」
   的纸面推演，不是已兑现收益。
2. 30 日前向样本只有 402 条且均值为负，长线结论的持有上限就取 20 日
   （再平衡/时间止损口径），不是「买入持有 60 天」。
3. 单用户 paper 口径：全部基于 `execution_semantic_mode='paper'` 的信号，
   未启用 live 交易（`LIVE_TRADING_ENABLED=0`）。

## 2026-10-21 修复记录：paper NAV 快照此前从未写入

24h 长跑期间 `strategy_factory` 日志反复出现
`'SQLiteAdapter' object has no attribute 'fetch'`（paper_trading_scheduler.py:168
吞掉 AttributeError 后继续），`paper_nav` / 模拟盘日循环的 NAV 快照实际从未落库。
根因：`fetch`/`fetchrow`/`execute` 只存在于 `SQLiteConnection` 包装器（`acquire()`
才拿到），而跨包传入 scheduler 的是 `SQLiteAdapter`（`SchemaBase`）实例。
修复：`SchemaBase` 增加 `fetch`/`fetchrow`/`fetchval`/`execute` 代理（quant-core
`schema_base.py`），`execute` 返回可 `await cursor.fetchall()` 的句柄（tuple 行，
execution_universe 契约形状），原生 `?` 占位符直接绑定、`$n` 走
`_prepare_statement`；单测 `packages/aiask-quant-core/tests/test_schema_base_async_proxies.py`。
修复后重启 strategy_factory 子进程，日志中该错误计数冻结（不再新增），
E2E 验证 proxy 链路的 INSERT/SELECT 正常提交。本目录证据不受影响
（forward returns 来自 `strategy_signals` 表，不依赖 paper NAV）。
