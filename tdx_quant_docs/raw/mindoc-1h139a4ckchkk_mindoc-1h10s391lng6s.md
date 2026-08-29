# 删除自定义板块

> 来源: https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10s391lng6s.html

删除自定义板块

#  删除通达信客户端中的自定义板块

### 
```pythondelete_sector(block_code:str = ''):

```
1
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| block_code | Y | str | 自定义板块简称 |

 接口使用

### 
```pythonfrom tqcenter import tq
tq.initialize(__file__)
delete_ptr = tq.delete_sector(block_code='CSBK')
print(delete_ptr)

```
1
2
3
4
 数据样本

### 
```text{
   "Error" : "删除CSBK板块成功",
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
        创建自定义板块create_sector
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10rrkuj1drs.html)[
        重命名自定义板块rename_sector
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10s7n863d50.html) →