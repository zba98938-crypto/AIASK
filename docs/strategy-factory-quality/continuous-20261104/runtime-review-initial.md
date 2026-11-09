# 四工厂 24 小时运行入口只读复核

日期：2026-11-04。仅阅读代码、现有进程清单及工具路径；未启动任何工厂、未打开生产数据库、未修改业务源码、未输出任何 .env 凭据。

## 结论

应以 `scripts/factories/run_three_factories.py`（`run_all_factories.py` 的真实委托目标）为四工厂进程所有者，另行管理 SignalTracker 与质量观察器。当前 supervisor 具备四进程日志、60 秒 heartbeat、子进程退出重启、故障非零退出，但**没有 24 小时截止机制、全局单实例锁或孵化固定间隔执行**。保持进程常驻不等于每个工厂持续完成业务轮次。

现有 `run_strategy_factory_quality_session.py --hours 24` 是另一条业务会话路径，不能和完整 supervisor 并行叠加：它自己调用策略、因子、SignalTracker、孵化，会重复执行这些业务；它没有事件摄取，还硬编码缩减因子搜索到 rule_seed/4 candidates/1 generation/strict limit=1，不适合作为此次完整四工厂生产能力的替代验收。

## 可直接使用的现有 supervisor 参数

从仓库根目录运行，命令仅为建议，未执行：

```sh
MARKET_EVENT_INGEST_ENABLED=1 MARKET_EVENT_INGEST_INTERVAL_SEC=1800 \
/opt/miniconda3/envs/aiask/bin/python -u scripts/factories/run_three_factories.py \
  --python /opt/miniconda3/envs/aiask/bin/python \
  --log-dir /Users/mac/.aiask/logs/four_runtime_supervisor \
  --strategy-interval 300 \
  --factor-interval 3600 --factor-candidates 30 --factor-generations 5 \
  --heartbeat-interval 60 --restart-delay 30 --quiet
```

- 策略立即首轮，完成后等待 300 秒再跑；因子立即首轮，完成后等待 3600 秒。两者为串行轮次，不会因轮次耗时超过间隔而在同一 runner 内叠加。因子不指定 `--factor-engines` 保留完整已启用引擎配置。
- 事件默认 3600 秒，可用上述环境变量改 1800 秒；成功后按该间隔，失败按默认 300 秒重试。默认网络允许，显式 `MARKET_EVENT_INGEST_ENABLED=1` 防止项目环境将第四工厂关掉。supervisor 的非 LLM 环境键优先保留父进程显式值。
- 策略不指定 codes 时使用当前业务默认研究范围；若要全市场分片，需要显式 `--strategy-dispatch-run --strategy-dispatch-default-universe --strategy-dispatch-default-universe-limit N --strategy-dispatch-shard-size M`。并发不是此次验收前提，建议先单个完整周期，按资源和真实日志再调整。
- supervisor 子环境强制禁用实盘写入（LIVE_TRADING_ENABLED/ALLOW_WRITE/BROKER_ALLOW_WRITE=0），并强制 observe-first / wide-intake / grade C / Gate-3 record_only=0。这里的 observe 不是高质量认证；质量审查必须按实际 admission_stage、OOS、forward、execution gate 统计。
- supervisor 默认启一次孵化 catchup，随后孵化进程等待每日 18:30。若补了固定 interval，应避免 catchup 与立即首轮同时跑。

## 孵化和 SignalTracker 的持续执行缺口

`IncubationRuntime.run_daemon` 明确是每日一轮：启动 paper daemons → 等待每日 run_time → run_once → 失败退避 → 下一次仍等次日。失败后的 backoff 不是当日重试。当前 CLI 无 `--interval`，supervisor 也无 incubation interval 参数。

因此，如果“24小时持续不停跑四工厂”要求持续完成孵化轮次，需要补一个串行 interval 模式（例如每 900 或 1800 秒完成后再等一轮），并保留 daemon 期间一次 start/stop 对 paper engines 的所有权；不要用额外无限启动 one-shot 去绕开所有权。根 agent 决定实现，我未修改代码。

SignalTracker 不属于此 supervisor。独立 runner 当前只有：

```sh
/opt/miniconda3/envs/aiask/bin/python -u scripts/factories/run_signal_tracker.py --once
/opt/miniconda3/envs/aiask/bin/python -u scripts/factories/run_signal_tracker.py --daemon --run-time 18:00
```

它的 daemon 也是每日一轮，60 秒写状态，失败等待 300 秒仅重试一次。若要期间持续回填，也需要单一所有者的 interval 或编排串行调用。重复回填不会让尚未成熟的 5/20/40 日证据提前成熟，24 小时运行本身不能满足这些时间门槛。

`run_all_factories.py._build_specs` 虽然有 signal_tracker 分支，但 CLI 的 main 委托 `run_three_factories.py`，后者并无此参数，不能把那段测试兼容 helper 当成实际启用 sidecar。

## 防重复启动和持续性

- supervisor 与各直接 runner 无全局 pid/flock 锁。策略 scheduler 的 asyncio.Lock 只约束同一 scheduler 实例；task board 会清理死 PID 的前任任务，但看到别的活 PID 时只是继续，不能阻止第二进程另建任务。
- Agent 的 factory_run_locks 仅保护通过 dispatcher/worker 路径的运行；直接 supervisor 不自动获得这层锁。
- quality session 的 `session.lock.json` 仅约束相同 session_id 的目录，不能阻止不同 session_id 或 supervisor 并行运行。
- 本机 `/bin/launchctl`、`/usr/bin/caffeinate` 存在。单个固定 label 的服务管理与进程身份校验可提供单一所有者和 supervisor 故障后的重启；若使用脱离会话子进程，也应记录真实 PID、启动时刻、完整命令和唯一 session，不靠旧 pid 文件判活。
- 24 小时验收需记录起始时刻与连续覆盖窗口。现有 quality session 的 `--resume` 仍从原 started_at 计算 wall-clock deadline，期间停机时间不自动补齐；completed_at 只表示循环结束，不证明持续运行或高质量。

## 应查看的权威证据

1. OS 真实 PID/PPID/完整命令，确认 supervisor 和四个 child 身份一致，并持续存在；observer 的 pid 文件不能替代进程检查。
2. `supervisor_startup.json` 记录计划；`execution_records.jsonl` 记录 factory_start/exit/restart、supervisor_heartbeat/task_failed/stopped。计划不是启动证据；heartbeat 需新鲜（当前 observer 180 秒阈值）且 PID 活跃。连续覆盖应按这些事件重建，而不是只看最后一次 heartbeat。
3. 每工厂自己的实际轮次：strategy_factory_runs 的 run_id、started_at/completed_at、status、summary；factor_mining_runs 的新增周期及 quality_funnel；孵化日志/metrics/pipeline/report/domain events 的新时间戳与 phase_failures；事件摄取日志的轮次与实际持久条目/bridge 计数。进程 sleep 或 readiness skipped 都应记录但不算业务成功产出。
4. SignalTracker 的 `AIASK_SIGNAL_TRACKER_STATUS_PATH`（默认 `~/.aiask/run/signal_tracker.status.json`）：心跳中的 running 表示 daemon 活着；last_run_at、summary、errors 才表示最近业务结果。等待心跳会覆盖上次完成 payload，不能只靠该文件累计业务证据，需保留 stdout/轮次结果。
5. 本次生成策略应按运行起点和 run_id/candidate lineage 关联，保存完整策略与质量报告。不要把存量报告、通用 passed=1、observe 数量或某次成功提交当作持续高质量。

质量观察器原先固定 `four_runtime_supervisor` 日志目录；兄弟 agent 正在补 `--log-dir`/本次起点等参数。启动时务必把 observer 指向实际 supervisor 日志目录并用本次独立 state 文件，避免继承旧历史游标。

## 当前进程快照

只读检查时，未发现实际四工厂、SignalTracker 或 quality-session 进程（仅检查命令自身匹配）；这只是本次检查时点，不应据此覆盖 root 稍后实际启动的状态。
