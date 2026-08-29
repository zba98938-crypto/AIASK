import os
import time
from typing import Any, Optional

try:
    import akshare as ak
except ImportError:
    ak = None
import pandas as pd

from ..core.cache_manager import cached
from ..core.rate_limiter import get_limiter
from ..data_source import data_source
from ..provider_contracts import attach_tool_provider_contract_meta
from .manager_protocol import ERR_INTERNAL, ERR_PARAM, fail_with_meta, ok_with_meta
from ..utils import (
    fetch_mofcom_shrzgm_via_curl,
    format_period,
    format_publish_date,
    parse_numeric,
)


def _read_only_extra_meta(
    *,
    status: str,
    target: str,
    degraded: bool = False,
    extra_quality: dict[str, Any] | None = None,
) -> dict[str, Any]:
    quality = {"status": status}
    if isinstance(extra_quality, dict):
        quality.update(extra_quality)
    return {
        "quality": quality,
        "side_effect": {
            "level": "read_only",
            "target": target,
            "confirmation_required": False,
            "idempotent": True,
        },
        "degraded": degraded,
    }


def _with_provider_contract(result: dict, **kwargs: Any) -> dict:
    return attach_tool_provider_contract_meta(
        result,
        tool_name="get_macro_indicator",
        standard_model="MacroIndicator",
        **kwargs,
    )


def _ak_func(name: str):
    return getattr(ak, name, None) if ak is not None else None


def _get_social_financing_df() -> tuple[pd.DataFrame, list[str], bool]:
    fn = _ak_func("macro_china_shrzgm")
    try:
        if not callable(fn):
            raise RuntimeError("akshare.macro_china_shrzgm unavailable")
        return fn(), ["akshare.macro_china_shrzgm"], False
    except Exception as exc:
        try:
            return (
                fetch_mofcom_shrzgm_via_curl(),
                ["akshare.macro_china_shrzgm", "curl.mofcom.macro_china_shrzgm"],
                True,
            )
        except Exception as curl_exc:
            raise RuntimeError(f"Mofcom TLS 失败且 curl 兜底失败: {curl_exc}") from exc


def _try_tushare_macro(indicator: str, limit: int) -> Optional[tuple[dict[str, Any], str]]:
    pro = data_source.get_tushare_pro()
    if not pro:
        return None

    def _format_month(value: Any) -> str:
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        if len(text) == 6 and text.isdigit():
            return f"{text[:4]}-{text[4:6]}"
        return format_period(text)

    try:
        if indicator == "cpi":
            df = pro.cpi()
            if df is None or df.empty:
                return None
            records = []
            for _, row in df.iterrows():
                period = _format_month(row.get("month") or row.get("period"))
                value = parse_numeric(row.get("nt_val"))
                yoy = parse_numeric(row.get("nt_yoy"))
                mom = parse_numeric(row.get("nt_mom"))
                if period and value is not None:
                    records.append({"period": period, "value": value, "yoyChange": yoy, "momChange": mom, "publishDate": period})
            if not records:
                return None
            records = records[-limit:][::-1]
            return {"indicator": indicator, "records": records}, "tushare_pro.cpi"

        if indicator == "ppi":
            df = pro.ppi()
            if df is None or df.empty:
                return None
            records = []
            for _, row in df.iterrows():
                period = _format_month(row.get("month") or row.get("period"))
                value = parse_numeric(row.get("ppi"))
                yoy = parse_numeric(row.get("ppi_yoy"))
                mom = parse_numeric(row.get("ppi_mom"))
                if period and value is not None:
                    records.append({"period": period, "value": value, "yoyChange": yoy, "momChange": mom, "publishDate": period})
            if not records:
                return None
            records = records[-limit:][::-1]
            return {"indicator": indicator, "records": records}, "tushare_pro.ppi"

        if indicator in {"m2", "m2_growth"}:
            df = pro.money_supply()
            if df is None or df.empty:
                return None
            records = []
            for _, row in df.iterrows():
                period = _format_month(row.get("month") or row.get("period"))
                value = parse_numeric(row.get("m2")) if indicator == "m2" else parse_numeric(row.get("m2_yoy"))
                mom = parse_numeric(row.get("m2_mom")) if indicator == "m2_growth" else None
                if period and value is not None:
                    records.append({"period": period, "value": value, "yoyChange": None if indicator == "m2" else value, "momChange": mom, "publishDate": period})
            if not records:
                return None
            records = records[-limit:][::-1]
            return {"indicator": indicator, "records": records}, "tushare_pro.money_supply"

        if indicator == "shibor":
            df = pro.shibor()
            if df is None or df.empty:
                return None
            records = []
            for _, row in df.iterrows():
                period = format_period(row.get("date") or row.get("trade_date") or row.get("period"))
                value = parse_numeric(row.get("on")) or parse_numeric(row.get("overnight"))
                if period and value is not None:
                    records.append({"period": period, "value": value, "yoyChange": None, "momChange": None, "publishDate": period})
            if not records:
                return None
            records = records[-limit:][::-1]
            return {"indicator": indicator, "records": records}, "tushare_pro.shibor"

    except Exception:
        return None
    return None


@cached(ttl=3600.0)  # 1小时缓存，宏观数据更新频率低
def get_macro_indicator(indicator: str, limit: int = 120) -> dict:
    """
    获取宏观经济指标数据（标准化输出）

    Args:
        indicator: 指标代码，如 gdp/cpi/pmi/m2 等
        limit: 返回记录条数，默认120
    """
    limiter = get_limiter("macro", rate=3.0)  # 3次/秒
    limiter.acquire()
    started_at = time.perf_counter()
    requested_code = str(indicator or "").strip().lower() or "macro_indicator"
    source_chain = ["macro.get_indicator"]

    try:
        code = requested_code

        # 0. Try Tushare Pro first for supported macro indicators
        ts_result = _try_tushare_macro(code, min(limit, 480))
        if ts_result:
            payload, backend_source = ts_result
            return _with_provider_contract(
                ok_with_meta(
                    payload,
                    tool_name="get_macro_indicator",
                    action="query",
                    started_at=started_at,
                    source_chain=["macro.get_indicator", backend_source],
                    extra_meta=_read_only_extra_meta(
                        status="available",
                        target=code,
                        extra_quality={
                            "indicator": code,
                            "record_count": len(payload.get("records") or []),
                            "backend_used": backend_source,
                        },
                    ),
                ),
                provider_used=backend_source,
                source_chain=["macro.get_indicator", backend_source],
                data_timestamp=(payload.get("records") or [{}])[0].get("publishDate") if isinstance(payload.get("records"), list) else None,
            )

        def _unemployment_df() -> pd.DataFrame:
            fn = _ak_func("macro_china_urban_unemployment")
            if not callable(fn):
                raise RuntimeError("akshare.macro_china_urban_unemployment unavailable")
            df = fn()
            if df is None or df.empty:
                return df
            if "item" in df.columns:
                df = df[df["item"].astype(str).str.contains("失业率")]
            return df

        macro_specs = {
            "gdp": {
                "func": _ak_func("macro_china_gdp"),
                "period": "季度",
                "value": "国内生产总值-绝对值",
                "yoy": "国内生产总值-同比增长",
                "source": "akshare.macro_china_gdp",
            },
            "gdp_growth": {
                "func": _ak_func("macro_china_gdp"),
                "period": "季度",
                "value": "国内生产总值-同比增长",
                "source": "akshare.macro_china_gdp",
            },
            "cpi": {
                "func": _ak_func("macro_china_cpi"),
                "period": "月份",
                "value": "全国-同比增长",
                "mom": "全国-环比增长",
                "source": "akshare.macro_china_cpi",
            },
            "ppi": {
                "func": _ak_func("macro_china_ppi"),
                "period": "月份",
                "value": "当月同比增长",
                "source": "akshare.macro_china_ppi",
            },
            "pmi": {
                "func": _ak_func("macro_china_pmi"),
                "period": "月份",
                "value": "制造业-指数",
                "source": "akshare.macro_china_pmi",
            },
            "pmi_service": {
                "func": _ak_func("macro_china_pmi"),
                "period": "月份",
                "value": "非制造业-指数",
                "source": "akshare.macro_china_pmi",
            },
            "m2": {
                "func": _ak_func("macro_china_money_supply"),
                "period": "月份",
                "value": "货币和准货币(M2)-数量(亿元)",
                "scale": 1 / 10000,
                "source": "akshare.macro_china_money_supply",
            },
            "m2_growth": {
                "func": _ak_func("macro_china_money_supply"),
                "period": "月份",
                "value": "货币和准货币(M2)-同比增长",
                "mom": "货币和准货币(M2)-环比增长",
                "source": "akshare.macro_china_money_supply",
            },
            "social_financing": {
                "func": _get_social_financing_df,
                "period": "月份",
                "value": "社会融资规模增量",
                "scale": 1 / 10000,
                "source": "akshare.macro_china_shrzgm",
            },
            "lpr_1y": {
                "func": _ak_func("macro_china_lpr"),
                "period": "TRADE_DATE",
                "value": "LPR1Y",
                "source": "akshare.macro_china_lpr",
            },
            "lpr_5y": {
                "func": _ak_func("macro_china_lpr"),
                "period": "TRADE_DATE",
                "value": "LPR5Y",
                "source": "akshare.macro_china_lpr",
            },
            "rrr": {
                "func": _ak_func("macro_china_reserve_requirement_ratio"),
                "period": "公布时间",
                "value": "大型金融机构-调整后",
                "mom": "大型金融机构-调整幅度",
                "publish": "公布时间",
                "source": "akshare.macro_china_reserve_requirement_ratio",
            },
            "industrial_output": {
                "func": _ak_func("macro_china_industrial_production_yoy"),
                "period": "日期",
                "value": "今值",
                "publish": "日期",
                "source": "akshare.macro_china_industrial_production_yoy",
            },
            "retail_sales": {
                "func": _ak_func("macro_china_consumer_goods_retail"),
                "period": "月份",
                "value": "同比增长",
                "mom": "环比增长",
                "source": "akshare.macro_china_consumer_goods_retail",
            },
            "fixed_investment": {
                "func": _ak_func("macro_china_gdzctz"),
                "period": "月份",
                "value": "同比增长",
                "mom": "环比增长",
                "source": "akshare.macro_china_gdzctz",
            },
            "export": {
                "func": _ak_func("macro_china_exports_yoy"),
                "period": "日期",
                "value": "今值",
                "publish": "日期",
                "source": "akshare.macro_china_exports_yoy",
            },
            "import": {
                "func": _ak_func("macro_china_imports_yoy"),
                "period": "日期",
                "value": "今值",
                "publish": "日期",
                "source": "akshare.macro_china_imports_yoy",
            },
            "trade_balance": {
                "func": _ak_func("macro_china_trade_balance"),
                "period": "日期",
                "value": "今值",
                "publish": "日期",
                "source": "akshare.macro_china_trade_balance",
            },
            "fx_reserve": {
                "func": _ak_func("macro_china_fx_reserves_yearly"),
                "period": "日期",
                "value": "今值",
                "publish": "日期",
                "source": "akshare.macro_china_fx_reserves_yearly",
            },
            "usdcny": {
                "func": _ak_func("macro_china_rmb"),
                "period": "日期",
                "value": "美元/人民币_中间价",
                "mom": "美元/人民币_涨跌幅",
                "publish": "日期",
                "source": "akshare.macro_china_rmb",
            },
            "unemployment": {
                "func": _unemployment_df,
                "period": "date",
                "value": "value",
                "source": "akshare.macro_china_urban_unemployment",
            },
        }

        spec = macro_specs.get(code)
        if not spec:
            return fail_with_meta(
                f"未支持的指标: {indicator}",
                tool_name="get_macro_indicator",
                action="query",
                started_at=started_at,
                source_chain=source_chain,
                error_code=ERR_PARAM,
                extra_meta=_read_only_extra_meta(
                    status="invalid_request",
                    target=requested_code,
                    degraded=True,
                    extra_quality={"indicator": requested_code},
                ),
            )

        limit = int(limit)
        if limit <= 0:
            limit = 120
        limit = min(limit, 480)

        if not callable(spec.get("func")):
            resolved_source_chain = ["macro.get_indicator", str(spec.get("source") or f"macro.{code}")]
            return _with_provider_contract(
                ok_with_meta(
                    {
                        "indicator": code,
                        "records": [],
                        "degraded": True,
                        "fallback_reason": f"provider unavailable: {resolved_source_chain[-1]}",
                    },
                    tool_name="get_macro_indicator",
                    action="query",
                    started_at=started_at,
                    source_chain=resolved_source_chain,
                    extra_meta=_read_only_extra_meta(
                        status="empty",
                        target=code,
                        degraded=True,
                        extra_quality={
                            "indicator": code,
                            "record_count": 0,
                            "backend_used": "none",
                            "fallback_reason": f"provider unavailable: {resolved_source_chain[-1]}",
                        },
                    ),
                ),
                provider_used="none",
                source_chain=resolved_source_chain,
                fallback_reason=f"provider unavailable: {resolved_source_chain[-1]}",
            )

        df_result = spec["func"]()
        degraded = False
        resolved_source_chain = ["macro.get_indicator", str(spec.get("source") or f"macro.{code}")]
        if isinstance(df_result, tuple):
            df, fallback_chain, degraded = df_result
            resolved_source_chain = ["macro.get_indicator", *fallback_chain]
        else:
            df = df_result
        if df is None or df.empty:
            return fail_with_meta(
                f"指标 {indicator} 数据为空",
                tool_name="get_macro_indicator",
                action="query",
                started_at=started_at,
                source_chain=resolved_source_chain,
                error_code=ERR_INTERNAL,
                extra_meta=_read_only_extra_meta(
                    status="empty",
                    target=code,
                    degraded=degraded,
                    extra_quality={"indicator": code},
                ),
            )

        period_col = spec["period"]
        value_col = spec["value"]
        yoy_col = spec.get("yoy")
        mom_col = spec.get("mom")
        publish_col = spec.get("publish")
        scale = spec.get("scale")

        records: list[dict[str, Any]] = []
        for _, row in df.iterrows():
            period = format_period(row.get(period_col))
            if not period:
                continue
            value = parse_numeric(row.get(value_col))
            if value is None:
                continue
            if isinstance(scale, (int, float)) and scale != 1:
                value = value * float(scale)

            yoy = parse_numeric(row.get(yoy_col)) if yoy_col else None
            mom = parse_numeric(row.get(mom_col)) if mom_col else None
            publish = format_publish_date(row.get(publish_col) if publish_col else None, period)

            records.append(
                {
                    "period": period,
                    "value": value,
                    "yoyChange": yoy,
                    "momChange": mom,
                    "publishDate": publish,
                }
            )

        if not records:
            return fail_with_meta(
                f"指标 {indicator} 无有效数据",
                tool_name="get_macro_indicator",
                action="query",
                started_at=started_at,
                source_chain=resolved_source_chain,
                error_code=ERR_INTERNAL,
                extra_meta=_read_only_extra_meta(
                    status="empty",
                    target=code,
                    degraded=degraded,
                    extra_quality={"indicator": code},
                ),
            )

        records = sorted(records, key=lambda x: str(x.get("period") or ""))
        records = records[-limit:]
        records.reverse()

        return _with_provider_contract(
            ok_with_meta(
                {
                    "indicator": code,
                    "records": records,
                },
                tool_name="get_macro_indicator",
                action="query",
                started_at=started_at,
                source_chain=resolved_source_chain,
                extra_meta=_read_only_extra_meta(
                    status="available",
                    target=code,
                    degraded=degraded,
                    extra_quality={
                        "indicator": code,
                        "record_count": len(records),
                        "backend_used": resolved_source_chain[-1],
                    },
                ),
            ),
            provider_used=resolved_source_chain[-1],
            source_chain=resolved_source_chain,
            data_timestamp=records[0].get("publishDate") if records else None,
        )
    except Exception as e:
        return _with_provider_contract(
            fail_with_meta(
                e,
                tool_name="get_macro_indicator",
                action="query",
                started_at=started_at,
                source_chain=source_chain,
                error_code=ERR_INTERNAL,
                extra_meta=_read_only_extra_meta(
                    status="failed",
                    target=requested_code,
                    degraded=True,
                    extra_quality={"indicator": requested_code},
                ),
            ),
            provider_used="none",
            source_chain=source_chain,
            fallback_reason=str(e),
        )



# 兼容旧测试与历史调用：保留 _try_akshare_macro 入口
# 语义：优先走当前统一实现；若失败则返回 None（与旧行为兼容）。
def _try_akshare_macro(indicator: str, limit: int = 120) -> Optional[dict]:
    try:
        result = get_macro_indicator(indicator=indicator, limit=limit)
        if isinstance(result, dict) and result.get("success"):
            return result
    except Exception:
        pass
    return None

def register(mcp):
    mcp.tool()(get_macro_indicator)
