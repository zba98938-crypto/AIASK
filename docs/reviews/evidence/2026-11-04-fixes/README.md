# 四工厂修复验收证据

对应[修复验收报告](../../四工厂确认问题修复验收-2026-11-04.md)和[原始审查](../../项目能力与四工厂全面审查-2026-11-04.md)。此目录保留的是代码修复与隔离测试证据，不是生产运行记录。

- [策略修复](strategy-fixes.md)：S1–S5，包括真实权益、现金分母、交易日期、样本外回放状态与零值语义。
- [因子与事件修复](factor-event-fixes.md)：F1–F4、E1，包括数值准入、隔离因子晋级、质量反馈与方向桥接。
- [孵化修复](incubation-fixes.md)：I1–I6，包括只读连接、完整阶段、真实写入异常和计数。
- [控制面修复](control-fixes.md)：C1–C4，包括独立 runner 结果文件、启动失败、长日志与只读诊断。

`*-independent-review-initial.md` 是补修前的独立复核发现，不是当前未解决问题清单。现金分母与 holdout 状态由策略修复报告及 `pytest-strategy-review-closure.log` 闭合；真实指标/报告/反馈写入异常由孵化修复报告、`pytest-incubation-final.log` 和 `incubation-independent-repro-fixed.log` 闭合。复现日志中的异常为主动注入的失败条件，末尾 JSON 给出修正后的状态。

`pytest-root-before-review-closure.log` 是补修前第一轮集成测试，3500 passed / 22 skipped。最终验收以 `pytest-root-final.log` 为准。各定向测试组互有重叠，不累加为测试总数。

`tested-worktree-python-sha256.json` 记录最终回归期间工作区中已修改及未跟踪的 packages/scripts Python 文件，包含任务开始前已有修改。它用于验证测试对应的文件内容，不是本轮修改归属清单。

各分项交接报告保留原始 `/tmp` 日志路径；对应日志已经按本目录的 `pytest-*.log` 名称归档。
