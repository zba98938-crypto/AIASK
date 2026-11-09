# 因子挖掘工厂与市场事件摄取审查（2026-11-04）

审查根目录：`/Users/mac/Desktop/张勇/AIASK`。只读业务源码与既有测试；未修改项目文件、未启动工厂、未接触生产数据库。独立复现使用仓库 hermetic 隔离器、SQLite `:memory:` 与内存 event provider。未联网验证公告接口，不声称任何源今天可达。

## 结论

两个运行体具备实质研究能力，不能按空壳处理；但尚不宜认定形成可靠的持续策略生产线。最重要的缺陷是因子池跨进程更新不传播，以及公告负面方向被标成正面。其次，新编排的质量统计/持久记忆不完整，正交化与动态预算存在“模块已写但生产主链未接线”的情况。

## 实际能力和边界

| 能力 | 实现依据 | 判断 |
|---|---|---|
| 五引擎生成 | `factor_mining_factory/engines/engine_scheduler.py` 注册 LLM、GP、MCTS、RL、Rule | 已实现；RL 是 epsilon-greedy/Q-table 简化实现，不能称完整 PPO/神经 AlphaGen |
| 搜索→进化→quick→strict→入池 | `strategy_factory/application/factor_mining/orchestrator.py` + `runtime/provider_adapters.py:229` | 生产路径存在，provider 默认仍来自 AKShare |
| DB-only 健康验证集合 | `engines/context.py` | 默认至少 120 只、500 bars、最新日期偏离≤3天、缺行比例≤5%；数据不够跳过 |
| 因子编译和数值验证 | `factor_validation_pipeline.py` | 有 DSL 编译、横截面IC、样本外验证、bootstrap、lookahead、多重检验、IC历史持久化 |
| 池治理 | `pool/active_pool.py`、`reappraise.py`、`feedback/decay_monitor.py` | 有准入、替换、衰减、隔离/复评/退役；缓存传播有严重缺陷 |
| QC | `factory.py:108`、`qc_pipeline.py` | 可运行 OOS/分层/鲁棒性；默认关闭，自动上下架另有开关；不能当默认启用的能力 |
| 正交/经济验证 | `pool/orthogonalizer.py`、`validation/economic_sense.py` | 实现了 helper，但源码全树检索未见生产调用；准入实际只用表达式 token Jaccard |
| 组合优化 | `api.py:145`、`pool/portfolio_optimizer.py` | 有方法接口；gateway 从 fitness 伪造 IC/固定std，且不传相关矩阵，max_sharpe 实际退回 ICIR；未查到生产消费者，列为能力边界 |
| 公告来源 | `market_event_sources.py:76` | cninfo/SSE adapter 已实现；szse/bse/csrc 和 Wind/iFind/Choice/Tushare adapters 标明未实现；不要把已声明当已接入 |
| 事件规范化和可信桥接 | `market_event_sources.py:471,545,667` | 有稳定ID、tier、时间、来源链、跨源签名合并、冲突隔离、单锚置信封顶、cluster/signal入库 |
| 事件第四运行体 | `scripts/factories/run_market_event_ingest.py` | 调用 canonical runtime；默认 AKShare support 有 `_resolve_runtime_args`，实际走 runtime `_run_legacy_ingest`，不是新 application orchestrator |

## 已确认问题

### P1：因子池只载入一次，跨运行体新晋级和退役长期不可见

- 主位置：`/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/factory.py:69-79`，尤其 `_pool_loaded_from_db` 为真就返回。
- 消费路径：`.../factor_mining_factory/api.py:104-130` 的 `get_active_factors()` 每次先调用上述方法，然后直接返回内存池。
- 真实消费者：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/research/factor_research_builder.py:100-123`。这里的5分钟TTL只缓存gateway返回值，并不能刷新gateway背后的永久内存缓存；只有列表为空才走带 `include_quarantine=True` 的DB路径。因此“已有可消费缓存”的长期策略进程会持续使用已退役因子，也看不到新因子。
- 同进程 QC/复评直接写 DB，也未全面同步/清除该缓存。
- 真实 storage+内存SQLite复现：初次载入 active `old` → DB把 `old` 改 retired 并新增 active `new` → 第二次 gateway 仍返回 `['old']`。
- 建议：使DB成为消费权威，或带版本/TTL增量刷新并替换整个池，必须同时删除退役项；增加“两provider/两进程语义”回归。

### P1：业绩预减默认解释为利好，并进入可信正向事件信号

- 主位置：`/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/market_event_sources.py:42` 把 `预减` 与 `预增`、`扭亏` 放在同一组，统一 `up`。
- 传播：同文件 `420-425` 用缺省方向；`667+` 将 `up` 映射为 `positive` 保存给策略工厂。
- 真实 normalize→persist→bridge 复现标题：`2026年三季度业绩预减公告：预计净利润同比下降80%`，tier_a/cninfo、600519、有效日期。得到 `('earnings_forecast','up','verified')`，桥接信号 `('600519','positive')`。
- 不应把可信来源等同于正面投资方向。单锚需要额外确认的字段仍在，但方向已经错误，会污染事件研究输入；并不意味着当场自动实盘买入。
- 建议：类别与方向分离；预减/亏损/终止合同/失败重组等显式负向，未能确定时为neutral/unknown；加从公告到信号的行为测试。

### P2：新因子编排错误统计晋级/隔离，quick漏斗也混入strict失败

- 主位置：`/Users/mac/Desktop/张勇/AIASK/packages/strategy-factory/src/strategy_factory/application/factor_mining/orchestrator.py:242-274`。
- `admitted` 实际是 `{admitted, promoted_active, quarantined, record:{status,...}}` 字典（`pool/active_pool.py:432+`），却用 `getattr(factor,'status')`；`validated` 是无status字段的 FactorCandidate。因此本轮晋级和隔离计数始终0（另加复评promoted计数不受这点影响）。
- `quick_passed = len(validated)` 把严格验证后的结果冒充quick通过数；strict拒绝也被归为quick_evidence拒绝。
- 复现4个evolved、1个validated、1个真实active admitted：`promoted=0`、`active_promoted_count=0`；非真实漏斗。
- 新summary未输出旧版 `strict_candidate_results` / `quick_candidate_results` / `by_engine` / `by_blueprint` / `reject_reasons`。持久模式记忆读端（host factory.py:287-304）只识别前两个字段，新的run无法提供重启后成功/失败模式学习。
- 建议：建立 typed report DTO 和结构测试；明确每阶段人数；保留生产读端需要的明细。不要只测试host旧 `_build_quality_summary`（它仍留着但生产run未调用）。

### P2：真实因子相关性未接入准入，文本相似度冒充 correlation

- 主位置：`/Users/mac/Desktop/张勇/AIASK/packages/akshare-mcp/src/akshare_mcp/services/factor_mining_factory/pool/active_pool.py:292-307,578-591`。
- 准入把 token 集合Jaccard写入 `max_correlation` / `correlation_to_pool`，据0.7阈值判重复/替换；独立 `FactorOrthogonalizer.check_orthogonality` 没有生产调用。
- `ts_mean(close,5)` 与数学等价 `ts_sum(close,5)/5` 的门禁值是0.5，不是实际1.0。此复现证明相关性字段口径错误，不声称该简单表达式单独能越过复杂度准入。
- 后果：语义相同因子可因写法不同绕过去重，文本相似但经济不同的因子可能被误拒；不能声称已经保证组合独立信息或增量IC。
- 建议：文本值命名为similarity，仅预筛；在共同股票/日期上计算因子值相关性和残差IC，样本不足明确未知。

### P2：引擎基于质量动态调预算的反馈没有生产调用

- 位置：`.../engines/engine_scheduler.py:198-214` 的 `_apply_quality_feedback` 依赖 `_quality_feedback`，唯一填充函数 `record_quality_feedback` 全树调用只出现在测试。
- canonical orchestrator 调 provider.record_feedback；legacy provider→host `_record_feedback` (`factory.py:1066-1074`) 只写 MetaLearner，未调用 engine scheduler。
- 后果：基于入池率/验证率调高或调低预算的分支在生产永远无历史；实际只有池大小/衰减率决定固定配比。与持久模式明细丢失共同削弱“持续自进化”。
- 建议：每轮完成后写scheduler反馈，重启后从版本化run报告恢复，并测连续两轮budget会变化。

## 其他缺口/统计限制（与以上已复现bug分开）

1. 搜索多重检验分母只计quick筛选且strict_limit截断后的候选数量：host `factory.py:482-508`；快速搜索/进化评估的其余表达式未纳入；下游DSR只取max(该数,基准family列数)。目前不能声称已经按全部搜索尝试校正选择偏差。应记录唯一被评估表达式数，并保留时间上的最终未见样本。
2. 公告请求共享总limit，按cninfo→SSE顺序；首源填满limit后SSE得到limit=0，并仍标status=ok（`market_event_sources.py:333-367`）。官方多源覆盖/交叉确认不是均衡调度；应有provider配额和“预算跳过”状态。
3. 因子validation universe不足120时 `orchestrator.py:65-72` 提前返回，不调用persist_mining_run；常见数据阻塞轮次不会进入因子运行台账。
4. `FactorMiningFactory.status()` 的 `_last_run_at/_run_count` 仅初始化和读取，全树未找到更新；不能当真实运行计数。
5. 事件modern application orchestrator的逐事件异常只log，最终status仍completed，BRIDGE_TIMEOUT_SEC未使用；当前AKShare默认legacy路径不受该段直接影响，未来provider迁移需补合同。
6. 组合权重gateway使用fitness*0.03作为ic_mean、固定0.02作为ic_std、fitness*0.5作为ic_ir（api.py:161-164）；这些不是观测IC。无相关矩阵时max_sharpe返回ICIR。当前未找到真实消费者，属接口能力未完成。

## 验证

环境：`/opt/miniconda3/envs/aiask/bin/python`；各tests/conftest先装hermetic隔离。

选定以下7文件：factor_mining_provider_protocol、factor_mining_runtime_contract、market_event_ingest_bridge_contract、market_event_ingest_lookback_alias、factor_mining_quality_bounds、market_event_sources、market_event_ingest_runner：**68 passed in 1.50s**。

再跑 factory_deep_repair、factory_deep_repair_part2、factor_validation_bootstrap：**48 passed in 3.96s**。合计 **116 passed**。不是全库测试结论，也不是真实数据/真实供应商/完整生产cycle通过。

独立可重复脚本：`/Users/mac/Desktop/张勇/AIASK/docs/reviews/evidence/2026-11-04/factor_events_repro.py`，真实storage/规范化桥接、内存隔离；其stdout复现了缓存不刷新、promoted=0、负面公告positive、等价表达式文本相关性0.5。未将临时复现加入仓库。
