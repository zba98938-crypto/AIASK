# 发送回测数据sendbtdata

> 来源: https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10vc2pot87c.html

发送回测数据send_bt_data 

#  往客户端发送指定股票的回测数据

### 
```pythonsend_bt_data(stock_code:          str  = '',
			time_list:         List[str] = [],
			data_list:         List[List[str]] = [],
			count:        int  = 1) -> Dict:

```
1
2
3
4
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| stock_code | Y | List[str] | 证券代码 |
| time_list | Y | List[str] | 时间列表 |
| data_list | N | List[List[str]] | 回测数据列表 |
| count | N | int | 有效数据个数 |
- data_list为二维List，每个子元素对应time_list的一个元素时间点，且每个子元素最多有16个有效纯数字字符串，即data_list每个子List的前16个数据为一个时间点的有效数据
- count限定入参中每个list中的有效数据个数，即每个list前count个数据会传给客户端
 接口使用

### 
```pythonfrom tqcenter import tq

tq.initialize(__file__)

bt_data = tq.send_bt_data(stock_code = '688318.SH',
                          time_list = ['20251215141115'],
                          data_list = [['11']],
                          count = 1)
print(bt_data)

```
1
2
3
4
5
6
7
8
9
 数据样本

### 
```text{'Error': '发送回测结果成功.', 'ErrorId': '0', 'run_id': '1'}

```
1


 ← [
        发送文件到客户端send_file
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h10u17ue9464.html)[
        打印数据到客户端print_to_tdx
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h62l8kg2k4jc.html) →