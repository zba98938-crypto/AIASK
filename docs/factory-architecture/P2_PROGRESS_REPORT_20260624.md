# P2 阶段进度报告 - 2026-06-24


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

**日期**: 2026-06-24  
**最后提交**: 627d567f

## 执行摘要

P2 阶段（Provider 架构实现）正在进行中。架构验证已完成（4/4 工厂通过测试），当前正在修复生产环境运行时错误。

## 当前状态

### ✅ 已完成

1. **Phase 1: 架构重构** (已完成)
   - ✅ PR-1: Runtime 命名去 MCP 化
   - ✅ PR-2: Canonical provider registry 与 bootstrap 回迁
   - ✅ PR-3: ExecutionUniverseContract 回迁

2. **Provider 接口定义** (Strategy Factory 侧)
   - ✅ `SignalTrackerProvider` Protocol - 18个方法
   - ✅ `FactorMiningProvider` Protocol - 13个方法
   - ✅ `SignalTrackerOrchestrator` - 完整 Phase A-I 编排逻辑
   - ✅ `FactorMiningOrchestrator` - 完整挖掘流程编排

3. **测试验证**
   - ✅ 4个工厂 MockProvider 测试全部通过
   - ✅ `scripts/test_factory_architecture.py` 验证通过

### 🔄 进行中

**Phase 2A: Provider 实现**

#### SignalTracker Provider (当前任务)
- ✅ `AKShareSignalTrackerProvider` 框架创建
- ✅ 基础方法实现（18/18 方法已定义）
- ✅ 导入修复 (commit 627d567f)
- ⚠️ **运行时错误待修复**:
  1. `ModuleNotFoundError: No module named 'akshare_mcp.services.execution_universe_adapter'`
  2. `AttributeError: 'str' object has no attribute 'get'` (Phase A 策略合并)
  3. `TypeError: SignalTracker._run_lifecycle_scan() takes 1 positional argument but 2 were given`

**测试结果**（run_signal_tracker.py --once）:
```
✓ 能够启动并执行完整 Phase A-I 流程
✓ 数据库初始化成功
✗ Phase A 加载策略失败
✗ Phase G 生命周期扫描签名不匹配
- 总耗时: 3.1s
- 错误数: 3
```

### ⏳ 待完成

**Phase 2A 剩余工作**:
- [ ] 修复 SignalTracker 3个运行时错误
- [ ] 实现 FactorMiningProvider 完整逻辑
- [ ] 实现 IncubationProvider 完整逻辑
- [ ] 实现 MarketEventIngestProvider 完整逻辑

**Phase 2B: 生产部署**:
- [ ] 更新所有 runner 脚本使用新 Provider 架构
- [ ] 更新 MCP 工具收口
- [ ] 添加集成测试

## 架构现状

### 当前架构层次

```
┌─────────────────────────────────────────────┐
│  Runtime (strategy-factory/runtime)         │
│  - SignalTrackerRuntime                     │
│  - build_signal_tracker_runtime()           │
└──────────┬──────────────────────────────────┘
           │ delegates to
           ▼
┌─────────────────────────────────────────────┐
│  Orchestrator (strategy-factory/application)│
│  - SignalTrackerOrchestrator                │
│  - Phase A-I 编排逻辑                        │
└──────────┬──────────────────────────────────┘
           │ calls (18 methods)
           ▼
┌─────────────────────────────────────────────┐
│  Provider (akshare-mcp/adapters)            │
│  - AKShareSignalTrackerProvider             │
│  - 实现 SignalTrackerProvider Protocol      │
└──────────┬──────────────────────────────────┘
           │ delegates to
           ▼
┌─────────────────────────────────────────────┐
│  Services (akshare-mcp/services)            │
│  - SignalTracker (legacy implementation)    │
│  - 具体业务逻辑                             │
└─────────────────────────────────────────────┘
```

### Provider 接口覆盖

**SignalTrackerProvider** (18个方法):
- ✅ 数据加载: `get_db()`, `get_default_universe()`, `load_executable_strategies()`, `load_runtime_submitted_strategies()`, `load_runtime_observation_strategies()`, `get_klines()`
- ✅ 信号生成: `generate_signals()`, `create_signal_event_snapshot()`, `backfill_forward_returns()`
- ✅ 孵化同步: `sync_incubation_orders()`, `sync_incubation_nav_snapshots()`, `sync_incubation_metrics()`, `run_incubation_pipeline()`
- ✅ 提交运行时: `run_submitted_runtime_pipeline()`
- ✅ 风险与治理: `run_runtime_risk_scan()`, `run_lifecycle_scan()`, `reconcile_vector_registry()`, `snapshot_domain_projections()`
- ✅ 配置: `phase_timeout_seconds()`

## 关键问题与解决方案

### 问题1: 模块导入路径错误
**错误**: `ModuleNotFoundError: No module named 'akshare_mcp.services.execution_universe_adapter'`

**原因**: `specs.py` 中使用相对导入 `from .execution_universe_adapter import ...`，但文件实际在 `signal_tracker_parts/` 目录下

**解决方案**: 修正导入路径为 `from .execution_universe_adapter import ...` 或使用绝对路径

### 问题2: 方法签名不匹配
**错误**: `TypeError: SignalTracker._run_lifecycle_scan() takes 1 positional argument but 2 were given`

**原因**: Provider 期望 `async def _run_lifecycle_scan(self, db, strategies)` 但实现只有 `def _run_lifecycle_scan(self)`

**解决方案**: 统一方法签名，确保 Provider 实现匹配接口定义

### 问题3: 返回值类型不匹配
**错误**: `AttributeError: 'str' object has no attribute 'get'`

**原因**: Provider 方法返回了错误类型（如返回错误消息字符串而非空列表）

**解决方案**: 确保所有 Provider 方法返回正确的数据结构

## 文件清单

### 新增文件
- `packages/akshare-mcp/src/akshare_mcp/adapters/signal_tracker/provider.py` - AKShareSignalTrackerProvider 实现
- `packages/akshare-mcp/src/akshare_mcp/adapters/signal_tracker/__init__.py` - Adapter 导出
- `packages/strategy-factory/src/strategy_factory/application/signal_tracker/provider.py` - Provider Protocol 定义
- `packages/strategy-factory/src/strategy_factory/application/signal_tracker/orchestrator.py` - Orchestrator 实现
- `packages/strategy-factory/src/strategy_factory/application/signal_tracker/contracts.py` - 契约定义

### 修改文件
- `packages/akshare-mcp/src/akshare_mcp/services/signal_tracker_parts/specs.py` - 添加缺失导入
- `packages/akshare-mcp/src/akshare_mcp/adapters/strategy_factory_runtime.py` - 注册 Provider
- `packages/strategy-factory/src/strategy_factory/runtime/signal_tracker.py` - Runtime 包装器
- `packages/strategy-factory/src/strategy_factory/api/runtime.py` - API 导出

## 下一步行动

### 优先级 P0 (本次会话)
1. ✅ 修复 `specs.py` 导入问题 (已完成)
2. **修复 execution_universe_adapter 导入路径**
3. **修复 _run_lifecycle_scan 方法签名**
4. **修复策略加载返回值类型**
5. 完整运行 SignalTracker 并验证 Phase A-I

### 优先级 P1 (后续)
1. 实现 FactorMiningProvider 完整逻辑
2. 实现 IncubationProvider 完整逻辑
3. 实现 MarketEventIngestProvider 完整逻辑
4. 更新生产 runner 脚本

### 优先级 P2 (优化)
1. 添加集成测试
2. 性能优化
3. 错误处理完善
4. 日志规范化

## 参考文档

- [14-Provider架构重构完成报告.md](./14-Provider架构重构完成报告.md) - 架构设计
- [03-四工厂运行规范.md](./03-四工厂运行规范.md) - 运行规范
- [02-策略工厂全链路生命周期规范.md](./02-策略工厂全链路生命周期规范.md) - 生命周期规范

## 提交历史

```
627d567f fix(signal-tracker): add missing imports to specs.py
f3d491a2 feat(strategy-factory): Phase 1 PR-1 - runtime naming de-MCP-ification
37b272dd docs(factory): add Provider architecture completion report
93283355 fix(factory): complete Provider architecture refactor
```

---

**状态**: 🔄 进行中  
**阻塞项**: SignalTracker 运行时错误（3个）  
**预计完成**: SignalTracker Provider 修复后，即可进入其他工厂 Provider 实现
