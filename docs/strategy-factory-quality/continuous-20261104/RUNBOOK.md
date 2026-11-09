> **2026-11-09 当前状态**：已恢复launchd持久托管，旧停机缺口全部保留；最新四厂连续起点 **11-09 10:14:38 +08:00**，最早24小时时点 **11-10 10:14:38**。本页下方11-04/05内容为历史配置，当前PID/配置以session.json、deployment-events.jsonl及[11-09恢复审查](recovery-review-20261109.md)为准。没有Codex定时automation；当前active goal持续推进，SQLite observer每60秒只读记录。高质量验证仍0，目标active。

> **2026-11-05 恢复状态**：11-04 17:05:23 已停止，11-05 21:03:27 恢复。归档修复部署后连续覆盖从 **11-05 21:10:59 +08:00** 计起，最早24小时时点 **11-06 21:10:59**。原起点与15分钟heartbeat描述是历史配置；当前未发现持久化Codex automation。目标仍active。详见 [恢复与质量复核](recovery-review-20261105.md) 和 session.json。

# 四工厂持续运行与质量改进

目标保持 active：至少 24 小时持续运行，完整记录实际策略，审查并修复问题，直到连续多轮产出有可信证据的高质量策略。

## 运行起点与所有权

- 实际四子进程启动：**2026-11-04 14:37:42 +08:00**。最早 24 小时时点为 **2026-11-05 14:37:42 +08:00**；若中间停机，必须记录缺口并重新核对连续覆盖，不按墙钟直接结项。
- 主配置和 PID：[`session.json`](session.json)。以操作系统 PID、完整命令、PPID 与新心跳核实状态，不能只相信此文件。
- supervisor 当前由已授权工作区执行器启动为独立后台进程。它负责四个子进程退出后的 30 秒重启。主监督进程、SignalTracker、observer 的故障由本聊天每 15 分钟的 heartbeat 检查处理。
- launchd 启动 Python 时停在解释器 `os_listdir/open` 初始化，尚未进入工厂代码。已撤下该两项 launchd job，停掉挂起 PID。目录中的 `.plist` 是尝试记录，**不是仍在运行的服务配置**。
- 仅 `com.aiask.keep-awake.continuous-quality` launchd 服务保持运行，使用 `caffeinate -i -s` 保持本次持续任务所需的唤醒状态。目标完成或用户停止后应清理它。
- 实盘与券商写入关闭；工厂继续使用既有 research/paper 路径。

## 调度与记录

| 组件 | 行为 |
|---|---|
| 策略工厂 | 立即首轮，完成后等待 300 秒；不并发叠加完整轮次 |
| 因子工厂 | 完整配置的五引擎、30 候选/5 代预算，完成后等待 3600 秒；实际产出另行统计 |
| 孵化工厂 | 新增串行 interval 模式，立即首轮，完成后等待 1800 秒；不额外启动重复 catch-up |
| 事件摄取 | 完成后等待 1800 秒，失败使用 runner 既有退避 |
| SignalTracker | 独立 sidecar，初次 catch-up 完成后启 daemon，每轮完成后等待 1800 秒；心跳保留最近业务结果 |
| 质量观察器 | 每 60 秒只读扫描本会话新增/新完成轮次，刷新策略与前向/执行证据 |
| 本聊天 heartbeat | 每 15 分钟核对实际进程、产出和可行动问题；目标未验收时保持 active |

日志目录：`/Users/mac/.aiask/logs/four_runtime_supervisor`。observer 状态：`/Users/mac/.aiask/run/continuous-20261104-observer.state.json`。

- [`CURRENT.md`](CURRENT.md)：逐轮质量审查，自动刷新。
- [`candidates.json`](candidates.json)：本会话提交候选、实际策略参数和最新前向/执行证据。
- `runs/`：逐轮持久化产物归档。
- [`baseline.json`](baseline.json)：运行前基线，策略工厂 rowid=1485。
- [`first-cycle-evidence.json`](first-cycle-evidence.json)：四工厂首轮实际业务证据。
- 运行前 SQLite 一致性备份：`/Users/mac/.aiask/backups/four-factory-continuous-20261104.sqlite3`。

## 首轮质量结论

策略首轮 `factory_run_1793774266_c4443295`：生成并提交 13 个观察候选，严格孵化评估通过 **0/13**，10 份负 Sharpe；平均 Sharpe 约 −0.8119。主要阻塞为真实 OOS 交易收益缺失、PKF/Bootstrap 证据弱及 D 级验证。run 的 `success_observe_only` 和个别 Gate-3 `passed` 不代表高质量。

因子首轮 `mining_1793774266_4666b01b`：实际生成 4、快筛通过 0、入池 0。RL/MCTS 90 秒超时、GP 120 秒超时、LLM ReadTimeout，是需要诊断的真实性能/供给问题。

孵化首轮 `fe55233183d4`：completed、验证 1700、晋级 0；执行审计 hard_gate_passed=0，仍待证据。事件首轮保存 110 份文档，67 条规范事件，30 verified，50 条桥接事件、158 个信号，错误数 0。

## 当前改进事项

1. **完整候选记录**：上游 compact artifact 会省略 candidate_briefs，观察器已标记 recording_gaps。需要完整持久化生成候选、参数/DSL、身份和阶段决策，再验证新的实际轮次。
2. **真实 OOS 冻结链**：当前回放可验证调用方提供的契约，但生产尚缺自动冻结与成熟候选回放闭环。不能将已经参与选择的历史数据倒签成样本外。未来 20 交易日证据需要真实经过这些交易日。
3. **因子引擎超时**：先定位计算、数据重复加载或 provider 故障，再修复并复测；保留完整已启用引擎，不能用关掉困难引擎或放宽验证替代质量提升。
4. **持续性证明**：保留每次 PID 变化、退出/重启、部署时间及实际完成轮次。仅心跳不够，24 小时到点也不能自动完成目标。

## 每次继续工作

先读 session 与 CURRENT，再核对真实进程、supervisor 的 `execution_records.jsonl` 和四工厂最新业务记录。诊断数据库必须 SQLite `mode=ro` + `query_only`，不能用会 initialize/migrate 的应用服务做只读测量。pytest/复现遵守 CLAUDE.md 的 hermetic 隔离。

代码修复后运行对应行为回归，记录源码版本，选择相关工厂轮次完成后的边界受控更新。重启前先确认当前实例和子进程身份，不能根据过期 PID 文件重复拉起。所有历史产物保留，新的质量结论按实际证据生成。
