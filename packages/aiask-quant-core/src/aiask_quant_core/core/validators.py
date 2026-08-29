"""
数据验证器（使用Pydantic）
确保返回数据的完整性和正确性
"""

import logging
import os
from typing import Any, Optional
from pydantic import BaseModel, Field, ConfigDict, field_validator, model_validator

_logger = logging.getLogger(__name__)


class StockQuote(BaseModel):
    """股票行情数据模型"""

    code: str = Field(..., description="股票代码")
    name: str = Field(default="", description="股票名称")
    price: Optional[float] = Field(None, description="当前价格")
    change: Optional[float] = Field(None, description="涨跌额")
    changePercent: Optional[float] = Field(None, description="涨跌幅(%)")
    open: Optional[float] = Field(None, description="开盘价")
    high: Optional[float] = Field(None, description="最高价")
    low: Optional[float] = Field(None, description="最低价")
    preClose: Optional[float] = Field(None, description="昨收价")
    volume: Optional[int] = Field(None, description="成交量")
    amount: Optional[float] = Field(None, description="成交额")
    turnoverRate: Optional[float] = Field(None, description="换手率")
    source: str = Field(default="unknown", description="数据源")

    model_config = ConfigDict(extra='allow')

    @field_validator('price')
    @classmethod
    def price_must_be_positive(cls, v):
        """价格必须大于0"""
        if v is not None and v <= 0:
            raise ValueError('价格必须大于0')
        return v

    @field_validator('volume', mode='before')
    @classmethod
    def convert_volume_to_int(cls, v):
        """将成交量转换为整数（处理浮点数输入）"""
        if v is None:
            return None
        try:
            # 如果是浮点数，转换为整数
            return int(float(v))
        except (ValueError, TypeError):
            return None

    @field_validator('volume')
    @classmethod
    def volume_must_be_non_negative(cls, v):
        """成交量不能为负"""
        if v is not None and v < 0:
            raise ValueError('成交量不能为负')
        return v

    @field_validator('amount')
    @classmethod
    def amount_must_be_non_negative(cls, v):
        """成交额不能为负"""
        if v is not None and v < 0:
            raise ValueError('成交额不能为负')
        return v


class KlineData(BaseModel):
    """K线数据模型"""

    date: str = Field(..., description="日期")
    open: Optional[float] = Field(None, description="开盘价")
    close: Optional[float] = Field(None, description="收盘价")
    high: Optional[float] = Field(None, description="最高价")
    low: Optional[float] = Field(None, description="最低价")
    volume: Optional[int] = Field(None, description="成交量")
    amount: Optional[float] = Field(None, description="成交额")
    turnover: Optional[float] = Field(None, description="换手率(%)")
    change_pct: Optional[float] = Field(None, description="涨跌幅(%)")
    source: str = Field(default="unknown", description="数据源")

    model_config = ConfigDict(extra='allow')

    @field_validator('open', 'close', 'high', 'low')
    @classmethod
    def price_must_be_positive(cls, v):
        """价格必须大于0"""
        if v is not None and v <= 0:
            raise ValueError('价格必须大于0')
        return v

    @field_validator('volume', mode='before')
    @classmethod
    def convert_volume_to_int(cls, v):
        """将成交量转换为整数（处理浮点数输入）"""
        if v is None:
            return None
        try:
            # 如果是浮点数，转换为整数
            return int(float(v))
        except (ValueError, TypeError):
            return None

    @field_validator('volume')
    @classmethod
    def volume_must_be_non_negative(cls, v):
        """成交量不能为负"""
        if v is not None and v < 0:
            raise ValueError('成交量不能为负')
        return v

    @model_validator(mode='after')
    def validate_ohlc_relationships(self):
        """确保 high/low 与 open/close 之间满足基本 OHLC 约束。"""
        upper_bounds = [value for value in (self.open, self.close, self.low) if value is not None]
        if self.high is not None and upper_bounds and self.high < max(upper_bounds):
            raise ValueError('最高价不能低于开盘价、收盘价或最低价')

        lower_bounds = [value for value in (self.open, self.close, self.high) if value is not None]
        if self.low is not None and lower_bounds and self.low > min(lower_bounds):
            raise ValueError('最低价不能高于开盘价、收盘价或最高价')

        return self


def validate_quote(data: dict) -> StockQuote:
    """
    验证行情数据

    Args:
        data: 原始数据字典

    Returns:
        验证后的StockQuote对象

    Raises:
        ValueError: 数据验证失败
    """
    try:
        return StockQuote(**data)
    except Exception as e:
        raise ValueError(f"行情数据验证失败: {e}")


def _is_chinese_index_code(code: object) -> bool:
    """Detect Chinese A-share index codes that need numeric sanity range check.

    FIX-10 (2026-06-02): 指数 K 线在本系统中**始终以带市场前缀的代码入库**
    （如 ``sh000001`` / ``sz399006``，见 storage.sqlite.kline.get_index_klines 文档），
    以避免与个股代码冲突。因此**裸 6 位代码一律视为个股**，不再套用指数价格区间。

    历史 bug：旧实现把裸 ``000001`` 等纳入 bare_index 集合，导致深市个股
    000001（平安银行，股价≈11 元）入库时被当成上证指数、按 [1000,30000] 拒绝
    （dead-letter 持续证据）。修复后仅对显式带市场前缀的指数代码做区间校验，
    个股 000001.SZ 不再被误判。

    判定保留：``sh000``（上证系列指数）/ ``sz399``（深证系列指数）/ ``index_`` 前缀。
    """
    if not code:
        return False
    text = str(code).strip().lower()
    # 仅显式市场前缀的指数代码参与区间校验；裸 6 位代码视为个股，不在此拦截。
    if text.startswith("sh000") or text.startswith("sz399") or text.startswith("index_"):
        return True
    return False


def _check_index_close_in_range(code: object, close: object) -> bool:
    """Validate that index close falls in 1000-30000 range; reject mojibake/wrong-symbol writes.

    2026-05-28: 上限从 15000 放宽到 30000,因为深证成指/创业板等指数已突破 15000;
    主要目的还是拦截 sh000001 (上证) close=10.68 这种 cross-symbol 污染。
    """
    if not _is_chinese_index_code(code):
        return True
    try:
        close_value = float(close)
    except (TypeError, ValueError):
        return False
    return 1000.0 <= close_value <= 30000.0


def validate_kline(data: dict) -> KlineData:
    """
    验证K线数据

    Args:
        data: 原始数据字典

    Returns:
        验证后的KlineData对象

    Raises:
        ValueError: 数据验证失败
    """
    try:
        validated = KlineData(**data)
    except Exception as e:
        raise ValueError(f"K线数据验证失败: {e}")

    # P0-2 fix (诊断报告 §2.5): 指数代码数值合理性护栏
    # 历史问题: sh000001 错位写入 000001 平安银行的 close=10.68 (真实上证应在 1000-10000)
    # 在写入 db 前直接拦截,杜绝 silent corruption
    code_value = getattr(validated, "code", None) or data.get("code")
    close_value = getattr(validated, "close", None)
    if not _check_index_close_in_range(code_value, close_value):
        raise ValueError(
            f"index_close_out_of_range: code={code_value!r} close={close_value!r} "
            "expected [1000, 30000]; possible cross-symbol contamination"
        )
    return validated


class ValidatedKlineRows(list):
    """携带批量验证摘要的结果列表。"""

    def __init__(self, rows: list[dict], report: dict):
        super().__init__(rows)
        self.validation_report = report


def validate_kline_list(
    data_list: list,
    *,
    strict: bool = False,
    return_report: bool = False,
    min_accept_ratio: Optional[float] = None,
) -> list | dict:
    """
    批量验证K线数据

    Args:
        data_list: K线数据列表

    Returns:
        验证后的数据列表（跳过无效数据）
    """
    validated = []
    rejected: list[dict[str, Any]] = []
    total_count = len(list(data_list or []))
    threshold = min_accept_ratio
    if threshold is None:
        try:
            threshold = float(os.getenv("KLINE_MIN_ACCEPT_RATIO", "0.8"))
        except Exception:
            threshold = 0.8
    threshold = max(0.0, min(float(threshold), 1.0))

    for i, data in enumerate(data_list):
        try:
            validated_data = validate_kline(data)
            validated.append(validated_data.model_dump())
        except ValueError as e:
            _logger.warning("K线数据第%d条验证失败: %s", i, e)
            rejected.append({
                "index": i,
                "reason": str(e),
                "row": data if isinstance(data, dict) else {"raw": data},
            })

    accepted_count = len(validated)
    rejected_count = len(rejected)
    accept_ratio = accepted_count / total_count if total_count > 0 else 1.0
    report = {
        "accepted": validated,
        "accepted_count": accepted_count,
        "rejected": rejected,
        "rejected_count": rejected_count,
        "total_count": total_count,
        "accept_ratio": round(float(accept_ratio), 6),
        "minimum_quality_threshold": threshold,
        "minimum_quality_passed": bool(accept_ratio >= threshold),
    }

    if strict and rejected_count > 0:
        first_reason = str(rejected[0].get("reason") or "unknown_validation_error")
        raise ValueError(
            f"K线数据批量验证失败: rejected_count={rejected_count}, accepted_count={accepted_count}, first_error={first_reason}"
        )

    if return_report:
        return report
    return ValidatedKlineRows(validated, report)
