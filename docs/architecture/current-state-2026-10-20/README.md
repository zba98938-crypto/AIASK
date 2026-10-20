# AIASK 当前态架构梳理（2026-10-20）

这是一次 **current-state** 梳理，不是目标态方案，也不是完成度报告。

## 快速阅读

1. **[architecture-understanding.md](architecture-understanding.md)** — 先读这一份：项目定位、能力地图、容器、关键流程、当前事实与风险。
2. **[capability-map.svg](capability-map.svg)** — 一页看全六类业务能力与其关系。
3. **[deployment-topology.svg](deployment-topology.svg)** — 当前进程、端口、数据库、外部系统。
4. **[flow-strategy-factory-cycle.svg](flow-strategy-factory-cycle.svg)** — 策略工厂单轮 11 阶段与最新漏斗。
5. **[flow-paper-lifecycle.svg](flow-paper-lifecycle.svg)** — 孵化、paper、证据、晋级生命周期。
6. **[flow-data-ingestion.svg](flow-data-ingestion.svg)** — 数据源 → 同步/校验 → SQLite → 消费者。
7. **[flow-agent-tool-call.svg](flow-agent-tool-call.svg)** — Agent 请求如何落到工具与数据。
8. **[dependency-map.svg](dependency-map.svg)** — 8 个包的依赖、动态装配与边界例外。
9. **[evidence-index.md](evidence-index.md)** — 每条关键结论的证据、SQL、命令与置信度。
10. **[artifact-summary.md](artifact-summary.md)** — 产物维护说明。

C4 源文件：**[aiask-system.structurizr.dsl](aiask-system.structurizr.dsl)**（L1 系统上下文 + L2 容器），用 Qoder 的 Structurizr DSL viewer 打开。

## 证据时间点

- 代码与配置：当前工作树（分支 `backend-only-hardening`，48 个未提交修改）。
- 运行时/SQLite：2026-10-20 08:42–08:57 +08:00。
- 文档：README 的“Known state”是 2026-10-15 快照；`CURRENT_PRODUCT_TRUTH.md` 的 baseline 是 2026-07 历史。与本次实测冲突时，以本次实测为准，并在报告中标注。

## 重新生成图谱

DOT 源文件由 Graphviz 渲染。本次使用 `@viz-js/viz`（Graphviz WASM）在临时目录渲染，不改变项目依赖：

```bash
mkdir -p /tmp/aiask-viz && cd /tmp/aiask-viz
npm init -y && npm install @viz-js/viz
# 渲染脚本见 evidence-index.md 或直接使用本机 dot：
# dot -Tsvg capability-map.dot -o capability-map.svg
```

如果本机安装了 Graphviz，直接：

```bash
for f in *.dot; do dot -Tsvg "$f" -o "${f%.dot}.svg"; done
```

C4 DSL 不依赖 Graphviz；用 Structurizr DSL viewer 打开即可。

## 边界

- 本目录只描述当前态，不提出代码修改，不替代 `docs/factory-architecture/` 的规范文档。
- 报告中的“红色/缺口”是当前运行态判断，不是对代码质量的最终裁决。
- 没有运行完整测试套件；没有修改运行中的进程或数据库。
