# 获取新股申购信息getipoinfo

> 来源: https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhthaq5qmg/mindoc-1h137jr3khrqo.html

获取新股申购信息get_ipo_info

#  获取今天及未来的新股或新发债申购信息

### 
```pythonget_ipo_info(ipo_type:int = 0,
             ipo_date:int = 0):

```
1
2
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| ipo_type | Y | str | 自定义板块简称 |
| ipo_date | Y | int | 自定义板块名称 |
- ipo_type=0 表示获取新股申购信息
- ipo_type=1 表示获取新发债信息
- ipo_type=2 表示获取新股和新发债信息
- ipo_date=0 表示只获取今天信息
- ipo_date=1 表示获取今天及以后信息
 接口使用

### 
```pythonfrom tqcenter import tq
tq.initialize(__file__)
ipo_info = tq.get_ipo_info(ipo_type=2, ipo_date=1)
print(ipo_info)

```
1
2
3
4
 数据样本

### 
```text[{'MaxSG': '0.00', 'PE_Issue': '0.00', 'SGCode': '371036', 'SGDate': '20251226', 'SGPrice': '100.00', 'code': '301036', 'name': '双乐转债', 'setcode': '0'},
{'MaxSG': '0.00', 'PE_Issue': '0.00', 'SGCode': '718676', 'SGDate': '20251225', 'SGPrice': '100.00', 'code': '688676', 'name': '金05转债', 'setcode': '1'}]

```
1
2


 ← [
        获取股票所属板块get_relation
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhthaq5qmg/mindoc-1h84ec4p26qus.html)[
        获取每天的股本数据get_gb_info
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhthaq5qmg/mindoc-1h3ru0b1tssrc.html) →