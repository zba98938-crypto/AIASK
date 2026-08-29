# 创建自定义板块

> 来源: https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10rrkuj1drs.html

创建自定义板块

#  在通达信客户端中创建自定义板块

### 
```pythoncreate_sector(block_code:str = '',
				block_name:str = ''):

```
1
2
 输入参数

### 

| 参数 | 是否必选 | 参数类型 | 参数说明 |
| --- | --- | --- | --- |
| block_code | Y | str | 自定义板块简称 |
| block_name | Y | str | 自定义板块名称 |

 接口使用

### 
```pythonfrom tqcenter import tq
tq.initialize(__file__)
create_ptr = tq.create_sector(block_code='CSBK2', block_name='测试板块2')
print(create_ptr)

```
1
2
3
4
 数据样本

### 
```text{
   "Error" : "创建CSBK2板块成功",
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
        清空自定义板块成份股clear_sector
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10sbcnl1c94.html)[
        删除自定义板块delete_sector
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10s391lng6s.html) →