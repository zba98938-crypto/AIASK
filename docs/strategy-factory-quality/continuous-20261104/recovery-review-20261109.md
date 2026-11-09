# 2026-11-09 恢复、质量审查与受控修复

目标保持 **active**。实际连续24小时和连续多轮可信高质量产出均未满足，测试通过、运行成功和观察准入均不计为高质量。

## 中断、恢复与托管

11-06 21:06:30 supervisor 收到 SIGTERM，四子进程最迟21:06:32退出；信号发送者未知。系统另于21:55:36启动，不能将两项证据混为停机原因。11-09重新核对全机进程、父子关系、launchd与日志后，确认没有遗留工厂/sidecar；旧running状态文件已过期。

上一段四厂共同连续覆盖为11-05 21:51:53至11-06 21:06:30，83677秒（23小时14分37秒），不足24小时。11-06停机至11-09 09:11:50的缺口已入session，未计入覆盖。

11-09 09:11:50先恢复独立监督器及四子进程，同时恢复tracker/observer，daily sync于09:16:56恢复定时等待。09:54:17在已完成轮次边界受控停止，经ps确认旧父子全退，09:54:53由launchd接管，09:54:54四厂启动。本次交接缺口37秒。后续部署边界和最新24小时下界以session.json及deployment-events.jsonl为准。

当前launchd托管四厂supervisor、SignalTracker、只读observer、daily sync，另保留caffeinate。入口使用绝对脚本/解释器，cwd为运行目录。原始launchd失败没有足够证据确诊TCC；本次先以真实launchd只读探针验证初始化、源码读取，正常退出并卸载，随后才交接业务。全局环境与系统权限未扩大。

## 已审查真实产出

09:15数据库只读一致性快照，截断于策略rowid1763：自上次21:41复核后新增243轮、3506份提交报告，其中3504 rejected、2 research；956个历史advisory passed不计质量。2322份成本后Sharpe为负，460份压力风险不可测；实测冻结OOS=0、当前forward=0、执行硬门槛通过=0、可信高质量=0。

243轮最终候选归档的generated/evaluated均通过内容SHA256、轮次身份和数量核对。最早generated-only中间快照尚无evaluated属于预期时序；审查脚本最初将其误报KeyError，已纠正并保留说明，不能写成生产归档缺失。

新增22个因子轮次验证计数8、入池0；LLM失败包括ReadTimeout与503，RL保留预算耗尽partial候选。严格验证与入池分别计数，不将partial、快筛或validated直接当可用因子。

详细逐轮、逐候选、统计/风险、OOS/forward/执行证据见 `evidence/review-20261109-quality/`，后续真实轮次继续由只读observer写入 `runs/`、`candidates.json` 与 `CURRENT.md`。

## 修复与部署证据

- **历史seed伪指标**：12条旧rule_seed以固定IC=0.02、fitness=0.6和active/B/promoted入池；实际rowid1763候选仍消费这些值。daily sync现在不再制造指标/准入，消费端明确排除无实测验证证据的seed；旧数据库行保留。61项hermetic行为回归通过；09:54交接后真实工厂日志记录排除12条旧seed。
- **补数隐式选历史行**：存量同日重复时，字典覆盖会选择一条记录继续校验。新增隔离SQLite行为回归修复前2失败；修复后补数/通道/日调度48通过。现在拒绝重复日期，在验证前保留全部原始输入；对600519/000001/000858真实已归档输入只读复放均明确拒绝。没有清洗原始历史。
- **监督器重复实例**：真实隔离子进程复现原实现可同时启动两组子进程。修复使用同runtime的fcntl锁并让工厂子进程继承FD；父进程异常退出而子进程仍在时，新实例也被阻止。16项hermetic回归通过。真实launchd进程持锁已验证；去掉正常间隔超时的误报warning。
- **OOS成熟重放缺口**：新增独立只读检查/显式重放工具，从持久seal、数据库时钟、源码身份、全目标交易日/OHLC与完整输入快照进行核验。45项hermetic回归通过。原3个seal仍pending、收益null、当前源码身份不匹配，原窗口11-06至12-03；没有倒签样本外、没有自动发布指标或准入。需保留冻结源执行，或按新版本预先封存未来窗口。

## 数据与forward仍有实际阻断

11-05/11-06 daily sync均留下完整终局result，退出码1、failed/partial；分别追加11009/5588根bar，缺真实amount4684/2802根，拒绝63/50代码。缺amount保持NULL，未以close×volume补位。44个北交所volume冲突的整体比例约1、部分日期差异明显，不支持统一100倍单位错误归因；门禁保持原阈值。

SignalTracker的provider补取路径调用save_klines upsert，绕过本次审计追加流程。09:54交接时已显式关闭 `STRATEGY_FACTORY_FORWARD_RETURN_PROVIDER_REFRESH_ENABLED`，四厂和sidecar实际环境均核实为0；日线仍由审计追加同步提供。不能由updated_at推断实际价格变化或归因，相关历史逐字段核查另留证。

旧两轮Phase C超时前实际保存6条forward，summary却报0，已确认是未知结果被补零的报告缺陷；对应修复把C/G超时或失败写为null+稳定原因，23项hermetic回归通过。Tracker已于10:06:10单独替换为PID61594，未重启四厂；10:07:43真实完成结果中transitions=null、transitions_reason=phase_g_lifecycle_scan_timeout，C完成并真实返回0，原因null。此前关闭provider refresh后的第一轮C曾返回真实7条。这些属于存量策略，不能替代本会话新策略的forward证据。

另确认指数sh000001/sz399001从full_market_topn进入实际可交易target并参与等权成本回测，非benchmark用途。修复在来源阶段过滤、保留排除原因；对显式含指数的交易目标作结构拒绝，收益保持NULL，不悄悄改变旧组合重算权重。股票/ETF和benchmark/context保留。80项hermetic回归通过，旧报告保留。

进一步逐字段核查9349行更新时间变化：7132个股票旧行与既有基线完全相同，24个新增bar与追加计划相同；2108个指数旧行存在字段差异，其中601行amount由0变NULL、1988行change_pct变化，sh000300的54行OHLC有±0.001差异，另12行缺旧值基线。不能仅由这些差异独占归因具体进程和时间。真实warmup记录确认还存在指数upsert路径；`TDX_SYNC_DISABLE=sync_index_klines` 经真实调度链2项hermetic正反对照验证，保留其它预热。

## 10:14 最终本轮部署边界

策略rowid1772于10:11:15完成、因子rowid50于10:12:25完成后，10:14:26卸载旧supervisor服务。确认旧父子全部退出，10:14:38以指数结构修复和上述定向禁写配置重新加载，四厂supervisor62833、strategy62836、factor62837、incubation62838、event62839。本次缺口12秒，tracker/observer/daily sync继续运行。实际进程环境、继承关系与新heartbeat已复核。

最新四厂连续起点为 **2026-11-09 10:14:38 +08:00**，最早24小时下界为 **2026-11-10 10:14:38 +08:00**，到点仍需验证全过程覆盖、真实轮次和连续合格产出，不自动完成。完整版本、配置、测试、停启和初轮证据在 `evidence/index-target-deploy-20261109T101406/`。

首个部署后真实策略轮次1773于10:19:34结束：13个候选目标中指数0，13份报告全部rejected，9份成本后Sharpe为负，OOS/forward/执行硬通过均0，可信高质量仍0。候选完整归档SHA256通过。按09:54交接前已写明的非收益筛选规则，选归档顺序index2/3/4（两momentum、一ma_cross），先检查股票身份/已知冲突/同日重复，再于10:23:18真实数据库时钟封存未来20交易日窗口 **11-10至12-07**。新增3seal与当前源码身份只读复核一致，收益null、measured0、无准入；源包和当时选择输入快照已留存。旧3seal及旧窗口保留。

事件厂10:15:08首轮失败于`vtable constructor failed: market_doc_chunks_fts`，原进程62839按300秒退避于10:20:08开始第二轮。新mode=ro/query_only连接COUNT全文索引及内容表均成功3009，schema/shadow表/trigger齐全；没有持久损坏证据，未重建索引或把失败轮记为成功。下一步跟踪原进程重试终局，重复发生再据证修复。

## 安全边界与版本

诊断SQLite均使用URI mode=ro及PRAGMA query_only=ON；pytest按CLAUDE.md使用aiask解释器、包级hermetic隔离。运行环境实盘/券商写入0，只读开关1，因子quality profile为strict。无准入阈值放宽、无伪造收益、无历史清洗。

HEAD仍为11c16f2cd859b894f165acb3530cecd66e629edd；原有大量工作区修改未reset或commit。实际源码、SHA256、增量patch、测试、进程/launchd、停启时间及原始产出保存在本日各evidence目录。每个运行组件加载的部署版本以session.processes.source_manifest为准。

## 15:24 接续核验与后续修复

13:32:48在各厂真实终局边界停止旧树，13:33:04四厂新树启动，缺口16秒；此前10:14段未满24小时。实际部署五项修复（FTS幂等DDL、PF缺失、target风险、候选身份元数据、生命周期审计引用）及完整源码版本见 `evidence/quality-repair-deploy-20261109T130100/DEPLOYMENT-REVIEW.md`。15:05主控恢复后重新核实launchd、98111父与98117–98120四厂、98113tracker及新heartbeat，未重复启动。当前连续起点为13:33:04，最早24小时下界11-10 13:33:04，后续任何中断须重新计算。

15:21只读快照覆盖1773至1801完整轮，386报告全部rejected，312成本后Sharpe负，指数allocation为0，冻结OOS/forward/执行硬通过均0；1802尚在运行不计完整轮。因子50至54已完成的5轮validated合计1、admitted0。证据 `evidence/review-20261109-postfix/snapshot-20261109T152052/`。

实际1793暴露29字段执行合同被展示压缩丢5字段，12个目标风险因此诚实缺失。修复完整传递已完成74项隔离回归及独立2项检查，13原始归档合同逐值保留，尚未部署；旧报告不改写。另已证明gp_factor_6在14轮表示14种公式，却共用值/IC键，旧行混入不同公式；前瞻稳定身份和真实pool拒绝原因保存正在修复，历史拒绝原因仍未知。

生命周期仍实际45秒超时，compact引用修复未证实消除问题。公平扫描新实现以稳定ID、持久reservation/attempt推进，保留失败、取消与部分写入未知，42项隔离回归及独立审阅通过，尚未部署。轮转完成与策略质量通过严格分开。

多因子新增实证：全序列标准化让7个真实标的的历史前缀信号随未来数据改变；未知GP因子又被静默跳过产出success/0trades。修复因果标准化与显式不支持拒绝正在进行，保留旧回测，不把修复前收益视作无前视证据。

11-09三新OOS seal的320个冻结源已从10:14归档逐SHA提取并独立验证，三个execution_code_matches均true、pending/unmeasured。当前代码漂移不覆盖该版本；见 `evidence/pinned-oos-20261109/README.md`。
