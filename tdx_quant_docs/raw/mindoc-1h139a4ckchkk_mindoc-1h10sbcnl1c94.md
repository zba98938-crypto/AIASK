# 清空自定义板块成份股

> 来源: https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10sbcnl1c94.html

清空自定义板块成份股

#  清空指定通达信客户端自定义板块的成份股

### 
```pythonclear_sector(block_code:str = ''):

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
clear_ptr = tq.clear_sector(block_code='CSBK')
print(clear_ptr)

```
1
2
3
4
 数据样本

### 
```text{
   "Error" : "清空CSBK板块成功",
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
        添加自定义板块成份股send_user_block
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10sec960u0c.html)[
        创建自定义板块create_sector
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10rrkuj1drs.html) →