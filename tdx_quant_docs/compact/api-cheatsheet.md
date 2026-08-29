# API 速查

## 基础与缓存

| 场景 | 常用函数 | 说明 | 详页 |
| --- | --- | --- | --- |
| 初始化 | `initialize` | 每个脚本先调用 | [文档](../official/02-general-functions/initialize.md) |
| 刷新行情缓存 | `refresh_cache` | 快照/K线首次取数前可主动刷新 | [文档](../official/02-general-functions/refresh_cache.md) |
| 刷新历史K线 | `refresh_kline` | 下载指定品种、周期的历史K线 | [文档](../official/02-general-functions/refresh_kline.md) |
| 交易日 | `get_trading_dates` | 获取指定时间段交易日 | [文档](../official/02-general-functions/get_trading_dates.md) |
| 下载特定数据文件 | `download_file` | 下载股东、ETF申赎、舆情等数据文件 | [文档](../official/02-general-functions/download_file.md) |
| 导出到客户端 | `print_to_tdx` | 把多组 DataFrame 输出到客户端展示 | [文档](../official/02-general-functions/print_to_tdx.md) |
| 调用客户端功能 | `exec_to_tdx` | 让客户端按入参执行指定功能 | [文档](../official/02-general-functions/exec_to_tdx.md) |

## 行情与基础数据

| 场景 | 常用函数 | 说明 | 详页 |
| --- | --- | --- | --- |
| K线 | `get_market_data` | 历史行情主入口 | [文档](../official/03-market-data/get_market_data.md) |
| 快照 | `get_market_snapshot` | 最新行情快照 | [文档](../official/03-market-data/get_market_snapshot.md) |
| 证券基本信息 | `get_stock_info` | 基础财务/证券信息 | [文档](../official/03-market-data/get_stock_info.md) |
| 更多信息 | `get_more_info` | 股票更细节信息 | [文档](../official/03-market-data/get_more_info.md) |
| 所属板块 | `get_relation` | 查询股票所属板块 | [文档](../official/03-market-data/get_relation.md) |
| 股本 | `get_gb_info` / `get_gb_info_by_date` | 单日或时间段股本 | [文档](../official/03-market-data/get_gb_info_by_date.md) |

## 财务与特色交易数据

| 场景 | 常用函数 | 说明 | 详页 |
| --- | --- | --- | --- |
| 专业财务 | `get_financial_data` | 按区间取专业财务字段 | [文档](../official/04-financial-data/get_financial_data.md) |
| 指定日期财务 | `get_financial_data_by_date` | 按指定日期取财务字段 | [文档](../official/04-financial-data/get_financial_data_by_date.md) |
| 个股交易数据 | `get_gpjy_value` | 龙虎榜、融资融券、涨停等 GP 字段 | [文档](../official/04-financial-data/get_gpjy_value.md) |
| 市场交易数据 | `get_scjy_value` | 市场级交易数据 | [文档](../official/04-financial-data/get_scjy_value.md) |
| 板块交易数据 | `get_bkjy_value` | 板块级交易数据 | [文档](../official/04-financial-data/get_bkjy_value.md) |

## 板块与自选股

| 场景 | 常用函数 | 说明 | 详页 |
| --- | --- | --- | --- |
| 系统分类成份股 | `get_stock_list` | 按市场/分类取证券列表 | [文档](../official/05-sector-constituents/get_stock_list.md) |
| 板块列表 | `get_sector_list` | 获取 A 股板块代码 | [文档](../official/05-sector-constituents/get_sector_list.md) |
| 板块成份股 | `get_stock_list_in_sector` | 按板块代码取成份股 | [文档](../official/05-sector-constituents/get_stock_list_in_sector.md) |
| 自定义板块 | `create_sector` / `send_user_block` / `clear_sector` | 创建、写入、清空策略结果 | [文档](../official/06-watchlist-custom-sector/README.md) |

## ETF、可转债、常量与示例

| 场景 | 常用函数/栏目 | 说明 | 详页 |
| --- | --- | --- | --- |
| ETF 信息 | `get_trackzs_etf_info` | 获取跟踪指数的 ETF 信息 | [文档](../official/07-etf-bond-futures/get_trackzs_etf_info.md) |
| 可转债信息 | `get_kzz_info` | 根据可转债代码获取信息 | [文档](../official/07-etf-bond-futures/get_kzz_info.md) |
| 常量枚举 | 市场、周期、复权等常量 | 查接口入参枚举值 | [文档](../official/10-constants/constants.md) |
| 场景示例 | 场景化示例 | 选股入板块、实时预警、VBT回测等 | [文档](../official/12-scenarios/README.md) |
| 公众号长示例 | 公众号文章例子 | 更长的策略代码样例 | [文档](../official/13-wechat-examples/README.md) |

## 公式、预警与交易

| 场景 | 常用函数 | 说明 | 详页 |
| --- | --- | --- | --- |
| 单次公式计算 | `formula_set_data` / `formula_zb` | 设置数据后调用指标/选股/专家公式 | [文档](../official/08-tdx-formula/formula_zb.md) |
| 批量公式计算 | `formula_process_mul_xg` | 批量调用公式，适合全市场筛选 | [文档](../official/08-tdx-formula/formula_process_mul_xg.md) |
| 订阅行情 | `subscribe_hq` / `unsubscribe_hq` | 实时回调和取消订阅 | [文档](../official/02-general-functions/subscribe_hq.md) |
| 预警/消息 | `send_warn` / `send_message` | 客户端展示信号或消息 | [文档](../official/02-general-functions/send_warn.md) |
| 交易 | `stock_account` / `query_stock_asset` / `order_stock` / `cancel_order_stock` | 获取账户、查资产、下单、撤单 | [文档](../official/09-trading-functions/README.md) |
