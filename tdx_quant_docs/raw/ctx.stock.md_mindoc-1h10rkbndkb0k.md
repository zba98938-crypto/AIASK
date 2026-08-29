# 发送消息到通达信客户端send_message

> 来源: https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10rkbndkb0k.html

发送消息到通达信客户端send_message

#   发送消息给通达信客户端的TQ策略界面

### 
```pythonsend_message(msg_str: str) -> Dict:

```
1
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| msg_str | Y | str | 消息字符串 |
- 传入的字符串使用 | 可以让客户端将其分为两条（插入 \n 也可以分行显示）
 接口使用

### 
```pythonfrom tqcenter import tq
tq.initialize(__file__)
msg_str = "这是第一行. | 这是第二行. "
tq.send_message(msg_str)

```
1
2
3
4


 ← [
        获取交易日列表get_trading_dates
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10q7i3702rk.html)[
        发送预警信号到客户端send_warn
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10u5k9qjh8o.html) →