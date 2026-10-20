# 证据索引

> 测量时间：2026-10-20 08:42–08:57 +08:00  
> 原则：代码/配置/数据库/进程直接证据优先；无法直接证实的写入 `unknown`，不把推断写成事实。

## 1. 仓库规模与结构

```bash
# 每个包的 Python 文件数与总行数
for p in packages/*; do
  echo "$p $(find "$p/src" -name '*.py' | wc -l) $(find "$p/src" -name '*.py' -print0 | xargs -0 cat | wc -l)"
done
```

结果：

| 包 | py 文件 | LOC |
|---|---:|---:|
| `agent` | 155 | 48,131 |
| `aiask-quant-core` | 120 | 39,355 |
| `aiask-skills` | 3 | 89 |
| `akshare-mcp` | 507 | 200,372 |
| `data-mcp` | 2 | 172 |
| `finance-mcp-servers` | 12 | 1,892 |
| `reference-host` | 2 | 315 |
| `strategy-factory` | 183 | 95,035 |
| **合计** | **984** | **385,361** |

## 2. 运行进程与端口

```bash
ps aux | grep -E 'run_three_factories|run_strategy_factory|run_factor_mining_factory|run_incubation_factory|run_market_event_ingest|run_signal_tracker|aiask_agent' | grep -v grep
lsof -nP -iTCP:8765 -sTCP:LISTEN
ps -p <pid_on_8765> -o pid,ppid,lstart,command
curl -s --max-time 5 http://127.0.0.1:8765/health
```

关键结果：

- `run_three_factories.py`：PID 1194，2026-10-18 16:02 启动。
- `run_strategy_factory.py --dispatch-run`：PID 4755。
- `run_factor_mining_factory.py`：PID 1196。
- `run_incubation_factory.py --daemon --run-time 18:30`：PID 1197。
- `run_market_event_ingest.py`：PID 1198。
- 未发现 `run_signal_tracker.py` 进程。
- 未发现 `aiask_agent.server` 进程。
- 08:51–08:57 的 8765 监听者是 `python -m http.server 8765 --bind 127.0.0.1`（PID 90009），cwd 为 `/Users/mac/Desktop/FDE/deliverables/projects/project-f1ae81704476404d`，父进程是另一个 Qoder shell 会话；不是 AIASK。
- `GET /health` 返回 Python SimpleHTTPServer 的 404 页面，不是 Agent 健康响应。
- **08:59 复查：8765 已无监听**，该静态 server 是短生命周期进程；AIASK Agent 仍未启动。

Agent 元数据：

```bash
cat ~/.aiask/run/agent.meta.json
ls -ld ~/.aiask/venv
ls -ld "/Users/mac/Desktop/Hermes agent/integrations/aiask"
```

结果：meta 指向的 `~/.aiask/venv` 与 `/Users/mac/Desktop/Hermes agent/integrations/aiask` 都不存在；meta 里的 PID 42827 已退出，`agent.log` 以 `Application shutdown complete` 结束。

## 3. 运行时 SQLite 查询

数据库：`/Users/mac/.aiask/data/db/akshare_mcp.sqlite3`（4.0 GB + 230 MB WAL）。  
查询方式：`sqlite3 -readonly "file:...?mode=ro"`，只读，不写库。

### 3.1 市场与因子

```sql
SELECT COUNT(*) FROM stocks;                 -- 5,582
SELECT COUNT(*) FROM kline_1d;               -- 1,738,883
SELECT MAX(time) FROM kline_1d;              -- 2026-10-19T15:00:00+08:00
SELECT COUNT(*) FROM financials;             -- 119,837
SELECT MAX(report_date) FROM financials;     -- 2026-09-30
SELECT COUNT(*) FROM factor_values;          -- 386,706
SELECT COUNT(DISTINCT stock_code) FROM factor_values; -- 5,561
SELECT COUNT(DISTINCT factor_name) FROM factor_values; -- 44
SELECT MAX(factor_date) FROM factor_values;  -- 2026-10-16
SELECT COUNT(*) FROM factor_pool_active;     -- 29
SELECT COUNT(*) FROM data_quality_issues;    -- 0
```

### 3.2 策略与工厂运行

```sql
SELECT status, COUNT(*) FROM strategies GROUP BY status;
-- rejected 6,079 / submitted 1,644 / draft 493 / archived_draft 17

SELECT status, COUNT(*), MIN(started_at), MAX(started_at)
FROM strategy_factory_runs GROUP BY status;
-- success_observe_only 492 (2026-10-15..2026-10-20)
-- success 16, partial_infra 2, skipped 2, failed 1 (2026-07-16..2026-07-23)

SELECT run_id,status,started_at,completed_at,elapsed_seconds
FROM strategy_factory_runs ORDER BY started_at DESC LIMIT 5;
-- 最新：2026-10-20 08:41:18，77.1s，success_observe_only

SELECT
 json_extract(summary,'$.candidates_spawned'),
 json_extract(summary,'$.autonomy_generated'),
 json_extract(summary,'$.gate_3_input'),
 json_extract(summary,'$.gate_3_passed'),
 json_extract(summary,'$.gate_3_failed'),
 json_extract(summary,'$.submitted'),
 json_extract(summary,'$.created_audit_only')
FROM strategy_factory_runs ORDER BY started_at DESC LIMIT 1;
-- 29 | 21 | 15 | 0 | 15 | 3 | 12

SELECT status, COUNT(*) FROM strategy_factory_dispatches GROUP BY status; -- success 492
SELECT COUNT(*) FROM factory_run_ledger;                                   -- 0
```

控制面读取 `factory_run_ledger` 的代码证据：`packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_ai_repos/factory_control.py:167-193`。

### 3.3 孵化、paper 与证据

```sql
SELECT stage, status, COUNT(*) FROM strategy_incubation_accounts GROUP BY stage,status;
-- paper/active 1,642；warmup/active 17

SELECT COUNT(*) FROM paper_accounts WHERE status='active'; -- 1,661
SELECT COUNT(*) FROM paper_orders;                          -- 119
SELECT COUNT(*) FROM paper_trades;                          -- 155
SELECT status, COUNT(*) FROM strategy_trade_positions GROUP BY status;
-- open 118 / closed 1 / orphaned_exit 36

SELECT COUNT(*) FROM paper_nav;                             -- 1,397
SELECT COUNT(*) FROM strategy_incubation_metrics;           -- 1,383
SELECT COUNT(*) FROM strategy_signals;                      -- 7,288
SELECT MIN(signal_date), MAX(signal_date), COUNT(DISTINCT signal_date) FROM strategy_signals;
-- 2026-01-15 .. 2026-10-18, 122 个交易日
SELECT COUNT(*) FROM signal_forward_returns;                -- 27,690
SELECT COUNT(*) FROM strategy_execution_audit_snapshots;    -- 7,707
SELECT COUNT(*) FROM strategy_closure_snapshots;            -- 7,738
SELECT COUNT(*) FROM strategy_signal_evidence;              -- 2
SELECT COUNT(*) FROM strategy_promotion_reviews;            -- 0
SELECT COUNT(*) FROM strategy_quality_reports;              -- 7,718
SELECT COUNT(*) FROM backtest_results;                      -- 0
```

### 3.4 调度器状态

```sql
SELECT * FROM strategy_factory_scheduler_state;
```

关键字段：`cycle_count=512`、`consecutive_failures=0`、`circuit_state=closed`；多个 family 的 `gate_3_passed_count=0`，高频失败原因包括：

- `missing_parameter_perturbation_trade_stability`
- `insufficient_statistical_evidence`
- `missing_param_sensitivity`
- `supplemental_statistical_gate_failed`

## 4. MCP 能力枚举

```bash
/opt/miniconda3/envs/aiask/bin/python - <<'PY'
import asyncio
from akshare_mcp.server import mcp
async def main():
    print("tools", len(await mcp.list_tools()))
    print("resources", len(await mcp.list_resources()))
    print("templates", len(await mcp.list_resource_templates()))
    print("prompts", len(await mcp.list_prompts()))
asyncio.run(main())
PY
```

结果：`tools 179`、`resources 3`、`templates 14`、`prompts 7`。

> 导入时出现两个降级警告：`ray` 未安装（并行回测降级）；`ParallelBacktestEngine` 导入失败（`akshare_mcp.services.backtest` 没有该符号）。这两条属于能力降级，不是本次架构结论的核心，但会影响“并行回测可用性”的判断。

```bash
/opt/miniconda3/envs/aiask/bin/python - <<'PY'
import asyncio
from strategy_factory.mcp_server import mcp
async def main():
    tools = await mcp.list_tools()
    print(len(tools), [t.name for t in tools])
    print("resources", len(await mcp.list_resources()))
asyncio.run(main())
PY
```

结果：5 tools（`factory_list`、`factory_status_read`、`factory_runs_read`、`factory_run_read`、`factory_readiness_read`），2 resources。

## 5. 依赖与边界

```bash
cnt(){ grep -rn "$1" "$2" --include='*.py' 2>/dev/null | wc -l | tr -d ' '; }
cnt 'from strategy_factory\|import strategy_factory' packages/akshare-mcp/src       # 128
cnt 'from akshare_mcp\|import akshare_mcp' packages/strategy-factory/src             # 0
cnt 'from akshare_mcp\|import akshare_mcp\|from strategy_factory\|import strategy_factory\|from aiask_agent\|import aiask_agent' packages/aiask-quant-core/src  # 1
cnt 'from akshare_mcp\|import akshare_mcp' packages/agent/src                        # 15
cnt 'from strategy_factory\|import strategy_factory' packages/agent/src              # 19
cnt 'from aiask_quant_core\|import aiask_quant_core' packages/agent/src               # 3
cnt 'from aiask_quant_core\|import aiask_quant_core' packages/akshare-mcp/src         # 36
cnt 'from aiask_quant_core\|import aiask_quant_core' packages/strategy-factory/src    # 19
```

- `quant-core -> strategy-factory` 的唯一静态命中：`packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_strategy.py:46`（兼容 re-export）。
- `data-mcp -> akshare-mcp` 是 0 静态 import，但 `__init__.py` 用 `importlib.import_module` 动态转发；`NEUTRAL_SUBMODULES = ("storage","env_loader","server")`。
- `strategy-factory -> akshare-mcp` 的动态边：`runtime/default_bootstrap.py` 的 `DEFAULT_RUNTIME_PROVIDER_CONFIGURATOR`。

## 6. 代码证据索引

| 结论 | 代码/配置证据 |
|---|---|
| Agent 路由与 app 装配 | `packages/agent/src/aiask_agent/app_route_assembly.py:132-490` |
| Agent run 循环 | `packages/agent/src/aiask_agent/runtime.py:243-999` |
| `agent_*` 工具注册 | `packages/agent/src/aiask_agent/tool_registry.py:231-263` |
| 工具策略（finance_safe / general_full） | `packages/agent/src/aiask_agent/tools/policy.py:18-66` |
| 进程内金融工具适配 | `packages/agent/src/aiask_agent/adapters/akshare.py:37-61` |
| 外部 MCP 聚合 | `packages/agent/src/aiask_agent/mcp_client.py:242` |
| AKShare MCP 工具注册与 profile | `packages/akshare-mcp/src/akshare_mcp/server.py:94-115, 346-400` |
| AKShare MCP 启动/关闭 | `packages/akshare-mcp/src/akshare_mcp/server.py:606-660` |
| 工厂 11 阶段顺序 | `packages/strategy-factory/src/strategy_factory/application/cycle_pipeline.py:18-31` |
| 工厂单轮 runner | `packages/strategy-factory/src/strategy_factory/application/cycle_runner.py:124` |
| 工厂 runtime provider 契约 | `packages/strategy-factory/src/strategy_factory/runtime/default_bootstrap.py:24-68` |
| 工厂只读 MCP | `packages/strategy-factory/src/strategy_factory/mcp_server.py:38-80` |
| 工厂控制面读 ledger | `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_ai_repos/factory_control.py:167-193` |
| 四运行时 supervisor | `scripts/factories/run_three_factories.py:1-28` |
| runner 300s 默认间隔 | `scripts/factories/run_strategy_factory.py:34, 109-131` |
| SignalTracker A–I 阶段 | `packages/strategy-factory/src/strategy_factory/application/signal_tracker/orchestrator.py:194-377` |
| Incubation 阶段 | `packages/strategy-factory/src/strategy_factory/application/incubation/orchestrator.py:142-333` |
| Market Event Ingest 阶段 | `packages/strategy-factory/src/strategy_factory/application/market_event_ingest/orchestrator.py:69-181` |
| MatchingEngine | `packages/akshare-mcp/src/akshare_mcp/services/matching_engine.py:44, 120, 159, 270` |
| Incubation pipeline | `packages/akshare-mcp/src/akshare_mcp/services/incubation_pipeline.py:15, 398, 516` |
| Promotion pipeline | `packages/akshare-mcp/src/akshare_mcp/services/promotion_pipeline.py:11, 196` |
| 物理孵化阶段 | `packages/aiask-quant-core/src/aiask_quant_core/incubation_stages.py:1-49` |
| 回测引擎 | `packages/aiask-quant-core/src/aiask_quant_core/backtest/engine.py:195` |
| 验证器 | `packages/aiask-quant-core/src/aiask_quant_core/validation.py:84, 240, 368` |
| 风险模型 | `packages/aiask-quant-core/src/aiask_quant_core/risk_model.py:23` |
| quant-core 兼容 re-export | `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/schema_strategy.py:38-52` |

## 7. 置信度与未验证项

| 主张 | 置信度 | 说明 |
|---|---|---|
| 8 包 / 385k LOC / 984 py | high | 本次直接统计 |
| 四运行时 supervisor 正在运行 | high | `ps` + `supervisor_startup.json` + DB 时间序列 |
| 策略工厂每 300s 一轮 | high | runner 默认常量 + DB 每 5 分钟 run |
| 492 轮 observe-only、Gate3 0 通过 | high | `strategy_factory_runs.summary` |
| 0 晋级 / 0 listed | high | `strategy_promotion_reviews`、`strategies.status` |
| 1,642 paper 账户 / 155 成交 / 1 平仓 | high | SQLite |
| SignalTracker 未运行 | high | 无进程、无 status 文件 |
| 10-18 信号来源未知 | unknown | 没有 trace；需要单独验证 |
| 8765 曾被他项目占用（08:51–08:57），08:59 已释放 | high | `lsof` + `ps` + `curl`；08:59 复查 |
| Agent meta 过期 | high | 文件路径不存在 |
| `factory_run_ledger` 未接线 | high | 0 行 + 控制面代码 |
| `backtest_results` 0 行的原因 | unknown | 可能设计为只写 artifacts；未验证 |
| 监控是否在线 | unknown | 只有 YAML，没有进程/端口证据 |
| 测试套件当前是否全绿 | unknown | 本次未运行；README 只给命令，不给当前计数 |

## 8. 修复后验证（2026-10-20 09:20）

### 8.1 rejected 策略持仓清理

```text
preview: 14 candidate strategies / 60 unique codes
run:     orders_created=55, orders_filled=55, errors=0
post:    strategy_trade_positions = 57 open / 56 closed / 42 orphaned_exit
         open by strategy status = submitted 57 / rejected 0
         paper_positions quantity>0 = 25
later:   incubation catchup filled 134 pending entries
         declared holding-period parser fixed; 35 stale rows handled
         (3 real closes + 30 legacy orphans)
final:   156 open (all submitted) / 61 closed / 70 orphaned_exit
```

- 备份：`/tmp/aiask-paper-backup-20261020.sql`（paper_accounts / paper_positions / paper_orders / paper_trades / strategy_trade_positions 等）。
- 代码：`packages/akshare-mcp/src/akshare_mcp/services/incubation_factory/runner.py` 新增 `_run_rejected_strategy_position_cleanup`；`_strategy_factory_toggles.py` 新增 `INCUBATION_FACTORY_REJECTED_POSITION_CLEANUP_ENABLED`（默认开）与 batch limit。
- 测试：`packages/akshare-mcp/tests/test_rejected_strategy_position_cleanup.py`（4 passed）。

### 8.2 价格路径审计

- `packages/aiask-quant-core/src/aiask_quant_core/storage/sqlite/strategy_incubation_repos/writes.py`：未平仓持仓不再回退到 `last_trade_time`；只有 `status=closed` 时才允许该回退。
- 已刷新 57 个 open 持仓：35 个 `audited_open_position`，22 个 `missing_kline`（新开仓无后续 bar）。
- 测试：`packages/aiask-quant-core/tests/test_trade_position_price_path.py`（2 passed）。

### 8.3 SignalTracker

```text
status file: ~/.aiask/run/signal_tracker.status.json
once run:    signals_generated=474, snapshots=200, errors=0
runtime_universe: strategies=223, held_open_positions=22
supervisor:  run_sidecars.py PID 53997
daemon:      SignalTracker PID 54000, --run-time 18:30, status=running
factor:      FactorScheduler PID 53999, daily at 18:00
agent:       Agent HTTP PID 54001, 127.0.0.1:8765 /health ok
```

- `scripts/factories/run_signal_tracker.py` 现在写 running/completed/degraded/failed/stopped 状态。
- `packages/strategy-factory/src/strategy_factory/application/signal_tracker/orchestrator.py` 新增 `_load_open_position_strategies`，把有 open 持仓的策略并入执行宇宙。
- 测试：`packages/strategy-factory/tests/test_signal_tracker_open_positions.py`（2 passed）。

### 8.4 控制面与 Agent

```text
factory_status("strategy_factory") -> status=partial, source=strategy_factory_runs
latest run -> factory_run_1792458378_dc81c3cf (2026-10-20T09:06:18+08:00)
factory_runs -> 3 rows, sources=[factory_run_ledger, strategy_factory_runs]
Agent /health -> {"status":"ok","service":"aiask-agent"}
Agent /v1/tools -> JSON list
agent.meta.json -> aiask_root=/Users/mac/Desktop/张勇/AIASK, venv_python=/opt/miniconda3/envs/aiask/bin/python
```

- 测试：`packages/strategy-factory/tests/test_factory_control_strategy_runs.py`（4 passed）。

### 8.5 测试结果

```text
packages/aiask-quant-core:  179 passed
packages/strategy-factory:  1071 passed, 3 skipped
packages/akshare-mcp:       1144 passed, 6 skipped
```

### 8.6 Execution audit acceptance（真实库一次运行）

```text
candidate_count=170, selected/evaluated=80
status_counts: needs_attention=4, pending_data=76
gate_status_counts: insufficient_samples=1, failed_metrics=3, bootstrap_pending=76
hard_gate_passed_count=0, overall_ready_count=0
real_paper_round_trip_count=20, closed_round_trip_count=20
bootstrap_round_trip_count=0
```

- Phase 7 已接入 `IncubationOrchestrator`/`IncubationFactoryRunnerAdapter`，daemon 的 18:30 周期会刷新 verdict。
- **不是代码阻塞**：4 个指标失败、76 个样本不足；门禁拒绝是策略质量/样本债务，不应通过放宽阈值解决。

### 8.7 仍未解决

- `execution audit gate` 仍 BLOCKED：本次 80 个策略 0 个 hard-gate 通过；4 个指标失败、76 个样本不足。当前 9 个 `incubating`、0 个 `listed`。
- 156 个 open 持仓全部属于 submitted 策略，且都在各自声明的持有期内；均线交叉未转负，因此短期不会平仓。已声明持有期策略会在到期后由 Phase 3.5 清理。
- 其余三个 factory 没有持久化 DB run ledger，只有 supervisor `execution_records.jsonl` 的 start/exit 历史；`factory_runs` 已可回放该历史，但不是业务级 run 记录。
- Prometheus/OTel 仅配置；健康诊断已改为 `ps` 回退并覆盖 7 个进程，新增 Agent `/health` 探测，并且只统计买入信号的 entry backlog、正确读取 `effective_n_5d`。当前 9 项检查：7 通过、1 警告、1 阻塞（audit gate）。
- `akshare-mcp` 仍是 200k 行巨石；`quant-core → strategy-factory` 的 1 条兼容 import 仍与边界规则冲突，已有 `test_package_dag_boundary.py` 将其列为唯一例外。

## 9. 本次没有做的事

- 没有运行完整 pytest 套件；没有把“测试通过”作为结论。
- 没有调用真实 LLM 或交易接口。
- 没有回放 paper 平仓/退出规则；只读取了 DB 与代码。
- 没有修改任何运行中的进程或数据库。
- 没有停止 8765 上的其他项目进程；它不属于本仓库。
- 没有用 Structurizr CLI 机器解析 `aiask-system.structurizr.dsl`；本机未安装该 CLI。DSL 需用 Qoder 的 Structurizr DSL viewer 预览，若解析报错以 DSL 源为准修正。
