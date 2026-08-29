# 初始化initialize

> 来源: https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1cv85e8u9nb0c.html

初始化initialize

# 
```pythoninitialize(__file__) #所有策略连接通达信客户端都必须调用此函数进行初始化

```
1
 调用方法:

### 
```pythonfrom tqcenter import tq

tq.initialize(__file__)

```
1
2
3
 注意事项:

### 

1."initialize"不可修改。
 2.该函数用于初始化，任何一个策略都必须有该函数。

 ← [
        快速开始第一个策略
      ](https://help.tdx.com.cn/quant/docs/markdown/mindoc-1cfsjkbf8f3is/mindoc-1cv7o3nje2gu8.html)[
        订阅行情subscribe_hq
      ](https://help.tdx.com.cn/quant/docs/markdown/ctx.stock.md/mindoc-1h1104d65vr68.html) →