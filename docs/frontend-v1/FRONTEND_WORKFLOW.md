# 前端开发流程与原则（AIASK）


> **Hermes 集成副本勘误（2026-07-18）**  
> 本文是历史/上游阶段材料，不是当前 Hermes 树完成证明。  
> 在当前 `integrations/aiask/` 副本中：`packages/desktop-api` 不存在；Hermes Desktop 不是完整 AIASK workbench；live trading 默认关闭。  
> 权威状态：`AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md`、`docs/GAP_INVENTORY_STATUS.md`。

本文件是 **aiask 前端开发的固定流程**。每次做前端任务，先读这里。
主前端项目在 [`desktop/`](../../desktop/)（React + Vite + Tauri），其落地细节见 [`desktop/CLAUDE.md`](../../desktop/CLAUDE.md)。本文件讲**原则与流程**，desktop/CLAUDE.md 讲**该项目的具体 token / 组件 / 命令**。

---

## 一、可调用的工具与 skills（实测，2026-06-17）

只用下面这些真正能调用的，不要引用幻影工具。

| 工具 | 状态 | 用途 |
|---|---|---|
| **Claude_Preview** MCP | 本会话内置，直接可用 | 起 dev server、截图、inspect 计算样式、eval、看 console/network。**前端自查首选** |
| **playwright** MCP | ✔ 已连接 | 浏览器自动化、e2e、跨页交互测试（比 Preview 重，留给端到端） |
| **context7** MCP | ✔ 已连接 | 查 React/Radix/TanStack/lightweight-charts 等库的**实时文档**，写组件前先查 |
| **sequential-thinking** MCP | ✔ 已连接 | 复杂布局/状态拆解时的分步推理 |
| **frontend-design** skill | ✔ 已安装并可用 | 视觉方向、配色、排版、避免模板感的设计决策 |

> frontend-design 是插件，**必须用 `claude plugin install frontend-design@claude-plugins-official` 安装**（手写 `~/.claude.json` 的 enabledPlugins/marketplaces 键无效，CLI 会报 "No plugins installed"）。装完重启 Claude Code 才进 Skill 列表。playwright 是 MCP（手写 mcpServers 有效），与插件是两套机制。
> Claude_Preview 的 dev server 配置见 [`.claude/launch.json`](../../.claude/launch.json) 的 `desktop-preview`（端口 5199，避开 desktop 占用的 1420）。

---

## 二、核心原则

1. **Token 唯一来源，禁止硬编码。** 所有颜色/间距/字号/圆角/阴影走 `desktop/src/styles/globals-layout.css` 的 CSS 变量。CSS 里写死 `#fff` / `16px` / `rgba(...)` 一律视为缺陷。缺 token 就**先补 token 再用**。
2. **新增颜色 token 必须三主题块同步给值**：浅色 `:root` / 深色 `[data-aiask-theme="dark"]` / 跟随系统 `@media prefers-color-scheme:dark [data-aiask-theme="system"]`。漏一块 = 深色模式破。
3. **先查复用，再造新件。** 交互件先查 `desktop/src/ui/`（Button/Input/Dialog/Tabs/Tooltip）；金融语义件先查 `desktop/src/components/shared.tsx`（PriceDelta/MetricCard/StatusBadge/EmptyState/GatedState）。有方向的数值一律 `<PriceDelta>`。
4. **新交互件包 Radix，不写原生 sprawl。** 已装 dialog/tabs/tooltip。需要新原语先查 context7 文档再装。
5. **样式方案不换。** 原生 CSS + token，渐进迁移。**不引入** Tailwind / CSS-in-JS / CSS Modules / 状态管理库（硬约束）。
6. **触碰即改，不开大重构。** 改旧文件时顺手把触及范围的硬编码换成 token；不做一次性大爆改，不用脚本批量替换 px。
7. **金融控制台定位。** 高密度仪表盘、语义状态色、数据表格风格，不是消费级 App。
8. **质量地板默认达标。** 响应式到移动端、键盘焦点可见、尊重 reduce-motion、深色模式不破。

---

## 三、固定流程（6 步）

每个前端任务按顺序走：

1. **Brief** — 明确页面的单一职责，确认金融/量化控制台风格。需要设计方向时调 frontend-design skill（重启后）。
2. **Token 决策** — 查 `globals-layout.css` 有没有现成 token；缺则先补（三主题块都补）再用。颜色走语义层（`--text` / `--bg-panel` / `--fin-up` 而非具体 hex）。
3. **组件决策** — 先查 `src/ui/` 和 `shared.tsx`；缺通用件建 ui 原子，缺业务件进 features。写新库用法前用 **context7** 查文档。
4. **实现** — 组件配同名 `.css`，CSS 内只用 `var(--*)`。参照 `src/ui/*.css` 的纪律样板。
5. **自查截图** — 用 **Claude_Preview**：
   - `preview_start`（name: `desktop-preview`）→ `preview_screenshot` 看布局
   - **`preview_inspect` 验精确色值/字号/间距**（截图判断颜色/尺寸不可靠，inspect 取 computed style 才准）
6. **双模式验证** — `preview_eval` 执行 `document.documentElement.setAttribute('data-aiask-theme','dark')` 切深色，截图确认**无白底白卡、文字对比度达标**；再切回 light 确认无回归。compact 密度、reduce-motion 同理。

> 铁律：**第 6 步不可省**。纯 grep / 代码审查抓不全硬编码——`rgba(255,255,255,..)`、写死的标题文字色、渐变端点，只有双模式截图 + inspect 才暴露。

---

## 四、验证命令（在 desktop/ 下）

```bash
npx tsc --noEmit                              # 类型检查
npx vitest run src --environment jsdom        # 单元/组件测试
npx vitest run src/ui --environment jsdom     # 仅 ui 原子冒烟
```

新建 ui 原子要补 `*.test.tsx` 冒烟用例（渲染 + variant class + 交互），参照 `src/ui/ui.test.tsx`。

---

## 五、不做（避免过度工程）

- 不引入 Tailwind / CSS-in-JS / CSS Modules。
- 不引入 JS ThemeProvider 或多预设换肤（属性选择器够用，当前只需 light/dark/system）。
- 不照搬 Hermes 的 `@nous-research/ui` 私有包与 `color-mix()` 三元色派生（依赖 Tailwind v4，与本项目冲突）。
- 不用脚本批量替换 px（结构性 px 是有意保留，盲替会视觉回归）。
- 不引用本会话连不上或未加载的工具（如 frontend-design 重启前、tavily 断连时）。
