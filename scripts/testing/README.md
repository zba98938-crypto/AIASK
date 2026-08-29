# 测试脚本

本目录包含项目的各类测试脚本。

## 🧪 脚本列表

### 孵化工厂测试

1. **incubation_factory_50_rounds_test.py**
   - 50 轮孵化工厂压力测试
   - Dry-run 模式，不写数据库
   - 生成详细 JSON 报告
   - 使用: `python scripts/testing/incubation_factory_50_rounds_test.py`

2. **monitor_50_rounds_test.py**
   - 监控 50 轮测试进度
   - 实时显示完成状态
   - 计算 ETA
   - 使用: `python scripts/testing/monitor_50_rounds_test.py`

### 策略分析

3. **analyze_strategy_quality.py**
   - 分析策略质量指标
   - 统计策略生命周期分布
   - 查询命中率和 Skill
   - 使用: `python scripts/testing/analyze_strategy_quality.py`

### 功能验证

4. **run_factory_verification.py**
   - 工厂功能验证脚本
   - 检查各阶段运行状态

5. **test_exit_fix.py**
   - 持仓退出率修复验证
   - Phase 3c2/3d 测试

## 📝 使用说明

### 运行环境

所有脚本需要正确的 PYTHONPATH：
```python
from strategy_factory.runtime_bootstrap import ensure_factory_runtime

ensure_factory_runtime(
    project_root=PROJECT_ROOT,
    script_path=Path(__file__).resolve(),
    argv=[],
    editable_packages=(
        "packages/strategy-factory",
        "packages/aiask-quant-core",
        "packages/akshare-mcp",
    ),
)
```

### 日志输出

- 控制台输出: 实时进度
- 文件输出: `logs/testing/` 目录
- JSON 报告: `docs/reports/` 目录

---

**最后更新**: 2026-06-24
