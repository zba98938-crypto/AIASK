# 取消订阅更新unsubscribe_hq

> 来源: https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h112vh7jtsms.html

取消订阅更新unsubscribe_hq

#  取消订阅股票实时更新

### 
```pythonunsubscribe_hq(stock_list: List[str] = []):

```
1
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| stock_list | Y | List[str] | 证券代码 |
- 订阅股票更新 传入回调函数，订阅的股票有更新时，系统会调用回调函数，最多订阅100条
- 回调函数格式定义为on_data(datas) datas格式为 {"Code":"XXXXXX.XX","ErrorId":"0"}
 接口使用

### 
```pythonfrom tqcenter import tq
tq.initialize(__file__)
un_sub_ptr = tq.unsubscribe_hq(stock_list=['688318.SH'])
print(un_sub_ptr)

```
1
2
3
4
 数据样本

### 
```text{
   "Error" : "取消全部订阅更新失败.",
   "ErrorId" : "0",
   "run_id" : "1"
}

```
1
2
3
4
5


 ← [
        订阅行情subscribe_hq
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h1104d65vr68.html)[
        获得订阅列表get_subscribe_hq_stock_list
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h1137r4k2mas.html) →