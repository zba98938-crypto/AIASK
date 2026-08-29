# 通达信量化平台 (TdxQuant) 完整索引

> 来源: https://help.tdx.com.cn/quant/
> 整理日期: 2026-05-18
> 官方正文页数: 79

## 目录

### TdxQuant概述

- [TdxQuant概述索引](./official/01-tdxquant-overview/README.md)
- [TdxQuant 简介](./official/01-tdxquant-overview/tdxquant_intro.md)
- [版本更新说明](./official/01-tdxquant-overview/version_updates.md)
- [安装Python及VSCode等开发环境](./official/01-tdxquant-overview/install_python_dev_env.md)
- [1. 安装通达信终端](./official/01-tdxquant-overview/install_tdx_terminal.md)
- [步骤分解](./official/01-tdxquant-overview/quick_start_first_strategy.md)

### 通用函数

- [通用函数索引](./official/02-general-functions/README.md)
- [通用函数](./official/02-general-functions/general_functions.md)
- [初始化initialize](./official/02-general-functions/initialize.md)
- [订阅行情subscribe_hq](./official/02-general-functions/subscribe_hq.md)
- [取消订阅更新unsubscribe_hq](./official/02-general-functions/unsubscribe_hq.md)
- [获得订阅列表get_subscribe_hq_stock_list](./official/02-general-functions/get_subscribe_hq_stock_list.md)
- [刷新行情缓存(最新snapshot和K线数据)refresh_cache](./official/02-general-functions/refresh_cache.md)
- [刷新历史K线缓存refresh_kline](./official/02-general-functions/refresh_kline.md)
- [下载特定数据文件download_file](./official/02-general-functions/download_file.md)
- [获取交易日列表get_trading_dates](./official/02-general-functions/get_trading_dates.md)
- [发送消息到通达信客户端send_message](./official/02-general-functions/send_message.md)
- [发送预警信号send_warn](./official/02-general-functions/send_warn.md)
- [发送文件到客户端send_file](./official/02-general-functions/send_file.md)
- [发送回测数据send_bt_data](./official/02-general-functions/send_bt_data.md)
- [导出多组数据到通达信客户端 print_to_tdx](./official/02-general-functions/print_to_tdx.md)
- [调用客户端功能](./official/02-general-functions/exec_to_tdx.md)

### 行情类信息

- [行情类信息索引](./official/03-market-data/README.md)
- [通达信TQ可以获取如下数据](./official/03-market-data/market_data_overview.md)
- [获取K线行情get_market_data](./official/03-market-data/get_market_data.md)
- [获取快照数据get_market_snapshot](./official/03-market-data/get_market_snapshot.md)
- [获取证券基本信息get_stock_info](./official/03-market-data/get_stock_info.md)
- [获取股票更多信息get_more_info](./official/03-market-data/get_more_info.md)
- [获取分红配送数据get_divid_factors](./official/03-market-data/get_divid_factors.md)
- [获取股票所属板块](./official/03-market-data/get_relation.md)
- [获取新股申购信息get_ipo_info](./official/03-market-data/get_ipo_info.md)
- [获取每天的股本数据get_gb_info](./official/03-market-data/get_gb_info.md)
- [根据时间段获取股本数据get_gb_info_by_date](./official/03-market-data/get_gb_info_by_date.md)

### 财务类数据

- [财务类数据索引](./official/04-financial-data/README.md)
- [财务类数据](./official/04-financial-data/financial_data_overview.md)
- [获取专业财务数据get_financial_data](./official/04-financial-data/get_financial_data.md)
- [获取指定日期专业财务数据get_financial_data_by_date](./official/04-financial-data/get_financial_data_by_date.md)
- [获取股票交易数据get_gpjy_value](./official/04-financial-data/get_gpjy_value.md)
- [获取指定日期股票交易数据get_gpjy_value_by_date](./official/04-financial-data/get_gpjy_value_by_date.md)
- [获取板块交易数据get_bkjy_value](./official/04-financial-data/get_bkjy_value.md)
- [获取指定日期板块交易数据get_bkjy_value_by_date](./official/04-financial-data/get_bkjy_value_by_date.md)
- [获取市场交易数据](./official/04-financial-data/get_scjy_value.md)
- [获取指定日期市场交易数据get_scjy_value_by_date](./official/04-financial-data/get_scjy_value_by_date.md)
- [获取股票的单个财务数据get_gp_one_data](./official/04-financial-data/get_gp_one_data.md)

### 分类/板块成份股

- [分类/板块成份股索引](./official/05-sector-constituents/README.md)
- [分类/板块成份股](./official/05-sector-constituents/sector_constituents_overview.md)
- [获取系统分类成份股get_stock_list](./official/05-sector-constituents/get_stock_list.md)
- [获取A股板块代码列表get_sector_list](./official/05-sector-constituents/get_sector_list.md)
- [获取板块成份股get_stock_list_in_sector](./official/05-sector-constituents/get_stock_list_in_sector.md)

### 自选股/自定义板块

- [自选股/自定义板块索引](./official/06-watchlist-custom-sector/README.md)
- [自选股/自定义板块](./official/06-watchlist-custom-sector/watchlist_custom_sector_overview.md)
- [获取自定义板块列表get_user_sector](./official/06-watchlist-custom-sector/get_user_sector.md)
- [添加自定义板块成份股](./official/06-watchlist-custom-sector/send_user_block.md)
- [清空自定义板块成份股](./official/06-watchlist-custom-sector/clear_sector.md)
- [创建自定义板块](./official/06-watchlist-custom-sector/create_sector.md)
- [删除自定义板块](./official/06-watchlist-custom-sector/delete_sector.md)
- [重命名自定义板块](./official/06-watchlist-custom-sector/rename_sector.md)

### ETF/可转债/期货数据

- [ETF/可转债/期货数据索引](./official/07-etf-bond-futures/README.md)
- [ETF/可转债/期货数据](./official/07-etf-bond-futures/etf_bond_futures_overview.md)
- [获取跟踪指数的ETF信息get_trackzs_etf_info](./official/07-etf-bond-futures/get_trackzs_etf_info.md)
- [获取可转债信息get_kzz_info](./official/07-etf-bond-futures/get_kzz_info.md)

### 调用通达信公式

- [调用通达信公式索引](./official/08-tdx-formula/README.md)
- [调用通达信公式](./official/08-tdx-formula/tdx_formula_overview.md)
- [格式化K线数据formula_format_data](./official/08-tdx-formula/formula_format_data.md)
- [向通达信公式设置数据formula_set_data](./official/08-tdx-formula/formula_set_data.md)
- [向通达信公式设置数据信息formula_set_data_info](./official/08-tdx-formula/formula_set_data_info.md)
- [获取公式中的设置数据formula_get_data](./official/08-tdx-formula/formula_get_data.md)
- [调用通达信公式进行计算formula_zb/xg/exp](./official/08-tdx-formula/formula_zb.md)
- [批量调用通达信公式formula_process_mul_xg/zb](./official/08-tdx-formula/formula_process_mul_xg.md)

### 交易函数

- [交易函数索引](./official/09-trading-functions/README.md)
- [交易函数](./official/09-trading-functions/trading_functions_overview.md)
- [获取资金账户句柄](./official/09-trading-functions/stock_account.md)
- [查询账户资产信息](./official/09-trading-functions/query_stock_asset.md)
- [查询账户委托信息](./official/09-trading-functions/query_stock_orders.md)
- [查询账户持仓信息](./official/09-trading-functions/query_stock_positions.md)
- [交易执行函数](./official/09-trading-functions/order_stock.md)
- [撤单](./official/09-trading-functions/cancel_order_stock.md)

### 常量枚举

- [常量枚举索引](./official/10-constants/README.md)
- [常量枚举](./official/10-constants/constants.md)

### 回测及模拟交易

- [回测及模拟交易索引](./official/11-backtesting-paper-trading/README.md)
- [什么是量化交易](./official/11-backtesting-paper-trading/backtesting_paper_trading.md)

### 场景化示例

- [场景化示例索引](./official/12-scenarios/README.md)
- [场景化示例](./official/12-scenarios/scenario_overview.md)
- [执行选股策略并加入客户端自定义板块](./official/12-scenarios/stock_selection_to_custom_sector.md)
- [订阅行情涨幅突破实时预计](./official/12-scenarios/realtime_breakout_subscription.md)
- [计算调仓信号并快速买卖](./official/12-scenarios/rebalance_signal_fast_trade.md)
- [VBT简单回测并输出图形](./official/12-scenarios/vbt_backtest_plot.md)

### 公众号文章例子

- [公众号文章例子索引](./official/13-wechat-examples/README.md)
- [通达信TQ策略介绍和应用示例](./official/13-wechat-examples/tq_strategy_intro_examples.md)
- [通达信TQ策略介绍和应用示例](./official/13-wechat-examples/wechat_20260122_strategy_examples.md)
- [打通通达信量化任督二脉：公式与Python双向数据互通闭环](./official/13-wechat-examples/wechat_20260302_formula_python_loop.md)

### 常见问题

- [常见问题索引](./official/14-faq/README.md)
- [**Q：运行的python文件可不可以随便放，不一定在PYPlugins\user目录下？**](./official/14-faq/python_file_location_faq.md)

## 原始文件

旧的扁平 Markdown 导出已原样归档到 [raw](./raw/)。
