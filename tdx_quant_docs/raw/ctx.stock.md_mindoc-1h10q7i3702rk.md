# 获取交易日列表gettradingdates

> 来源: https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10q7i3702rk.html

获取交易日列表get_trading_dates

#  根据指定时间段获取交易日列表

### 
```pythonget_trading_dates(market: str,
				start_time: str,
				end_time: str,
				count:int = -1) -> List:

```
1
2
3
4
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| market | Y | str | 市场代码（暂固定为SH） |
| start_time | N | str | 起始日期 |
| end_time | N | str | 结束日期 |
| count | N | int | 返回最近的count个交易日 |
- 需要现在客户端下载上证指数（999999）的盘后数据 目前仅支持A股
- count > 0时，限制返回从结束日期往前最近的count个在限定时间段中的交易日
 接口使用

### 
```pythonfrom tqcenter import tq

tq.initialize(__file__)

trade_dates = tq.get_trading_dates(market = 'SH', start_time = '20220101', end_time = '', count = 10);
print(trade_dates)

```
1
2
3
4
5
6
 数据样本

### 
```text['20251211', '20251212', '20251215', '20251216', '20251217', '20251218', '20251219', '20251222', '20251223', '20251224']

```
1


 ← [
        下载特定数据文件download_file
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10pqrdlj71o.html)[
        发送消息到TQ策略界面send_message
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10rkbndkb0k.html) →