# 获得订阅列表getsubscribehqstocklist

> 来源: https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h1137r4k2mas.html

获得订阅列表get_subscribe_hq_stock_list

#  获得当前策略订阅的股票列表

### 
```pythonget_subscribe_hq_stock_list():

```
1
 接口使用

### 
```pythonfrom tqcenter import tq

tq.initialize(__file__)

sub_list = tq.get_subscribe_hq_stock_list()
print(sub_list)

```
1
2
3
4
5
6
 数据样本

### 
```text['600519.SH']

```
1


 ← [
        取消订阅更新unsubscribe_hq
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h112vh7jtsms.html)[
        刷新行情缓存refresh_cache
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10f9145us1g.html) →