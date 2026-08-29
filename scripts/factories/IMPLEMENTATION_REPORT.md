# 自动化诊断脚本实现报告

**日期**: 2026-06-21
**任务**: 实现策略工厂自动化健康诊断工具
**状态**: ✅ 已完成

---

## 实现内容

### 1. 核心脚本

**文件**: `scripts/factories/diagnose_factory_health.py`

**功能**:
- 自动执行 8 项健康检查（进程、信号、订单、成交、持仓、前向收益、审计、生命周期）
- 支持详细模式 (`--verbose`) 显示 SQL 查询和详细信息
- 支持 JSON 输出 (`--output`) 便于集成和自动化
- 兼容 Windows 控制台（自动处理 Unicode 编码问题）
- 优雅降级（psutil 未安装时跳过进程检查，仅检查数据库证据）

**输出格式**:
```
[OK] 通过 / [WARN] 警告 / [FAIL] 失败 / [BLOCK] 阻塞
整体状态: HEALTHY / PENDING_EVIDENCE / DEGRADED / BLOCKED
```

### 2. 配套文档

**文件**: `scripts/factories/README_DIAGNOSTICS.md`

**内容**:
- 快速开始指南
- 8 项检查详细说明
- 输出示例（控制台 + JSON）
- 常见问题排查（6 个常见场景）
- 高级用法（自定义数据库、CI/CD 集成、定期检查）
- 参考文档链接

### 3. 规范文档更新

**文件**: `docs/factory-architecture/06-运行与诊断手册.md`

**更新内容**:
- 添加"自动化诊断工具（推荐）"章节
- 将手工 SQL 改为备选方案
- 推荐优先使用自动化脚本

---

## 测试验证

### ✅ 基础功能测试

```powershell
# 测试 1: 标准运行
uv run python scripts/factories/diagnose_factory_health.py
# 结果: 8 项检查全部执行，正确识别 blocked 状态

# 测试 2: 详细模式
uv run python scripts/factories/diagnose_factory_health.py --verbose
# 结果: 显示 SQL 查询和详细错误信息

# 测试 3: JSON 输出
uv run python scripts/factories/diagnose_factory_health.py --output test_report.json
# 结果: 生成格式正确的 JSON 报告
```

### ✅ 兼容性测试

- **Windows 控制台**: ✅ 自动处理 GBK 编码问题
- **psutil 缺失**: ✅ 优雅降级，仅跳过进程检查
- **数据库缺失**: ✅ 正确标记为 blocked
- **表缺失**: ✅ 逐项检查，精确定位缺失的证据表

### ✅ 实际运行结果

当前环境（空数据库）诊断结果：
```
总检查项: 8
[OK] 通过: 0
[WARN] 警告: 5
[FAIL] 失败: 0
[BLOCK] 阻塞: 3

整体状态: BLOCKED
```

**符合预期**：空数据库应该被标记为 BLOCKED，而不是误报为健康。

---

## 实现的诊断检查

| # | 检查项 | SQL 来源 | 规范文档 |
|---|--------|---------|---------|
| 1 | supervisor_processes | psutil API | 03-四工厂运行规范.md |
| 2 | signal_tracker_recent_run | `strategy_signals` 最新记录 | 04-SignalTracker与证据闭环规范.md |
| 3 | signal_to_order_conversion | `strategy_signals` vs `paper_orders` | 02-策略工厂全链路生命周期规范.md |
| 4 | order_to_trade_conversion | `paper_orders` vs `paper_trades` | 02-策略工厂全链路生命周期规范.md |
| 5 | position_status | `strategy_trade_positions.status` | 02-策略工厂全链路生命周期规范.md |
| 6 | forward_returns | `signal_forward_returns` + `strategy_incubation_metrics` | 02-策略工厂全链路生命周期规范.md |
| 7 | execution_audit_gate | `strategy_execution_audit_snapshots.verdict_status` | 02-策略工厂全链路生命周期规范.md |
| 8 | strategy_lifecycle_state | `strategies.status` | 02-策略工厂全链路生命周期规范.md |

---

## 解决的问题

### 问题 1: 诊断流程仅为手工 SQL

**之前**: 规范文档中只有 SQL 查询和 Mermaid 决策树，需要手工复制粘贴执行。

**现在**: 一键执行所有诊断，自动解释结果，自动判断整体状态。

### 问题 2: SignalTracker 健康无法快速确认

**之前**: 需要手工查询 `strategy_signals` 表，计算最后运行时间。

**现在**: 自动检查最后运行时间，超过 1 天警告，超过 2 天标记为失败。

### 问题 3: 证据链断裂难以定位

**之前**: 不知道是信号缺失、订单缺失、成交缺失还是审计缺失。

**现在**: 逐项检查，精确定位哪个环节断链。

### 问题 4: hard_gate 未通过原因不清

**之前**: 只看到 `hard_gate_passed=0`，不知道是链路断裂还是样本债。

**现在**: 区分 `missing`（链路断裂）、`insufficient_samples`（样本债）、`passed`（通过）。

---

## 与规范文档的一致性

### ✅ 完全符合规范

1. **生命周期状态** - 使用规范定义的物理状态和业务覆盖层
2. **证据表名称** - 准确使用 `signal_forward_returns`、`strategy_execution_audit_snapshots` 等
3. **Hard Gate 语义** - 正确区分 6 种状态（missing/bootstrap_pending/insufficient_samples/failed_metrics/bootstrap_ready/passed）
4. **SignalTracker 定位** - 定位为 sidecar，而非四工厂成员
5. **诊断顺序** - 遵循 06 文档的诊断决策树

### ✅ 实现了规范建议

引用自 [06-运行与诊断手册.md](docs/factory-architecture/06-运行与诊断手册.md#L129-136):

> 未来代码应把该检查放入 supervisor preflight 或只读 health command；
> 在实现前，`run_three_factories.py` 进程存活不能等同于证据闭环健康。

**实现**: 本脚本提供了"只读 health command"，可被集成到 supervisor preflight。

---

## 使用场景

### 1. 运维日常检查

```powershell
# 每天运行一次，快速确认健康状态
uv run python scripts/factories/diagnose_factory_health.py
```

### 2. 问题排查

```powershell
# 遇到异常时，使用详细模式定位问题
uv run python scripts/factories/diagnose_factory_health.py --verbose
```

### 3. 自动化监控

```powershell
# 定期运行并保存报告，用于趋势分析
uv run python scripts/factories/diagnose_factory_health.py --output daily_$(date +%Y%m%d).json
```

### 4. CI/CD 集成

```yaml
# 在部署前检查工厂健康
- run: uv run python scripts/factories/diagnose_factory_health.py --output ci_report.json
- run: |
    status=$(jq -r .overall_status ci_report.json)
    if [ "$status" = "blocked" ]; then exit 1; fi
```

---

## 后续改进建议

### 短期（可选）

1. **添加 threshold 配置** - 允许用户自定义警告/失败阈值
2. **HTML 报告** - 生成可视化 HTML 报告
3. **历史对比** - 对比前一次诊断，显示趋势

### 中期（推荐）

1. **集成到 supervisor** - 在 `run_three_factories.py` 的 preflight 中调用
2. **实时监控模式** - 持续运行并推送告警
3. **修复建议** - 根据诊断结果自动生成修复命令

### 长期（规划）

1. **Web Dashboard** - 提供 Web 界面显示健康状态
2. **告警集成** - 集成到企业微信/钉钉/Slack
3. **自动修复** - 对简单问题自动执行修复（需要审慎设计）

---

## 总结

✅ **任务完成度**: 100%

✅ **规范一致性**: 完全符合最新更新的规范文档

✅ **可用性**: 即刻可用，无需额外依赖（psutil 可选）

✅ **可维护性**: 代码结构清晰，易于扩展新的检查项

✅ **文档完整性**: 包含使用指南、故障排查、参考文档

**下一步**: 根据实际使用反馈，逐步添加更多检查项和功能。
