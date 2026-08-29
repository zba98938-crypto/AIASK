"""TDX/TQCenter provider facade (split into _core/info_queries/market_queries)."""

from ._core import *  # noqa: F401,F403
from ._core import (
    _TQ_PERIOD_MAP,
    _call_with_retry,
    _format_date,
    _is_required,
    _normalize_code,
    _resolve_pyplugins_path,
    _safe_float,
    _safe_int,
    _to_bid_ask,
    _to_tq_period,
)
from .info_queries import (
    get_divid_factors,
    get_gb_info,
    get_ipo_info,
    get_kzz_info,
    get_more_info,
    get_relation,
)
from .market_queries import (
    get_bkjy_value,
    get_bkjy_value_by_date,
    get_financial_data,
    get_financial_data_by_date,
    get_gp_one_data,
    get_gpjy_value,
    get_gpjy_value_by_date,
    get_scjy_value,
    get_scjy_value_by_date,
)

def get_stock_list(market: str = "5", list_type: int = 1) -> list[dict]:
    """市场代码 ``5`` 全 A，``23`` HS300，``24`` ZZ500，``25`` ZZ1000，``28`` A500，
    ``31`` ETF，``32`` 可转债，``51`` 创业板，``52`` 科创板，``53`` 北交所，
    ``16/17/18`` 研究行业一/二/三级。详见 Dict.md。
    """
    tq = get_tq()
    if tq is None:
        return []
    try:
        result = _call_with_retry(tq.get_stock_list, market=str(market), list_type=int(list_type))
    except Exception as exc:
        logger.warning("[TdxTQ] get_stock_list fail %s: %s", market, exc)
        return []
    if not result:
        return []
    if list_type == 0:
        # 仅代码
        return [{"code": str(c), "full_code": str(c)} for c in result]
    out: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        full = str(item.get("Code", ""))
        out.append({
            "code": full.split(".")[0] if "." in full else full,
            "full_code": full,
            "name": str(item.get("Name", "")),
        })
    return out


def get_sector_list(list_type: int = 1) -> list[dict]:
    tq = get_tq()
    if tq is None:
        return []
    try:
        result = _call_with_retry(tq.get_sector_list, list_type=int(list_type))
    except Exception as exc:
        logger.warning("[TdxTQ] get_sector_list fail: %s", exc)
        return []
    if not result:
        return []
    if list_type == 0:
        return [{"block_code": str(c)} for c in result]
    return [
        {
            "block_code": str(item.get("Code", "")),
            "block_name": str(item.get("Name", "")),
        }
        for item in result if isinstance(item, dict)
    ]


def get_stock_info(code: str) -> Optional[dict]:
    """``tq.get_stock_info`` 透传：63 字段含基础财务（J_yysy 营收 / J_jly 净利润 /
    J_jyl ROE / J_mgsy EPS / J_mgjzc BVPS 等，单位多为万元）。"""
    tq = get_tq()
    if tq is None:
        return None
    stock = _normalize_code(code)
    try:
        info = _call_with_retry(tq.get_stock_info, stock_code=stock, field_list=[])
    except Exception as exc:
        logger.warning("[TdxTQ] get_stock_info fail %s: %s", code, exc)
        return None
    if not info:
        return None
    return dict(info)


def get_stock_list_in_sector(
    block_code: str,
    block_type: int = 0,
    list_type: int = 0,
) -> list[Any]:
    tq = get_tq()
    if tq is None:
        return []
    if not block_code:
        return []
    try:
        return _call_with_retry(
            tq.get_stock_list_in_sector,
            block_code=str(block_code),
            block_type=int(block_type),
            list_type=int(list_type),
        ) or []
    except Exception as exc:
        logger.warning("[TdxTQ] get_stock_list_in_sector fail %s: %s", block_code, exc)
        return []


# ---------------------------------------------------------------------------
# 15. 交易日历
# ---------------------------------------------------------------------------

def get_trading_dates(
    start_time: str = "",
    end_time: str = "",
    count: int = -1,
) -> list[str]:
    """A 股交易日列表，``YYYYMMDD`` 升序。需客户端下载上证指数 999999 盘后数据。"""
    tq = get_tq()
    if tq is None:
        return []
    try:
        dates = _call_with_retry(
            tq.get_trading_dates,
            market="SH",
            start_time=start_time,
            end_time=end_time,
            count=int(count),
        )
    except Exception as exc:
        logger.warning("[TdxTQ] get_trading_dates fail: %s", exc)
        return []
    if not isinstance(dates, list):
        return []
    return [str(d) for d in dates if d]


# ---------------------------------------------------------------------------
# 16. 公式互通 (formula_*)
# ---------------------------------------------------------------------------

def formula_zb_batch(
    formula_name: str,
    formula_arg: str,
    codes: list[str],
    period: str = "1d",
    count: int = 30,
    return_count: int = 1,
    return_date: bool = False,
    dividend_type: int = 1,
) -> dict:
    """批量调用通达信指标公式 (formula_process_mul_zb)。

    返回 ``{code: {sub_name: [values]}, "ErrorId": "0"}``。
    """
    tq = get_tq()
    if tq is None:
        return {}
    stock_list = [_normalize_code(c) for c in (codes or [])]
    if not stock_list or not formula_name:
        return {}
    try:
        return _call_with_retry(
            tq.formula_process_mul_zb,
            formula_name=str(formula_name),
            formula_arg=str(formula_arg or ""),
            return_count=int(return_count),
            return_date=bool(return_date),
            stock_list=stock_list,
            stock_period=str(period),
            count=int(count),
            dividend_type=int(dividend_type),
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] formula_process_mul_zb fail %s: %s", formula_name, exc)
        return {}


def formula_xg_batch(
    formula_name: str,
    formula_arg: str,
    codes: list[str],
    period: str = "1d",
    count: int = 30,
    return_count: int = 1,
    return_date: bool = False,
    dividend_type: int = 1,
    start_time: str = "",
    end_time: str = "",
) -> dict:
    """批量条件选股 (formula_process_mul_xg)。"""
    tq = get_tq()
    if tq is None:
        return {}
    stock_list = [_normalize_code(c) for c in (codes or [])]
    if not stock_list or not formula_name:
        return {}
    try:
        return _call_with_retry(
            tq.formula_process_mul_xg,
            formula_name=str(formula_name),
            formula_arg=str(formula_arg or ""),
            return_count=int(return_count),
            return_date=bool(return_date),
            start_time=start_time,
            end_time=end_time,
            stock_list=stock_list,
            stock_period=str(period),
            count=int(count),
            dividend_type=int(dividend_type),
        ) or {}
    except Exception as exc:
        logger.warning("[TdxTQ] formula_process_mul_xg fail %s: %s", formula_name, exc)
        return {}


# ---------------------------------------------------------------------------
# 17. download_file (10 大股东 / ETF 申赎 / 舆情 / 综合信息)
# ---------------------------------------------------------------------------

def download_tdx_file(
    code: str = "",
    down_time: str = "",
    down_type: int = 3,
) -> dict:
    """下载文件到 ``$TDX_DOWNLOAD_DIR`` (默认 ``${TDX_INSTALL_DIR}/PYPlugins/data``)。

    down_type:
    - 1 = 10 大股东 (down_time 仅取年份)
    - 2 = ETF 申赎清单 (down_time 取到日期)
    - 3 = 最近舆情 (其余参数无效)
    - 4 = 综合信息文件 (其余参数无效)

    返回 ``{ok, msg, raw}``。
    """
    tq = get_tq()
    if tq is None:
        return {"ok": False, "msg": "tqcenter unavailable", "raw": ""}
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            raw = tq.download_file(stock_code=str(code), down_time=str(down_time),
                                   down_type=int(down_type))
    except Exception as exc:
        logger.warning("[TdxTQ] download_file fail type=%s: %s", down_type, exc)
        return {"ok": False, "msg": str(exc), "raw": ""}
    raw_str = str(raw or "")
    ok = '"ErrorId":"0"' in raw_str or '"ErrorId": "0"' in raw_str
    return {"ok": ok, "msg": raw_str, "raw": raw_str}


# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------

def status() -> dict:
    """供运维 / 诊断使用。"""
    return {
        "initialized": _tq_initialized,
        "tq_available": _tq is not None,
        "pyplugins": _resolve_pyplugins_path(),
        "required": _is_required(),
    }
