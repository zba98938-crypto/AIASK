# 审查证据与复现

日期：2026-11-04；代码工作区包含原有未提交修复。主报告给出审查结论、问题索引与验证边界。

- `runtime-snapshot.json`：标准库SQLite `mode=ro` 和query_only查询结果。部分工厂大JSON字段仅保存前2500字符，不能作为完整run导出；计数查询完整。
- `runtime-addendum.json`：补充数据，含实际SQL、数据库路径、测量时刻与只读连接方式。
- `pytest-root.log`：根目录完整pytest原始输出，3431通过、22跳过。
- 三份专项Markdown：能力、源码行号、影响边界、复现和测试范围。
- `control-repro.log`：四个控制/诊断问题的原始行为输出。

所有Python复现只使用hermetic临时目录、临时SQLite/内存替身或合成子进程，不启动真实工厂。必须从本项目根目录、使用指定解释器执行，以满足脚本的路径依赖。它们用于复现本次审查基线，源码后续修复会改变结果；不是新增生产组件。

```bash
cd /Users/mac/Desktop/张勇/AIASK
/opt/miniconda3/envs/aiask/bin/python docs/reviews/evidence/2026-11-04/strategy_repro.py
/opt/miniconda3/envs/aiask/bin/python docs/reviews/evidence/2026-11-04/strategy_benchmark_repro.py
/opt/miniconda3/envs/aiask/bin/python docs/reviews/evidence/2026-11-04/factor_events_repro.py
/opt/miniconda3/envs/aiask/bin/python docs/reviews/evidence/2026-11-04/incubation_repro.py
/opt/miniconda3/envs/aiask/bin/python docs/reviews/evidence/2026-11-04/forward_order_repro.py
/opt/miniconda3/envs/aiask/bin/python docs/reviews/evidence/2026-11-04/signal_horizon_repro.py
/opt/miniconda3/envs/aiask/bin/python docs/reviews/evidence/2026-11-04/control_repro.py
```

其中质量反馈调预算未接入、未接线模块等结论主要来自调用链审查。不能把内存复现或mock供应商当作真实供应商联网、长期运行或实盘验收。
