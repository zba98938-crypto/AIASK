# 快速开始

## 1. 准备环境

- 安装客户端并下载数据：[安装通达信终端并获取数据](../official/01-tdxquant-overview/install_tdx_terminal.md)
- 安装 Python/IDE：[安装 Python 及开发环境](../official/01-tdxquant-overview/install_python_dev_env.md)
- 第一个策略示例：[快速开始第一个策略](../official/01-tdxquant-overview/quick_start_first_strategy.md)

## 2. 初始化

```python
from tqcenter import tq

tq.initialize(__file__)
```

## 3. 获取日线行情

```python
df = tq.get_market_data(
    field_list=['Open', 'High', 'Low', 'Close', 'Volume'],
    stock_list=['688318.SH'],
    period='1d',
    start_time='20250101',
    end_time='',
    count=-1,
    dividend_type='none',
    fill_data=True,
)
print(df)
```

详见：[get_market_data](../official/03-market-data/get_market_data.md)

## 4. 把选股结果写入自定义板块

```python
stocks = ['688318.SH']
tq.create_sector(block_code='', block_name='策略结果')
tq.send_user_block(block_code='', stocks=stocks)
```

相关接口：[create_sector](../official/06-watchlist-custom-sector/create_sector.md) / [send_user_block](../official/06-watchlist-custom-sector/send_user_block.md)

## 5. 实时消息与预警

```python
tq.send_message('策略运行完成')
# send_warn 适合把买卖信号推到客户端预警窗口
```

相关接口：[send_message](../official/02-general-functions/send_message.md) / [send_warn](../official/02-general-functions/send_warn.md)
