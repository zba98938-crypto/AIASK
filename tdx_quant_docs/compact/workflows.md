# 常见工作流

## 选股后加入客户端自定义板块

1. 用 `get_market_data` 或公式接口生成股票列表。
2. 用 `create_sector` 创建板块。
3. 用 `send_user_block` 写入股票。

完整示例：[执行选股策略并加入客户端自定义板块](../official/12-scenarios/stock_selection_to_custom_sector.md)

## 实时订阅并发送预警

1. 用 `subscribe_hq` 订阅不超过接口限制的股票列表。
2. 在回调中取快照或最新 K 线。
3. 满足条件时调用 `send_warn`。
4. 退出时调用 `unsubscribe_hq`。

完整示例：[订阅行情涨幅突破实时预计](../official/12-scenarios/realtime_breakout_subscription.md)

## 使用通达信公式筛选股票

小批量可用 `formula_set_data` + `formula_zb/xg/exp`；全市场筛选优先看 `formula_process_mul_xg/zb`。

公式接口：[调用通达信公式](../official/08-tdx-formula/README.md)

## 交易执行前的最小检查

1. `stock_account` 获取账户句柄。
2. `query_stock_asset` / `query_stock_positions` 确认资金和持仓。
3. `order_stock` 下单。
4. 必要时 `cancel_order_stock` 撤单。

交易接口：[交易函数](../official/09-trading-functions/README.md)

## 回测与模拟

先读概念页：[什么是量化交易](../official/11-backtesting-paper-trading/backtesting_paper_trading.md)

VBT 示例：[VBT简单回测并输出图形](../official/12-scenarios/vbt_backtest_plot.md)
