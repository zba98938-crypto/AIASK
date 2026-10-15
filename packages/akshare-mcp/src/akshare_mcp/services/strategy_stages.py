"""多阶段 AI 策略生成 — Stage 定义与注册表。

每个 Stage 拥有:
- 专属 system prompt（短而聚焦）
- 输入/输出 JSON schema（用于验证）
- 独立 fallback 函数（LLM 不可用时降级到本地规则引擎）
- 独立的 max_tokens / temperature 配置
"""
from __future__ import annotations

# --- strategy_stages_parts/context.py ---
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class StageDefinition:
    """一个 Pipeline 阶段的完整定义。"""

    stage_id: str
    system_prompt: str
    max_tokens: int = 500
    temperature: float = 0.2
    # 某些阶段本地规则更稳定时，可直接优先走 fallback，避免白白消耗 LLM 超时预算。
    prefer_fallback: bool = False
    # 输出中的必填顶层 key（用于快速验证）
    required_output_keys: list[str] = field(default_factory=list)
    # fallback 函数签名: (db, input_data, snapshot) -> dict
    fallback_fn: Optional[Callable[..., Coroutine[Any, Any, dict]]] = None


@dataclass
class StageResult:
    """单阶段执行结果。"""

    stage_id: str
    output: dict[str, Any]
    used_fallback: bool = False
    llm_attempted: bool = False
    prompt_chars: int = 0
    response_chars: int = 0
    elapsed_sec: float = 0.0
    error: Optional[str] = None
    llm_error: Optional[str] = None
    llm_error_type: Optional[str] = None
    llm_error_metrics: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Extended theme library (扩展到 ~20 个预定义主题)
# ---------------------------------------------------------------------------

EXTENDED_THEME_LIBRARY: list[dict[str, Any]] = [
    {"theme_code": "upstream_oil_gas", "name": "上游油气", "aliases": ["石油", "油气", "原油", "炼化", "油服"], "parent": "commodities"},
    {"theme_code": "shipping_trade", "name": "航运贸易", "aliases": ["航运", "港口", "物流", "集运"], "parent": "global_trade"},
    {"theme_code": "chip_domestic", "name": "芯片半导体", "aliases": ["芯片", "半导体", "设备", "算力", "ai", "服务器"], "parent": "technology"},
    {"theme_code": "military_industry", "name": "军工国防", "aliases": ["军工", "国防", "航空", "航天"], "parent": "defense"},
    {"theme_code": "high_dividend_banks", "name": "高股息金融", "aliases": ["银行", "保险", "高股息", "运营商", "公用事业"], "parent": "defensive"},
    {"theme_code": "liquor_consumption", "name": "消费龙头", "aliases": ["白酒", "消费", "家电", "食品", "饮料"], "parent": "consumer"},
    {"theme_code": "new_energy_vehicle", "name": "新能源汽车", "aliases": ["新能源", "电动车", "锂电", "充电桩", "汽车"], "parent": "new_energy"},
    # 以下为扩展主题
    {"theme_code": "pharma_biotech", "name": "医药生物", "aliases": ["医药", "生物", "创新药", "CXO", "医疗器械"], "parent": "healthcare"},
    {"theme_code": "real_estate_chain", "name": "地产链", "aliases": ["房地产", "地产", "建材", "家居", "装修"], "parent": "real_estate"},
    {"theme_code": "photovoltaic_wind", "name": "光伏风电", "aliases": ["光伏", "风电", "储能", "新能源发电", "逆变器"], "parent": "new_energy"},
    {"theme_code": "rare_earth_metals", "name": "稀土有色", "aliases": ["稀土", "有色", "铜", "铝", "锂矿", "黄金"], "parent": "commodities"},
    {"theme_code": "telecom_5g", "name": "通信5G", "aliases": ["5G", "通信", "光模块", "光纤", "基站"], "parent": "technology"},
    {"theme_code": "software_saas", "name": "软件信创", "aliases": ["软件", "信创", "国产替代", "操作系统", "ERP"], "parent": "technology"},
    {"theme_code": "agriculture_food", "name": "农业食品", "aliases": ["农业", "种业", "化肥", "猪肉", "养殖"], "parent": "consumer"},
    {"theme_code": "infrastructure", "name": "基建工程", "aliases": ["基建", "建筑", "水利", "铁路", "城轨"], "parent": "infrastructure"},
    {"theme_code": "tourism_leisure", "name": "旅游休闲", "aliases": ["旅游", "酒店", "免税", "影视", "游戏"], "parent": "consumer"},
    {"theme_code": "carbon_neutral", "name": "碳中和", "aliases": ["碳中和", "碳交易", "环保", "节能", "绿电"], "parent": "policy"},
    {"theme_code": "robotics_automation", "name": "机器人自动化", "aliases": ["机器人", "自动化", "人形机器人", "减速器", "伺服"], "parent": "technology"},
    {"theme_code": "data_center_cloud", "name": "数据中心与云", "aliases": ["数据中心", "云计算", "IDC", "算力租赁", "液冷"], "parent": "technology"},
    {"theme_code": "insurance_pension", "name": "保险养老", "aliases": ["保险", "养老", "年金", "寿险"], "parent": "defensive"},
]

# 方便按 theme_code 快速查找
_THEME_LOOKUP: dict[str, dict[str, Any]] = {t["theme_code"]: t for t in EXTENDED_THEME_LIBRARY}


# ---------------------------------------------------------------------------
# System Prompts — 每个 ~150-250 token，聚焦单一任务
# ---------------------------------------------------------------------------

_PROMPT_EVENT_RECOGNITION = """\
你是 A 股事件识别器。根据 market_snapshot、matched_theme_candidates、event_detection_hints、factor_research、research_task 输出 1-4 个最值得跟踪的事件。

规则:
1. 只要上述任一线索非空，就禁止输出 {"events":[]}
2. 优先使用 matched_theme_candidates 中已有的 theme_code
3. 若证据较弱，也要输出候选事件，并把 severity 设为 1-2
4. evidence 必须直接引用输入线索，不能留空
5. 只输出 JSON object，顶层 key 只能是 events
6. events 数组的每个元素必须是 JSON object（含 theme_code、event_type 等字段），严禁输出字符串数组（如 ["半导体","白酒"]）

常用映射:
半导体/芯片/算力 -> chip_domestic
机器人/自动化 -> robotics_automation
白酒/食品饮料 -> liquor_consumption
银行/保险/高股息 -> high_dividend_banks
原油/油气 -> upstream_oil_gas

输出格式（每个 event 必须是对象，不能是字符串）:
{"events":[{"theme_code":"chip_domestic","event_type":"sector_rotation","event_id":"ev_chip_001","title":"半导体板块走强","severity":3,"affected_sectors":["半导体"],"evidence":["半导体板块涨3.2%"]}]}"""

_PROMPT_THEME_PROPAGATION = """\
你是一个产业链传导分析引擎。根据识别出的事件，将每个事件映射到产业链传导路径。

规则:
1. 为每个事件识别上游→中游→下游传导链条
2. 给出传导方向(bullish/bearish)和置信度(0-1)
3. 只关注产业链逻辑，不涉及具体个股
4. 传导链条需符合 A 股市场实际产业关系
5. themes 数组的每个元素必须是 JSON object（含 theme_code、theme_name 等字段），严禁输出字符串数组（如 ["新能源","白酒"]）

严格按以下 JSON 格式输出（每个 theme 必须是对象，不能是字符串）:
{"themes": [{"theme_code": "chip_domestic", "theme_name": "芯片半导体", "source_event_id": "ev_001", "propagation_chain": ["technology", "chip_domestic"], "direction": "bullish", "confidence": 0.7}]}"""

_PROMPT_EXPOSURE_MAPPING = """\
你是一个股票暴露映射引擎。将主题映射到具体的股票或行业板块暴露。

规则:
1. 从给定的股票池基本面摘要中选择与主题匹配的标的
2. 给出暴露类型(direct_beneficiary/indirect_beneficiary/hedge)和权重(0-1)
3. 每个主题映射 2-6 只标的
4. 优先选择基本面健康、流动性好的标的
5. exposures 数组的每个元素必须是 JSON object（含 theme_code 和 target_symbols 字段），严禁输出股票代码字符串数组（如 ["600519","000858"]）

严格按以下 JSON 格式输出（每个 exposure 必须是对象，不能是字符串）:
{"exposures": [{"theme_code": "chip_domestic", "target_symbols": ["600519", "000858"], "sector": "芯片半导体", "exposure_type": "direct_beneficiary", "weight": 0.7}]}"""

_PROMPT_MARKET_CONFIRMATION = """\
你是一个技术面确认引擎。用技术信号验证基本面暴露是否当前可操作。

规则:
1. 对每只标的判断 confirmed(true/false)
2. 评估信号强度(strong/moderate/weak)和进场时机(immediate/wait_pullback/avoid)
3. 给出风险等级(low/medium/high)
4. 判断依据包括趋势方向、量价配合、RSI/MACD/均线位置

严格按以下 JSON 格式输出:
{"confirmations": [{"symbol": "600519", "confirmed": true, "theme_code": "liquor_consumption", "signal_strength": "moderate", "entry_timing": "immediate", "risk_level": "medium"}]}"""

_PROMPT_STRATEGY_GENERATION = """\
你是一个策略 DSL 生成引擎。必须为输入中的标的生成至少 1-2 个可回测的交易策略。

重要: 你必须生成候选策略，不得返回空列表。即使市场环境不理想，也要生成适合当前环境的防御性或均值回归策略。

如果输入中包含 stock_fundamentals（真实基本面数据，来自数据库），你的策略逻辑必须与这些数据一致：
- 若 PE > 50，不得声称"估值低"或使用价值策略
- 若 ROE < 0.10，不得声称"质量优秀"
- 若 revenue_growth < 0，不得声称"高成长"
- 若 momentum_20d < 0，趋势策略需额外确认条件（如量能放大）
- 优先使用与基本面匹配的策略类型（低 PE → 均值回归；高 momentum → 趋势跟踪）

DSL 格式要求:
- version: "1.0"
- timeframe: "daily"
- entry: 使用 {"any":[...]} 或 {"all":[...]} 组合条件
- exit: 同 entry 格式
- 条件支持的 op: gt, gte, lt, lte, cross_above, cross_below
- 支持的 indicator: sma, ema, rsi, roc, stddev, zscore, atr, volume_ratio
- 每个 indicator 需要 field(默认 "close") 和 window 参数

策略与标的匹配规则:
- 科技/成长/周期类(芯片、新能源、军工): 适合趋势跟踪(MA cross)和动量突破策略
- 银行/保险/高股息/公用事业类: 适合均值回归(RSI超卖买入、超买卖出)和区间震荡策略
- 消费类(白酒、家电): 适合趋势跟踪或RSI策略

规则:
1. 每个候选策略必须有 name, strategy_type, target_symbols, dsl, tags
2. entry 和 exit 条件必须完整可执行
3. 策略参数应合理(均线窗口 5-60，RSI 阈值 20-80)
4. 至少生成 1 个候选策略
5. 必须严格服从 research_task.allowed_strategy_types；若该字段非空，不得生成未被允许的 strategy_type
6. 若 research_task.task_source="snapshot" 且 opportunity_type 属于 candidate_family_activation / candidate_factor_activation / factor_acceleration:
   - 默认只输出 1 个最高置信候选
   - 不得同时输出 momentum 和 ma_cross
   - 若 allowed_strategy_types 中不含 momentum，则禁止生成 momentum
   - ma_cross 必须使用更长周期(short>=10,long>=40)，并附加 close>sma20、roc20>0.01、volume_ratio20>=1.0 等确认
   - momentum 仅在 research_task 明确允许时可用，且 lookback>=20、threshold>=0.02，并附加 close>sma40、sma12>sma48、volume_ratio>=1.1 等确认

严格按以下 JSON 格式输出(趋势跟踪示例):
{"candidates": [{"name": "芯片趋势跟踪", "strategy_type": "ma_cross", "target_symbols": ["600519"], "dsl": {"version": "1.0", "timeframe": "daily", "entry": {"any": [{"op": "cross_above", "left": {"indicator": "sma", "field": "close", "window": 5}, "right": {"indicator": "sma", "field": "close", "window": 20}}]}, "exit": {"any": [{"op": "cross_below", "left": {"indicator": "sma", "field": "close", "window": 5}, "right": {"indicator": "sma", "field": "close", "window": 20}}]}}, "tags": ["ai_staged", "ma_cross"]}]}

均值回归示例:
{"candidates": [{"name": "银行超卖回归", "strategy_type": "rsi", "target_symbols": ["601398"], "dsl": {"version": "1.0", "timeframe": "daily", "entry": {"all": [{"op": "lt", "left": {"indicator": "rsi", "field": "close", "window": 14}, "right": {"value": 35}}]}, "exit": {"any": [{"op": "gt", "left": {"indicator": "rsi", "field": "close", "window": 14}, "right": {"value": 65}}]}}, "tags": ["ai_staged", "rsi", "mean_reversion"]}]}"""


# ---------------------------------------------------------------------------
# Output validators
# ---------------------------------------------------------------------------

def _try_find_list(output: dict[str, Any], primary_key: str, alt_keys: tuple[str, ...] = ()) -> list[Any] | None:
    """尝试从 LLM 输出中提取目标列表，容忍常见的键名变体。

    处理策略:
    1. 精确匹配 primary_key
    2. 尝试 alt_keys 别名
    3. 如果 output 只有 1 个 key 且其值是非空 list，直接采用
    4. 如果整个 output 看起来像是单个 item（含 theme_code/target_symbols 等关键字段），
       将其包装成单元素列表
    """
    val = output.get(primary_key)
    if isinstance(val, list) and val:
        return val
    for k in alt_keys:
        val = output.get(k)
        if isinstance(val, list) and val:
            logger.debug("Validator: accepted alt key '%s' instead of '%s'", k, primary_key)
            return val
    # 单 key fallback
    if len(output) == 1:
        only_val = next(iter(output.values()))
        if isinstance(only_val, list) and only_val:
            logger.debug("Validator: accepted single-key fallback for '%s'", primary_key)
            return only_val
    # 单对象兜底：output 里包含 item 字段（如 theme_code/target_symbols）但没包数组
    item_signals = {
        "events": ("event_id", "event_type", "affected_sectors", "evidence"),
        "themes": ("propagation_chain", "theme_name", "source_event_id", "direction"),
        "exposures": ("target_symbols", "exposure_type", "weight", "sector"),
        "confirmations": ("symbol", "confirmed", "signal_strength", "entry_timing"),
        "candidates": ("dsl", "strategy_type", "target_symbols", "tags"),
    }
    signals = item_signals.get(primary_key, ())
    if signals and any(k in output for k in signals):
        logger.debug("Validator: wrapping singleton object as %s list", primary_key)
        return [dict(output)]
    return None


def _is_valid_empty_list_result(
    output: dict[str, Any], primary_key: str, alt_keys: tuple[str, ...] = ()
) -> bool:
    """判定 output 是否"显式给出了合法的空列表"——LLM 的合法保守输出。

    P0-A:契约(runtime.py)授权 LLM "Use [] only when the input has no usable signal"。
    `_try_find_list` 把空列表判为 falsy 返回 None,validator 又 `if not X: return False`,
    导致 LLM 合法返回 `{"events":[]}` 被误判失败 → 触发 fallback(病灶 A)。
    本函数区分"键存在但为合法空 list"(valid-empty,应放行)与"键缺失/类型错"(真失败)。
    要求:相关 key 之一显式存在且值为 list 类型(空也算),且 output 不含可被包装的单对象信号。
    """
    if not isinstance(output, dict):
        return False
    for key in (primary_key, *alt_keys):
        if key in output and isinstance(output.get(key), list):
            return True
    return False


def _lazy_match_sector_to_theme(sector_name: str) -> str:
    """根据主题库 alias 把板块名匹配到 theme_code。

    为避免与 ``postprocess.py`` 的循环 import（postprocess 依赖 context 的运行
    时片段执行），这里使用本地实现，行为与 postprocess._match_sector_to_theme
    保持一致。
    """
    text = str(sector_name or "").strip().lower()
    if not text:
        return ""
    for theme in EXTENDED_THEME_LIBRARY:
        for alias in list(theme.get("aliases") or []):
            a = str(alias or "").strip().lower()
            if not a:
                continue
            if a in text or text in a:
                return str(theme.get("theme_code") or "")
    return ""


def _is_a_share_symbol(text: str) -> bool:
    """快速识别 6 位 A 股股票代码。"""
    cleaned = str(text or "").strip()
    return cleaned.isdigit() and len(cleaned) == 6


def _coerce_event_string(item: str) -> dict[str, Any] | None:
    """把字符串形态的 event 升级为最小合法 event 对象。"""
    text = str(item or "").strip()
    if not text:
        return None
    matched_code = _lazy_match_sector_to_theme(text)
    theme_code = matched_code or "unknown"
    info = _THEME_LOOKUP.get(theme_code, {})
    title = info.get("name") or text
    return {
        "event_id": f"llm_str_{theme_code}_{abs(hash(text)) % 10**8}",
        "theme_code": theme_code,
        "event_type": "sector_rotation",
        "title": str(title),
        "severity": 2,
        "affected_sectors": [text],
        "evidence": [f"LLM string-only output: {text}"],
        "_coerced_from_string": True,
    }


def _coerce_theme_string(item: str) -> dict[str, Any] | None:
    """把字符串形态的 theme 升级为最小合法 theme 对象。"""
    text = str(item or "").strip()
    if not text or text.lower() == "theme_code":
        return None
    matched_code = _lazy_match_sector_to_theme(text)
    theme_code = matched_code or "unknown"
    info = _THEME_LOOKUP.get(theme_code, {})
    parent = info.get("parent", "")
    chain: list[str] = []
    if parent:
        chain.append(parent)
    chain.append(theme_code)
    return {
        "theme_code": theme_code,
        "theme_name": info.get("name") or text,
        "source_event_id": "",
        "propagation_chain": chain,
        "direction": "bullish",
        "confidence": 0.4,
        "_coerced_from_string": True,
    }


def _coerce_exposure_string(item: str, default_theme_code: str = "unknown") -> dict[str, Any] | None:
    """把字符串形态的 exposure 升级为最小合法 exposure 对象。

    - 6 位数字 → 视为股票代码，theme_code 从默认上下文继承
    - 其它字符串 → 视为行业名称，匹配主题库
    """
    text = str(item or "").strip()
    if not text:
        return None
    if _is_a_share_symbol(text):
        return {
            "theme_code": default_theme_code or "unknown",
            "target_symbols": [text],
            "sector": "",
            "exposure_type": "direct_beneficiary",
            "weight": 0.5,
            "_coerced_from_string": True,
        }
    matched_code = _lazy_match_sector_to_theme(text)
    theme_code = matched_code or "unknown"
    info = _THEME_LOOKUP.get(theme_code, {})
    return {
        "theme_code": theme_code,
        "target_symbols": [],
        "sector": info.get("name") or text,
        "exposure_type": "direct_beneficiary",
        "weight": 0.4,
        "_coerced_from_string": True,
    }


def _coerce_string_items(
    items: list[Any],
    coerce_fn: Callable[[str], dict[str, Any] | None],
) -> tuple[list[Any], int, int]:
    """对列表中的字符串元素统一调用 coerce_fn。

    Returns:
        (新列表, 转换成功数量, 转换失败数量)
    """
    coerced_count = 0
    dropped_count = 0
    new_items: list[Any] = []
    for item in items:
        if isinstance(item, str):
            obj = coerce_fn(item)
            if obj is None:
                dropped_count += 1
                continue
            new_items.append(obj)
            coerced_count += 1
        else:
            new_items.append(item)
    return new_items, coerced_count, dropped_count


def _validate_event_recognition(output: dict[str, Any]) -> bool:
    events = _try_find_list(output, "events", ("event_list", "market_events", "results"))
    if not events:
        if _is_valid_empty_list_result(output, "events", ("event_list", "market_events", "results")):
            output["events"] = []  # P0-A: LLM 合法返回空,放行而非 fallback
            return True
        logger.debug("event_recognition validation failed: no 'events' list, keys=%s", list(output.keys()))
        return False
    # 兼容降级：模型偶尔会输出字符串数组，将其规则化为最小合法对象
    has_string = any(isinstance(ev, str) for ev in events)
    if has_string:
        events, coerced, dropped = _coerce_string_items(events, _coerce_event_string)
        if coerced:
            logger.info(
                "event_recognition: coerced %d string events to objects (dropped=%d)",
                coerced, dropped,
            )
    # 回写到标准 key 以便下游消费
    output["events"] = events
    if not events:
        return False
    for ev in events:
        if not isinstance(ev, dict):
            return False
        if not ev.get("theme_code") or not ev.get("event_type"):
            logger.debug("event_recognition item missing theme_code/event_type: %s", list(ev.keys()))
            return False
    return True


def _validate_theme_propagation(output: dict[str, Any]) -> bool:
    themes = _try_find_list(output, "themes", ("theme_list", "propagation", "propagation_result", "results"))
    if not themes:
        if _is_valid_empty_list_result(output, "themes", ("theme_list", "propagation", "propagation_result", "results")):
            output["themes"] = []  # P0-A: LLM 合法返回空,放行而非 fallback
            return True
        logger.debug("theme_propagation validation failed: no 'themes' list, keys=%s", list(output.keys()))
        return False
    # 兼容降级：字符串数组形态
    has_string = any(isinstance(th, str) for th in themes)
    if has_string:
        themes, coerced, dropped = _coerce_string_items(themes, _coerce_theme_string)
        if coerced:
            logger.info(
                "theme_propagation: coerced %d string themes to objects (dropped=%d)",
                coerced, dropped,
            )
    output["themes"] = themes
    if not themes:
        return False
    for th in themes:
        if not isinstance(th, dict):
            return False
        if not th.get("theme_code"):
            logger.debug("theme_propagation item missing theme_code: %s", list(th.keys()))
            return False
    return True


def _validate_exposure_mapping(output: dict[str, Any]) -> bool:
    exposures = _try_find_list(output, "exposures", ("exposure_list", "mappings", "exposure_mapping", "results"))
    if not exposures:
        if _is_valid_empty_list_result(output, "exposures", ("exposure_list", "mappings", "exposure_mapping", "results")):
            output["exposures"] = []  # P0-A: LLM 合法返回空,放行而非 fallback
            return True
        logger.debug("exposure_mapping validation failed: no 'exposures' list, keys=%s", list(output.keys()))
        return False
    # 兼容降级：字符串数组形态（股票代码列表 / 行业名称列表）
    has_string = any(isinstance(exp, str) for exp in exposures)
    if has_string:
        # 默认 theme_code 取 output 顶层（若有）或上游 themes 第一个
        default_theme_code = str(output.get("theme_code") or "").strip() or "unknown"
        exposures, coerced, dropped = _coerce_string_items(
            exposures,
            lambda s: _coerce_exposure_string(s, default_theme_code=default_theme_code),
        )
        if coerced:
            logger.info(
                "exposure_mapping: coerced %d string exposures to objects (dropped=%d)",
                coerced, dropped,
            )
        # 字符串模式下若全是股票代码，target_symbols 仅 1 只，合并到一个 exposure 减少噪音
        symbol_only = [
            exp for exp in exposures
            if exp.get("_coerced_from_string") and exp.get("target_symbols")
            and not exp.get("sector")
        ]
        if len(symbol_only) > 1 and len(symbol_only) == len(exposures):
            merged_symbols: list[str] = []
            for exp in symbol_only:
                for sym in exp.get("target_symbols") or []:
                    if sym not in merged_symbols:
                        merged_symbols.append(sym)
            exposures = [
                {
                    "theme_code": default_theme_code or "unknown",
                    "target_symbols": merged_symbols[:6],
                    "sector": "",
                    "exposure_type": "direct_beneficiary",
                    "weight": 0.5,
                    "_coerced_from_string": True,
                }
            ]
            logger.info(
                "exposure_mapping: merged %d string-only symbol exposures into single exposure",
                len(symbol_only),
            )
    output["exposures"] = exposures
    if not exposures:
        return False
    for exp in exposures:
        if not isinstance(exp, dict):
            return False
        # 容忍 target_symbols 的别名
        if not exp.get("target_symbols"):
            for alt in ("symbols", "stocks", "stock_codes", "targets"):
                if exp.get(alt):
                    exp["target_symbols"] = exp[alt]
                    break
        if not exp.get("theme_code") or not exp.get("target_symbols"):
            logger.debug("exposure_mapping item missing theme_code/target_symbols: %s", list(exp.keys()))
            return False
    return True


def _validate_market_confirmation(output: dict[str, Any]) -> bool:
    confirmations = _try_find_list(output, "confirmations", ("confirmation_list", "confirmed_stocks", "results"))
    if not confirmations:
        if _is_valid_empty_list_result(output, "confirmations", ("confirmation_list", "confirmed_stocks", "results")):
            output["confirmations"] = []  # P0-A: 熊市"全部未确认"是最常见合法输出,放行而非 fallback
            return True
        logger.debug("market_confirmation validation failed: no 'confirmations' list, keys=%s", list(output.keys()))
        return False
    output["confirmations"] = confirmations
    for conf in confirmations:
        if not isinstance(conf, dict):
            return False
        # 容忍 symbol 的别名
        if not conf.get("symbol"):
            for alt in ("code", "stock_code", "stock", "ticker"):
                if conf.get(alt):
                    conf["symbol"] = conf[alt]
                    break
        if "confirmed" not in conf or not conf.get("symbol"):
            logger.debug("market_confirmation item missing symbol/confirmed: %s", list(conf.keys()))
            return False
    return True


def _validate_strategy_generation(output: dict[str, Any]) -> bool:
    candidates = _try_find_list(output, "candidates", ("strategies", "strategy_list", "results"))
    if not candidates:
        if _is_valid_empty_list_result(output, "candidates", ("strategies", "strategy_list", "results")):
            # P0-A: LLM 合法判定"本轮无可生成策略"≠LLM 失败。放行为合法空,
            # 让其计入 valid_empty 而非 used_fallback(避免污染 skip_llm 熔断/cooldown);
            # 空 specs 的后续兜底由 staged-empty 路径治理,不靠把本 stage 判失败触发。
            output["candidates"] = []
            return True
        logger.debug("strategy_generation validation failed: no 'candidates' list, keys=%s", list(output.keys()))
        return False
    output["candidates"] = candidates
    for cand in candidates:
        if not isinstance(cand, dict):
            return False
        dsl = cand.get("dsl")
        if not isinstance(dsl, dict) or not dsl.get("entry") or not dsl.get("exit"):
            logger.debug("strategy_generation item missing dsl.entry/exit: %s", list((dsl or {}).keys()) if isinstance(dsl, dict) else "no dsl")
            return False
        # 补全 strategy_type（LLM 可能遗漏）
        if not cand.get("strategy_type"):
            cand["strategy_type"] = "dsl_rule"
        # 补全 dsl.version
        if not dsl.get("version"):
            dsl["version"] = "1.0"
        if not dsl.get("timeframe"):
            dsl["timeframe"] = "daily"
    return True


_VALIDATORS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "event_recognition": _validate_event_recognition,
    "theme_propagation": _validate_theme_propagation,
    "exposure_mapping": _validate_exposure_mapping,
    "market_confirmation": _validate_market_confirmation,
    "strategy_generation": _validate_strategy_generation,
}


# ---------------------------------------------------------------------------
# Fallback functions — 调用现有规则引擎
# ---------------------------------------------------------------------------

def _sector_item_name(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in ("group", "name", "sector", "theme_name", "label"):
        value = str(item.get(key) or "").strip()
        if value:
            return value
    return ""


def _snapshot_sector_names(snapshot: dict[str, Any], input_data: dict[str, Any]) -> list[str]:
    hot_sectors = list(
        (snapshot.get("hot_sectors") or input_data.get("market_snapshot", {}).get("sectors")) or []
    )
    names: list[str] = []
    for sector_item in hot_sectors[:8]:
        name = _sector_item_name(sector_item)
        if name and name not in names:
            names.append(name)
    return names

async def _fallback_event_recognition(db: Any, input_data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """使用 LocalEventDrivenResearchEngine 的规则检测作为 fallback。"""
    from strategy_factory.api import get_local_event_engine

    engine = get_local_event_engine()
    result = await engine.refresh(db, snapshot=snapshot)
    events: list[dict[str, Any]] = []
    # 从 event_engine 结果中提取活跃事件
    for cluster in list(result.get("event_clusters") or []):
        theme_code = str(cluster.get("theme_code") or "").strip()
        if not theme_code:
            continue
        events.append({
            "event_id": cluster.get("event_id", f"local_{theme_code}"),
            "theme_code": theme_code,
            "event_type": "theme_rotation",
            "title": cluster.get("theme_name") or theme_code,
            "severity": 3,
            "affected_sectors": list(cluster.get("affected_sectors") or []),
            "evidence": [cluster.get("direction_reason") or "rule-based detection"],
        })
    # 如果 event_engine 没有返回 event_clusters，从 market_internals 推断
    if not events:
        internals = dict(result.get("market_internals") or {})
        hot_sectors = list(internals.get("hot_sectors") or [])
        for sector_info in hot_sectors[:5]:
            name = str(sector_info if isinstance(sector_info, str) else (sector_info or {}).get("group", "")).strip()
            if not name:
                continue
            # 尝试匹配主题库
            matched_code = _match_sector_to_theme(name)
            events.append({
                "event_id": f"local_hot_{name}",
                "theme_code": matched_code or "unknown",
                "event_type": "sector_rotation",
                "title": f"{name}板块活跃",
                "severity": 2,
                "affected_sectors": [name],
                "evidence": ["rule-based hot sector detection"],
            })
    return {"events": events[:5]}


async def _fallback_theme_propagation(db: Any, input_data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """基于主题库的关键词匹配做简单传导推断。
    
    当 events 为空（并行 pipeline 模式下 event_recognition 尚未完成时）
    自动降级为从 snapshot 热门板块推断主题，保证输出非空。
    """
    events = list(input_data.get("events") or [])
    themes: list[dict[str, Any]] = []
    for ev in events:
        theme_code = str(ev.get("theme_code") or "").strip()
        theme_info = _THEME_LOOKUP.get(theme_code, {})
        parent = theme_info.get("parent", "")
        themes.append({
            "theme_code": theme_code,
            "theme_name": theme_info.get("name", theme_code),
            "source_event_id": ev.get("event_id", ""),
            "propagation_chain": [parent, theme_code] if parent else [theme_code],
            "direction": "bullish" if ev.get("severity", 3) <= 3 else "bearish",
            "confidence": 0.5,
        })

    if not themes:
        # 并行模式降级：从 snapshot 热门板块匹配主题库
        for name in _snapshot_sector_names(snapshot, input_data)[:6]:
            if not name:
                continue
            matched_code = _match_sector_to_theme(name)
            if not matched_code:
                continue
            theme_info = _THEME_LOOKUP.get(matched_code, {})
            parent = theme_info.get("parent", "")
            themes.append({
                "theme_code": matched_code,
                "theme_name": theme_info.get("name", matched_code),
                "source_event_id": f"snapshot_sector_{matched_code}",
                "propagation_chain": [parent, matched_code] if parent else [matched_code],
                "direction": "bullish",
                "confidence": 0.4,
            })

    return {"themes": themes}

# --- strategy_stages_parts/specs.py ---
async def _fallback_exposure_mapping(db: Any, input_data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """使用 opportunity.py 的规则扫描做板块→成分股映射。"""
    themes = list(input_data.get("themes") or [])
    if not themes:
        # Phase-1 并行时 exposure_mapping 拿不到 theme_propagation 输出，
        # 需要直接从 snapshot 的热门板块自举主题线索。
        for name in _snapshot_sector_names(snapshot, input_data)[:6]:
            matched_code = _match_sector_to_theme(name)
            theme_info = _THEME_LOOKUP.get(matched_code, {})
            themes.append({
                "theme_code": matched_code or f"snapshot_sector_{name}",
                "theme_name": theme_info.get("name", name) or name,
                "sector_hint": name,
            })

    # 尝试从 DB 加载股票池并按主题关键词匹配
    from strategy_factory.api import call_optional_async
    universe = await call_optional_async(db, "list_stock_universe", limit=200, offset=0, default=[])

    normalized_universe = [
        {
            "code": str((row or {}).get("code") or "").strip(),
            "text": " ".join(
                [
                    str((row or {}).get("name") or "").lower(),
                    str((row or {}).get("industry") or "").lower(),
                    str((row or {}).get("sector") or "").lower(),
                ]
            ),
        }
        for row in list(universe or [])
    ]

    def _map_theme(th: dict[str, Any]) -> Optional[dict[str, Any]]:
        theme_code = str(th.get("theme_code") or "").strip()
        theme_info = _THEME_LOOKUP.get(theme_code, {})
        sector_hint = str(th.get("sector_hint") or th.get("theme_name") or "").strip()
        aliases = [
            str(alias or "").strip().lower()
            for alias in list(theme_info.get("aliases") or [])
            if str(alias or "").strip()
        ]
        if sector_hint:
            aliases.extend(
                [
                    sector_hint.lower(),
                    sector_hint.replace("板块", "").strip().lower(),
                ]
            )
        aliases = list(dict.fromkeys([alias for alias in aliases if alias]))
        if not aliases:
            return None
        matched_symbols: list[str] = []
        for row in normalized_universe:
            text = str(row.get("text") or "")
            if any(alias in text for alias in aliases):
                code = str(row.get("code") or "").strip()
                if code:
                    matched_symbols.append(code)
            if len(matched_symbols) >= 6:
                break
        if matched_symbols:
            return {
                "theme_code": theme_code,
                "target_symbols": matched_symbols,
                "sector": theme_info.get("name", sector_hint or theme_code),
                "exposure_type": "direct_beneficiary",
                "weight": 0.5,
            }
        return None
    exposures = [
        item
        for item in await asyncio.gather(*[asyncio.to_thread(_map_theme, th) for th in themes])
        if item
    ]
    return {"exposures": exposures}


async def _fallback_market_confirmation(db: Any, input_data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """使用技术面扫描逻辑做确认。"""
    from strategy_factory.api import call_optional_async

    exposures = list(input_data.get("exposures") or [])
    symbol_jobs: list[tuple[str, str]] = []
    for exp in exposures:
        theme_code = str(exp.get("theme_code") or "").strip()
        for symbol in list(exp.get("target_symbols") or [])[:4]:
            symbol_jobs.append((theme_code, symbol))
    if not symbol_jobs:
        research_task = dict(input_data.get("research_task") or {})
        stock_pool = dict(research_task.get("stock_pool") or {})
        fallback_symbols: list[str] = []
        for raw_value in (
            research_task.get("target_symbols"),
            stock_pool.get("symbols"),
            stock_pool.get("codes"),
        ):
            values = list(raw_value or []) if isinstance(raw_value, (list, tuple, set)) else [raw_value]
            for symbol in values:
                token = str(symbol or "").strip()
                if token and token not in fallback_symbols:
                    fallback_symbols.append(token)
        theme_code = str(
            research_task.get("theme_code")
            or research_task.get("candidate_family")
            or "target_context"
        ).strip() or "target_context"
        for symbol in fallback_symbols[:6]:
            symbol_jobs.append((theme_code, symbol))

    if not symbol_jobs:
        return {
            "confirmations": [
                {
                    "theme_code": "target_context",
                    "symbol": "UNKNOWN",
                    "confirmed": False,
                    "signal_strength": "weak",
                    "entry_timing": "avoid",
                    "risk_level": "high",
                    "fallback_reason": "market_confirmation_no_exposure_or_target_symbols",
                }
            ]
        }

    async def _load_symbol_klines(symbol: str) -> list[dict[str, Any]]:
        try:
            return list(await call_optional_async(db, "get_klines", symbol, limit=30, default=[]))
        except TypeError:
            return list(await call_optional_async(db, "get_klines", symbol, default=[]))

    kline_payloads = await asyncio.gather(
        *[_load_symbol_klines(symbol) for _theme_code, symbol in symbol_jobs],
        return_exceptions=True,
    )

    confirmations: list[dict[str, Any]] = []
    for (theme_code, symbol), kline_payload in zip(symbol_jobs, kline_payloads):
        if isinstance(kline_payload, Exception):
            klines = []
        else:
            klines = list(kline_payload or [])
        confirmed = False
        signal_strength = "weak"
        if len(klines) >= 5:
            closes = [float(k.get("close") or 0) for k in klines if k.get("close") is not None]
            if len(closes) >= 5:
                ma5 = sum(closes[-5:]) / 5
                ma20 = sum(closes[-20:]) / max(len(closes[-20:]), 1) if len(closes) >= 20 else sum(closes) / len(closes)
                last_close = closes[-1]
                # 简单确认: 价格在均线上方且短均线>长均线
                if last_close > ma20 and ma5 > ma20:
                    confirmed = True
                    signal_strength = "moderate"
                if last_close > ma5 > ma20:
                    signal_strength = "strong"
        confirmations.append({
            "theme_code": theme_code,
            "symbol": symbol,
            "confirmed": confirmed,
            "signal_strength": signal_strength,
            "entry_timing": "immediate" if confirmed else "avoid",
            "risk_level": "medium",
            "fallback_reason": "market_confirmation_local_rules",
        })
    return {"confirmations": confirmations}


def _is_defensive_theme(theme_code: str) -> bool:
    """判断主题是否属于防御/价值类（适合均值回归而非趋势跟踪）。"""
    theme_info = _THEME_LOOKUP.get(theme_code, {})
    parent = theme_info.get("parent", "")
    if parent in ("defensive",):
        return True
    if theme_code in ("high_dividend_banks", "insurance_pension"):
        return True
    return False


def _fear_greed_score(snapshot: dict[str, Any]) -> int:
    raw = snapshot.get("fear_greed_index")
    if raw is None:
        raw = dict(snapshot.get("fear_greed") or {}).get("score")
    try:
        return int(float(raw or 50))
    except (TypeError, ValueError):
        return 50


def _north_fund_inflow(snapshot: dict[str, Any]) -> float:
    north_fund = dict(snapshot.get("north_fund") or {})
    for key in ("net_inflow", "net_inflow_amount", "amount", "inflow"):
        value = north_fund.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError) as _swallowed:
            logger.warning("packages/akshare-mcp/src/akshare_mcp/services/strategy_stages.py:887: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
            continue
    return 0.0


def _theme_parent(theme_code: str) -> str:
    return str(_THEME_LOOKUP.get(theme_code, {}).get("parent") or "").strip()


def _is_growth_theme(theme_code: str) -> bool:
    parent = _theme_parent(theme_code)
    if parent in {"technology", "new_energy", "defense"}:
        return True
    return theme_code in {
        "chip_domestic",
        "new_energy_vehicle",
        "photovoltaic_wind",
        "telecom_5g",
        "software_saas",
        "robotics_automation",
        "data_center_cloud",
    }


def _is_rotation_theme(theme_code: str) -> bool:
    parent = _theme_parent(theme_code)
    if parent in {"commodities", "policy", "infrastructure", "global_trade", "real_estate"}:
        return True
    return theme_code in {
        "upstream_oil_gas",
        "shipping_trade",
        "real_estate_chain",
        "rare_earth_metals",
        "infrastructure",
        "carbon_neutral",
    }


def _is_flow_preferred_theme(theme_code: str) -> bool:
    parent = _theme_parent(theme_code)
    if parent in {"consumer", "defensive", "technology", "new_energy"}:
        return True
    return theme_code in {
        "liquor_consumption",
        "high_dividend_banks",
        "insurance_pension",
        "chip_domestic",
        "software_saas",
        "data_center_cloud",
    }


def _theme_hot_sector_strength(theme_code: str, snapshot: dict[str, Any], input_data: Optional[dict[str, Any]] = None) -> float:
    theme_info = _THEME_LOOKUP.get(theme_code, {})
    aliases = {
        str(alias or "").strip().lower()
        for alias in list(theme_info.get("aliases") or [])
        if str(alias or "").strip()
    }
    aliases.add(str(theme_info.get("name") or theme_code).strip().lower())
    max_change = 0.0
    for sector_item in list((snapshot.get("hot_sectors") or (input_data or {}).get("market_snapshot", {}).get("sectors")) or []):
        name = _sector_item_name(sector_item).lower()
        if not name:
            continue
        if not any(alias and (alias in name or name in alias) for alias in aliases):
            continue
        try:
            max_change = max(max_change, abs(float((sector_item or {}).get("change_pct") or 0.0)))
        except (AttributeError, TypeError, ValueError):
            max_change = max(max_change, 0.0)
    return max_change


def _collapsed_hint_text(value: Any) -> str:
    return (
        str(value or "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )


def _snapshot_strategy_generation_profile(research_task: Optional[dict[str, Any]]) -> dict[str, Any]:
    task = dict(research_task or {})
    task_source = str(task.get("task_source") or "").strip().lower()
    opportunity_type = str(task.get("opportunity_type") or "").strip().lower()
    validation_focus = str(task.get("validation_focus") or "").strip().lower()
    allowed_strategy_types = [
        str(item).strip()
        for item in list(task.get("allowed_strategy_types") or [])
        if str(item).strip()
    ]
    template_profile = str(task.get("template_generation_profile") or "").strip().lower()
    hint_blob = " ".join(
        str(item or "")
        for item in (
            task.get("candidate_family"),
            task.get("factor_name"),
            task.get("candidate_name"),
            task.get("preference_reason"),
            task.get("rationale"),
        )
        if str(item or "").strip()
    )
    collapsed = _collapsed_hint_text(hint_blob)
    conservative_snapshot_task = (
        task_source == "snapshot"
        and (
            opportunity_type in {"candidate_family_activation", "candidate_factor_activation", "factor_acceleration"}
            or validation_focus == "candidate_target_only"
            or template_profile.startswith("conservative_")
        )
    )
    mean_reversion_tokens = (
        "closelocation",
        "intradayresilience",
        "trendefficiency",
        "pullback",
        "quality",
        "stability",
        "quiet",
        "resilience",
        "repair",
        "reversion",
        "defensive",
        "rsi",
    )
    flow_tokens = (
        "capitalflow",
        "northcapital",
        "northbound",
        "fundflow",
        "liquidity",
        "turnover",
    )
    rotation_tokens = (
        "rotation",
        "sector",
        "cycle",
        "divergence",
        "breadth",
    )
    breakout_tokens = (
        "momentum",
        "macross",
        "cross",
        "trend",
        "breakout",
        "gapcontinuation",
        "expansion",
        "acceleration",
        "volatility",
    )
    if not template_profile:
        if any(token in collapsed for token in mean_reversion_tokens):
            template_profile = "conservative_mean_reversion"
        elif any(token in collapsed for token in flow_tokens):
            template_profile = "conservative_flow"
        elif any(token in collapsed for token in rotation_tokens):
            template_profile = "conservative_rotation"
        elif any(token in collapsed for token in breakout_tokens):
            template_profile = "conservative_breakout"
        elif conservative_snapshot_task:
            template_profile = "conservative_trend"

    disable_momentum = (
        conservative_snapshot_task
        and (
            "momentum" not in allowed_strategy_types
            or template_profile in {
                "conservative_mean_reversion",
                "conservative_flow",
                "conservative_rotation",
                "conservative_trend",
                "conservative_breakout",
            }
        )
    )
    candidate_cap = 4
    if conservative_snapshot_task:
        candidate_cap = 1 if opportunity_type in {"candidate_family_activation", "candidate_factor_activation"} else 2

    return {
        "task_source": task_source,
        "opportunity_type": opportunity_type,
        "validation_focus": validation_focus,
        "allowed_strategy_types": allowed_strategy_types,
        "template_generation_profile": template_profile,
        "conservative_snapshot_task": conservative_snapshot_task,
        "disable_momentum": disable_momentum,
        "candidate_cap": candidate_cap,
    }


def _filter_templates_by_allowed_types(
    templates: list[dict[str, Any]],
    allowed_strategy_types: list[str],
) -> list[dict[str, Any]]:
    allowed = {
        str(item).strip()
        for item in list(allowed_strategy_types or [])
        if str(item).strip()
    }
    if not allowed:
        return list(templates)
    return [
        item
        for item in templates
        if str((item or {}).get("strategy_type") or "").strip() in allowed
    ]


def _build_template_candidate(
    *,
    theme_name: str,
    theme_code: str,
    family: str,
    title_suffix: str,
    symbols: list[str],
    stock_pool: dict[str, Any],
    params: dict[str, Any],
    entry: dict[str, Any],
    exit_rule: dict[str, Any],
    tags: list[str],
    description: str,
) -> dict[str, Any]:
    metadata = {"target_symbols": list(symbols), "stock_pool": dict(stock_pool)}
    return {
        "name": f"{theme_name}_{title_suffix}",
        "strategy_type": family,
        "target_symbols": list(symbols),
        "params": dict(params),
        "stock_pool": dict(stock_pool),
        "description": description,
        "dsl": {
            "version": "1.0",
            "timeframe": "daily",
            "entry": entry,
            "exit": exit_rule,
            "metadata": metadata,
        },
        "tags": ["ai_staged", theme_code, family, *tags],
    }


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for candidate in candidates:
        strategy_type = str(candidate.get("strategy_type") or "").strip()
        target_symbols = tuple(str(symbol).strip() for symbol in list(candidate.get("target_symbols") or []))
        if not strategy_type or not target_symbols:
            continue
        key = (strategy_type, target_symbols)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped

# --- strategy_stages_parts/runtime.py ---
def _build_trend_templates(
    theme_name: str,
    theme_code: str,
    symbols: list[str],
    stock_pool: dict[str, Any],
    *,
    include_breakout: bool = False,
    conservative: bool = False,
    disable_momentum: bool = False,
    prefer_breakout_first: bool = False,
) -> list[dict[str, Any]]:
    """趋势跟踪模板，兼顾传统趋势与高波动成长场景。"""
    short_window = 12 if conservative else 6
    long_window = 48 if conservative else 24
    ma_entry = {
        "any": [{
            "op": "cross_above",
            "left": {"indicator": "sma", "field": "close", "window": short_window},
            "right": {"indicator": "sma", "field": "close", "window": long_window},
        }],
    }
    ma_exit = {
        "any": [{
            "op": "cross_below",
            "left": {"indicator": "sma", "field": "close", "window": short_window},
            "right": {"indicator": "sma", "field": "close", "window": long_window},
        }],
    }
    ma_tags = ["trend", "ma_cross"]
    ma_description = f"{theme_name}主线以均线趋势跟踪为主，适合景气延续与抱团强化阶段。"
    if conservative:
        ma_entry = {
            "all": [
                {
                    "op": "cross_above",
                    "left": {"indicator": "sma", "field": "close", "window": short_window},
                    "right": {"indicator": "sma", "field": "close", "window": long_window},
                },
                {
                    "op": "gt",
                    "left": {"field": "close"},
                    "right": {"indicator": "sma", "field": "close", "window": 20},
                },
                {
                    "op": "gt",
                    "left": {"indicator": "roc", "field": "close", "window": 20},
                    "right": {"value": 0.01},
                },
                {
                    "op": "gt",
                    "left": {"indicator": "volume_ratio", "field": "volume", "window": 20},
                    "right": {"value": 1.0},
                },
            ],
        }
        ma_exit = {
            "any": [
                {
                    "op": "cross_below",
                    "left": {"indicator": "sma", "field": "close", "window": short_window},
                    "right": {"indicator": "sma", "field": "close", "window": long_window},
                },
                {
                    "op": "lt",
                    "left": {"field": "close"},
                    "right": {"indicator": "sma", "field": "close", "window": 20},
                },
                {
                    "op": "lt",
                    "left": {"indicator": "roc", "field": "close", "window": 10},
                    "right": {"value": -0.012},
                },
            ],
        }
        ma_tags = ["trend", "ma_cross", "conservative"]
        ma_description = f"{theme_name}在定向 target pool 下改用长周期均线和量价确认，优先保证信号稳定性而不是追求高频触发。"

    templates = [
        _build_template_candidate(
            theme_name=theme_name,
            theme_code=theme_code,
            family="ma_cross",
            title_suffix="趋势跟踪",
            symbols=symbols,
            stock_pool=stock_pool,
            params={"short_period": short_window, "long_period": long_window},
            entry=ma_entry,
            exit_rule=ma_exit,
            tags=ma_tags,
            description=ma_description,
        ),
    ]
    if not disable_momentum:
        momentum_lookback = 24 if conservative else 12
        momentum_threshold = 0.03 if conservative else 0.018
        momentum_entry = {
            "all": [
                {"op": "gt", "left": {"indicator": "roc", "field": "close", "window": momentum_lookback}, "right": {"value": momentum_threshold}},
                {"op": "gt", "left": {"indicator": "volume_ratio", "field": "volume", "window": 20}, "right": {"value": 1.15 if conservative else 1.1}},
            ],
        }
        momentum_exit = {
            "any": [
                {"op": "lt", "left": {"indicator": "roc", "field": "close", "window": 12 if conservative else 8}, "right": {"value": -0.01 if conservative else -0.02}},
                {"op": "gt", "left": {"indicator": "rsi", "field": "close", "window": 14}, "right": {"value": 76 if conservative else 78}},
            ],
        }
        momentum_tags = ["trend", "momentum"]
        momentum_description = f"{theme_name}热点扩散阶段更适合用短中周期动量确认主升浪。"
        if conservative:
            momentum_entry = {
                "all": [
                    {"op": "gt", "left": {"indicator": "roc", "field": "close", "window": momentum_lookback}, "right": {"value": momentum_threshold}},
                    {
                        "op": "gt",
                        "left": {"field": "close"},
                        "right": {"indicator": "sma", "field": "close", "window": 40},
                    },
                    {
                        "op": "gt",
                        "left": {"indicator": "sma", "field": "close", "window": 12},
                        "right": {"indicator": "sma", "field": "close", "window": 48},
                    },
                    {"op": "gt", "left": {"indicator": "volume_ratio", "field": "volume", "window": 20}, "right": {"value": 1.15}},
                ],
            }
            momentum_tags = ["trend", "momentum", "conservative"]
            momentum_description = f"{theme_name}仅在强趋势已确认时才允许动量突破，优先过滤掉高换手、低持续性的追涨信号。"
        templates.append(
            _build_template_candidate(
                theme_name=theme_name,
                theme_code=theme_code,
                family="momentum",
                title_suffix="动量突破",
                symbols=symbols,
                stock_pool=stock_pool,
                params={"lookback": momentum_lookback, "threshold": momentum_threshold},
                entry=momentum_entry,
                exit_rule=momentum_exit,
                tags=momentum_tags,
                description=momentum_description,
            )
        )
    if include_breakout:
        breakout_candidate = _build_template_candidate(
            theme_name=theme_name,
            theme_code=theme_code,
            family="volatility_breakout",
            title_suffix="波动突破",
            symbols=symbols,
            stock_pool=stock_pool,
            params={"lookback": 30 if conservative else 20, "threshold": 0.03 if conservative else 0.025},
            entry={
                "all": [
                    {"op": "gt", "left": {"indicator": "roc", "field": "close", "window": 30 if conservative else 20}, "right": {"value": 0.03 if conservative else 0.025}},
                    {"op": "gt", "left": {"indicator": "stddev", "field": "close", "window": 20}, "right": {"value": 0.02 if conservative else 0.018}},
                    {"op": "gt", "left": {"indicator": "volume_ratio", "field": "volume", "window": 20}, "right": {"value": 1.12 if conservative else 1.0}},
                ],
            },
            exit_rule={
                "any": [
                    {"op": "lt", "left": {"indicator": "roc", "field": "close", "window": 12 if conservative else 10}, "right": {"value": -0.012 if conservative else -0.015}},
                    {
                        "op": "cross_below",
                        "left": {"indicator": "sma", "field": "close", "window": 10 if conservative else 5},
                        "right": {"indicator": "sma", "field": "close", "window": 40 if conservative else 20},
                    },
                ],
            },
            tags=["trend", "breakout", "high_beta", *(['conservative'] if conservative else [])],
            description=(
                f"{theme_name}处于高弹性放量阶段时，优先用波动突破捕捉主升与加速段。"
                if not conservative
                else f"{theme_name}在定向 basket 下优先用更长确认窗口的波动突破，只保留趋势和量能都足够清晰的候选。"
            ),
        )
        if prefer_breakout_first:
            templates = [breakout_candidate, *templates]
        else:
            templates.append(breakout_candidate)
    return templates


def _build_mean_reversion_templates(
    theme_name: str,
    theme_code: str,
    symbols: list[str],
    stock_pool: dict[str, Any],
    *,
    include_gap_fill: bool = False,
) -> list[dict[str, Any]]:
    """均值回归模板，覆盖防御与错杀修复场景。"""
    templates = [
        _build_template_candidate(
            theme_name=theme_name,
            theme_code=theme_code,
            family="rsi",
            title_suffix="超卖回归",
            symbols=symbols,
            stock_pool=stock_pool,
            params={"rsi_period": 14, "oversold": 35, "overbought": 65},
            entry={
                "all": [
                    {"op": "lt", "left": {"indicator": "rsi", "field": "close", "window": 14}, "right": {"value": 35}},
                    {"op": "gt", "left": {"indicator": "volume_ratio", "field": "volume", "window": 20}, "right": {"value": 0.8}},
                ],
            },
            exit_rule={
                "any": [
                    {"op": "gt", "left": {"indicator": "rsi", "field": "close", "window": 14}, "right": {"value": 65}},
                ],
            },
            tags=["mean_reversion", "rsi"],
            description=f"{theme_name}偏低波防御属性，适合用经典 RSI 超卖回归吸收短线回撤。",
        ),
        _build_template_candidate(
            theme_name=theme_name,
            theme_code=theme_code,
            family="mean_reversion_short",
            title_suffix="短线回归",
            symbols=symbols,
            stock_pool=stock_pool,
            params={"rsi_period": 6, "oversold": 26, "overbought": 62},
            entry={
                "all": [
                    {"op": "lt", "left": {"indicator": "rsi", "field": "close", "window": 6}, "right": {"value": 26}},
                    {"op": "lt", "left": {"indicator": "zscore", "field": "close", "window": 20}, "right": {"value": -1.0}},
                ],
            },
            exit_rule={
                "any": [
                    {"op": "gt", "left": {"indicator": "rsi", "field": "close", "window": 6}, "right": {"value": 58}},
                    {"op": "gt", "left": {"indicator": "zscore", "field": "close", "window": 20}, "right": {"value": 0.8}},
                ],
            },
            tags=["mean_reversion", "short_horizon"],
            description=f"{theme_name}震荡期更容易出现短周期偏离修复，适合短线均值回归模板。",
        ),
    ]
    if include_gap_fill:
        templates.append(
            _build_template_candidate(
                theme_name=theme_name,
                theme_code=theme_code,
                family="gap_fill",
                title_suffix="跳空回补",
                symbols=symbols,
                stock_pool=stock_pool,
                params={"gap_threshold": 0.02, "rsi_period": 5, "oversold": 24, "overbought": 58},
                entry={
                    "all": [
                        {"op": "lt", "left": {"indicator": "rsi", "field": "close", "window": 5}, "right": {"value": 24}},
                        {"op": "lt", "left": {"indicator": "roc", "field": "close", "window": 2}, "right": {"value": -0.025}},
                    ],
                },
                exit_rule={
                    "any": [
                        {"op": "gt", "left": {"indicator": "rsi", "field": "close", "window": 5}, "right": {"value": 58}},
                        {"op": "gt", "left": {"indicator": "roc", "field": "close", "window": 3}, "right": {"value": 0.02}},
                    ],
                },
                tags=["mean_reversion", "event_repair"],
                description=f"{theme_name}若因情绪冲击出现快速错杀，更适合用跳空回补模板承接修复。",
            )
        )
    return templates


def _build_rotation_templates(
    theme_name: str,
    theme_code: str,
    symbols: list[str],
    stock_pool: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        _build_template_candidate(
            theme_name=theme_name,
            theme_code=theme_code,
            family="sector_rotation",
            title_suffix="行业轮动",
            symbols=symbols,
            stock_pool=stock_pool,
            params={"lookback": 20, "factor_weights": {"momentum": 0.45, "quality": 0.30, "value": 0.25}},
            entry={
                "all": [
                    {"op": "gt", "left": {"indicator": "roc", "field": "close", "window": 20}, "right": {"value": 0.015}},
                    {"op": "gt", "left": {"indicator": "zscore", "field": "close", "window": 20}, "right": {"value": -0.3}},
                ],
            },
            exit_rule={
                "any": [
                    {"op": "lt", "left": {"indicator": "roc", "field": "close", "window": 10}, "right": {"value": -0.015}},
                    {"op": "lt", "left": {"indicator": "zscore", "field": "close", "window": 20}, "right": {"value": -1.2}},
                ],
            },
            tags=["rotation", "macro"],
            description=f"{theme_name}更偏政策/商品/顺周期轮动，适合用行业轮动打分模板捕捉切换。",
        )
    ]


def _build_flow_templates(
    theme_name: str,
    theme_code: str,
    symbols: list[str],
    stock_pool: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        _build_template_candidate(
            theme_name=theme_name,
            theme_code=theme_code,
            family="north_capital_track",
            title_suffix="北向跟踪",
            symbols=symbols,
            stock_pool=stock_pool,
            params={"lookback": 15, "threshold": 0.015},
            entry={
                "all": [
                    {"op": "gt", "left": {"indicator": "roc", "field": "close", "window": 15}, "right": {"value": 0.015}},
                    {"op": "gt", "left": {"indicator": "volume_ratio", "field": "volume", "window": 15}, "right": {"value": 1.1}},
                ],
            },
            exit_rule={
                "any": [
                    {"op": "lt", "left": {"indicator": "roc", "field": "close", "window": 10}, "right": {"value": -0.012}},
                    {"op": "lt", "left": {"indicator": "volume_ratio", "field": "volume", "window": 10}, "right": {"value": 0.92}},
                ],
            },
            tags=["capital_flow", "north_fund"],
            description=f"{theme_name}受资金偏好驱动较强时，优先使用北向跟踪模板承接趋势与量能共振。",
        )
    ]


def _build_divergence_templates(
    theme_name: str,
    theme_code: str,
    symbols: list[str],
    stock_pool: dict[str, Any],
) -> list[dict[str, Any]]:
    return [
        _build_template_candidate(
            theme_name=theme_name,
            theme_code=theme_code,
            family="margin_divergence",
            title_suffix="融资背离",
            symbols=symbols,
            stock_pool=stock_pool,
            params={"fear_threshold": 40, "greed_threshold": 60, "lookback": 15},
            entry={
                "all": [
                    {"op": "lt", "left": {"indicator": "roc", "field": "close", "window": 5}, "right": {"value": 0.0}},
                    {"op": "gt", "left": {"indicator": "volume_ratio", "field": "volume", "window": 15}, "right": {"value": 0.95}},
                ],
            },
            exit_rule={
                "any": [
                    {"op": "gt", "left": {"indicator": "rsi", "field": "close", "window": 10}, "right": {"value": 68}},
                    {"op": "lt", "left": {"indicator": "roc", "field": "close", "window": 10}, "right": {"value": -0.02}},
                ],
            },
            tags=["capital_flow", "divergence"],
            description=f"{theme_name}在情绪分歧与量价背离阶段，更适合用融资背离模板捕捉修复回归。",
        )
    ]

# --- strategy_stages_parts/postprocess.py ---
async def _fallback_strategy_generation(db: Any, input_data: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    """使用 DSL 模板库生成策略，根据主题类型选择合适的策略模板。"""
    confirmations = list(input_data.get("confirmations") or [])
    confirmed = [c for c in confirmations if c.get("confirmed")]
    if not confirmed:
        confirmed = confirmations[:2]  # 至少生成一些候选

    # 按 theme_code 分组
    theme_symbols: dict[str, list[str]] = {}
    for conf in confirmed:
        tc = str(conf.get("theme_code") or "default").strip()
        sym = str(conf.get("symbol") or "").strip()
        if sym:
            theme_symbols.setdefault(tc, []).append(sym)

    candidates: list[dict[str, Any]] = []
    fear_greed = _fear_greed_score(snapshot)
    north_inflow = _north_fund_inflow(snapshot)
    hot_sector_count = len(_snapshot_sector_names(snapshot, input_data))
    generation_profile = _snapshot_strategy_generation_profile(input_data.get("research_task"))
    risk_on = fear_greed >= 58 or north_inflow > 0 or hot_sector_count >= 3
    risk_off = fear_greed <= 45 or str(snapshot.get("sentiment") or "").strip().lower() in {"fear", "risk_off", "weak"}
    for theme_code, symbols in theme_symbols.items():
        theme_info = _THEME_LOOKUP.get(theme_code, {})
        theme_name = theme_info.get("name", theme_code)
        stock_pool = {"selection_mode": "explicit", "symbols": list(symbols)}
        hot_strength = _theme_hot_sector_strength(theme_code, snapshot, input_data)
        include_breakout = _is_growth_theme(theme_code) and (risk_on or hot_strength >= 1.5)
        include_gap_fill = _is_defensive_theme(theme_code) or risk_off
        templates: list[dict[str, Any]] = []
        if generation_profile.get("conservative_snapshot_task"):
            template_profile = str(generation_profile.get("template_generation_profile") or "").strip().lower()
            if template_profile == "conservative_mean_reversion":
                templates.extend(
                    _build_mean_reversion_templates(
                        theme_name,
                        theme_code,
                        symbols,
                        stock_pool,
                        include_gap_fill=True,
                    )
                )
                if not risk_off:
                    templates.extend(_build_rotation_templates(theme_name, theme_code, symbols, stock_pool))
            elif template_profile == "conservative_flow":
                templates.extend(_build_flow_templates(theme_name, theme_code, symbols, stock_pool))
                templates.extend(_build_rotation_templates(theme_name, theme_code, symbols, stock_pool))
                templates.extend(
                    _build_trend_templates(
                        theme_name,
                        theme_code,
                        symbols,
                        stock_pool,
                        conservative=True,
                        disable_momentum=True,
                    )
                )
            elif template_profile == "conservative_rotation":
                templates.extend(_build_rotation_templates(theme_name, theme_code, symbols, stock_pool))
                templates.extend(_build_divergence_templates(theme_name, theme_code, symbols, stock_pool))
                if risk_on or hot_strength >= 1.0:
                    templates.extend(
                        _build_trend_templates(
                            theme_name,
                            theme_code,
                            symbols,
                            stock_pool,
                            conservative=True,
                            disable_momentum=True,
                        )
                    )
            elif template_profile == "conservative_breakout":
                templates.extend(
                    _build_trend_templates(
                        theme_name,
                        theme_code,
                        symbols,
                        stock_pool,
                        include_breakout=include_breakout or hot_strength >= 1.0,
                        conservative=True,
                        disable_momentum=True,
                        prefer_breakout_first=True,
                    )
                )
                if north_inflow > 0 and _is_flow_preferred_theme(theme_code):
                    templates.extend(_build_flow_templates(theme_name, theme_code, symbols, stock_pool))
            else:
                if risk_off or not _is_growth_theme(theme_code):
                    templates.extend(
                        _build_mean_reversion_templates(
                            theme_name,
                            theme_code,
                            symbols,
                            stock_pool,
                            include_gap_fill=include_gap_fill,
                        )
                    )
                templates.extend(_build_rotation_templates(theme_name, theme_code, symbols, stock_pool))
                templates.extend(
                    _build_trend_templates(
                        theme_name,
                        theme_code,
                        symbols,
                        stock_pool,
                        include_breakout=include_breakout and risk_on,
                        conservative=True,
                        disable_momentum=True,
                    )
                )
            templates = _filter_templates_by_allowed_types(
                templates,
                list(generation_profile.get("allowed_strategy_types") or []),
            )
            capped_templates = _dedupe_candidates(templates)[: int(generation_profile.get("candidate_cap") or 1)]
            for item in capped_templates:
                item["research_task"] = dict(input_data.get("research_task") or {})
            candidates.extend(capped_templates)
            continue
        if _is_defensive_theme(theme_code):
            templates.extend(
                _build_mean_reversion_templates(
                    theme_name,
                    theme_code,
                    symbols,
                    stock_pool,
                    include_gap_fill=include_gap_fill,
                )
            )
            logger.debug("Fallback: using mean-reversion templates for defensive theme '%s'", theme_code)
        elif _is_rotation_theme(theme_code):
            templates.extend(_build_rotation_templates(theme_name, theme_code, symbols, stock_pool))
            if risk_off:
                templates.extend(
                    _build_mean_reversion_templates(
                        theme_name,
                        theme_code,
                        symbols,
                        stock_pool,
                        include_gap_fill=True,
                    )
                )
            else:
                templates.extend(
                    _build_trend_templates(
                        theme_name,
                        theme_code,
                        symbols,
                        stock_pool,
                        include_breakout=include_breakout or hot_strength >= 1.0,
                    )
                )
        else:
            if risk_off and not _is_growth_theme(theme_code):
                templates.extend(
                    _build_mean_reversion_templates(
                        theme_name,
                        theme_code,
                        symbols,
                        stock_pool,
                        include_gap_fill=include_gap_fill,
                    )
                )
            templates.extend(
                _build_trend_templates(
                    theme_name,
                    theme_code,
                    symbols,
                    stock_pool,
                    include_breakout=include_breakout,
                )
            )

        if hot_sector_count >= 3 and _is_rotation_theme(theme_code):
            templates.extend(_build_rotation_templates(theme_name, theme_code, symbols, stock_pool))
        if north_inflow > 0 and _is_flow_preferred_theme(theme_code):
            templates.extend(_build_flow_templates(theme_name, theme_code, symbols, stock_pool))
        if risk_off and not _is_defensive_theme(theme_code):
            templates = _build_divergence_templates(theme_name, theme_code, symbols, stock_pool) + templates

        normal_templates = _dedupe_candidates(
            _filter_templates_by_allowed_types(
                templates,
                list(generation_profile.get("allowed_strategy_types") or []),
            )
        )[:4]
        for item in normal_templates:
            item["research_task"] = dict(input_data.get("research_task") or {})
        candidates.extend(normal_templates)

    return {"candidates": candidates[:6]}


def _match_sector_to_theme(sector_name: str) -> str:
    """将板块名称匹配到主题库中的 theme_code。"""
    sector_lower = sector_name.lower()
    for theme in EXTENDED_THEME_LIBRARY:
        for alias in list(theme.get("aliases") or []):
            if alias.lower() in sector_lower or sector_lower in alias.lower():
                return theme["theme_code"]
    return ""


# ---------------------------------------------------------------------------
# Stage Definitions — 注册表
# ---------------------------------------------------------------------------

def _build_stage_definitions() -> dict[str, StageDefinition]:
    """构建 5 个 Stage 的定义。"""
    from strategy_factory.api.constants import (
        PIPELINE_STAGE_MAX_TOKENS,
        PIPELINE_STAGE_TEMPERATURE,
    )

    return {
        "event_recognition": StageDefinition(
            stage_id="event_recognition",
            system_prompt=_PROMPT_EVENT_RECOGNITION,
            max_tokens=PIPELINE_STAGE_MAX_TOKENS.get("event_recognition", 600),
            temperature=PIPELINE_STAGE_TEMPERATURE.get("event_recognition", 0.2),
            required_output_keys=["events"],
            fallback_fn=_fallback_event_recognition,
        ),
        "theme_propagation": StageDefinition(
            stage_id="theme_propagation",
            system_prompt=_PROMPT_THEME_PROPAGATION,
            max_tokens=PIPELINE_STAGE_MAX_TOKENS.get("theme_propagation", 400),
            temperature=PIPELINE_STAGE_TEMPERATURE.get("theme_propagation", 0.2),
            required_output_keys=["themes"],
            fallback_fn=_fallback_theme_propagation,
        ),
        "exposure_mapping": StageDefinition(
            stage_id="exposure_mapping",
            system_prompt=_PROMPT_EXPOSURE_MAPPING,
            max_tokens=PIPELINE_STAGE_MAX_TOKENS.get("exposure_mapping", 500),
            temperature=PIPELINE_STAGE_TEMPERATURE.get("exposure_mapping", 0.25),
            required_output_keys=["exposures"],
            fallback_fn=_fallback_exposure_mapping,
        ),
        "market_confirmation": StageDefinition(
            stage_id="market_confirmation",
            system_prompt=_PROMPT_MARKET_CONFIRMATION,
            max_tokens=PIPELINE_STAGE_MAX_TOKENS.get("market_confirmation", 500),
            temperature=PIPELINE_STAGE_TEMPERATURE.get("market_confirmation", 0.15),
            required_output_keys=["confirmations"],
            fallback_fn=_fallback_market_confirmation,
        ),
        "strategy_generation": StageDefinition(
            stage_id="strategy_generation",
            system_prompt=_PROMPT_STRATEGY_GENERATION,
            max_tokens=PIPELINE_STAGE_MAX_TOKENS.get("strategy_generation", 800),
            temperature=PIPELINE_STAGE_TEMPERATURE.get("strategy_generation", 0.3),
            required_output_keys=["candidates"],
            fallback_fn=_fallback_strategy_generation,
        ),
    }


# 惰性初始化的全局注册表
_stage_registry: Optional[dict[str, StageDefinition]] = None


def get_stage_registry() -> dict[str, StageDefinition]:
    global _stage_registry
    if _stage_registry is None:
        _stage_registry = _build_stage_definitions()
    return _stage_registry


def validate_stage_output(stage_id: str, output: dict[str, Any]) -> bool:
    """验证阶段输出是否合法。"""
    validator = _VALIDATORS.get(stage_id)
    if validator is None:
        return True
    try:
        return validator(output)
    except Exception as _swallowed:
        logger.warning("packages/akshare-mcp/src/akshare_mcp/services/strategy_stages.py:1793: swallowed %s and continued: %s", type(_swallowed).__name__, _swallowed)
        return False


# Pipeline 阶段执行顺序
STAGE_ORDER: list[str] = [
    "event_recognition",
    "theme_propagation",
    "exposure_mapping",
    "market_confirmation",
    "strategy_generation",
]

