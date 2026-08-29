# AIASK 金融软件 MCP Server 集合

将国内主流金融软件封装为 MCP (Model Context Protocol) Server，通过 AIASK Agent 的 MCPAggregator 动态注册，使 Agent 可以直接查询行情、下单交易。

## 包含的 MCP Server

| Server | 底层库 | 行情 | 交易 | 状态 |
|--------|--------|------|------|------|
| 通达信 (`tongdaxin`) | pytdx | ✅ | ✅ | 可用 |
| 同花顺 (`tonghuashun`) | easytrader | ❌ | ✅ | 需 Windows |
| 东方财富 (`eastmoney`) | efinance | ✅ | ❌ | 可用 |
| MiniQMT (`qmt`) | XtQuant SDK | ✅ | ✅ | 需 QMT 客户端 |

## 快速开始

### 安装

```bash
# 安装全部
pip install -e "packages/finance-mcp-servers[all]"

# 仅安装通达信
pip install -e "packages/finance-mcp-servers[tongdaxin]"

# 仅安装东方财富
pip install -e "packages/finance-mcp-servers[eastmoney]"
```

### 配置

将 MCP Server 注册到 AIASK Agent：

```bash
# 复制示例配置
cp packages/finance-mcp-servers/mcp_servers_example.json ~/.aiask-agent/mcp_servers.json
```

或手动添加到 `~/.aiask-agent/mcp_servers.json`：

```json
{
  "mcpServers": {
    "tongdaxin": {
      "command": "python",
      "args": ["-m", "aiask_finance_mcp.tongdaxin.server"],
      "env": {"TDX_SERVER_IP": "119.147.212.81"},
      "disabled": false
    }
  }
}
```

### 环境变量

#### 通达信
```bash
TDX_SERVER_IP=119.147.212.81    # 行情服务器 IP
TDX_SERVER_PORT=7709            # 行情服务器端口
```

#### 同花顺
```bash
THS_CLIENT_PATH=/path/to/xiadan.exe  # 下单客户端路径
THS_BROKER=ths                        # 券商标识
```

#### 东方财富
```bash
# 无需配置，公开数据接口
EM_API_TOKEN=xxx  # 可选，部分高级接口需要
```

#### MiniQMT
```bash
QMT_PATH=/path/to/XtMiniQmt    # QMT 安装路径
QMT_ACCOUNT=your_account        # 交易账号
QMT_ACCOUNT_TYPE=STOCK          # 账户类型
```

## 安全说明

- 所有交易类工具标记为 `side_effect: stateful`
- 通过 AIASK Agent 的 `ActionIntentStore` 进行确认后才执行
- 默认 `finance_safe` 模式下，交易工具不可用
- 需要 `AIASK_AGENT_ENABLE_HERMES_FULL=1` 才能使用交易功能

## 测试

```bash
pytest packages/finance-mcp-servers/tests/ -v
```
