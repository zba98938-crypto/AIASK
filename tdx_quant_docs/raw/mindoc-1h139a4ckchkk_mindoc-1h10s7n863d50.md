# 重命名自定义板块

> 来源: https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10s7n863d50.html

重命名自定义板块

#  重命名通达信客户端中的自定义板块

### 
```pythonrename_sector(block_code:str = '',
				block_name:str = ''):

```
1
2
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| block_code | Y | str | 自定义板块简称 |
| block_name | Y | str | 重命名后的自定义板块名称 |

 接口使用

### 
```pythonfrom tqcenter import tq
tq.initialize(__file__)
rename_ptr = tq.rename_sector(block_code='CSBK', block_name='测试板块重命名')
print(rename_ptr)

```
1
2
3
4
 数据样本

### 
```text{
   "Error" : "重命名CSBK板块成功",
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
        删除自定义板块delete_sector
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10s391lng6s.html)[
        跟踪指数的ETF信息get_trackzs_etf_info
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h13a594nhvb4/mindoc-1h6hknp6pjppc.html) →