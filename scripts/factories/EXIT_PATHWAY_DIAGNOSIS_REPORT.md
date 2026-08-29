# Exit 路径诊断报告

**日期**: 2026-06-21  
**问题**: 99% 持仓是 open，缺少 closed round-trip，阻止策略晋级  
**根因**: Exit 路径完全断链

---

## 数据库现状

| 指标 | 数量 | 占比 |
|------|------|------|
| **总策略数** | 23,008 | 100% |
| **listed** | 1 | 0.004% |
| **incubating** | 1,000 | 4.3% |
| **submitted** | 15,461 | 67.2% |
| **信号总数** | 19,312 | - |
| **entry signal** | 6,727 | 34.8% |
| **exit signal** | 12,585 | 65.2% |
| **paper orders** | 3,835 | - |
| **paper trades** | 3,764 | 98.1% 成交率 |
| **open positions** | 3,602 | 98.6% |
| **closed positions** | 51 | 1.4% |
| **hard_gate_passed** | 1 | 0.005% |

---

## 三大问题诊断

### 问题 1: stale close policy 未启用 ❌

**文件**: `packages/akshare-mcp/src/akshare_mcp/config/_strategy_factory_toggles.py:158`

```python
def stale_paper_position_closure_enabled() -> bool:
    return _env_bool("INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED", default=False)
    #                                                                            ^^^^^^^^
    #                                                                            默认禁用
```

**影响**: 老旧 open position 永远不会被强制平仓

**修复**: 启用开关

```powershell
$env:INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED = "1"
$env:INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT = "100"
$env:INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_GRACE_DAYS = "0"
```

---

### 问题 2: exit signal 生成 ✅

**现状**: SignalTracker 正常生成 exit signal

| 统计 | 数量 |
|------|------|
| exit signal (signal=-1) | 12,585 |
| entry signal (signal=1) | 6,727 |
| exit/entry 比例 | 1.87:1 |

**结论**: SignalTracker 工作正常，生成了大量 exit signal

---

### 问题 3: exit signal → exit order 严重断链 ❌

**核心数据**:

| 环节 | 数量 | 转化率 |
|------|------|--------|
| 有 exit signal 的策略 | 6,340 | - |
| 有 exit signal + open position | 1,471 | - |
| 生成了 exit order | 26 | **1.8%** ❌ |

**根因**: `paper_execution_backlog` 只处理 **"signal-only backlog"**（有 signal 但没有任何 order），不处理 **"exit backlog"**（有 entry order/position 需要 exit order）

**代码位置**: `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py:962`

```python
async def _run_signal_only_paper_execution_backlog(...):
    """Convert active signal-only incubation backlog into auditable paper execution."""
    #            ^^^^^^^^^^^
    #            只处理从未下过单的策略
```

**候选筛选逻辑** (line 955):

```python
if signals and not orders:  # 有信号但没有任何订单
    #          ^^^^^^^^^^
    #          这个条件排除了所有已经有 entry order 的策略
    candidates.append(strategy)
```

**问题**: 已经有 entry order/open position 的策略永远不会进入候选列表，即使有 exit signal 也不会生成 exit order

---

## 根本原因总结

### Entry 路径（完整） ✅

```
entry signal (6,727)
  ↓ 100% 转化 (paper_execution_backlog)
entry order (3,790)
  ↓ 98% 成交
entry trade (3,706)
  ↓ 97% 开仓
open position (3,602)
```

### Exit 路径（断链） ❌

```
exit signal (12,585)
  ↓ ❌ 无组件处理 exit signal → exit order 转换
exit order (45)  ← 只有 1.8% 转化率
  ↓ 129% 成交
exit trade (58)
  ↓
closed position (51)
```

**关键断点**: 没有组件负责 "有 open position 的策略 + exit signal → exit order" 转换

---

## 修复方案

### 方案 A: 启用 stale close policy（立即可行）✅

**优点**:
- 配置即可启用，无需代码修改
- 会强制平仓超过 `max_holding_days` 的持仓
- 立即增加 closed position 数量

**缺点**:
- 只处理"老旧"持仓，不处理新持仓
- 不基于策略信号，是时间触发
- 不是真正的 "signal-driven exit"

**实施步骤**:

1. 设置环境变量:
```powershell
$env:INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED = "1"
$env:INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT = "100"
$env:INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_GRACE_DAYS = "0"
```

2. 重启四工厂:
```powershell
uv run python scripts/factories/run_three_factories.py
```

3. 等待 1-2 个运行周期（每天 18:30）

4. 验证效果:
```powershell
uv run python scripts/factories/diagnose_factory_health.py
```

---

### 方案 B: 实现 exit signal → exit order 转换（需要开发）🔧

**目标**: 让 `paper_execution_backlog` 也处理 exit backlog

**需要修改的文件**:
- `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py`

**修改逻辑**:

#### 1. 新增 `_select_exit_signal_candidates` 方法

```python
async def _select_exit_signal_candidates(
    self,
    db: Any,
    *,
    strategies: Optional[list[dict[str, Any]]] = None,
    limit: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    """选择有 exit signal 且有 open position 但无 exit order 的策略"""
    
    # 查询条件:
    # 1. 有 exit signal (signal=-1)
    # 2. 有 open position
    # 3. 没有 exit order (direction in ('sell', 'exit', 'short'))
    
    candidates = []
    for strategy in strategies or []:
        sid = str(strategy.get("id") or "").strip()
        
        # 检查是否有 exit signal
        signals = await db.get_signals(sid, limit=10)
        has_exit_signal = any(s.get("signal") == -1 for s in signals or [])
        
        if not has_exit_signal:
            continue
        
        # 检查是否有 open position
        positions = await db.list_strategy_positions(sid)
        open_positions = [p for p in positions or [] if p.get("status") == "open"]
        
        if not open_positions:
            continue
        
        # 检查是否已有 exit order
        orders = await db.list_strategy_paper_orders(sid)
        has_exit_order = any(
            o.get("direction") in ("sell", "exit", "short") 
            for o in orders or []
        )
        
        if has_exit_order:
            continue
        
        strategy["_exit_signal_count"] = sum(1 for s in signals if s.get("signal") == -1)
        strategy["_open_position_count"] = len(open_positions)
        strategy["_open_positions"] = open_positions
        candidates.append(strategy)
    
    return candidates[:limit], len(candidates)
```

#### 2. 新增 `_run_exit_signal_paper_execution` 方法

```python
async def _run_exit_signal_paper_execution(
    self,
    db: Any,
    *,
    strategies: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Convert exit signals with open positions into exit orders"""
    
    if self.dry_run:
        return {"status": "skipped", "reason": "dry_run"}
    
    # 选择候选策略
    selected, total_count = await self._select_exit_signal_candidates(
        db, strategies=strategies, limit=200
    )
    
    if not selected:
        return {
            "status": "skipped",
            "reason": "no_exit_signal_backlog",
            "exit_signal_backlog_count": 0,
        }
    
    # 为每个策略的 open position 生成 exit order
    exit_orders_created = 0
    errors = []
    
    for strategy in selected:
        sid = str(strategy.get("id") or "").strip()
        open_positions = strategy.get("_open_positions") or []
        
        for position in open_positions:
            try:
                # 调用 incubation_service.force_close_open_positions
                # 或直接生成 exit order
                result = await incubation_service.force_close_open_positions(
                    db,
                    strategy,
                    as_of_date=date.today(),
                    reason="exit_signal_driven_close",
                    source="incubation_factory_exit_signal",
                    codes=[position.get("code")],
                )
                exit_orders_created += result.get("orders_created", 0)
            except Exception as exc:
                errors.append({
                    "strategy_id": sid,
                    "position_id": position.get("id"),
                    "error": str(exc),
                })
    
    return {
        "status": "ok" if not errors else "partial",
        "exit_signal_backlog_count": total_count,
        "selected_count": len(selected),
        "exit_orders_created": exit_orders_created,
        "errors": errors,
    }
```

#### 3. 在 `run_once` 中调用

在 Phase 3c (paper_execution_backlog) 之后添加 Phase 3c2:

```python
logger.info("IncubationFactory [%s] Phase 3c2: Exit signal paper execution", run_id)
exit_signal_execution_result = await _run_phase(
    "exit_signal_paper_execution",
    lambda: self._run_exit_signal_paper_execution(
        db,
        strategies=list(incubating) + list(paper_observation),
    ),
    timeout=BATCH_TIMEOUT_SEC,
) or {}
```

**优点**:
- 真正的 signal-driven exit
- 遵循策略逻辑，不是时间触发
- 完整闭环：entry signal → order → trade → position → exit signal → exit order → closed

**缺点**:
- 需要代码修改和测试
- 可能需要 1-2 天开发时间

---

## 建议执行顺序

### 第一步: 立即启用 stale close policy（今天）

```powershell
# 1. 创建启动脚本
bash scripts/factories/enable_exit_pathways.sh

# 2. 重启四工厂（使配置生效）
# 停止现有进程
bash stop_all_factories.sh

# 启动
$env:INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_ENABLED = "1"
$env:INCUBATION_FACTORY_STALE_PAPER_POSITION_CLOSURE_BATCH_LIMIT = "100"
uv run python scripts/factories/run_three_factories.py

# 3. 等待明天 18:30 运行后验证
uv run python scripts/factories/diagnose_factory_health.py
```

### 第二步: 开发 exit signal → exit order 功能（本周）

1. 实现上述方案 B 的代码修改
2. 添加单元测试
3. 在 quality session 中验证
4. 部署到生产环境

### 第三步: 持续监控（每周）

```powershell
# 每周运行诊断，确认 closed position 增长
uv run python scripts/factories/diagnose_factory_health.py --output weekly_report.json
```

**预期结果**:
- 1 周内：closed positions 从 51 增加到 500+（stale close）
- 2 周内：exit order 转化率从 1.8% 提升到 80%+（exit signal 功能）
- 1 月内：hard_gate_passed 从 1 增加到 100+
- 2 月内：listed 策略从 1 增加到 10+

---

## 附录：相关文件

| 文件 | 用途 |
|------|------|
| `scripts/factories/diagnose_factory_health.py` | 健康诊断脚本 |
| `scripts/factories/investigate_exit_signal_gap.py` | exit signal 断链调查 |
| `scripts/factories/enable_exit_pathways.sh` | 启用 exit 路径脚本 |
| `packages/akshare-mcp/src/akshare_mcp/config/_strategy_factory_toggles.py` | 工厂开关配置 |
| `packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py` | 孵化工厂主逻辑 |
| `docs/factory-architecture/06-运行与诊断手册.md` | 运行规范文档 |

---

## 结论

**根本问题**: Exit 路径完全断链，导致 99% 持仓无法平仓，策略无法晋级

**核心根因**: 
1. stale close policy 默认禁用
2. exit signal → exit order 转换逻辑缺失

**修复路径**: 
1. 立即启用 stale close（配置）
2. 开发 exit signal 转换功能（代码）

**预期效果**: 2 个月内 listed 策略从 1 个增加到 10+ 个
