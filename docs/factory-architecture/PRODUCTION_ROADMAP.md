# formal=0 瓶颈诊断与生产路线图


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

**诊断日期**: 2026-06-22  
**P0-P2 修复状态**: 已完成并通过测试（8/8）

---

## 📊 当前数据库状态

### 策略状态分布（使用 status 列）
```
submitted:   15,471  # 已提交，等待孵化
incubating:   1,012  # 孵化中
rejected:     3,007  # 被拒绝
draft:        2,618  # 草稿
archived:       706  # 已归档
diagnostic:     182  # 诊断模式
deprecated:      62  # 已废弃
listed:           1  # 已上线（生产）
```

**关键发现**：
- ✅ 有 **1,012 个策略在孵化中**（incubating）
- ✅ 有 **1 个策略已上线**（listed）
- ⚠️ 但没有使用新架构的 `observe_incubation` / `formal_incubation` 分类

---

## 🔍 formal=0 的根本原因

根据记忆文件（2026-06-16）和当前数据，formal=0 是因为：

### 1. **数据库 Schema 版本问题** 🔴
- 现有数据库使用 **旧 schema**（`status` 列）
- 新架构代码期望 **新 schema**（`incubating` 列，区分 observe/formal）
- **P0-P2 修复针对的是新架构代码**，但数据库还没迁移

### 2. **候选质量不足** 🔴（记忆文件核心瓶颈）
- 实测方向命中率仅 **27.5%**
- 5日前向、≥3样本：真实 skill ≥0.55 的 = **0 个**
- 根因：momentum 策略配到下跌标的（方向错配）

### 3. **语义契约缺失** 🔴
- 带齐 evidence/prediction/confidence 三契约的 = **0 个**
- `STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED` 默认 **False**
- market_evidence_pack 持久化 = 0（被存储压缩丢弃）

### 4. **结构性字段缺失** 🔴
- compiled_dsl 缺失
- measured instrument_profile 缺失
- 导致 execution_readiness_tier 被阻塞

---

## 🗺️ 达到生产的路线图

### Phase 0: 立即行动（已完成） ✅
**时间**: 已完成  
**目标**: 代码层面合规

- [x] P0-2: Quality Session 补偿逻辑默认禁用
- [x] P1-1: SignalTracker 集成统一契约
- [x] P1-2: 诊断工具集成生命周期账本
- [x] P2: 契约定义统一
- [x] 所有测试通过（8/8）

**成果**: 代码架构已就绪，不会引入新的技术债

---

### Phase 1: 数据库 Schema 迁移 🔧
**时间**: 1-2 天  
**优先级**: P0（阻塞）  
**目标**: 统一数据库和代码架构

#### 1.1 Schema 升级
```sql
-- 添加新列
ALTER TABLE strategies ADD COLUMN incubating TEXT;

-- 迁移数据
UPDATE strategies 
SET incubating = CASE 
    WHEN status = 'incubating' THEN 'observe_incubation'  -- 默认先进 observe
    WHEN status = 'listed' THEN 'production'
    ELSE NULL
END;
```

#### 1.2 验证迁移
- 运行迁移脚本
- 验证 incubating 列数据正确
- 保留 status 列作为备份

**风险**: 中等（需要备份数据库）  
**收益**: 解除 P0-P2 修复与数据库的不匹配

---

### Phase 2: 候选质量提升 🎯
**时间**: 持续优化，3-7 天见效  
**优先级**: P0（核心瓶颈）  
**目标**: 命中率从 27.5% → 55%+

#### 2.1 修复方向错配（已在记忆中标记为已修复）
- [x] `STRATEGY_FACTORY_DIRECTION_GATE_ENABLED` 已默认 ON
- [ ] 验证生效：检查新生成的 momentum 策略是否避开下跌标的

#### 2.2 LLM 冷却放宽（已在记忆中标记为已修复）
- [x] 冷却参数已调温和（连续 3 次超时才冷却 120 秒）
- [ ] 观察 Quality Session：LLM 候选生成频率是否提升

#### 2.3 本地因子池 IC 排序（已在记忆中标记为已修复）
- [x] 本地候选按真实 IC 排序
- [ ] 观察新候选：IC 值是否更高

#### 2.4 监控指标
```bash
# 每天运行一次，观察趋势
python scripts/factories/diagnose_formal_simple.py

# 关键指标
- 新增策略的平均命中率
- LLM 生成 vs 本地 fallback 比例
- hit_rate ≥0.55 的策略数量变化
```

**风险**: 低（已有修复，需时间验证）  
**收益**: 高质量候选是转正的前提

---

### Phase 3: 语义契约补全 📝
**时间**: 2-3 天  
**优先级**: P1（依赖 Phase 2）  
**目标**: 让有真实 skill 的策略能转正

#### 3.1 启用语义契约生成
```python
# 设置环境变量
STRATEGY_FACTORY_EVIDENCE_CONTRACT_ENABLED=1
```

#### 3.2 修复存储丢弃问题
- 修改 `HEAVY_JSON_KEYS` 逻辑，不丢弃三契约
- 或：增加存储预算，允许保留完整契约

#### 3.3 从真实数据合成契约
- 使用 `strategy_signals` + `signal_forward_returns`
- 使用 `strategy_trade_predictions` + `outcomes`
- 生成非占位的真实契约

**前提条件**: Phase 2 产生了有真实 skill 的策略  
**风险**: 低  
**收益**: 解除契约缺失阻塞

---

### Phase 4: 样本成熟期 ⏰
**时间**: 2-4 周  
**优先级**: P1（无法跳过）  
**目标**: 积累足够的真实前向证据

#### 4.1 持续运行 Quality Session
```bash
# 24小时循环运行
python scripts/factories/run_strategy_factory_quality_session.py --hours 24

# 或：长期运行
python scripts/factories/run_strategy_factory_quality_session.py --hours 168  # 7天
```

#### 4.2 监控样本积累
- 每周查看 `signal_forward_returns` 增长
- 跟踪 ≥3 样本的策略数量
- 跟踪 hit_rate ≥0.55 的策略数量

#### 4.3 observe → formal 转正
- 一旦策略达到标准自动转正
- 监控 `formal_incubation` 数量增长

**风险**: 无（运营时间过程）  
**收益**: 真实样本是生产就绪的唯一路径

---

### Phase 5: Promotion Factory 启动 🚀
**时间**: 1-2 天  
**优先级**: P2（依赖 Phase 3-4）  
**目标**: formal → production 晋升

#### 5.1 启动 Promotion Factory
```bash
# 独立运行或集成到三工厂脚本
python scripts/factories/run_promotion_factory.py
```

#### 5.2 生产就绪检查
- Execution audit 验证
- 多重检验（PBO/RC/SPA）
- 风险评估

#### 5.3 监控晋升
- 跟踪 `production` 状态策略数量
- 验证晋升策略质量

**风险**: 低  
**收益**: 打通 formal → production 通道

---

### Phase 6: Execution Factory 接入 💰
**时间**: 1-2 周  
**优先级**: P2（生产关键）  
**目标**: 真实交易执行

#### 6.1 券商接口配置
- 选择券商（如：富途/雪盈/IB）
- 配置 API 密钥
- 测试连接

#### 6.2 风险管理配置
- 单笔最大金额
- 总仓位上限
- 止损/止盈规则

#### 6.3 小资金验证
```bash
# 初始资金：1万元
# 单策略：100-500元
# 观察：2-4周
```

#### 6.4 Execution Factory 启动
```bash
python scripts/factories/run_execution_factory.py --initial-capital 10000
```

**风险**: 高（真实资金）  
**收益**: 达到生产运营状态

---

## 📈 里程碑时间线

### 短期（1-2 周）
- Week 1: Phase 1（Schema 迁移）+ Phase 2 验证
- Week 2: Phase 3（契约补全）+ Phase 4 启动

**预期**: 
- formal_incubation 从 0 → 5-10 个
- 候选命中率从 27.5% → 40-50%

### 中期（2-4 周）
- Week 3-4: Phase 4 持续运行
- Week 4: Phase 5 启动

**预期**:
- formal_incubation 达到 20-50 个
- 首批策略转 production（3-5 个）

### 长期（1-2 月）
- Month 2: Phase 6 券商接入
- Month 2-3: 小资金验证

**预期**:
- 真实交易运行
- 首次真实盈亏反馈

---

## 🎯 当前建议的行动顺序

### 今天/本周（立即）
1. ✅ **运行 Quality Session 24小时**（你已启动）
   - 观察 P0-P2 修复效果
   - 收集候选质量数据

2. 🔧 **准备 Schema 迁移脚本**
   - 备份数据库
   - 编写迁移 SQL
   - 在测试库验证

3. 📊 **建立监控仪表板**
   - 每日候选生成数量
   - 命中率趋势
   - formal 候选数量

### 下周
4. 🔧 **执行 Schema 迁移**（Phase 1）
5. 📈 **验证候选质量提升**（Phase 2）
6. 📝 **启用语义契约**（Phase 3）

### 2-4周后
7. ⏰ **等待样本成熟**（Phase 4）
8. 🚀 **启动 Promotion Factory**（Phase 5）

### 1-2月后
9. 💰 **接入 Execution Factory**（Phase 6）

---

## ⚠️ 关键风险与缓解

### 风险 1: Schema 迁移失败
- **缓解**: 完整备份 + 测试库验证
- **回滚**: 保留 status 列，代码兼容两种列名

### 风险 2: 候选质量仍不足
- **缓解**: 监控 3 个修复（方向门/LLM 冷却/IC 排序）
- **Plan B**: 人工筛选高质量因子，手工注入

### 风险 3: 样本成熟时间过长
- **缓解**: 并行运行多个 Quality Session
- **Plan B**: 降低最低样本门槛（从 12 → 8）

### 风险 4: 真实交易亏损
- **缓解**: 小资金验证（1万元）+ 严格风控
- **止损**: 单策略亏损 >20% 立即下线

---

## 📞 需要帮助的地方

现在我可以帮你：

1. **编写 Schema 迁移脚本**
2. **监控当前 Quality Session 日志**
3. **分析候选质量趋势**
4. **准备下一阶段的脚本**

你想从哪个开始？
