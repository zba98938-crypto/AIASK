# 策略工厂健康诊断工具

## 概述

`diagnose_factory_health.py` 是自动化诊断脚本，用于检查策略工厂四工厂 + SignalTracker 的健康状态。

该脚本实现了 [docs/factory-architecture/06-运行与诊断手册.md](../../docs/factory-architecture/06-运行与诊断手册.md) 中定义的诊断流程。

## 快速开始

```powershell
# 运行完整诊断
uv run python scripts/factories/diagnose_factory_health.py

# 详细模式（显示 SQL 查询和详细信息）
uv run python scripts/factories/diagnose_factory_health.py --verbose

# 保存诊断报告到 JSON 文件
uv run python scripts/factories/diagnose_factory_health.py --output report.json
```

## 诊断检查项

脚本自动执行以下 8 项检查：

| # | 检查项 | 检查内容 | 可能状态 |
|---|--------|----------|---------|
| 1 | supervisor_processes | 四工厂 supervisor 进程是否存活 | passed / warning |
| 2 | signal_tracker_recent_run | SignalTracker 最后运行时间（< 2 天） | passed / warning / failed / blocked |
| 3 | signal_to_order_conversion | 非零信号是否转成 paper order | passed / warning / failed |
| 4 | order_to_trade_conversion | paper order 是否转成 trade | passed / warning / blocked |
| 5 | position_status | 持仓状态分布（open vs closed） | passed / warning / failed |
| 6 | forward_returns | 前向收益证据是否存在 | passed / warning / blocked |
| 7 | execution_audit_gate | execution audit gate 状态 | passed / warning / blocked |
| 8 | strategy_lifecycle_state | 策略生命周期状态分布 | passed / warning |

## 输出示例

### 控制台输出

```
================================================================================
策略工厂健康诊断
================================================================================

[1] 检查四工厂 supervisor 进程...
[OK] 找到 4 个工厂相关进程

[2] 检查 SignalTracker sidecar...
[OK] SignalTracker 最近运行：0 天前

[3] 检查信号到订单的转换...
[WARN] signal-only backlog: 15/50 策略

[4] 检查订单到成交的转换...
[OK] paper orders 正常转成 trades: 234 笔成交

[5] 检查持仓状态...
[WARN] 持仓状态：120 open / 30 closed (80% open)

[6] 检查前向收益证据...
[OK] 前向收益证据：456 条 signal_forward_returns

[7] 检查 execution audit gate...
[WARN] hard_gate_passed=3/50（样本债：45 insufficient, 2 bootstrap_pending）

[8] 检查策略生命周期状态...
[OK] 策略状态分布：3 listed, 25 incubating, 共 50 个策略

================================================================================
诊断总结
================================================================================
总检查项: 8
[OK] 通过: 5
[WARN] 警告: 3
[FAIL] 失败: 0
[BLOCK] 阻塞: 0

整体状态: PENDING_EVIDENCE
```

### JSON 输出格式

使用 `--output report.json` 生成的报告结构：

```json
{
  "timestamp": "2026-06-21T14:00:00.000000",
  "checks": [
    {
      "name": "supervisor_processes",
      "status": "passed",
      "message": "找到 4 个工厂相关进程",
      "details": {
        "processes": [...]
      }
    },
    ...
  ],
  "summary": {
    "total": 8,
    "passed": 5,
    "warning": 3,
    "failed": 0,
    "blocked": 0
  },
  "overall_status": "pending_evidence"
}
```

## 状态说明

### 检查状态

- **passed** ✓ - 检查通过，该项健康
- **warning** ⚠ - 有警告，但不阻塞运行
- **failed** ✗ - 检查失败，需要修复
- **blocked** 🚫 - 完全阻塞，无法继续

### 整体状态

根据所有检查项计算出的整体状态：

- **healthy** - 所有检查通过
- **pending_evidence** - 有警告，但主要是等待样本成熟
- **degraded** - 有失败项，需要修复
- **blocked** - 有阻塞项，无法正常运行

## 常见问题

### 1. "psutil 未安装" 警告

**原因**：进程检查需要 `psutil` 库，但该库不在核心依赖中。

**影响**：只影响进程检查，其他数据库检查仍会执行。

**解决**（可选）：
```powershell
pip install psutil
```

### 2. "strategy_signals 表为空"

**原因**：SignalTracker 从未运行，或数据库路径不正确。

**解决**：
```powershell
# 单次运行 SignalTracker
uv run python scripts/factories/run_signal_tracker.py --once

# 或检查数据库路径
uv run python scripts/factories/diagnose_factory_health.py --db path/to/aiask_quant.db
```

### 3. "signal-only backlog" 高比例

**原因**：策略产生了信号，但未转成 paper order。

**可能原因**：
- execution universe 未覆盖该策略
- 缺少价格数据
- 账户配置缺失
- shares 规则不满足

**排查**：
```powershell
# 检查 skip reason
uv run python scripts/factories/diagnose_factory_health.py --verbose
```

### 4. "持仓 80% 为 open"

**原因**：大量持仓未退出，缺少 closed round-trip。

**解决**：
- 检查 stale close policy 是否启用
- 确认退出信号是否产生
- 运行 Incubation Phase 3d (stale paper position closure)

### 5. "hard_gate_passed=0"

**原因**：真实 paper closed round-trip 样本不足 20 个（production floor）。

**注意**：这不是错误！在真实样本不足时，这是**正确结果**。

**解决**：
- 继续运行 paper execution，积累真实 closed round-trip
- 不要试图用 bootstrap 代替真实样本
- 参考 [02-策略工厂全链路生命周期规范.md](../../docs/factory-architecture/02-策略工厂全链路生命周期规范.md) 中的 Hard Gate 语义

## 高级用法

### 自定义数据库路径

```powershell
uv run python scripts/factories/diagnose_factory_health.py --db D:\data\custom.db
```

### 集成到 CI/CD

```yaml
# GitHub Actions / GitLab CI 示例
- name: Run factory health diagnostics
  run: |
    uv run python scripts/factories/diagnose_factory_health.py --output report.json
    # 解析 report.json 的 overall_status
    # 如果为 blocked 或 degraded，则失败
```

### 定期健康检查

```powershell
# Windows 任务计划程序
# 每天 19:00 运行（建议在 Incubation Factory 运行后）
uv run python scripts/factories/diagnose_factory_health.py --output daily_report.json
```

## 参考文档

- [02-策略工厂全链路生命周期规范.md](../../docs/factory-architecture/02-策略工厂全链路生命周期规范.md) - 生命周期状态定义
- [03-四工厂运行规范.md](../../docs/factory-architecture/03-四工厂运行规范.md) - 四工厂职责
- [04-SignalTracker与证据闭环规范.md](../../docs/factory-architecture/04-SignalTracker与证据闭环规范.md) - SignalTracker 职责
- [06-运行与诊断手册.md](../../docs/factory-architecture/06-运行与诊断手册.md) - 完整诊断决策树

## 贡献

如果需要添加新的检查项：

1. 在 `FactoryHealthDiagnostics` 类中添加 `check_xxx()` 方法
2. 在 `run_diagnostics()` 中调用该方法
3. 更新本 README 的检查项表格
4. 更新 [06-运行与诊断手册.md](../../docs/factory-architecture/06-运行与诊断手册.md)

## 许可

本脚本是 AIASK 策略工厂的一部分，遵循项目整体许可。
