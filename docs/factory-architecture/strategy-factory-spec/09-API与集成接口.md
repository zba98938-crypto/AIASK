# API与集成接口


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

## 文档信息

| 项目 | 内容 |
|---|---|
| 文档版本 | 1.0.0 |
| 更新日期 | 2026-06-21 |

## 1. Agent HTTP接口

**位置**: `packages/agent/src/aiask_agent/routes/`

**V1状态**: 四工厂不进入V1前端产品入口 (Deferred)

## 2. 核心接口（内部使用）

### 2.1 策略工厂

```
POST /v1/strategy-factory/generate
GET /v1/strategy-factory/strategies/{id}
POST /v1/strategy-factory/batch-generate
```

### 2.2 因子工厂

```
POST /v1/factor-factory/generate
GET /v1/factor-factory/active-pool
GET /v1/factor-factory/factors/{id}
```

### 2.3 孵化工厂

```
POST /v1/incubation/intake
GET /v1/incubation/strategies/{id}/status
POST /v1/incubation/stale-close
```

### 2.4 SignalTracker

```
GET /v1/signal-tracker/status
GET /v1/strategies/{id}/signals
POST /v1/signal-tracker/run
```

## 3. 数据库直接访问（运维用）

### 3.1 SQLite连接

```python
from aiask_quant_core.config import get_settings
import sqlite3

settings = get_settings()
conn = sqlite3.connect(settings.sqlite_path)
cursor = conn.cursor()
```

### 3.2 CRUD层

**位置**: `packages/strategy-factory/src/strategy_factory/infrastructure/persistence/sqlite/_strategy_crud_core.py`

```python
from strategy_factory.infrastructure.persistence.sqlite import StrategyCRUD

crud = StrategyCRUD(db_path)
strategy = crud.get_strategy(strategy_id)
```

## 4. 诊断脚本接口

### 4.1 健康诊断

```bash
python scripts/factories/diagnose_factory_health.py --output health.json
```

**输出JSON结构**:
```json
{
    "overall_status": "pending_evidence",
    "checks": [
        {
            "name": "supervisor_processes",
            "status": "warning",
            "details": {...}
        }
    ],
    "summary": {
        "passed": 5,
        "warning": 3,
        "failed": 0,
        "blocked": 0
    }
}
```

## 5. 集成方式

### 5.1 Python集成

```python
from strategy_factory.application import StrategyFactory

factory = StrategyFactory()
result = factory.generate_candidates(market_context, factor_pool)
```

### 5.2 CLI集成

```bash
python scripts/factories/run_strategy_factory.py --count 10
```

## 相关文档

- [00-四工厂体系总览](00-四工厂体系总览.md)
- [01-策略工厂核心流程](01-策略工厂核心流程.md)
