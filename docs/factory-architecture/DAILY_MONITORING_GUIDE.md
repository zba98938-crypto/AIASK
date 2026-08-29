# 策略工厂每日监控 - 一键运行


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

## 快速检查（推荐）

```bash
# Windows PowerShell
F:/Python311/python.exe scripts/factories/daily_check.py

# 或者简化版
python scripts/factories/daily_check.py
```

**显示内容**：核心指标、高 skill 策略分布、24小时活动、系统状态、建议

---

## 完整监控

```bash
# 完整系统监控
python scripts/factories/dashboard_full_monitor.py

# Quality Session 监控
python scripts/factories/monitor_quality_session.py

# 高 skill 策略详情
python scripts/factories/analyze_high_skill_strategies.py
```

---

## 诊断工具

```bash
# 快速诊断 formal=0 根因
python scripts/factories/diagnose_formal_simple.py

# 验证 Schema 迁移
python scripts/factories/verify_migration.py

# 诊断 submitted 队列
python scripts/factories/diagnose_submitted_queue.py
```

---

## 每日检查清单

### ✅ 早上（9:00）
```bash
python scripts/factories/daily_check.py
```
查看：
- formal 转正数量变化
- 新增策略和信号
- 系统健康状态

### ✅ 晚上（21:00）
```bash
python scripts/factories/monitor_quality_session.py
```
查看：
- Quality Session 运行状态
- 是否有异常日志
- P0-P2 修复验证

---

## 关键里程碑监控

### 🎯 里程碑 1: formal > 50 （预期 24-48h）
```bash
python scripts/factories/daily_check.py
```
看到：`formal_incubation: 50+`

### 🎯 里程碑 2: production > 5 （预期 3-5天）
```bash
python scripts/factories/daily_check.py
```
看到：`production: 5+`

### 🎯 里程碑 3: 准备真实交易 （预期 1-2周）
- production > 20
- 所有工厂运行稳定
- 执行契约完整

---

## 环境变量检查

如果 Quality Session 重启，记得设置：

```powershell
# Windows PowerShell
$env:STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED="1"
$env:STRATEGY_FACTORY_PREDICTION_CONTRACT_ENABLED="1"
$env:STRATEGY_FACTORY_CONFIDENCE_CONTRACT_ENABLED="1"
```

---

## 问题排查

### formal 转正很慢？
```bash
# 检查高 skill 策略是否在 observe
python scripts/factories/analyze_high_skill_strategies.py

# 检查是否有语义契约
# 查看数据库 strategies 表的 params 字段
```

### Quality Session 异常？
```bash
# 查看日志
python scripts/factories/monitor_quality_session.py

# 查看实时日志
tail -f logs/strategy_factory_quality_sessions/*/session.log
```

### 补偿逻辑触发？
```bash
# 查看监控
python scripts/factories/monitor_quality_session.py

# 应该显示: [OK] 未发现补偿逻辑触发
```

---

## 报告位置

- 里程碑报告: `docs/factory-architecture/MILESTONE_REPORT_20260622.md`
- 生产路线图: `docs/factory-architecture/PRODUCTION_ROADMAP.md`
- Schema 迁移记忆: `.claude/projects/.../memory/schema_migration_20260622.md`

---

**最后更新**: 2026-06-22
