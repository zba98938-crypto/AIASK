"""
数据源管理模块

全局策略：**本地通达信优先**，按以下顺序降级
    1. 本地 TDX vipdoc（mootdx Reader / struct 解析）
    2. 在线 TDX 行情服务器（pytdx）
    3. Tushare Pro / AKShare 等公开数据源（仅在 ``TDX_LOCAL_ONLY!=1`` 时启用）

向后兼容：DataSourceManager 类通过 Mixin 组合所有子模块功能。
所有外部 import（如 from ..data_source import data_source）保持不变。
"""

import os
import logging
from concurrent.futures import ThreadPoolExecutor

try:
    import tushare as ts  # 可选：仅在未启用 TDX_LOCAL_ONLY 时使用
except ImportError:  # pragma: no cover - 离线环境兼容
    ts = None

from ..env_loader import load_mcp_env
from ..tushare_whitelist import load_tushare_whitelist
from ..utils import safe_stderr_print
from .quotes import QuotesMixin
from .market_data import MarketDataMixin
from .tdx_local import get_tdx_local_source
from . import tdx_tqcenter as _tqcenter
from .tdx_tqcenter import (
    get_tq as _get_tqcenter,
    get_kline as _tqcenter_get_kline,
    get_realtime_quote as _tqcenter_get_quote,
    get_trading_dates as _tqcenter_get_trading_dates,
)

logger = logging.getLogger(__name__)


class TdxQCenterMixin:
    """把 tqcenter 12 个方法挂到 DataSourceManager 上，供上层 tools 直接调用。

    所有方法实际都是对 ``data_source.tdx_tqcenter`` 的薄代理，目的是让
    ``from ..data_source import data_source; data_source.get_more_info(...)``
    这种调用方式生效，避免每个 tool 自己 import 适配器。
    """

    # 实时行情 / 详细信息
    def get_more_info(self, code: str):
        return _tqcenter.get_more_info(code)

    def get_relation(self, code: str):
        return _tqcenter.get_relation(code)

    # 分红 / 配股
    def get_divid_factors(self, code: str, start_time: str = "", end_time: str = ""):
        return _tqcenter.get_divid_factors(code, start_time=start_time, end_time=end_time)

    # 一致预期 / 业绩预告 / 业绩快报 (GO 字段)
    def get_gp_one_data(self, codes: list, fields: list):
        return _tqcenter.get_gp_one_data(codes, fields)

    # 个股交易数据 (GP 字段)
    def get_gpjy_value(self, codes: list, fields: list,
                       start_time: str = "", end_time: str = ""):
        return _tqcenter.get_gpjy_value(codes, fields,
                                         start_time=start_time, end_time=end_time)

    def get_gpjy_value_by_date(self, codes: list, fields: list,
                                year: int = 0, mmdd: int = 0):
        return _tqcenter.get_gpjy_value_by_date(codes, fields, year=year, mmdd=mmdd)

    # 板块交易数据 (BK 字段)
    def get_bkjy_value(self, blocks: list, fields: list,
                       start_time: str = "", end_time: str = ""):
        return _tqcenter.get_bkjy_value(blocks, fields,
                                         start_time=start_time, end_time=end_time)

    def get_bkjy_value_by_date(self, blocks: list, fields: list,
                                year: int = 0, mmdd: int = 0):
        return _tqcenter.get_bkjy_value_by_date(blocks, fields, year=year, mmdd=mmdd)

    # 市场交易数据 (SC 字段)
    def get_scjy_value(self, fields: list,
                       start_time: str = "", end_time: str = ""):
        return _tqcenter.get_scjy_value(fields,
                                         start_time=start_time, end_time=end_time)

    def get_scjy_value_by_date(self, fields: list,
                                year: int = 0, mmdd: int = 0):
        return _tqcenter.get_scjy_value_by_date(fields, year=year, mmdd=mmdd)

    # 专业财务 (FN 字段)
    def get_financial_data(self, codes: list, fields: list,
                            start_time: str = "", end_time: str = "",
                            report_type: str = "announce_time"):
        return _tqcenter.get_financial_data(codes, fields,
                                             start_time=start_time,
                                             end_time=end_time,
                                             report_type=report_type)

    def get_financial_data_by_date(self, codes: list, fields: list,
                                    year: int = 0, mmdd: int = 0):
        return _tqcenter.get_financial_data_by_date(codes, fields, year=year, mmdd=mmdd)

    # 板块 / 列表
    def get_sector_list(self, list_type: int = 1):
        return _tqcenter.get_sector_list(list_type=list_type)

    def get_stock_list_in_sector(self, block_code: str,
                                  block_type: int = 0, list_type: int = 0):
        return _tqcenter.get_stock_list_in_sector(block_code,
                                                   block_type=block_type,
                                                   list_type=list_type)

    def get_tdx_stock_list(self, market: str = "5", list_type: int = 1):
        """避免与已有 get_stock_list 冲突；专属 TDX 版本。"""
        return _tqcenter.get_stock_list(market=market, list_type=list_type)

    # 公式互通
    def formula_zb_batch(self, formula_name: str, formula_arg: str,
                         codes: list, period: str = "1d", count: int = 30,
                         return_count: int = 1, return_date: bool = False,
                         dividend_type: int = 1):
        return _tqcenter.formula_zb_batch(
            formula_name=formula_name, formula_arg=formula_arg,
            codes=codes, period=period, count=count,
            return_count=return_count, return_date=return_date,
            dividend_type=dividend_type,
        )

    def formula_xg_batch(self, formula_name: str, formula_arg: str,
                         codes: list, period: str = "1d", count: int = 30,
                         return_count: int = 1, return_date: bool = False,
                         dividend_type: int = 1,
                         start_time: str = "", end_time: str = ""):
        return _tqcenter.formula_xg_batch(
            formula_name=formula_name, formula_arg=formula_arg,
            codes=codes, period=period, count=count,
            return_count=return_count, return_date=return_date,
            dividend_type=dividend_type,
            start_time=start_time, end_time=end_time,
        )

    # 文件下载（10 大股东 / ETF 申赎 / 舆情 / 综合信息）
    def download_tdx_file(self, code: str = "", down_time: str = "",
                          down_type: int = 3):
        return _tqcenter.download_tdx_file(code=code, down_time=down_time,
                                            down_type=down_type)


def _bypass_proxy_for_url(url: str) -> None:
    """将 URL 的主机名加入 NO_PROXY，避免被系统 HTTP 代理拦截。"""
    try:
        from urllib.parse import urlparse
        hostname = urlparse(url).hostname
        if not hostname:
            return
        no_proxy = os.getenv("NO_PROXY", "")
        if hostname not in no_proxy:
            updated = f"{no_proxy},{hostname}" if no_proxy else hostname
            os.environ["NO_PROXY"] = updated
            os.environ["no_proxy"] = updated
    except Exception:
        pass


class DataSourceManager(QuotesMixin, MarketDataMixin, TdxQCenterMixin):
    """
    数据源管理器 — 单例模式

    通过 Mixin 组合:
    - QuotesMixin: 实时行情、K线（tqcenter → tdx_local → 旧降级链可选）
    - MarketDataMixin: 交易日历、IPO、可转债、股本
    - TdxQCenterMixin: 12 个 tqcenter 直通方法（more_info / relation /
      gpjy / bkjy / scjy / financial_data / formula 批量 / download_file ...）
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DataSourceManager, cls).__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self):
        # 加载 .env，方便在 TDX_LOCAL_ONLY 等场景识别本地配置
        try:
            load_mcp_env(override=False, only_prefixes=("TDX_", "TUSHARE_", "AKSHARE_"))
        except Exception:
            pass

        # 本地通达信单例（优先链路）
        self.tdx_local = get_tdx_local_source()

        # Tushare 初始化采用懒加载；TDX_LOCAL_ONLY=1 时跳过
        self.tushare_token = ""
        self.tushare_http_url = ""
        self.ts_pro = None
        if not self.tdx_local.is_local_only:
            self._ensure_tushare_ready(force_reload_env=True)
        else:
            safe_stderr_print(
                "[DataSource] TDX_LOCAL_ONLY=1, skip Tushare/AkShare init; "
                f"tdx_dir={self.tdx_local.install_dir}"
            )

    def _ensure_tushare_ready(self, *, force_reload_env: bool = False):
        if self.tdx_local.is_local_only or ts is None:
            return None
        if force_reload_env or not str(self.tushare_token or "").strip():
            try:
                load_mcp_env(override=False, only_prefixes=("TUSHARE_",))
            except Exception:
                pass
            self.tushare_token = os.getenv("TUSHARE_TOKEN", "").strip()
            self.tushare_http_url = os.getenv("TUSHARE_HTTP_URL", "").strip()
        if self.ts_pro is not None or not self.tushare_token:
            return self.ts_pro
        try:
            ts.set_token(self.tushare_token)
            self.ts_pro = ts.pro_api(self.tushare_token)
            tushare_timeout = int(os.getenv("TUSHARE_TIMEOUT", "8"))
            if hasattr(self.ts_pro, "_DataApi__timeout"):
                self.ts_pro._DataApi__timeout = tushare_timeout
            if self.tushare_http_url:
                try:
                    self.ts_pro._DataApi__token = self.tushare_token
                    self.ts_pro._DataApi__http_url = self.tushare_http_url.rstrip("/")
                    _bypass_proxy_for_url(self.tushare_http_url)
                except Exception:
                    pass
        except Exception as e:
            safe_stderr_print(f"[DataSource] Tushare init failed: {e}")
            self.ts_pro = None
        return self.ts_pro

    # ---- Tushare 辅助方法 ----

    def get_tushare_pro(self):
        """获取 Tushare Pro API 实例"""
        return self._ensure_tushare_ready(force_reload_env=not bool(self.ts_pro))

    def get_tushare_http_url(self) -> str:
        """获取 Tushare HTTP URL"""
        if not self.tushare_http_url:
            self._ensure_tushare_ready(force_reload_env=True)
        return self.tushare_http_url

    def get_tushare_whitelist(self) -> set:
        """获取 Tushare 白名单"""
        return load_tushare_whitelist()


# 全局单例
data_source = DataSourceManager()


class DataSource:
    """便捷的数据源访问类，优先级 Tushare → AkShare/公开数据源"""

    def __init__(self):
        self.manager = data_source

    async def get_batch_quotes(self, codes: list[str]) -> list[dict]:
        """批量获取实时行情"""
        results = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(self.manager.get_realtime_quote, code) for code in codes]
            for future in futures:
                try:
                    quote = future.result(timeout=10)
                    if quote:
                        results.append(quote)
                except Exception as e:
                    safe_stderr_print(f"[DataSource] Batch quote failed: {e}")
        return results

    def get_quote(self, code: str) -> dict:
        """获取单只股票实时行情"""
        return self.manager.get_realtime_quote(code)

    def get_kline(self, code: str, period: str = "daily", limit: int = 100) -> list[dict]:
        """获取K线数据"""
        return self.manager.get_kline(code, period, limit)


__all__ = [
    'DataSourceManager',
    'data_source',
    'DataSource',
]
