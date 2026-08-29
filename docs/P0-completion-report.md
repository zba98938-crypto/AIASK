# P0 阶段开发完成总结

> **Hermes 集成副本勘误（2026-07-15）**  
> 本文描述的是上游/历史 AIASK 产品化工作（含 `packages/desktop-api`、完整前端页面）。  
> **在当前 Hermes `integrations/aiask/` 副本中：**
>
> - `packages/desktop-api` **不存在**
> - 前端为 **focused workbench**（`desktop/src/views.ts` 仅 5 个视图），不是本文列举的完整页面集
> - 权威进度见仓库根 `AIASK_FINANCE_AGENT_COMPLETION_AUDIT.md`、`integrations/aiask/docs/CURRENT_PRODUCT_TRUTH.md` 与 `docs/GAP_INVENTORY_STATUS.md`
>
> 请勿把本文当作 Hermes 金融升级已完成的证明。

## 架构重构：服务解耦

### 问题识别
前端产品化功能（对话管理、个人资产、配置管理）不应塞入 MCP 服务，会导致：
- MCP 服务臃肿，职责不清
- 金融数据服务与前端状态管理混在一起
- 违反单一职责原则

### 解决方案
创建独立的 `desktop-api` 包作为前端产品化 API 层

## 新增服务：desktop-api

### 位置
```
packages/desktop-api/
├── src/desktop_api/
│   ├── main.py           # FastAPI 主应用
│   ├── database.py       # SQLite 数据库
│   ├── models.py         # Pydantic 数据模型
│   └── routes/
│       ├── threads.py        # 对话管理 5个端点
│       ├── mcp_servers.py    # MCP配置 4个端点
│       ├── skills.py         # Skills配置 4个端点
│       ├── strategies.py     # 用户策略 4个端点
│       └── stock_pools.py    # 股票池 6个端点
├── pyproject.toml
├── README.md
├── run_server.py
└── test_api.py
```

### 技术栈
- **FastAPI** - 现代 Python Web 框架
- **SQLite** - 本地数据持久化（~/.aiask/desktop.db）
- **Pydantic** - 数据验证和序列化
- **aiosqlite** - 异步数据库访问

### API 端点统计
共 **23 个 REST API 端点**，分为 5 个模块：

1. **对话管理** (5个)
   - POST /v1/threads - 创建对话
   - GET /v1/threads - 列出对话
   - GET /v1/threads/search - 搜索对话
   - PATCH /v1/threads/{id} - 更新对话
   - DELETE /v1/threads/{id} - 删除对话

2. **MCP 配置** (4个)
   - GET /v1/mcp/servers
   - POST /v1/mcp/servers
   - PATCH /v1/mcp/servers/{id}
   - DELETE /v1/mcp/servers/{id}

3. **Skills 管理** (4个)
   - GET /v1/skills
   - POST /v1/skills
   - PATCH /v1/skills/{id}
   - DELETE /v1/skills/{id}

4. **用户策略** (4个)
   - GET /v1/users/{user_id}/strategies
   - POST /v1/users/{user_id}/strategies
   - PATCH /v1/users/{user_id}/strategies/{id}
   - DELETE /v1/users/{user_id}/strategies/{id}

5. **股票池** (6个)
   - GET /v1/users/{user_id}/stock-pools
   - POST /v1/users/{user_id}/stock-pools
   - PATCH /v1/users/{user_id}/stock-pools/{id}
   - DELETE /v1/users/{user_id}/stock-pools/{id}
   - POST /v1/users/{user_id}/stock-pools/{id}/stocks
   - DELETE /v1/users/{user_id}/stock-pools/{id}/stocks/{code}

### 数据库设计
5 个表，存储在 `~/.aiask/desktop.db`：

```sql
threads          -- 对话会话（id, title, description, status, message_count, created_at, updated_at, user_id）
mcp_servers      -- MCP服务器配置（id, name, command, args, env, enabled, created_at）
skills           -- Skills配置（id, name, type, path, enabled, config, created_at）
strategies       -- 用户策略（id, name, type, description, stocks, config, status, performance, created_at, updated_at, user_id）
stock_pools      -- 股票池（id, name, description, stocks, created_at, updated_at, user_id）
```

## 前端开发完成

### 新增组件（6个）
- ✅ StatusLight.tsx - 5种状态红绿灯
- ✅ ModelSelector.tsx - 模型快速切换
- ✅ NewThreadDialog.tsx - 对话创建弹窗
- ✅ SearchAndFilter.tsx - 搜索筛选组件
- ✅ SearchBar.tsx - 搜索框
- ✅ FilterPanel.tsx - 筛选面板

### 新增页面（2个）
- ✅ MyStrategyPage.tsx - 策略管理（CRUD + 表格展示）
- ✅ MyStocksPage.tsx - 股票池管理（CRUD + 股票添加删除）

### 系统集成
- ✅ 路由配置 - 新增 personal 导航组
- ✅ API 层扩展 - 23个新端点定义
- ✅ 类型系统 - ViewId、group 类型扩展
- ✅ 前端构建成功 - 345.53 KB bundle

## 服务架构对比

### desktop-api（新服务）
- **端口**：8001
- **协议**：REST API
- **职责**：前端状态持久化、用户配置管理
- **数据**：SQLite 本地数据库
- **依赖**：无外部依赖

### akshare-mcp（原服务）
- **端口**：stdio/9000
- **协议**：MCP
- **职责**：金融数据获取、策略工厂、因子计算
- **数据**：PostgreSQL/SQLite（金融数据）
- **依赖**：strategy-factory、数据源API

**两个服务完全解耦，desktop-api 不依赖 akshare-mcp**

## 启动与测试

### 后端启动
```bash
cd packages/desktop-api
pip install -e .
python run_server.py
# 服务运行在 http://localhost:8001
```

### 前端配置
```bash
cd desktop
# 已创建 .env.local 配置文件
VITE_DESKTOP_API_URL=http://localhost:8001
```

### API 测试
```bash
cd packages/desktop-api
python test_api.py
# 测试 8 个基本流程：健康检查、对话、策略、股票池、MCP、Skills
```

## 文件清单

### 后端文件（12个）
```
packages/desktop-api/
├── pyproject.toml
├── README.md
├── run_server.py
├── test_api.py
└── src/desktop_api/
    ├── __init__.py
    ├── main.py
    ├── database.py
    ├── models.py
    └── routes/
        ├── __init__.py
        ├── threads.py
        ├── mcp_servers.py
        ├── skills.py
        ├── strategies.py
        └── stock_pools.py
```

### 前端文件（11个）
```
desktop/
├── .env.local
└── src/
    ├── components/
    │   ├── StatusLight.tsx
    │   ├── ModelSelector.tsx
    │   ├── NewThreadDialog.tsx
    │   ├── SearchAndFilter.tsx
    │   ├── SearchBar.tsx
    │   └── FilterPanel.tsx
    ├── pages/
    │   ├── AgentPages.tsx (修改)
    │   ├── MyStrategyPage.tsx
    │   └── MyStocksPage.tsx
    ├── services/
    │   └── aiaskApi.ts (扩展)
    └── types.ts (扩展)
```

## 下一步

### P0 收尾
1. 启动 desktop-api 服务
2. 运行 test_api.py 验证后端
3. 启动前端 `npm run dev`
4. 手动测试前端交互
