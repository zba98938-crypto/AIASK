# 获取自定义板块列表get_user_sector

> 来源: https://help.tdx.com.cn/quant/docs/markdown/mindoc-1h139a4ckchkk/mindoc-1h1hauh9inaac.html
> 栏目: 自选股/自定义板块

### 获取自定义板块代码列表

```python
get_user_sector(cls) -> List:
```

### 接口使用

```python
from tqcenter import tq
tq.initialize(__file__)
user_list = tq.get_user_sector()
print(user_list)
print(len(user_list))
```

### 数据样本

```text
[{'Code': 'CSBK', 'Name': '测试板块'}, {'Code': 'CSBK2', 'Name': '测试板块2'}]
```
