# 测试 / 运维诊断脚本

本目录包含可独立运行的运维诊断脚本。**诊断脚本一律命名为 `check_*.py`**，
不要用 `test_*.py`（会被 pytest 误收集）。

## 🧪 脚本列表

### 孵化工厂测试

1. **check_incubation_factory_50_rounds.py**
   - 50 轮孵化工厂压力测试
   - Dry-run 模式，不写数据库
   - 生成详细 JSON 报告
   - 使用: `python scripts/testing/check_incubation_factory_50_rounds.py`
   - 2026-10 起更名：原名 `incubation_factory_50_rounds_test.py`，
     `test_*` 命名会被 pytest 收集，改为 `check_*` 约定

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

已删除（2026-10 清理，均为失效脚本）：

- `monitor_50_rounds_test.py` — 读取 50 轮脚本从不写入的 `LOG_PATH`，死契约
- `test_exit_fix.py` — import 不存在的 `strategy_factory.db.connection.get_connection`

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

**最后更新**: 2026-10-20（清理失效脚本；50 轮压测脚本更名为 `check_*`）
