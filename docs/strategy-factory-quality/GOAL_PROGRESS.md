# 四工厂质量目标进度

## 2026-11-01 22:12 +08:00 — 自动继续第1轮

目标：循环运行 Market Event Ingest、Factor Mining、Strategy Factory、Incubation，逐轮记录与质量审查、修复后复跑；首批至少30个独立高质量策略，并验证持续性。尚未达成，不以报告总数、advisory passed 或 observe intake 代替验收。

### 运行恢复与证据

- 本轮检查发现此前四工厂 supervisor 已不在进程列表，仅质量观察器与独立sidecars存活，旧 heartbeat 停在21:11。没有把旧 running 标签当作真实运行。
- 22:05:58 启动 managed background job `bash-cb288530be8844c8`，四子进程 pid 44079/44080/44081/44082。沿用 dispatch default universe limit=200、strategy interval=300 秒；保留全部 live/broker write 禁用开关。
- 为避免重复触碰当日晚间孵化账本，沿用 `--no-incubation-catchup`；孵化 daemon 按18:30计划等待，下轮需明确区分已启动和已完成新孵化轮次。
- 首个新轮次 `factory_run_1793541961_86396ced`：22:06:01开始、22:07:18完成，`success_observe_only`，15份submission报告，实际admission_stage全部rejected。不能报为高质量产出。
- 其中9份参数扰动study没有reason错误，6份为 `no_numeric_params`，后者是下一轮应定位的问题：不能以调低门禁替代真实扰动研究。
- 下一轮 `factory_run_1793542261_cf2a53b3` 已于22:11:01开始。

### 本轮修复

只修改观察与验收基础设施，未弱化策略门禁：

1. 观察器不再把历史advisory `passed` 字段计为孵化评估通过；要求精确run关联、stage=incubation/live、孵化evaluation明确通过且无硬拒绝。此数仍不代表预算获配或高质量独立策略数。
2. 精确run ID关联不受时间窗限制；legacy时间回退只允许无run ID报告并标未验证，避免借用其他轮次报告。
3. 覆盖 `success_observe_only`、`success_no_submission`、`success_no_strategy`、`partial_infra`、`partial_llm`、`skipped`等终态；此前观察器漏掉20:11之后观察轮次。
4. 空报告渲染显示“—”，不再对None作百分比格式化导致进程退出。
5. 健康检查要求完整四工厂、180秒内心跳与存活PID；历史restart次数保留审计但不直接等于当前故障。
6. 数据库连接强制mode=ro；非有限指标视为缺失；优先展示真实孵化评估阻塞原因。
7. review_version=2升级时从权威报告回算，不混入旧passed计数。

### 验证与下一步

- 修改前策略工厂隔离套件：1176 passed, 3 skipped（29.27秒）。
- 新增观察器行为测试：全包回归1183 passed, 3 skipped（29.87秒）；随后补充终态收集测试，观察器专项8 passed（0.14秒）。使用内存SQLite和mock心跳，未触碰真实运行库。
- 只读审查队友另证实 admission_authority 两个需要下一轮修复的问题：gate-a reject提前observe分支绕过结构pre_observe_hard_reject；rejected lane/status可被provisional flag覆盖公共decision。不是“弱质量不得observe”的泛化结论，需专门行为测试保护有意宽进语义。
- 下一轮优先：记录观察器v2重算结果、定位no_numeric_params的有效参数补齐链、修复结构裁决旁路，再审查新模型调用是否确实成功（不能只凭配置值推断）。

### 持续任务

- supervisor managed job `bash-cb288530be8844c8`应保持运行；每轮先检查它和进程，不重复启动。
- 测试job `bash-4837f9ff887545f4`已收集成功；新增专项也已通过。
- 已校验命令行后停止老观察器67888；最初新版job `bash-2f429058e59c4c2e`因历史逐run全表扫描性能差，在第2轮主动停止，改为下述优化版。

## 2026-11-01 22:27 +08:00 — 自动继续第2轮

### 修复、验证与上线

1. 观察器历史重算改为一次扫描建立 run→报告ID 内存索引，不再对每个run反复解析所有大JSON。新版job `bash-060b03750238483b`已成功完成重算：state.review_version=2、1319条历史轮次记录，22:24:15已正常刷新，无last_error。旧job已确认SIGTERM结束。
2. `BacktestFilter`参数扰动读取真实运行时 `get_parameters()` 的有效默认值，同时保存每个基线实际使用的 `_resolved_strategy_params`，研究复用该参数而非重新随机采样。根因包含：事件结构候选只有lookback别名，但扰动白名单只认breakout_window；波动突破候选传vol_window/breakout_mult（引擎不读），引擎实际使用lookback/threshold构造默认值；multi_factor候选传rebalance_days/top_n但缺lookback。修复没有假造别名映射或调低门禁。
3. 结构硬拒绝优先级修复：非refresh的pre_observe_hard_reject前移到所有新准入分支之前，实际rejected优先于provisional/revise标记。保留合法弱质量observe、read_only和refresh既有语义。27个新增参数化行为案例覆盖。
4. 本轮全包隔离回归：1215 passed, 3 skipped in 28.18s。初次新增扰动测试有1个失败（event的8变体上限优先测到adverse_volume和breakout_buffer，未轮到breakout_window）；核查变体选择后改为断言确实被扰动的实际buffer参数，非放松生产逻辑。全包通过包含该修正。
5. 在上一轮完成、无活跃run的边界，仅向经命令行校验的策略子进程44079发送SIGTERM；supervisor于22:25:05启动新pid46759加载代码。其他三个工厂未重启；全部交易写入安全开关保持禁用。

### 新模型与真实新轮次证据

- 对配置的 `/chat/completions` 发起最小请求：HTTP 200，9.51秒，requested_model与response_model均为gpt-6.1-sol、choices存在，未回显密钥。此只证明轻量调用可达，尚不能据此宣称工厂真实生成链未回退。第一次探针因环境未装python-dotenv失败，改用已有标准库读取本地配置后复测成功，未新增依赖。
- 修复上线后的 `factory_run_1793543107_bc33654e`：22:25:07开始、22:26:09完成、success_observe_only、14份报告。14/14均真正运行参数扰动（multi_factor各4变体，其他各8变体），reason全部None，原no_numeric_params在本轮不再出现。
- 真实admission_stage：12 rejected、2 research、0 incubation/live。2个research均是multi_factor，回测post-cost Sharpe为-1.515/-1.203，绝不可计作高质量。孵化阻塞为walk-forward IC IR 0.259/0.272低于0.300，未改阈值。
- 最新日报的86份历史“孵化评估通过”包含修复前因子池统计冒充逐策略证据时期的报告，不是目标首批86个独立高质量策略；不可拿历史汇总宣称达标。

### 下一轮优先项与活动job

- 当前仍没有确认符合目标的30个独立高质量策略，继续保持目标active。
- 下一轮审查真实LLM生产路径是否调用新模型而非local fallback，以及full_market_topn为何只在极少股票间循环、因子/孵化工厂定时等待如何在安全幂等边界实际跑新轮次。工厂进程running不代表四者都已完成新周期。
- 将高质量目标的独立候选清单与历史孵化评估计数分开，补充收益/执行证据和重复身份审查。
- 活动supervisor：`bash-cb288530be8844c8`；活动observer：`bash-060b03750238483b`。两者保留运行，下轮先查状态避免重复启动。
- 已收集的测试job `bash-d17af7a0232141a5`完成exit0；模型探针job `bash-1b3b214f8e774b4d`完成exit0；最初缺dotenv探针job已收集exit1并明确修复。

## 2026-11-01 22:42 +08:00 — 自动继续第3轮

### 实际执行另外两工厂与独立批次审计

- 因子挖掘单轮 `mining_1793543547_3d374f6c`：使用llm_primary/gp_classic、8候选预算、1代进化，22:32:25–22:33:50执行退出0；实际raw4/evolved4/validated1/admitted0/pool12。生产LLM请求出现HTTP429，与上一轮轻量HTTP200不可混为一谈。启动banner显示未配置只是只看FACTOR_LLM_MODEL的错误展示口径，真实配置按FACTOR_*回退STRATEGY_*，不能据此说模型没配置。
- 孵化新轮 `8a23023dc86f`：22:32:56–22:33:16，20.397秒、partial，verified700、promoted0。执行审计evaluated80、hard_gate_passed0、real_paper_round_trip0、trade_evidence_ready0；样本内存在intraday_missing，未拿缺失数据写成命中率0或交易收益0。摘要证据为 incubation-round3.json；原始full_json retained in tool output。
- 新增只读 `check_strategy_quality_batch.py`：按明确带偏移时间窗审计报告，严格孵化evaluation、成本后Sharpe≥0.35、收益>0、成交≥30、回撤≤25%、扰动稳定性≥0.5、tested_object_hash/run ID均不可缺；这是透明的保守研究初筛，不是盈利保证，更不是自动批准。按tested_object_hash去重，始终声明需独立复算和前向证据。
- 首份目标批次清单 batch-audit-round3.json：本目标启动后81份报告，77rejected/3research/1incubation，81份全部成本后Sharpe低于0.35、65份成交不足30、0份进入独立初筛清单。历史孵化标签不能替代高质量目标。

### 找到小股票池重复生产的根因与已测试修复

- 请求200股票被截成40的确切原因：domain/targets.py `_normalize_target_codes`硬上限40；scheduler虽传limit64仍只有40，runner默认一次把200全送进去。因此每次只跑ORDER BY stock_code最前40，不是全市场覆盖。
- runner已在dispatch前分片，默认/上限40，并将CLI shard配置传入Runner；新增行为测试用真实normalizer证明200个代码全部抵达scheduler（默认5片、指定20则10片）。24个定向测试通过。
- **尚未生产重载此分片修复**：5片完整周期可能超过300秒；必须先加有界背压，避免每次定时再排5片造成无界队列。生产仍跑旧40范围，不能宣称已扩大覆盖。下一轮优先bounded pending/上一代完成后再派发，持久化或清晰恢复游标，随后在空闲边界重载。
- 40进一步缩成6：36只缺行业等字段同得4.0667，largest_tie_size36/equal_score_ratio0.9、valuation coverage0、profile0.1；unknown被当一个行业桶限2只，固定挑000006/7，另4只已知行业→6只。本地候选再从这6只注入每策略≤3标的。缺数据+代码排序不是选优。下一轮从上游补充元数据/诚实缺测与restricted-scope披露修复，不直接放宽质量门禁。

### 真实生成链发现（只读证据，不猜测）

- 最近12轮strategy autonomy task_count/generated/network/selected均0，provider_control=normal；22:25目标spawn18来自local_rule14+factor activation4，最终14报告。即不是LLM请求失败fallback，而是任务在调用前被剪为0。
- Router artifact计划bulk46，task artifact却0。准确原因尚不能唯一还原：历史存储compact把bulk controls/task_budget裁掉，现存scheduler_state suppress/failure streak不能倒装成该历史run的确切输入。
- 下一轮需给任务选择增加可存储的planned/blocked/selected按source/reason计数和budget、显式skip_reason，再验证真实任务为何被剪。不要虚构某个budget开关已证实是根因。

### 测试与持续状态

- 独立batch screen专项8 passed；新增分片专项及邻近测试24 passed；最终全包1226 passed, 3 skipped in 27.49s（job bash-188824d7648e49e2已收集）。此前batch全包1223 passed也已收集。
- supervisor job bash-cb288530be8844c8与observer job bash-060b03750238483b继续运行；本轮单次factor与incubation jobs均已收集退出0。
- 目标仍active，没有达到首批30，更没有持续性证据。
