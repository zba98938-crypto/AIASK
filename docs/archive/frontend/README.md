# 归档：AIASK V1 前端文档（不可执行，仅供历史追溯）

> **本目录已归档。** 这些文档描述的 Electron/React 桌面前端已随本仓库独立化被移除
> （`desktop/` 目录不再存在）。文中的构建命令、npm 脚本、文件路径和验收标准
> **均无法执行**，也不代表当前交付状态。保留它们只为追溯当初的设计意图与决策过程。

## 当前状态

本仓库现为**纯后端 Agent/MCP 运行时**：

- 前端专属的 `/v1/desktop/*` HTTP facade 已删除；旧路径统一返回结构化 404，
  响应体中的 `meta.replacement` 会指出替代的中性路径。
- 仍有价值的能力已迁移到按领域划分的中性 API：`/v1/settings`、`/v1/data`、
  `/v1/users`、`/v1/finance`、`/v1/research`、`/v1/workbench`、`/v1/capabilities`。
- 少数只服务于前端的能力（`/v1/desktop/files`，以及 `/v1/desktop/users/*` 下的
  strategies / stock-pools）**没有替代**：它们代理的是一个独立的桌面资产服务
  （`AIASK_DESKTOP_API_BASE_URL`，即 `127.0.0.1:8001`），该服务不属于后端树。
- readiness 不再把"缺少前端源码"当作产品阻塞项。

当前口径请看 [`../../CURRENT_PRODUCT_TRUTH.md`](../../CURRENT_PRODUCT_TRUTH.md)。

## 归档内容

| 文件 | 原用途 |
|---|---|
| `AIASK项目开发技术规范-V1前端产品化-2026-06-21.md` | 前端产品化主规范（原在 `docs/specs/`） |
| `FRONTEND_WORKFLOW.md` | 前端开发工作流 |
| `AIASK V1前端从零重建详细开发方案-2026-06-21.md` | 阶段拆解方案 |
| `AIASK_V1前端功能完成度报告-2026-06-22.md` | 阶段性完成度报告 |

## 阅读须知

- 文中出现的 `desktop/src/**`、`desktop/package.json` 等路径在本仓库中不存在。
- 文中出现的 `/v1/desktop/*` 端点已全部下线。
- 报告里的"P0–P3 100%"是上游历史结论，**不代表**本仓库的就绪度。
