# 四工厂完整运行指南


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

**日期**: 2026-06-22  
**状态**: Schema 迁移完成，可以启动  

> 更正 / 当前口径说明（截至 2026-06-23）
>
> 本文保留 2026-06-22 的运行指南语境，但当前真实架构应以 `docs/factory-architecture/01`、`03`、`12`、`13` 为准。特别是：四个 canonical runtime 入口已经落在 `strategy_factory.runtime.*`，`SignalTracker` 继续是独立 sidecar，不属于 supervisor 的四运行体；`server.py` 也不再拥有 factory lifecycle。

---

## 📖 什么是四工厂？

四工厂是 AIASK 策略生产线的**持续运营系统**，包含：

1. **Strategy Factory** - 策略生成工厂
2. **Factor Mining Factory** - 因子挖掘工厂
3. **Incubation Factory** - 孵化和转正工厂
4. **Market Event Ingest** - 市场事件摄入

**为什么叫"四工厂"但文件名是 `run_three_factories.py`？**
- 历史原因：最初只有三个工厂
- 后来加了 Market Event Ingest，变成四个
- 文件名没改，代码已更新（见文件第 36-41 行）

---

## 🆚 四工厂 vs Quality Session

### Quality Session（质量验证模式）
- **目的**: 验证 P0-P2 修复效果
- **运行**: 短期（24h）、高强度
- **特点**: 多模式对比、补偿逻辑监控
- **适用**: 架构修复阶段
- **产出**: observe 策略累积

### 四工厂（正式运营模式）
- **目的**: 持续生产运营
- **运行**: 长期、稳定节奏
- **特点**: 完整生命周期（observe → formal → production）
- **适用**: 生产就绪后
- **产出**: production 策略

**当前状态**: 
- ✅ P0-P2 修复完成（Quality Session 验证通过）
- ✅ Schema 迁移完成（868 个高 skill 策略就位）
- ✅ **可以从 Quality Session 切换到四工厂**

---

## 🏭 四个工厂详解

### 1️⃣ Strategy Factory（策略生成）

**职责**：
- 接收市场上下文、事件、因子
- 通过 LLM 或规则生成候选策略
- 执行 Quality Gate（方向门、IC 门等）
- 决定策略进入 observe/paper/diagnostic

**输入**：
- 市场数据（K线、行情、资金流）
- 市场事件（公告、新闻、事件锚点）
- 因子池（来自 Factor Mining Factory）
- 历史孵化反馈

**输出**：
- 候选策略 artifacts
- observe/paper/diagnostic handoff
- 策略提交到 strategies 表

**健康指标**：
- 每轮生成候选数量
- Quality Gate 通过率
- observe/paper/diagnostic 分布

---

### 2️⃣ Factor Mining Factory（因子挖掘）

**职责**：
- 通过多引擎搜索新因子（LLM、GP、MCTS、RL）
- 对候选因子执行验证（schema、sandbox、IC）
- 维护 active factor pool

**输入**：
- 市场数据（历史 K线）
- Factor catalog（已有因子库）
- Engine 配置

**输出**：
- 新因子候选
- 验证证据（IC、前向收益）
- Active factor pool 更新

**健康指标**：
- 每轮生成因子数
- 验证通过率
- Active pool 大小

**为什么重要**：
- Strategy Factory 依赖因子池生成策略
- 因子质量直接影响策略质量
- 持续发现新 alpha 源

---

### 3️⃣ Incubation Factory（孵化转正）⭐

**职责**（最关键）：
- 接收 Strategy Factory 提交的策略
- 生成信号、执行纸上交易
- 计算前向收益、命中率
- **执行转正决策**：observe → formal → production

**输入**：
- Strategy Factory handoff（observe/paper 策略）
- Paper accounts（纸上交易账户）
- 实时市场数据

**输出**：
- 策略信号（strategy_signals 表）
- 前向收益（signal_forward_returns 表）
- 命中率报告
- **转正决策**：formal_incubation、production

**Pipeline Phases**：
```
Phase 1:   Intake（接收新策略）
Phase 1.5: observe → formal 转正 ⭐
Phase 2:   加载孵化/paper/diagnostic 策略
Phase 3:   信号生成、前向验证
Phase 4:   Pipeline evaluation
Phase 5:   Hit-rate report
Phase 6:   Feedback 写入
Phase 7:   Acceleration check
Phase 8:   Alert check
Phase 9:   Heartbeat
```

**转正条件**（Phase 1.5）：
- ✅ 样本量 ≥3
- ✅ 命中率 ≥55%（skill_lcb > 0）
- ✅ 语义契约完整（evidence/prediction/confidence）
- ✅ 结构性字段完整（compiled_dsl、measured profile）

**为什么关键**：
- **这是 formal 转正的唯一执行者**
- observe 策略在这里积累证据
- formal 策略在这里验证稳定性
- production 策略在这里准备接入真实交易

---

### 4️⃣ Market Event Ingest（事件摄入）

**职责**：
- 接入官方事件源（CNINFO、上交所、深交所）
- 标准化市场事件（公告、分红、重组）
- 桥接到 Strategy Factory（作为策略锚点）

**输入**：
- CNINFO API
- 上交所公告
- 深交所公告
- 可信新闻源

**输出**：
- Normalized market events
- Event task anchors
- Strategy Factory event signals

**Event Tier**：
- Tier A: 官方公告（高可信）
- Tier B: 可信源交叉验证
- Tier C: 新闻/媒体（辅助诊断）

**为什么重要**：
- 事件驱动策略的核心输入
- 提供策略的"锚点"（为什么在这个时间点交易）
- 提升策略可解释性

---

## 🔄 四工厂运行流程

```
┌─────────────────────────────────────────────────────────┐
│                    Market Event Ingest                   │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐         │
│  │  CNINFO  │───▶│  标准化  │───▶│  桥接到  │         │
│  │  上交所  │    │  事件    │    │ Strategy │         │
│  │  深交所  │    │          │    │ Factory  │         │
│  └──────────┘    └──────────┘    └──────────┘         │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│              Factor Mining Factory                       │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐         │
│  │ 多引擎   │───▶│  验证    │───▶│ Active   │         │
│  │ 生成因子 │    │ (IC/前向)│    │ Factor   │         │
│  │(LLM/GP/RL)│   │          │    │ Pool     │         │
│  └──────────┘    └──────────┘    └──────────┘         │
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│                   Strategy Factory                       │
│  ┌──────────┐    ┌──────────┐    ┌──────────┐         │
│  │ 上下文   │───▶│ 候选生成 │───▶│ Quality  │         │
│  │ 构建     │    │(LLM/规则)│    │ Gate     │         │
│  │(事件+因子)│   │          │    │          │         │
│  └──────────┘    └──────────┘    └──────────┘         │
│                                         │               │
│                            ┌────────────┴────────────┐  │
│                            ▼                         ▼  │
│                    ┌──────────────┐      ┌──────────────┐
│                    │  observe     │      │  paper       │
│                    │  handoff     │      │  handoff     │
│                    └──────────────┘      └──────────────┘
└─────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────┐
│                   Incubation Factory ⭐                  │
│                                                          │
│  Phase 1: Intake（接收）                                │
│     ▼                                                    │
│  Phase 1.5: observe → formal 转正 ⭐                    │
│     ▼                                                    │
│  Phase 2: 加载策略                                       │
│     ▼                                                    │
│  Phase 3: 信号生成 + 前向验证                           │
│     ▼                                                    │
│  Phase 4: Pipeline evaluation                           │
│     ▼                                                    │
│  Phase 5: Hit-rate report                               │
│     ▼                                                    │
│  Phase 6: Feedback                                      │
│                                                          │
│  输出:                                                   │
│  - observe_incubation（积累证据）                        │
│  - formal_incubation（转正）⭐                          │
│  - production（准备真实交易）                           │
└─────────────────────────────────────────────────────────┘
```

---

## 🚀 启动四工厂

### 前置条件检查 ✅
```bash
# 已验证通过:
✅ incubating 列存在
✅ observe_incubation 策略: 16,491 个
✅ 包含 868 个高 skill 策略
✅ 所有脚本文件存在
```

### 启动命令
```bash
python scripts/factories/run_three_factories.py
```

### 启动参数（可选）
```bash
# 查看所有参数
python scripts/factories/run_three_factories.py --help

# 常用参数
--log-dir logs/three_factories   # 日志目录
--restart-delay 5                # 重启延迟（秒）
```

### 停止四工厂
```bash
# 发送 SIGINT (Ctrl+C)
# Supervisor 会优雅停止所有子进程
```

---

## 📊 监控和日志

### 日志位置
```
logs/three_factories/
├── supervisor.log              # Supervisor 主日志
├── strategy_factory.log        # 策略生成日志
├── factor_mining_factory.log   # 因子挖掘日志
├── incubation_factory.log      # 孵化转正日志 ⭐
└── market_event_ingest.log     # 事件摄入日志
```

### 实时监控
```bash
# Supervisor 总览
tail -f logs/three_factories/supervisor.log

# Incubation Factory（最关键）⭐
tail -f logs/three_factories/incubation_factory.log | grep -E "phase|formal|promotion"

# Strategy Factory
tail -f logs/three_factories/strategy_factory.log | grep -E "candidate|gate|submitted"

# 所有工厂
tail -f logs/three_factories/*.log
```

### 每日检查命令
```bash
# 快速健康检查
python scripts/factories/daily_check.py

# 完整监控仪表板
python scripts/factories/dashboard_full_monitor.py
```

---

## 📈 预期时间线

### 启动后 24 小时
- Incubation Factory Phase 1.5 开始工作
- **预期**: formal_incubation > 0（首批转正）
- 监控: `tail -f logs/three_factories/incubation_factory.log | grep "phase 1.5"`

### 2-3 天
- formal 策略加速转正
- **预期**: formal_incubation > 50
- 监控: `python scripts/factories/daily_check.py`

### 5 天
- formal 策略验证稳定
- **预期**: production > 5（首批进入生产）
- 监控: formal → production 晋升日志

### 1-2 周
- production 策略就绪
- **预期**: production > 20
- 下一步: 接入 Execution Factory（真实交易）

---

## ⚠️ 常见问题

### Q1: formal 转正很慢？
**原因**：
- 样本成熟需要时间（≥3 个信号）
- 命中率需要真实验证（≥55%）

**解决**：
- 继续运行，等待样本积累
- 检查 Incubation Factory Phase 3（信号生成）是否正常
- 查看日志: `grep "phase 3" logs/three_factories/incubation_factory.log`

### Q2: 四工厂崩溃重启？
**原因**：
- 数据库锁
- 内存不足
- API 超时

**解决**：
- 查看 Supervisor 日志: `tail -f logs/three_factories/supervisor.log`
- Supervisor 会自动重启子进程
- 如果频繁重启，检查资源使用情况

### Q3: Strategy Factory 不生成候选？
**原因**：
- 因子池为空（Factor Mining Factory 未运行）
- 事件锚点缺失（Market Event Ingest 未运行）
- LLM API 配额用尽

**解决**：
- 检查四个工厂是否都在运行
- 检查 API key: `echo $ANTHROPIC_API_KEY`
- 查看 Strategy Factory 日志

### Q4: 四工厂 vs Quality Session 可以同时运行吗？
**不建议**：
- 两者都会操作 strategies 表
- 可能有资源竞争
- 建议：Quality Session 停止后启动四工厂

---

## 🎯 成功标准

### 短期（1周内）
- ✅ 四个工厂稳定运行（无频繁重启）
- ✅ formal_incubation > 50
- ✅ production > 5

### 中期（1月内）
- ✅ production > 50
- ✅ 所有 production 策略有完整契约
- ✅ 准备接入 Execution Factory

### 长期（2-3月）
- ✅ 真实交易运行
- ✅ 首次真实盈亏反馈
- ✅ 完整闭环验证

---

## 📚 相关文档

- `docs/factory-architecture/03-四工厂运行规范.md` - 完整规范
- `docs/factory-architecture/PRODUCTION_ROADMAP.md` - 生产路线图
- `docs/factory-architecture/MILESTONE_REPORT_20260622.md` - 今日里程碑
- `docs/factory-architecture/DAILY_MONITORING_GUIDE.md` - 每日监控指南

---

**最后更新**: 2026-06-22
