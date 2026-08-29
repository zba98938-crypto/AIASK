"""验证 BJ 代码格式 + tqcenter 几个关键 API 的 raw 返回结构。"""
import json
import sys

TDX_PYPLUGINS = r"C:\new_tdx_test\PYPlugins\sys"
if TDX_PYPLUGINS not in sys.path:
    sys.path.insert(0, TDX_PYPLUGINS)

from tqcenter import tq

tq.initialize(__file__)

# 1. 北交所列表前 10 个，看代码区间
bj = tq.get_stock_list("53", list_type=1)
print(f"BJ count = {len(bj)}")
print(f"BJ first 10: {bj[:10]}")
codes = [(item.get("Code") if isinstance(item, dict) else item) for item in bj]
prefixes = {}
for c in codes:
    if not c:
        continue
    prefix = c.split(".")[0][:3]  # 前 3 位
    prefixes.setdefault(prefix, 0)
    prefixes[prefix] += 1
print(f"BJ prefix distribution: {prefixes}")

# 2. K 线返回，单股形态 + 多股形态
print("\n--- single-stock kline shape ---")
k1 = tq.get_market_data(field_list=[], stock_list=["600519.SH"], period="1d",
                        start_time="", end_time="", count=3,
                        dividend_type="front", fill_data=True)
for f, df in k1.items():
    print(f"  {f}: shape={df.shape} index_type={type(df.index[0]).__name__} cols={list(df.columns)}")
    print(f"     first row date={df.index[0]!r}")
    break

print("\n--- multi-stock kline shape ---")
k2 = tq.get_market_data(field_list=[], stock_list=["600519.SH", "000001.SZ"], period="1d",
                        start_time="", end_time="", count=3,
                        dividend_type="front", fill_data=True)
for f, df in k2.items():
    print(f"  {f}: shape={df.shape} index={list(df.index)[:3]} cols={list(df.columns)}")
    break

# 3. snapshot + more_info field types
print("\n--- snapshot raw ---")
snap = tq.get_market_snapshot(stock_code="600519.SH", field_list=[])
print(f"  ItemNum={snap.get('ItemNum')!r} Now={snap.get('Now')!r} LastClose={snap.get('LastClose')!r}")
print(f"  Buyp={snap.get('Buyp')!r}  Buyv={snap.get('Buyv')!r}")
print(f"  Inside={snap.get('Inside')!r} Outside={snap.get('Outside')!r}")

print("\n--- more_info 关键字段 ---")
more = tq.get_more_info(stock_code="600519.SH", field_list=[])
keys = ["ZTPrice", "DTPrice", "fHSL", "fLianB", "Zsz", "Ltsz", "ZAF",
        "StaticPE_TTM", "PB_MRQ", "DYRatio", "EverZTCount", "FCAmo",
        "MA5Value", "HisHigh", "HisLow"]
for k in keys:
    print(f"  {k} = {more.get(k)!r}")

# 4. relation
print("\n--- relation ---")
rel = tq.get_relation(stock_code="600519.SH")
print(f"  count = {len(rel)}")
print(f"  first = {rel[0]}")
print(f"  block_types = {sorted(set(r.get('BlockType','') for r in rel))}")

# 5. divid_factors structure
print("\n--- divid_factors ---")
df = tq.get_divid_factors(stock_code="600519.SH", start_time="20180101", end_time="20251231")
print(f"  type={type(df).__name__} shape={df.shape}")
print(f"  columns={list(df.columns)}")
print(f"  index={list(df.index)[:3]!r}")
print(f"  first row: {df.iloc[0].to_dict()}")
