# 四工厂恢复、归档修复与质量复核 — 2026-11-05

目标保持 **active**；24 小时连续运行与连续多轮高质量策略均未验证。

## 中断与真实进程

原 supervisor 在 2026-11-04 17:05:22 +08:00 收到 SIGTERM，17:05:23 四个子进程及监督器停止。发信号者与原因未知，不能归因于随后重启。系统启动时间为 11-04 19:22:26。11-05 21:00 的 ps 未发现工厂、SignalTracker、observer 或遗留子进程；旧 session/状态文件仍显示 running，是过期记录。

四工厂、SignalTracker、observer 于 11-05 21:03:27 恢复；监督器 PID 35760，因子 35764，孵化 35765，事件 35766，SignalTracker 35761，observer 35762。策略在归档修复后受控替换为 PID 36753，真实启动 21:10:59。每次身份均以完整命令和 PPID 核实，未重复启动任何组件。

四工厂新心跳均为 running。唯一仍加载的本次 launchd job 是 caffeinate PID 721；factory/tracker 的 launchd 配置仍未加载，运行所有权是已授权工作区启动的独立进程。未重新启用此前解释器初始化失败的 jobs。

停止到恢复缺口为 11-04 17:05:23 → 11-05 21:03:27，不能计入运行时长。部署替换的实际 factory_exit → factory_start 为 11-05 21:10:29 → 21:10:59，30 秒。最早新连续 24 小时时点为 **11-06 21:10:59 +08:00**；未来若中断仍须重新核对。

## 确认、修复、隔离回归及真实部署证据

确认 P1：完整外部归档中 `candidates[*]` 仍是运行预览。旧冻结尝试失败于 `holdout_compact_or_truncated_input`；选中候选的 backtest_metrics/backtest_outcome 内各有 6 处截断标记，包括成本假设和层级指标。完整原始结果已保存在同档 `backtest_candidate_results`，但冻结读取的是 candidates。归档接口的无损写入本身没有丢字节，上游传入了预览视图。

修复：BacktestFilter 在预览压缩前保留完整 metrics；CandidatePipeline 按全量 candidate_index 校验后，将完整 outcome/metrics 装入归档专用候选副本。提交对象和常规报告仍使用原预览，不改变准入、回测结果、阈值或历史档案。映射缺行直接失败。原始历史及先前冻结失败记录全部保留，未剥除截断标记来绕过校验。

隔离测试按 CLAUDE.md，使用 aiask Python 3.13，各包 conftest 首先安装 hermetic：

- 修复前新增真实 BacktestFilter → cycle → archive 行为断言得到 1 failed，确认缺陷。
- 修复后归档、OOS、观察器、质量修复和扰动等 115 passed（6.05s）；包括真实管线产物可封存、截断输入仍拒绝、缺结果映射失败。
- 相邻候选管线/回测/周期 30 passed（1.33s）。本修复共 145 passed。
- 启动前既有因子与运行边界回归另有 60 + 73 passed；30 条常量输入警告保留，不作为策略质量证明。

轮次 1514 于 21:09:55 完成后才请求重启，未中断活动轮次。新轮次 `factory_run_1793884262_e49fadbe`（rowid1515）21:11:39 完成；14/14 候选与原始结果和指标精确一致，0 个截断标记，内容 SHA256 核对通过。

## Prospective OOS

按预先写出的“归档顺序前三个具有六位代码目标的 momentum/ma_cross/buy_and_hold”规则选取新轮次 index 0/6/7，无收益筛选。真实数据库时间 11-05 21:12:08.708 +08:00 创建 3 个不可变 seal，计划区间 **11-06 → 12-03，20 个交易日**。代码、依赖、成本、目标、参数、协议、原档摘要均冻结。selection_data_end=11-05 是保守上界，未伪称已有逐源输入快照。

3 个均为 sealed_pending，total_return=null、measured_count=0、quality_admission_granted=false。到期后仍须验证实际交易日/输入完整性、执行源代码匹配并实现/验收成熟度评估；当前 `maturity_evaluator_implemented=false`，不能把封存成功等同于已经有样本外收益。

## 恢复后的真实产出与质量

恢复后三轮分别 17、13、14 份报告，共 44。全部 admission_stage=rejected，8 个旧式 passed 标记不计为准入；44 个策略均无 current_forward，执行硬门槛通过 0，高质量验收 0。成本后 Sharpe、统计、风险及实际缺失原因保存在每轮 runs/ 和本修复目录的 resumed-quality-reports.json，未把提交数或运行成功计为质量。

因子新轮 `mining_1793883810_3f2d46e0`：21 原始/21进化/18快筛/18严格尝试/0严格验证/0入池。16 个被 multiple_testing_risk_high 阻断、2 个 strict_gate_failed。GP 120秒和 RL 90秒预算结束时保留8/5个partial候选，MCTS完成返回3个、rule_seed5个；LLM重试3次得到HTTP429，返回0。既有预算修复已在本次受控恢复中执行，但快筛或partial产出不等于质量通过。

孵化首轮验证1700、晋级0、执行审计hard_gate_passed=0；新9条信号审计仍pending_evidence。SignalTracker完成2811信号、660快照、6条真实forward回填、errors=0；这6条属于存量，当前新44策略仍无forward。事件首轮保存110文档、65规范事件、30verified、203信号、errors=0。

## 已确认仍待处理

- 数据供给：全市场日线主体最新11-03（5574代码），11-04/11-05各仅4代码。daily_market_sync状态文件的PID19936实际不存在，重启后未恢复。只读单标的探测Sina600519返回11-05数据，源可达；应恢复真实数据同步并保留写前历史、来源、一致性和完整性证据。stock_quotes仍source_unavailable、截至10-18；不得放宽执行新鲜度检查。
- Prospective OOS maturity evaluator尚未实现；不能回填已参与选取的历史为OOS。
- LLM上游429与无严格合格因子仍需按真实后续轮次检查，不能停用困难引擎冒充完整运行。
- 本次没有持久化Codex automation可见，旧automation_id已撤销为null；继续由当前active goal及只读observer推进，不宣称不存在的15分钟调度生效。

## 文件与版本

- `session.json` 和 `deployment-events.jsonl` 保留新旧PID、时间、缺口、原因及状态。
- 启动前快照：`evidence/resume-20261105T210315/`，含运行记录、launchd、ps、源码包和manifest。
- 本修复证据：`evidence/archive-fix-20261105T211017/`，含相对于本轮启动版本的精确patch、源码快照、hash、测试、真实候选核验、报告、因子原始轮次、未来封存和供给诊断。
- HEAD仍为11c16f2cd859b894f165acb3530cecd66e629edd；工作区已有大量未提交修改，本次未reset、未commit，部署身份以实际源码SHA256和归档为准。
- 诊断均以SQLite mode=ro + query_only；仅授权工厂业务和显式未来封存写入。实盘/券商写入保持0，只读开关保持1。

## 21:18 续验

修复后第二个真实轮次 rowid1516、`factory_run_1793884600_d1f8786a` 于21:17:14完成。两轮共28个归档候选：28/28 outcome和metrics与原始记录一致、SHA256通过、0处截断。恢复后质量报告累计57，全部admission_stage=rejected；11个旧式passed不构成合格产出。高质量目标仍未达成。

数据恢复尚未执行写入。查阅现有同步器发现Sina路径将amount构造为close×volume，且默认upsert会覆盖重叠历史；目前没有把这一代理数当真实成交额写入。下一步需保留原始bar历史与逐源证据，隔离验证数据契约后受控恢复，避免用供给修复引入新的指标污染。仅源可达探测已完成，无新增诊断写库。


## 21:41 数据供应受控修复与历史冲突

- 依据实际 PID/PPID、launchd 和心跳复核：四厂持续运行，无本轮重启。24 小时下界仍为 2026-11-06 21:10:59+08:00；未验证完成。
- 复现 Sina/Tencent 的 amount=close×volume 代理值问题。修复为源未披露则 NULL+原因；Eastmoney f57 为真实披露成交额，保持原单位换算。不修改原有行。
- 新追加通道先持久化供应商响应、存量重叠和写入计划，再 INSERT 新日期；拒绝复权、价格/成交量、身份、非交易日等冲突。保留拒绝输入与逐标的结果。CLI 禁用 IC/因子池/指数/行业/北向/证据生成等无关变更。
- 46 项 hermetic 回归通过（包级 conftest；指定 aiask Python）；含真实隔离 SQLite、真实子进程失败/超时回收、完整 stdout/stderr、一次运行与守护运行共用锁、缺成交额与缺来源保持 partial。
- 五市场分层试运行返回 1：300750/688981/920000 共追加 6 根 Nov4/5 bar；5 标的全部 3884 个原有行逐字段一致，16 个试运行制品 SHA256 校验全通过。600519 与 000001 被拒绝。
- 只读复核发现两标的各 31 个交易日（Jun1–Jul14）存量 OHLC 与 Sina/Eastmoney 均不符；两供应商彼此一致，单位及不复权参数已核对。原因尚未归因，原始历史保留，不能将该段回测称为已核验。
- 全市场同步 sidecar 41478 / child 41479 于 21:40:37 启动，日志与实际结果落 /Users/mac/.aiask/artifacts/daily_market_sync/20261105T214037-6a3f2d44。当前仍在同步，未声称全量恢复。日后 16:10 调度。所有实盘/券商写入开关为关闭。
- 截至 21:41：恢复后 8 轮、110 报告，admission_stage 全 rejected；23 个历史 passed 标记不计质量；85 份成本后 Sharpe<0；真实 frozen OOS=0、当前 forward missing=110、execution hard pass=0。高质量验证=0，目标 active。
- 精确版本、diff、试运行、原始报告及冲突证据见 `evidence/data-recovery-20261105T211948/`。
