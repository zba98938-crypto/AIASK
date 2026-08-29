# 发送文件到客户端send_file

> 来源: https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10u17ue9464.html

发送文件到客户端send_file 

#  往通达信客户端发送文件名，可由TQ策略数据浏览中打开

### 
```pythonsend_file(file: str) -> Dict:

```
1
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| file | Y | str | 文件路径 |
- 文件放于 .\PYPlugins\file\ 文件夹中时，file可仅传入文件名
- 文件放于其他位置时，file需要传入绝对路径
- 目前支持的文件类型：txt，pdf，html
 接口使用

### 
```pythonfrom tqcenter import tq
tq.initialize(__file__)
file = "test.txt"
tq.send_file(file)

```
1
2
3
4


 ← [
        发送预警信号到客户端send_warn
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10u5k9qjh8o.html)[
        发送回测数据send_bt_data
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10vc2pot87c.html) →