# 工厂运行脚本

本目录包含策略工厂的运行脚本。

## 🏭 脚本列表

### 主运行脚本

1. **run_strategy_factory.py**
   - 运行策略工厂
   - 支持指定工厂类型
   - 使用: `python scripts/factories/run_strategy_factory.py`

2. **run_all_factories.py**
   - 批量运行所有工厂
   - 顺序执行各工厂
   - 汇总运行结果
   - 使用: `python scripts/factories/run_all_factories.py`

## 📝 使用说明

### 四个工厂类型

1. **Signal Factory** (信号工厂)
   - 生成交易信号
   - 因子组合

2. **Incubation Factory** (孵化工厂)
   - 策略孵化
   - 质量验证
   - 晋升管理

3. **Candidate Factory** (候选工厂)
   - 因子挖掘
   - 候选因子评估

4. **Market Event Factory** (市场事件工厂)
   - 事件监控
   - 事件驱动策略

### 运行模式

```bash
# 单个工厂 (交互模式)
python scripts/factories/run_strategy_factory.py

# 全部工厂 (自动模式)
python scripts/factories/run_all_factories.py

# 孵化工厂 (直接调用)
python packages/akshare-mcp/scripts/run_incubation_factory.py
```

### 日志和报告

- 运行日志: `logs/factories/`
- 运行报告: 控制台输出
- 数据持久化: SQLite 数据库

---

**最后更新**: 2026-06-24  
**当前状态**: Phase 2B/2D/3 完成，独立运行就绪
