# Docs 导航

## 文档分层

当前仓库文档按以下口径整理：

- 根目录只保留少量长期入口和被明确要求放在根目录的权威主方案。
- `docs/CURRENT_PRODUCT_TRUTH.md` 是 **Hermes 集成副本的当前产品真相**（双桌面、权威门禁、过期宣称清单）。与六月完成度报告冲突时，以它和 repo 根 `AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md` 为准。
- `docs/DATA_SOURCES.md` 是**数据源台账**：每一类数据来自哪个源、覆盖率、刷新成本、已验证不可用的路径，以及这些 provider 对调用方的约定。新增数据源前先读它。
- `docs/factory-architecture/` 放四工厂 current-state 文档、运行规范、审计台账和附录报告。
- `docs/archive/frontend/` 放 V1 前端专题流程、阶段性开发方案和完成度报告（**历史/上游口径**，不代表本 Hermes 树已包含完整前端）。
- `frontend-screenshots/AIASK标准化功能规范-2026-06-20/` 继续放前端专项功能规范与细分文档。

## 根目录保留文档

- `AGENT.md`
  - 仓库长期协作约定与开发规范。
- `AIASK项目开发技术规范-V1前端产品化-2026-06-21.md`
  - 当前仍被多处前端规范文档引用，继续作为根目录权威主规范保留。
- `四工厂独立化与MCP瘦身拆分开发方案-2026-06-23.md`
  - 按当前决策继续作为根目录 target-state 主方案保留。

## 前端专题文档

位置：`docs/archive/frontend/`

- `README.md`
- `FRONTEND_WORKFLOW.md`
- `AIASK V1前端从零重建详细开发方案-2026-06-21.md`
- `AIASK_V1前端功能完成度报告-2026-06-22.md`

## 四工厂附录报告

位置：`docs/factory-architecture/appendix/reports/`

- `策略工厂24小时运行与质量追踪-2026-06-22.md`

## 整理规则

- 如果文档是长期协作约定、全仓入口规范，保留在根目录。
- 如果文档是某一专题的实施细则、阶段性方案、完成度报告或运行报告，优先放进对应 `docs/` 子目录。
- 如果文档已经被现状文档明确声明“必须在根目录保留”，后续移动前必须先同步修正所有引用口径。
- **禁止**用 `docs/archive/frontend/` 完成度报告或 `P0-completion-report.md` 作为 Hermes 侧“金融产品完成”证明；那些文档描述的完整页面 / `desktop-api` 包在本集成副本中并不存在。
