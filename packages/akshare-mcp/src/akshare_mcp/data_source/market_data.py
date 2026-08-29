"""
数据源管理 - 市场数据方法

包含 get_trading_dates、get_ipo_info、get_cb_info、get_gb_info 等。
数据源优先级: 本地 TDX vipdoc → Tushare Pro → AKShare（``TDX_LOCAL_ONLY=1`` 时仅 TDX）。
"""

import datetime
import logging
import os

from ..utils import normalize_code, parse_numeric, safe_float, safe_int, safe_stderr_print

logger = logging.getLogger(__name__)

try:
    import akshare as ak
except ImportError:
    ak = None

from .tdx_local import get_tdx_local_source as _get_tdx_local
from . import tdx_tqcenter as _tqcenter


def _tdx_local_only() -> bool:
    return os.environ.get("TDX_LOCAL_ONLY", "0") == "1"


def _keep_legacy_fallback() -> bool:
    return os.environ.get("DATA_SOURCE_KEEP_LEGACY_FALLBACK", "0") == "1"


class MarketDataMixin:
    """市场数据 Mixin（交易日历、IPO、可转债、股本）"""

    @staticmethod
    def _build_quality_flags(*, success: bool, fallback_used: bool, fallback_reason: str | None) -> list[str]:
        flags: list[str] = []
        if fallback_used:
            flags.append("fallback")
        if not success:
            flags.append("degraded")
        if fallback_reason == "all_backends_failed":
            flags.append("failed")
        elif str(fallback_reason or "").startswith("invalid_"):
            flags.append("invalid_request")
        return flags

    def _fallback_meta_result(
        self,
        *,
        success: bool,
        data,
        source: str,
        message: str,
        backend_requested: str,
        backend_used: str,
        fallback_used: bool,
        fallback_reason: str | None,
        started_at: datetime.datetime,
    ) -> dict:
        now = datetime.datetime.now()
        latency_ms = round(max((now - started_at).total_seconds() * 1000, 0.0), 3)
        asof_time = now.astimezone().isoformat()
        return {
            "success": success,
            "data": data,
            "source": source,
            "message": message,
            "asof_time": asof_time,
            "freshness_sec": 0.0,
            "quality_flags": self._build_quality_flags(
                success=success,
                fallback_used=bool(fallback_used),
                fallback_reason=fallback_reason,
            ),
            "backend_requested": backend_requested,
            "backend_used": backend_used,
            "fallback_used": bool(fallback_used),
            "fallback_reason": fallback_reason,
            "latency_ms": latency_ms,
        }

    def _result_with_requested_backend(
        self,
        *,
        success: bool,
        data,
        source: str,
        message: str,
        backend_requested: str,
        backend_used: str,
        started_at: datetime.datetime,
    ) -> dict:
        return self._fallback_meta_result(
            success=success,
            data=data,
            source=source,
            message=message,
            backend_requested=backend_requested,
            backend_used=backend_used,
            fallback_used=backend_used != backend_requested,
            fallback_reason=None if success or backend_used == backend_requested else "all_backends_failed",
            started_at=started_at,
        )

    def _failed_fallback_result(
        self,
        *,
        data,
        message: str,
        backend_requested: str,
        backend_used: str = "none",
        fallback_reason: str = "all_backends_failed",
        started_at: datetime.datetime,
    ) -> dict:
        return self._fallback_meta_result(
            success=False,
            data=data,
            source="none",
            message=message,
            backend_requested=backend_requested,
            backend_used=backend_used,
            fallback_used=backend_requested != backend_used,
            fallback_reason=fallback_reason,
            started_at=started_at,
        )


    def get_trading_dates(
        self,
        market: str = "SH",
        start_time: str = "",
        end_time: str = "",
        count: int = -1
    ) -> dict:
        """
        获取交易日历

        Args:
            market: 市场代码 (暂固定为SH)
            start_time: 起始日期 (格式: YYYYMMDD)
            end_time: 结束日期 (格式: YYYYMMDD)
            count: 返回最近的count个交易日，-1表示全部

        Returns:
            dict: {"success": bool, "data": list, "source": str, "message": str}
        """
        started_at = datetime.datetime.now()
        backend_requested = "tushare_pro"

        def _valid_yyyymmdd(value: str) -> bool:
            if not value:
                return True
            if len(value) != 8 or not value.isdigit():
                return False
            try:
                datetime.datetime.strptime(value, "%Y%m%d")
                return True
            except ValueError:
                return False

        # 参数校验
        if not _valid_yyyymmdd(start_time):
            return self._failed_fallback_result(
                data=[],
                message="start_time 格式错误，应为 YYYYMMDD",
                backend_requested=backend_requested,
                fallback_reason="invalid_start_time",
                started_at=started_at,
            )
        if not _valid_yyyymmdd(end_time):
            return self._failed_fallback_result(
                data=[],
                message="end_time 格式错误，应为 YYYYMMDD",
                backend_requested=backend_requested,
                fallback_reason="invalid_end_time",
                started_at=started_at,
            )
        if start_time and end_time and start_time > end_time:
            return self._failed_fallback_result(
                data=[],
                message="start_time 不能晚于 end_time",
                backend_requested=backend_requested,
                fallback_reason="invalid_time_range",
                started_at=started_at,
            )
        if count == 0 or count < -1:
            return self._failed_fallback_result(
                data=[],
                message="count 仅支持 -1 或正整数",
                backend_requested=backend_requested,
                fallback_reason="invalid_count",
                started_at=started_at,
            )

        effective_end_time = end_time
        if count > 0 and not effective_end_time:
            effective_end_time = datetime.datetime.now().strftime("%Y%m%d")

        # 0a. tqcenter 主路径（客户端在线时拿权威 SH 交易日历）
        try:
            tq_dates = _tqcenter.get_trading_dates(
                start_time=start_time,
                end_time=effective_end_time,
                count=count,
            )
            if tq_dates:
                return self._result_with_requested_backend(
                    success=True,
                    data=tq_dates,
                    source="tqcenter",
                    message=f"tqcenter 获取到 {len(tq_dates)} 个交易日",
                    backend_requested=backend_requested,
                    backend_used="tqcenter",
                    started_at=started_at,
                )
        except Exception as e:
            safe_stderr_print(f"[DataSource] tqcenter trade_dates failed: {e}")

        # 0b. 本地 TDX vipdoc 优先（基于上证指数日线日期反推）
        try:
            tdx = _get_tdx_local()
            tdx_dates = tdx.get_trading_dates(
                start_date=start_time,
                end_date=effective_end_time,
                count=count,
            )
            if tdx_dates:
                return self._result_with_requested_backend(
                    success=True,
                    data=tdx_dates,
                    source="tdx_local",
                    message=f"本地 TDX 获取到 {len(tdx_dates)} 个交易日",
                    backend_requested=backend_requested,
                    backend_used="tdx_local",
                    started_at=started_at,
                )
        except Exception as e:
            safe_stderr_print(f"[DataSource] TDX trade_dates failed: {e}")

        if _tdx_local_only() or not _keep_legacy_fallback():
            return self._failed_fallback_result(
                data=[],
                message="TDX 通道未提供交易日（tqcenter+tdx_local 均失败）",
                backend_requested=backend_requested,
                backend_used="tdx",
                fallback_reason="tdx_only_mode",
                started_at=started_at,
            )

        # 1. 优先使用 Tushare Pro
        if self.ts_pro:
            try:
                start_date = start_time if start_time else None
                end_date = effective_end_time if effective_end_time else None

                df = self.ts_pro.trade_cal(
                    exchange='SSE',
                    start_date=start_date,
                    end_date=end_date,
                    is_open='1'
                )
                if df is not None and not df.empty:
                    dates = [str(d) for d in df['cal_date'].tolist() if d]
                    if effective_end_time:
                        dates = [d for d in dates if d <= effective_end_time]
                    dates = sorted(dates)
                    if count > 0:
                        dates = dates[-count:]
                    return self._result_with_requested_backend(
                        success=True,
                        data=dates,
                        source="tushare_pro",
                        message=f"获取到 {len(dates)} 个交易日",
                        backend_requested=backend_requested,
                        backend_used="tushare_pro",
                        started_at=started_at,
                    )
            except Exception as e:
                safe_stderr_print(f"[DataSource] Tushare Pro trade_cal failed: {e}")

        # 3. 降级到 AKShare
        if ak is not None:
            try:
                df = ak.tool_trade_date_hist_sina()
                if df is not None and not df.empty:
                    dates = [d.strftime('%Y%m%d') for d in df['trade_date'].tolist()]
                    if start_time:
                        dates = [d for d in dates if d >= start_time]
                    if effective_end_time:
                        dates = [d for d in dates if d <= effective_end_time]
                    dates = sorted(dates)
                    if count > 0:
                        dates = dates[-count:]
                    return self._result_with_requested_backend(
                        success=True,
                        data=dates,
                        source="akshare",
                        message=f"获取到 {len(dates)} 个交易日",
                        backend_requested=backend_requested,
                        backend_used="akshare",
                        started_at=started_at,
                    )
            except Exception as e:
                safe_stderr_print(f"[DataSource] AKShare trade_date failed: {e}")

        return self._failed_fallback_result(
            data=[],
            message="所有数据源均失败",
            backend_requested=backend_requested,
            fallback_reason="all_backends_failed",
            started_at=started_at,
        )

    def get_ipo_info(
        self,
        ipo_type: int = 0,
        ipo_date: int = 1
    ) -> dict:
        """
        获取新股/新债申购信息

        Args:
            ipo_type: 0=新股, 1=新发债, 2=新股和新发债
            ipo_date: 0=只获取今天, 1=今天及以后

        Returns:
            dict: {"success": bool, "data": list, "source": str, "message": str}
        """
        started_at = datetime.datetime.now()
        backend_requested = "tqcenter"

        # 0. tqcenter 主路径（实测客户端 < 50ms 直接返回今天 + 未来的新股+新债）
        try:
            tq_items = _tqcenter.get_ipo_info(int(ipo_type), int(ipo_date))
            if tq_items:
                normalized: list[dict] = []
                for item in tq_items:
                    if not isinstance(item, dict):
                        continue
                    normalized.append({
                        "code": item.get("code") or "",
                        "name": item.get("name") or "",
                        "SGCode": item.get("sg_code") or "",
                        "SGDate": (item.get("sg_date") or "").replace("-", ""),
                        "SGPrice": str(item.get("sg_price")) if item.get("sg_price") is not None else "",
                        "MaxSG": item.get("max_sg"),
                        "PE_Issue": item.get("pe_issue"),
                        "set_code": item.get("set_code") or "",
                        "type": item.get("type") or "stock",
                    })
                return self._result_with_requested_backend(
                    success=True,
                    data=normalized,
                    source="tqcenter",
                    message=f"tqcenter 获取到 {len(normalized)} 条申购信息",
                    backend_requested=backend_requested,
                    backend_used="tqcenter",
                    started_at=started_at,
                )
        except Exception as e:
            safe_stderr_print(f"[DataSource] tqcenter ipo_info failed: {e}")

        if _tdx_local_only() or not _keep_legacy_fallback():
            return self._failed_fallback_result(
                data=[],
                message="tqcenter 不可用且未启用旧降级",
                backend_requested=backend_requested,
                backend_used="tqcenter",
                fallback_reason="tdx_only_mode",
                started_at=started_at,
            )

        backend_requested = "tushare_pro"

        # 1. 优先使用 Tushare Pro (新股)
        if self.ts_pro and ipo_type in (0, 2):
            try:
                df = self.ts_pro.new_share(start_date='', end_date='')
                if df is not None and not df.empty:
                    today = datetime.datetime.now().strftime('%Y%m%d')
                    if ipo_date == 0:
                        df = df[df['ipo_date'] == today]
                    else:
                        past_90 = (datetime.datetime.now() - datetime.timedelta(days=90)).strftime('%Y%m%d')
                        df = df[df['ipo_date'] >= past_90]

                    data = df.to_dict('records')
                    return self._result_with_requested_backend(
                        success=True,
                        data=data,
                        source="tushare_pro",
                        message=f"获取到 {len(data)} 条新股申购信息",
                        backend_requested=backend_requested,
                        backend_used="tushare_pro",
                        started_at=started_at,
                    )
            except Exception as e:
                safe_stderr_print(f"[DataSource] Tushare Pro new_share failed: {e}")

        # 3. 降级到 AKShare
        if ak is not None:
            try:
                results = []

                if ipo_type in (0, 2):
                    try:
                        df = ak.stock_xgsglb_em()
                        if df is not None and not df.empty:
                            for _, row in df.iterrows():
                                results.append({
                                    "code": str(row.get("股票代码", "")),
                                    "name": str(row.get("股票简称", "")),
                                    "SGDate": str(row.get("申购日期", "")).replace("-", ""),
                                    "SGPrice": str(row.get("发行价格", "")),
                                    "type": "stock"
                                })
                    except Exception:
                        pass

                if ipo_type in (1, 2):
                    try:
                        df = ak.bond_cb_jsl()
                        if df is not None and not df.empty:
                            today = datetime.datetime.now().strftime('%Y-%m-%d')
                            for _, row in df.iterrows():
                                sg_date = str(row.get("申购日期", ""))
                                if ipo_date == 0 and sg_date != today:
                                    continue
                                if ipo_date == 1 and sg_date < today:
                                    continue
                                results.append({
                                    "code": str(row.get("转债代码", "")),
                                    "name": str(row.get("转债名称", "")),
                                    "SGDate": sg_date.replace("-", ""),
                                    "SGPrice": "100.00",
                                    "type": "bond"
                                })
                    except Exception:
                        pass

                if results:
                    return self._result_with_requested_backend(
                        success=True,
                        data=results,
                        source="akshare",
                        message=f"获取到 {len(results)} 条申购信息",
                        backend_requested=backend_requested,
                        backend_used="akshare",
                        started_at=started_at,
                    )
            except Exception as e:
                safe_stderr_print(f"[DataSource] AKShare IPO info failed: {e}")

        return self._failed_fallback_result(
            data=[],
            message="所有数据源均失败",
            backend_requested=backend_requested,
            fallback_reason="all_backends_failed",
            started_at=started_at,
        )

    def get_cb_info(self, stock_code: str) -> dict:
        """
        获取可转债基础信息

        Args:
            stock_code: 可转债代码 (如 123039.SZ 或 123039)

        Returns:
            dict: {"success": bool, "data": dict, "source": str, "message": str}
        """
        started_at = datetime.datetime.now()
        backend_requested = "tqcenter"

        if not stock_code:
            return self._failed_fallback_result(
                data={},
                message="股票代码不能为空",
                backend_requested=backend_requested,
                fallback_reason="invalid_stock_code",
                started_at=started_at,
            )

        # 0. tqcenter 主路径（25 字段含转股价/强赎/回售/评级/到期日/剩余规模）
        try:
            tq_kzz = _tqcenter.get_kzz_info(stock_code)
            if tq_kzz:
                tq_data = {
                    "KZZCode": tq_kzz.get("kzz_code", ""),
                    "HSCode": tq_kzz.get("stock_code", ""),
                    "name": "",  # tqcenter 不直接给可转债名（上层 _get_stock_name 补）
                    "ZGPrice": tq_kzz.get("convert_price"),
                    "ZGDate": (tq_kzz.get("convert_date") or "").replace("-", ""),
                    "EndDate": (tq_kzz.get("end_date") or "").replace("-", ""),
                    "RestScope": tq_kzz.get("remain_size_wan"),
                    "PutBack": tq_kzz.get("putback_price"),
                    "ForceRedeem": tq_kzz.get("force_redeem_price"),
                    "EndPrice": tq_kzz.get("end_price"),
                    "ZGRate": tq_kzz.get("convert_rate"),
                    "RealValue": tq_kzz.get("real_value"),
                    "ExpireYield": tq_kzz.get("expire_yield"),
                    "KZZScore": tq_kzz.get("kzz_score", ""),
                    "HSScore": tq_kzz.get("stock_score", ""),
                    "RedeemDate": (tq_kzz.get("redeem_date") or "").replace("-", ""),
                    "RedeemPrice": tq_kzz.get("redeem_price"),
                    "PutDate": (tq_kzz.get("put_date") or "").replace("-", ""),
                    "PutPrice": tq_kzz.get("put_price"),
                    "ZGCode": tq_kzz.get("convert_code", ""),
                    "AGPrice": tq_kzz.get("stock_price"),
                    "KZZPrice": tq_kzz.get("kzz_price"),
                    "KZZYj": tq_kzz.get("premium_rate"),
                    "ZGValue": tq_kzz.get("convert_value"),
                }
                return self._result_with_requested_backend(
                    success=True,
                    data=tq_data,
                    source="tqcenter",
                    message="tqcenter 获取可转债 25 字段",
                    backend_requested=backend_requested,
                    backend_used="tqcenter",
                    started_at=started_at,
                )
        except Exception as e:
            safe_stderr_print(f"[DataSource] tqcenter kzz_info failed: {e}")

        if _tdx_local_only() or not _keep_legacy_fallback():
            return self._failed_fallback_result(
                data={},
                message="tqcenter 不可用且未启用旧降级",
                backend_requested=backend_requested,
                backend_used="tqcenter",
                fallback_reason="tdx_only_mode",
                started_at=started_at,
            )

        backend_requested = "tushare_pro"

        # 1. 优先使用 Tushare Pro
        if self.ts_pro:
            try:
                code = normalize_code(stock_code)
                suffix = ""
                if "." in str(stock_code):
                    suffix = str(stock_code).split(".")[-1].upper()
                ts_candidates = []
                if suffix in {"SH", "SZ"}:
                    ts_candidates.append(f"{code}.{suffix}")
                if code.startswith("11"):
                    ts_candidates.extend([f"{code}.SH", f"{code}.SZ"])
                else:
                    ts_candidates.extend([f"{code}.SZ", f"{code}.SH"])
                seen_candidates = set()
                ts_candidates = [item for item in ts_candidates if not (item in seen_candidates or seen_candidates.add(item))]

                for ts_code in ts_candidates:
                    df = self.ts_pro.cb_basic(ts_code=ts_code)
                    if df is None or df.empty:
                        continue
                    row = df.iloc[0]
                    data = {
                        "KZZCode": code,
                        "HSCode": str(row.get("stk_code", "") or ""),
                        "name": str(row.get("bond_short_name", "") or row.get("bond_nm", "") or ""),
                        "ZGPrice": str(row.get("conv_price", "") or ""),
                        "ZGDate": str(row.get("conv_start_date", "") or ""),
                        "EndDate": str(row.get("maturity_date", "") or ""),
                        "RestScope": str(row.get("remain_size", "") or row.get("issue_size", "") or ""),
                    }
                    return self._result_with_requested_backend(
                        success=True,
                        data=data,
                        source="tushare_pro",
                        message="获取可转债信息成功",
                        backend_requested=backend_requested,
                        backend_used="tushare_pro",
                        started_at=started_at,
                    )
            except Exception as e:
                safe_stderr_print(f"[DataSource] Tushare Pro cb_basic failed: {e}")

        # 2. 降级到 AKShare
        if ak is not None:
            try:
                code = normalize_code(stock_code)
                if hasattr(ak, "bond_zh_cov_info"):
                    try:
                        df = ak.bond_zh_cov_info(symbol=code)
                        if df is not None and not df.empty:
                            row = df.iloc[0]
                            data = {
                                "KZZCode": str(row.get("SECURITY_CODE", "") or code),
                                "HSCode": str(row.get("CONVERT_STOCK_CODE", "") or ""),
                                "name": str(row.get("SECURITY_NAME_ABBR", "") or row.get("SECURITY_NAME", "") or ""),
                                "ZGPrice": str(
                                    row.get("TRANSFER_PRICE", "")
                                    or row.get("INITIAL_TRANSFER_PRICE", "")
                                    or row.get("CONVERT_STOCK_PRICE", "")
                                    or row.get("转股价", "")
                                    or ""
                                ),
                                "ZGDate": str(row.get("TRANSFER_START_DATE", "") or row.get("转股开始日", "") or ""),
                                "EndDate": str(row.get("EXPIRE_DATE", "") or row.get("到期日期", "") or ""),
                                "RestScope": str(
                                    row.get("CURRENT_BOND_AMT", "")
                                    or row.get("CURRENT_ISSUE_AMT", "")
                                    or row.get("ACTUAL_ISSUE_SCALE", "")
                                    or row.get("剩余规模", "")
                                    or ""
                                ),
                            }
                            return self._result_with_requested_backend(
                                success=True,
                                data=data,
                                source="akshare",
                                message="获取可转债信息成功",
                                backend_requested=backend_requested,
                                backend_used="akshare",
                                started_at=started_at,
                            )
                    except Exception as inner_e:
                        safe_stderr_print(f"[DataSource] AKShare bond_zh_cov_info failed: {inner_e}")

                df = ak.bond_cb_jsl()
                if df is not None and not df.empty:
                    row = df[df['转债代码'] == code]
                    if not row.empty:
                        row = row.iloc[0]
                        data = {
                            "KZZCode": code,
                            "HSCode": str(row.get("正股代码", "")),
                            "name": str(row.get("转债名称", "")),
                            "ZGPrice": str(row.get("转股价", "")),
                            "RestScope": str(row.get("剩余规模", "") or row.get("发行规模", "")),
                        }
                        return self._result_with_requested_backend(
                            success=True,
                            data=data,
                            source="akshare",
                            message="获取可转债信息成功",
                            backend_requested=backend_requested,
                            backend_used="akshare",
                            started_at=started_at,
                        )
            except Exception as e:
                safe_stderr_print(f"[DataSource] AKShare cb_info failed: {e}")

        return self._failed_fallback_result(
            data={},
            message="所有数据源均失败",
            backend_requested=backend_requested,
            fallback_reason="all_backends_failed",
            started_at=started_at,
        )

    def get_gb_info(
        self,
        stock_code: str,
        date_list: list = None,
        count: int = 1
    ) -> dict:
        """
        获取股本数据

        Args:
            stock_code: 股票代码 (如 600519 或 600519.SH)
            date_list: 日期数组 (格式: ['YYYYMMDD', ...])，须从小到大排序
            count: 日期有效个数

        Returns:
            dict: {"success": bool, "data": list, "source": str, "message": str}
        """
        started_at = datetime.datetime.now()
        backend_requested = "tqcenter"

        if not stock_code:
            return self._failed_fallback_result(
                data=[],
                message="股票代码不能为空",
                backend_requested=backend_requested,
                fallback_reason="invalid_stock_code",
                started_at=started_at,
            )

        date_list = date_list or []

        def _normalize_query_date(value: str) -> str | None:
            s = str(value or "").strip()
            if not s:
                return None
            if len(s) >= 10 and "-" in s:
                s = s[:10].replace("-", "")
            elif len(s) >= 8:
                s = s[:8]
            return s if len(s) == 8 and s.isdigit() else None

        def _share_value_to_shares(value) -> float | None:
            numeric = parse_numeric(value)
            if numeric is None:
                return None
            return float(numeric * 10000) if numeric < 1e7 else float(numeric)

        normalized_dates = [_normalize_query_date(d) for d in date_list]
        normalized_dates = [d for d in normalized_dates if d]

        # 0. tqcenter 主路径（每日总股本/流通股本）
        try:
            target_dates = sorted(set(normalized_dates))
            if not target_dates:
                # 默认查最近一天
                today = datetime.datetime.now().strftime("%Y%m%d")
                target_dates = [today]
            tq_rows = _tqcenter.get_gb_info(stock_code, target_dates)
            if tq_rows:
                results = []
                for row in tq_rows:
                    date_str = (row.get("date") or "").replace("-", "")
                    if not date_str:
                        continue
                    results.append({
                        "Date": int(date_str),
                        "ltgb": float(row.get("float_shares") or 0.0),
                        "zgb": float(row.get("total_shares") or 0.0),
                    })
                if results:
                    if count and count > 0:
                        results = results[:int(count)]
                    return self._result_with_requested_backend(
                        success=True,
                        data=results,
                        source="tqcenter",
                        message=f"tqcenter 获取到 {len(results)} 条股本数据",
                        backend_requested=backend_requested,
                        backend_used="tqcenter",
                        started_at=started_at,
                    )
        except Exception as e:
            safe_stderr_print(f"[DataSource] tqcenter gb_info failed: {e}")

        if _tdx_local_only() or not _keep_legacy_fallback():
            return self._failed_fallback_result(
                data=[],
                message="tqcenter 不可用且未启用旧降级",
                backend_requested=backend_requested,
                backend_used="tqcenter",
                fallback_reason="tdx_only_mode",
                started_at=started_at,
            )

        backend_requested = "tushare_pro"

        def _frame_empty(frame) -> bool:
            if frame is None:
                return True
            empty = getattr(frame, "empty", None)
            if isinstance(empty, bool):
                return empty
            try:
                return len(frame) == 0
            except Exception:
                return False

        def _frame_columns(frame) -> set[str]:
            raw = getattr(frame, "columns", None)
            if raw is not None:
                try:
                    return {str(col) for col in raw}
                except Exception:
                    pass
            keys = getattr(frame, "keys", None)
            if callable(keys):
                try:
                    return {str(col) for col in keys()}
                except Exception:
                    pass
            discovered: set[str] = set()
            for candidate in ("item", "项目", "value", "值"):
                try:
                    frame[candidate]
                    discovered.add(candidate)
                except Exception:
                    continue
            return discovered

        def _frame_to_info_dict(frame) -> dict[str, object]:
            if frame is None or _frame_empty(frame):
                return {}
            columns = _frame_columns(frame)
            item_col = 'item' if 'item' in columns else ('项目' if '项目' in columns else None)
            value_col = 'value' if 'value' in columns else ('值' if '值' in columns else None)
            if item_col is not None and value_col is not None:
                try:
                    return dict(zip(frame[item_col], frame[value_col]))
                except Exception:
                    return {}
            try:
                row = frame.iloc[0]
                return {str(col): row.get(col) for col in columns}
            except Exception:
                return {}

        def _build_akshare_snapshot(info_dict: dict[str, object], *, latest_date: str) -> list[dict] | None:
            ltgb = (
                parse_numeric(info_dict.get("流通股"))
                or parse_numeric(info_dict.get("流通A股"))
                or parse_numeric(info_dict.get("A股流通股"))
                or parse_numeric(info_dict.get("流通股本"))
                or parse_numeric(info_dict.get("流通股(股)"))
                or parse_numeric(info_dict.get("float_share"))
                or parse_numeric(info_dict.get("circulating_share"))
            )
            zgb = (
                parse_numeric(info_dict.get("总股本"))
                or parse_numeric(info_dict.get("总股本(股)"))
                or parse_numeric(info_dict.get("总股本A股"))
                or parse_numeric(info_dict.get("总股本(万股)"))
                or parse_numeric(info_dict.get("total_share"))
                or parse_numeric(info_dict.get("total_shares"))
            )
            if ltgb is None and zgb is None:
                return None
            return [{
                "Date": int(latest_date),
                "ltgb": float(ltgb or 0.0),
                "zgb": float(zgb or 0.0),
            }]

        # 1. 优先使用 Tushare Pro
        if self.ts_pro:
            try:
                code = normalize_code(stock_code)
                if code.startswith('6'):
                    ts_code = f"{code}.SH"
                else:
                    ts_code = f"{code}.SZ"

                today = datetime.datetime.now()
                if normalized_dates:
                    start_date = min(normalized_dates)
                    end_date = max(normalized_dates)
                else:
                    start_date = (today - datetime.timedelta(days=365)).strftime("%Y%m%d")
                    end_date = today.strftime("%Y%m%d")

                df = self.ts_pro.daily_basic(
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=end_date,
                    fields='ts_code,trade_date,total_share,float_share'
                )
                if df is not None and not df.empty:
                    records = []
                    for _, row in df.iterrows():
                        trade_date = _normalize_query_date(row.get('trade_date'))
                        if not trade_date:
                            continue
                        ltgb = _share_value_to_shares(row.get('float_share'))
                        zgb = _share_value_to_shares(row.get('total_share'))
                        records.append({
                            "Date": int(trade_date),
                            "ltgb": ltgb or 0.0,
                            "zgb": zgb or 0.0,
                        })

                    records.sort(key=lambda item: item["Date"], reverse=True)
                    results = []
                    if normalized_dates:
                        for target in normalized_dates:
                            target_int = int(target)
                            matched = next((item for item in records if item["Date"] <= target_int), None)
                            if matched is not None:
                                results.append(dict(matched))
                    else:
                        results = records[:max(1, int(count or 1))]

                    if results:
                        return self._result_with_requested_backend(
                            success=True,
                            data=results,
                            source="tushare_pro",
                            message=f"获取到 {len(results)} 条股本数据",
                            backend_requested=backend_requested,
                            backend_used="tushare_pro",
                            started_at=started_at,
                        )
            except Exception as e:
                safe_stderr_print(f"[DataSource] Tushare Pro daily_basic failed: {e}")

        # 2. 降级到 AKShare
        if ak is not None:
            code = normalize_code(stock_code)
            today = datetime.datetime.now().strftime('%Y%m%d')

            # 2a. 东财个股信息
            try:
                df = ak.stock_individual_info_em(symbol=code)
                info_dict = _frame_to_info_dict(df)
                data = _build_akshare_snapshot(info_dict, latest_date=today)
                if data:
                    return self._result_with_requested_backend(
                        success=True,
                        data=data,
                        source="akshare",
                        message="获取到 1 条股本数据",
                        backend_requested=backend_requested,
                        backend_used="akshare",
                        started_at=started_at,
                    )
            except Exception as e:
                safe_stderr_print(f"[DataSource] AKShare stock_individual_info_em failed: {e}")

            # 2b. 巨潮 CNInfo
            try:
                df_cninfo = ak.stock_profile_cninfo(symbol=code)
                info_dict_cninfo = _frame_to_info_dict(df_cninfo)
                data_cninfo = _build_akshare_snapshot(info_dict_cninfo, latest_date=today)
                if data_cninfo:
                    return self._result_with_requested_backend(
                        success=True,
                        data=data_cninfo,
                        source="akshare",
                        message="获取到 1 条股本数据（CNInfo 快照）",
                        backend_requested=backend_requested,
                        backend_used="akshare",
                        started_at=started_at,
                    )
            except Exception as e:
                safe_stderr_print(f"[DataSource] AKShare stock_profile_cninfo failed: {e}")

            # 2c. 东财日线 daily_basic 补充（从最近行情获取股本数据）
            try:
                df_hist = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="", start_date=(datetime.datetime.now() - datetime.timedelta(days=10)).strftime('%Y%m%d'))
                if df_hist is not None and not df_hist.empty:
                    latest_row = df_hist.iloc[-1]
                    zgb = parse_numeric(latest_row.get("总股本")) or parse_numeric(latest_row.get("total_share"))
                    ltgb = parse_numeric(latest_row.get("流通股")) or parse_numeric(latest_row.get("float_share"))
                    if zgb is not None or ltgb is not None:
                        return self._result_with_requested_backend(
                            success=True,
                            data=[{"Date": int(today), "ltgb": float(ltgb or 0.0), "zgb": float(zgb or 0.0)}],
                            source="akshare",
                            message="获取到 1 条股本数据（日线补充）",
                            backend_requested=backend_requested,
                            backend_used="akshare",
                            started_at=started_at,
                        )
            except Exception as e:
                safe_stderr_print(f"[DataSource] AKShare hist fallback failed: {e}")

        return self._failed_fallback_result(
            data=[],
            message="所有数据源均失败",
            backend_requested=backend_requested,
            fallback_reason="all_backends_failed",
            started_at=started_at,
        )
