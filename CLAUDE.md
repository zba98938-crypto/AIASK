# AIASK 开发规则

## Python 环境

统一使用 conda 环境 `aiask`（Python 3.13），**不使用项目内 `.venv`，不使用 `uv run`**。

conda 未加入 PATH，用全路径调用：

```bash
/opt/miniconda3/envs/aiask/bin/python -m pytest -q
```

各包的测试从该包目录执行（`pytest.ini` 的 `pythonpath` 是相对路径）：

```bash
cd packages/akshare-mcp && /opt/miniconda3/envs/aiask/bin/python -m pytest -q
```

依赖用 pip 直装到该环境（8 个本地包全部 editable 安装：aiask-quant-core、strategy-factory、akshare-mcp、data-mcp、finance-mcp-servers、aiask-skills、reference-host、agent）。新增依赖后同步更新对应 `pyproject.toml`。

`reference-host` 曾长期漏装，它唯一的测试正是验证「能否独立导入」，未装时该测试以 `ModuleNotFoundError` 失败。装包后再改测试断言真实隔离属性（导入图不拉入 agent/provider），而不是断言解释器里没装。

背景：此前 `.venv` 未安装 pytest，`uv run pytest` 会静默回落到 `/opt/miniconda3/bin/pytest`（base 环境），导致「全量测试通过」实际是在未锁定的解释器上验证的，`uv.lock` 形同虚设。统一到一个显式环境后不再有这种隐式回落。

## 交付形态

本项目最终形态是 **MCP 服务 + skills 能力**，不是面向固定用户行为的应用。无前端：`desktop/` 已随本树抽取移除，客户端直接用 Agent HTTP / MCP 接口。

## 用户体系裁决

**单机单用户，是负债，不新增。**

实测事实（2026-09-07）：`packages/agent/src` 有 547 处 `user_id` 引用，但运行库里 `app_users` 2 行、`users` 1 行、`user_roles` 2 行、`strategy_subscriptions` **0 行**；`require_user_scope` 的默认身份是字面量 `"local"`，跨身份访问退化为「需要 control token」。所以这 547 处引用支撑的不是多租户，而是一个**已退化到单用户的载体**。

由此产生的规则：

- **不要**把 `user_id` / `X-AIASK-User-Id` 缺失当漏洞报。没有多租户，就没有跨租户越权。
- **不要**为它新增 principal / scope / RBAC / 订阅层。方向是收敛，不是加固。
- **不要**在触碰相邻代码时顺手删它。547 处引用横跨 session、memory、audit、learning_loop，整体拆除是独立任务，不是重构的副产品。
- 新代码默认走单用户路径，不引入新的 `user_id` 参数。
- `STRATEGY_METRIC_NOISE_ENABLED`（默认开）会给未订阅身份的 Sharpe/Calmar 加 ±0.1% 噪声并标 `approximate: True`。既然订阅表为空，它对所有读取都生效 —— 任何跨源核对指标的代码要么关掉它，要么读 `approximate` 标记。

## 诚实优先于「永不报错」

降级路径必须让「测不出来」区别于「测出来是 0」。返回一个看似合理的数字并附一个披露标记是不够的：消费方读的是 `loss_percent`、`intrinsic_value`、`fitness`，不是 `assessment_basis`。

- 无法测量 → 返回 `None` + 稳定的 `reason` 原因码。上游门禁已经区分「缺失」与「弱」（`missing_*` vs `weak_*`），缺失在 incubation/live 级别会硬失败，这是正确结果。
- 不要用默认值补位关键敏感度。`stress_test` 曾对 `beta` 缺失默认按 1.0 处理，结果 179/179 份质量报告的 `stress_loss_percent` 全是 −20.0，而门禁阈值是 −25.0 —— 这条检查从未、也不可能触发。
- 评分不得符号盲。因子评级曾用 `abs()` 包裹 IC，且 `ci_lower > 0 or ci_upper < 0` 把「显著为负」也算满分，导致 OOS IC = −0.05 的因子拿 70 分评为 A 级。方向基准取样本内 IC 符号：合法的反向因子照常计分，样本外符号翻转的按 0 分。
- 不要 `except Exception: continue` 静默丢样本。记下 `skipped_codes` / `failed_codes`，否则 3 个存活标的也能产出一份完整的验证报告。
- **「省略」与「写 0」是两回事。** 因子包曾对每个缺失输入写 `0.0`：不足 60 根 K 线时把真实的 m20 和凭空造的 m60=0 平均，信号被砍半；不足 20 根时 `momentum` 恰为 0.0，与「完全不涨不跌」无法区分；缺财务数据时 `quality`/`growth` 为 0.0，而 `debt_ratio` 默认 1.0 让「没有资产负债表」和「零负债」同分。正确做法是**不写这一行**（读方 `.get()` 得到 `None`），而不是写一个数。
- **省略必须被计数。** 只把 0.0 换成缺行，等于把静默挪了个位置。`handle_batch_compute_factors` 因此返回 `unmeasurable_counts`，说明每个因子被省略了多少次、原因是 `insufficient_history` 还是 `no_financial_snapshot`。
- 合成的 0 会**稀释**真实信号，而不只是污染个别标的：修掉 `value` 的虚构 0 后，同一批数据的 rankIC 从 +0.125 变成 +0.179。

## 数据源

台账在 `docs/DATA_SOURCES.md`。规则：

- **先探测可达性，不要假设。** 这台机器的代理挡住了 `push2.eastmoney.com`（最常用的那个东财接口），但 `datacenter-web.eastmoney.com` 通、`push2his.eastmoney.com` 通。同一家供应商换个域名就可用，这决定了整类数据能不能拿到。
- **「声明了」不等于「装上了」。** `tushare` 是核心依赖但没有 token；`akshare` 未声明却被 26 个文件 `import`，全部走了 `except ImportError` 分支，且因为降级成空列表而**没人发现**。新增 provider 前先确认它在当前环境里真的能跑；不能跑的路径要么装、要么删，不要留。
- **Provider 必须能区分「没数据」与「没 provider」。** 返回结构里带上 `complete` / `failed_pages` / `truncated`；一行都读不到要**抛异常**，不要返回空列表——「列举失败」和「市场里没有股票」长得一样正是造出 `full_market` 这个假口径的原因。
- **降级要署名。** `resolve_stock_universe()`、`--with-industry` 之类必须返回数据来源，并在 stderr 说明是降级；上游不完整时标 `complete: False` 并列出失败页。
- **一列只能有一套口径。** 行业分类曾混用 Sina 的 56 类与东财的 27 类：同一家公司会落进两个组，无法比较。现在 `stocks.industry` 是东财 `EM2016` 一级、`stocks.sector` 是二级——同一字符串切出来的两级。
- **「表里有行」不等于「数据可用」。** `margin_market_flow`、`stock_fund_flow`、`block_stocks` 的读取端首先查 `tdx_data_completeness`，没有登记行就直接 `blocked: completeness_missing`。第一次接沪市融资融券写进 38 行、`get_margin_market_history()` 仍返回 0 行——同步必须在写数据的同时更新登记表，「接线」才算完成。
- **单位是数据契约的一部分。** `RPT_F10_FINANCE_MAINFINADATA` 给的是百分比（`ZCFZL=21.3`），而 `financials.debt_ratio` 的三个消费者都按分数用（`1.0 - debt_ratio`、示例 `0.21`），所以写入时要 `/100`。同类：`rqyl` 是融券余量（股）、`rqylje` 是融券余额（金额），别取错。
- **格式不匹配会静默丢数据。** `margin_market_flow` 的读取端用 `date.fromisoformat`，写 `20261015` 会让它 `continue` 跳过每一行；上交所对畸形日期不报错、只返回空。这类「错误格式 → 空结果」必须在写入前校验，不要靠事后发现。
- **把进程能不能退出来当成验收项。** pytdx 的 `heartbeat=True` 会起一个**非 daemon** 线程，而 CPython 的 `threading._shutdown()`（等非 daemon 线程）**先于** `atexit` 回调——所以注册 atexit 也救不了，任何开过 TDX 连接的进程都退不出去。手写的长跑/探针脚本要加超时，否则「挂死」会被误记成「没数据」。
- **备用源只有在「和主源对得上」之后才算备用源。** 三个源都"能返回数据"是不够的：东财/腾讯的成交量是**手**、Sina 是**股**（实测比值精确 100.00），东财的 `fqt=1` 是前复权而 Sina 只给不复权（120 天窗、12/12 只股票只对得上 `fqt=0`）。不一致的备用源比没有备用源更糟——它会静默把历史改掉。
- **比一致性只能用已收盘的 bar。** 盘中每家的"最新一根"刷新节奏不同，三个都正确的源会报出三个不同价格（实测 1312.97 / 1311.57 / 1314.20）。取**所有源共有、且不是今天**的那一天比；当日的 bar 还在动，拿它比会每天对健康标的误报，进而掩盖真正的不一致。
- **验证口径要用足够长的窗口。** 前复权以最新价为锚，所以窗口尾部两种复权**重合**：600519 在 40 天里 `fqt=0` 与 `fqt=1` 完全相同，看起来"没有区别"。换成 12 只股票、120 天才有定论。一个标的四十天的窗口什么都证明不了。
- **同一个代码可能是两只证券。** `430047`（北交所，代码被重新分配）在东财返回 **2020** 年序列、在 Sina 返回 **2025** 年序列。只有在换源时才写进去，且写之前要判断：有存量就必须日期重叠，无存量就必须序列结尾接近市场最新日（`STALE_SERIES_GRACE_DAYS = 30`）。不满足就**拒绝写入并计入失败**——只报告是不够的，实测会先把 2020 的数据写进去再打印警告。
- **`fetch_*` 的默认参数不要绑定常量。** `def f(order=KLINE_CHANNELS)` 把名字在**定义时**的值冻住了，于是覆盖模块常量静默不生效；更坏的是旁边用新常量做判断的代码会因此对每个标的都误判。默认值写 `None`，函数体内再取。
- **时间戳。** `CURRENT_TIMESTAMP` 是 **UTC 且不带偏移**；`kline_1d.time` 带 `+08:00`。同一列不要混格式（会破坏 `ORDER BY`），跨表比日期要换算——本地时间 00:00–08:00 写入的行，按本地日期查会漏掉。

## 批量改写与一次性 codemod

大范围机械改写（856 处静默吞异常、26 个文件的 akshare 引用）必须满足：

- **按行编辑，不要 `ast.unparse` 重写整个文件** —— 否则语义改动会被埋进全文件格式化噪音里，无法 review。
- **写回前 `ast.parse` 回验**。写不回去的文件宁可跳过也不写坏。
- **证明行为等价**，不是靠读：把插入的语句与样板剥离后，与改动前的 AST 比较，逐文件报告差异；有意改动之外的差异数必须是 0。
- **codemod 自己会有 bug**，本次三个：改写后的 `except` 头丢缩进、dedent 用绝对缩进而非相对缩进、删除的 `if` 是 `try` 里唯一语句导致空块。所以它必须**先在副本上跑**，并且让测试套件当最终判据——102 个模块因「函数内的 `import logging` 被当成模块级」而 `NameError`，是测试套件抓到的，不是 dry run。
- **一次性 codemod 用完就删**。留在 `scripts/` 会腐烂（它们引用的是已经不存在的前置状态）；持续有效的工具才配 `check_*.py` 的名字。

## 指标复核

- **头部指标看起来异常时，先独立复算再解释。** `volatility` 的 rankIC 报 +0.605，量级上不可能（常见因子 0.02–0.10）。从 `kline_1d` 重算后得到 +0.4720/+0.6037，与入库的 +0.4727/+0.6051 吻合——数值是对的，异常来自口径：这是**单日横截面 IC**（5,520 只、一个日期），是一个观测值，不是因子评级。
- **验证要经过数据本身的读取端。** 「表里有 269,446 行」不等于可用；`get_dragon_tiger("2026-10-15")` 真的返回 200 条才算通。日期格式、参数绑定这类问题只在读取端暴露。
- **空值不等于缺失。** 有 55 个 `value == 0.0`，看着像「缺失补零」，实际它们有完整财务数据——那是亏损公司的真实下限。先查清再动手。

## 测试

写**行为断言**：调用真实函数，验证返回值与错误码。

不要读源码文本做字符串匹配（`Path(...).read_text()` + `assert "xxx" in src`）。这类断言在模块搬迁时产生假失败，且不验证行为正确性。

实例：`prediction_diagnosis_workflow` 的 Platt 校准有最少 30 样本门槛，源码文本匹配对此完全无感，改成真实调用后立刻暴露。

更坏的变体，都出现过：

- 断言注释存在（`assert '# P0 FIX: 补偿逻辑默认禁用' in content`）—— 删掉守卫、留着注释即通过。
- 断言字符偏移量顺序（`assert add_idx < validate_idx < insert_idx`）来验证下单前的输入校验 —— 等价重排即失败，语义变更却可能通过。
- 断言含精确缩进的多行字符串不存在 —— 跑一次格式化工具即空过。
- 用 `return True/False` 代替 `assert`。pytest 忽略返回值，`passed=False` 也算通过。

诊断脚本命名为 `check_*.py`，不要叫 `test_*.py` —— 后者会被 pytest 收集，把「需要活库、空库时 return True」的运维检查伪装成单元测试。`scripts/factories/` 下 8 个文件（1603 行、近乎零断言）因此被改名。

根 `pytest.ini` 的 `testpaths` 覆盖全部 8 个包；CI 里每个包都是阻塞门禁。曾经只列 2 个包、且最大的两个包设 `continue-on-error`，等于 1900+ 测试（含公式执行安全套件）的回归无法让构建失败。
