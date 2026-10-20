workspace "AIASK 当前态架构" "A-share quantitative research backend delivered as MCP servers + skills + Agent HTTP API. Current state measured 2026-10-20." {

    model {

        researcher = person "量化研究员 / 运维" "在本机启动四工厂、运行研究任务、查看证据与诊断。"
        mcpClient = softwareSystem "MCP 客户端" "Cursor / Claude / Hermes 等通过 stdio 连接 AKShare MCP 与工厂只读控制面。"
        httpClient = softwareSystem "HTTP / CLI 客户端" "调用 Agent HTTP API 或 aiask CLI；当前 Agent 进程未运行。"
        chatPlatforms = softwareSystem "IM 平台（可选）" "Discord / Slack / Telegram / Matrix 等 gateway 通道；非默认常驻。"

        aiask = softwareSystem "AIASK 后端" "单机单用户、backend-only；无前端。外部入口只有 Agent HTTP 与 MCP。" {

            agentHttp = container "Agent HTTP Runtime" "FastAPI + AgentRuntime + agent_* 工具门面 + MCPAggregator + session/audit。" "Python / FastAPI" {
                tags "Container"
            }

            akshareMcp = container "AKShare MCP Server" "179 tools（full）/ 144（tool-only）、3 resources、14 templates、7 prompts；数据源、managers、services、SQLite 实现。" "Python / FastMCP" {
                tags "Container"
            }

            strategyFactory = container "Strategy Factory" "四工厂编排、候选/门禁/提交/孵化/晋级契约、run envelope、HostAdapter。" "Python" {
                tags "Container"
            }

            quantCore = container "Quant Core" "回测引擎、因子计算、walk-forward / purged K-fold / bootstrap、风险模型、SQLite schema 与存储。" "Python library" {
                tags "Container"
            }

            fourRuntime = container "Four-Runtime Supervisor" "进程监督器：Strategy / Factor Mining / Incubation / Market Event Ingest；SignalTracker 不在其中。" "Python" {
                tags "Container"
            }

            dataMcp = container "Data MCP Facade" "aiask-data-mcp：中性入口，动态转发到 akshare-mcp，不复制实现。" "Python" {
                tags "Container"
            }

            financeMcp = container "Finance MCP Servers" "通达信、同花顺、东方财富、QMT；默认 disabled，实盘路径需 token 与确认。" "Python" {
                tags "Container,DisabledByDefault"
            }

            skills = container "Skills Bundle" "仓库 19 个 finance skills；打包 20 个 bundle。" "Markdown / Python" {
                tags "Container"
            }

            referenceHost = container "Reference Host" "最小 HostAdapter 实现，用于证明契约可独立实现。" "Python" {
                tags "Container"
            }

            runtimeDb = container "SQLite Runtime DB" "akshare_mcp.sqlite3，4.0 GB + WAL；市场、工厂、孵化、审计、会话表。" "SQLite / WAL" {
                tags "Container,Database"
            }

            taskBoard = container "Task Board DB" "strategy_factory_task_board.sqlite3；任务板与运行辅助状态。" "SQLite" {
                tags "Container,Database"
            }
        }

        marketData = softwareSystem "行情 / 财务数据源" "Eastmoney datacenter、Sina、Tencent、SSE、TDX/TQCenter；Tushare 已声明但无 token。" {
            tags "External"
        }
        llm = softwareSystem "LLM 提供方" "OpenAI / Anthropic / DeepSeek 等；用于策略生成、评审与 Agent 推理。" {
            tags "External"
        }
        broker = softwareSystem "券商交易端" "同花顺 / QMT；live trading 默认关闭。" {
            tags "External,Disabled"
        }

        researcher -> fourRuntime "启停 / 诊断"
        researcher -> agentHttp "CLI / HTTP"
        httpClient -> agentHttp "HTTP :8765（当前未监听）" "HTTPS"
        mcpClient -> akshareMcp "MCP stdio" "stdio"
        mcpClient -> strategyFactory "MCP stdio 只读控制面" "stdio"
        mcpClient -> financeMcp "MCP stdio（默认关闭）" "stdio"
        mcpClient -> skills "加载能力说明"
        chatPlatforms -> agentHttp "gateway 消息投递" "HTTPS / WebSocket"

        fourRuntime -> strategyFactory "spawn 四个 runtime 脚本"
        fourRuntime -> runtimeDb "读取运行态 / 诊断"
        dataMcp -> akshareMcp "main() 动态转发"
        referenceHost -> strategyFactory "实现 HostAdapter 契约"
        referenceHost -> skills "发现 skill bundle"

        strategyFactory -> quantCore "回测 / 存储 / 生命周期契约"
        akshareMcp -> strategyFactory "通过 api / runtime 公共面调用"
        akshareMcp -> quantCore "存储 / 回测 / 因子原语"
        agentHttp -> akshareMcp "进程内工具函数 / MCP"
        agentHttp -> strategyFactory "只读工厂视图"
        agentHttp -> quantCore "量化工具"
        agentHttp -> runtimeDb "session / run / audit"
        akshareMcp -> runtimeDb "市场 / 工厂 / 证据表"
        strategyFactory -> runtimeDb "运行账本 / 证据 / 审计"
        strategyFactory -> taskBoard "任务板"
        strategyFactory -> llm "策略生成 / 评审"
        agentHttp -> llm "模型调用"
        akshareMcp -> marketData "拉取行情 / 财务 / 公告"
        financeMcp -> broker "下单 / 撤单（受控）"

        runtimeDb -> taskBoard "同一运行时目录，不同库" "file"
    }

    views {
        systemContext aiask "SystemContext" {
            include *
            autolayout lr
            description "L1：AIASK 与用户、客户端、行情源、LLM、券商的关系。"
        }

        container aiask "Containers" {
            include *
            autolayout tb
            description "L2：容器划分。当前 Agent HTTP 未运行；SignalTracker 是 sidecar，不在四运行时 supervisor 内。"
        }

        theme default
    }
}
