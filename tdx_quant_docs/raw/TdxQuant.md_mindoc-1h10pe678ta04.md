# 获取指定日期市场交易数据getscjyvaluebydate

> 来源: https://help.tdx.com.cn/quant/docs/markdown/TdxQuant.md/mindoc-1h10pe678ta04.html

获取指定日期市场交易数据get_scjy_value_by_date

#  获取指定时间的市场交易数据，需要先在客户端中下载股票数据包

### 
```pythonget_scjy_value_by_date(field_list: List[str] = [],
						year: int = 0,
						mmdd: int = 0) -> Dict:

```
1
2
3
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| field_list | Y | List[str] | 字段筛选，不能为空 |
| year | Y | int | 指定年份 |
| mmdd | Y | int | 指定月日 |
- 如果year为0,mmdd为0,表示最新数据,mmdd为1,2,3...,表示倒数第2,3,4...个数据。
- 需要先在客户端中下载股票数据包
 输出数据

### 

同get_scjy_value一样。 接口使用

### 
```pythonfrom tqcenter import tq

tq.initialize(__file__)

sc_one = tq.get_scjy_value_by_date(field_list=['SC6','SC7','SC8','SC9','SC10'],year=0,mmdd=0)
print(sc_one)

```
1
2
3
4
5
6
 数据样本

### 
```text{'SC10': ['0.00', '181415.13'], 'SC6': ['-30479.00', '0.00'], 'SC7': ['-26449.00', '0.00'], 'SC8': ['31752.86', '84.22'], 'SC9': ['993000.00', '2900.00']}

```
1


 ← [
        获取市场交易数据get_scjy_value
      ](https://help.tdx.com.cn/quant/docs/markdown/TdxQuant.md/mindoc-1h10p8op6ia9g.html)[
        获取股票的单个数据(非序列)get_gp_one_data
      ](https://help.tdx.com.cn/quant/docs/markdown/TdxQuant.md/mindoc-1h10pk3rsg044.html) →