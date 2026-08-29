# 获取自定义板块列表getusersector

> 来源: https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h1hauh9inaac.html

获取自定义板块列表get_user_sector

#  获取自定义板块代码列表

### 
```pythonget_user_sector(cls) -> List:

```
1
 接口使用

### 
```pythonfrom tqcenter import tq
tq.initialize(__file__)
user_list = tq.get_user_sector()
print(user_list)
print(len(user_list))

```
1
2
3
4
5
 数据样本

### 
```text[{'Code': 'CSBK', 'Name': '测试板块'}, {'Code': 'CSBK2', 'Name': '测试板块2'}]

```
1


 ← [
        获取板块成份股get_stock_list_in_sector
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1ctuhttn72svo/mindoc-1h10r92mchgug.html)[
        添加自定义板块成份股send_user_block
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h10sec960u0c.html) →