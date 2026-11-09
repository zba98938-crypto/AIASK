# 因子与事件修复交付（2026-11-04）

范围：审查索引 F1–F4、E1。五项已完成代码修复与隔离回归。保留其他代理和用户已有修改；没有提交 Git、启动真实工厂、联网或写生产数据库。

## 修复结果

- **F1 因子池刷新**：每次 gateway 获取时重新读取数据库并原子替换内存集合，清除已退役/删除项；两个独立消费方可看到新因子。读库失败明确抛错，不再返回陈旧缓存。上层 research builder 原有五分钟 TTL 仍保留，因此跨运行体可见性是有限 TTL，而非实时订阅。
- **F2 漏斗与记忆**：canonical orchestrator 使用实际 quick 通过列表，并按 admitted 字典的 record.status 统计 active/quarantine；新增版本 `factor_mining.quality.v2`，分开 strict attempted/deferred、各阶段拒绝数，恢复 by_engine/by_blueprint、quick/strict 决策明细，使持久模式记忆可恢复。
- **F3 数值正交**：生产 context 安装 DB-only 数值评价器，编译真实 DSL，在 `(交易日, 股票代码)` 对齐值上计算 pooled Spearman，记录观测数、样本来源、残差 IC 和独立比率。缺行情、日期无交集、常量或无效表达式明确不可测并拒绝，不能当零相关。文本辅助字段改名 max_similarity。正常 quarantine 复评与维护晋级也执行数值门槛，同批次晋级者立即进入后续候选的比较集合；dry-run 模拟相同顺序但不写库。晋级保存失败不再计成功。
- **F4 质量反馈预算**：生产 provider→factory 反馈调用 scheduler；运行报告持久化 by_engine，重启后按时间恢复最近反馈。刷新时替换历史，避免反复加载造成重复计数。行为回归验证下一轮预算实际变化及重启后的相同分配。
- **E1 事件方向**：收益公告类别与方向分离。预减/预亏/利润下降为负向，预增/扭亏/亏损收窄为正向，泛化或冲突表述为中性；终止合同/失败重组为负向。端到端验证 normalize→persist→bridge 不再将“净利润下降80%”变成 positive。

## 验证

Python：`/opt/miniconda3/envs/aiask/bin/python`。全部使用仓库 conftest 的 hermetic 隔离、内存 SQLite 和合成行情。

1. 定向七文件 **121 passed in 1.77s**。日志：`/tmp/aiask_factor_fix_tests.log`。
   - test_factor_event_audit_fixes.py
   - test_factory_deep_repair.py / test_factory_deep_repair_part2.py
   - test_market_event_sources.py / test_factor_mining_quality_bounds.py
   - test_factor_mining_runtime_contract.py / test_factor_mining_provider_protocol.py
2. 扩展八文件 **75 passed in 9.75s**。日志：`/tmp/aiask_factor_fix_extended_tests.log`。
   - 新回归、factor_validation_bootstrap、market_event_ingest_runner、factory_db_only_boundary
   - market_event_ingest_bridge_contract、market_event_ingest_lookback_alias
   - factor_research_active_pool_fallback、runtime_provider_boundary
3. 所有本分工文件 `git diff --check` 通过。

两个测试组有重叠，不能相加为独立测试总数。扩展组首次命令使用了三个不准确文件名，未执行测试；纠正为仓库实际路径后以上 75 项通过。

## 修改文件

宿主 `packages/akshare-mcp/src/akshare_mcp/services/`：
- factor_mining_factory/api.py、factory.py、reappraise.py
- factor_mining_factory/engines/engine_scheduler.py
- factor_mining_factory/pool/active_pool.py、orthogonalizer.py、storage.py
- 新增 factor_mining_factory/pool/numeric_orthogonality.py
- market_event_sources.py

Canonical `packages/strategy-factory/src/strategy_factory/application/factor_mining/`：
- orchestrator.py
- 新增 quality_summary.py

测试：新增 `packages/akshare-mcp/tests/test_factor_event_audit_fixes.py`；调整已有 deep_repair 两文件的假数据，移除“读库失败回落旧缓存”“用文本相关性做准入”“无正交证据直接晋级”的旧假设。

## 验收边界

- 数值相关性是明确标注的 pooled Spearman，不代表所有市场状态下正交，也不代表组合优化或全量搜索选择偏差已解决。残差统计不足时留空，不伪造有效数值。
- 已有池和旧报告未重算，旧错误事件也未批量修复。本次仅验证新路径行为，不能宣称生产历史证据已清洁。
- 原审查列出的经济意义 validator、组合接口 fitness 代理 IC、全搜索多重检验分母、额外公告源 adapter/配额等“能力缺口”未纳入本次五个确认项，不应升级为已经完成的能力。
- 真实数据吞吐、网络可达性、长时间运行和实盘均未验收。完整根测试由主代理统一运行。
