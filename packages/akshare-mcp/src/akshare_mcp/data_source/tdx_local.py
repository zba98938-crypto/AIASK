"""通达信本地数据源（TDX）。

本模块负责把"通达信"统一抽象成两路：
1. **本地 vipdoc**：直接读取 `${TDX_INSTALL_DIR}/vipdoc/{sh,sz,bj}/lday/*.day` 等
   二进制文件，零网络、零鉴权，最快路径。基于 `mootdx.reader.Reader`，
   失败时退化为纯 struct 解析以避免 mootdx 不可用。
2. **在线行情**：通过 `pytdx.hq.TdxHq_API` 连接通达信公网行情服务器，
   补齐"实时报价 / 分钟 K / 盘口 / 分笔 / 财务"等本地文件缺失或滞后的数据。

设计原则：
- 单例 + 多 IP 故障切换池，连接懒加载。
- 同步阻塞型 API，调用方需在异步路径用 `asyncio.to_thread` 包裹。
- 输出字段与 `data_source/quotes.py` / `market_data.py` 已存在的契约保持一致
  （日期 `YYYY-MM-DD`、`open/high/low/close/volume/amount`、价格元、量股、额元）。
- 找不到本地数据 / 在线连接失败时，统一返回空，由上游决定是否继续降级。

环境变量：
- `TDX_INSTALL_DIR` 通达信安装目录，含 `vipdoc/`，例：`C:\\new_tdx_test`
- `TDX_DATA_SOURCE` 数据源偏好：`local`(默认) / `online` / `local_first` / `online_first`
- `TDX_LOCAL_ONLY`  设为 `1` 时禁用所有非本地降级
- `TDX_SERVER_POOL` 行情服务器列表，逗号分隔 `ip:port`，覆盖默认池
- `TDX_CONNECT_TIMEOUT_MS` 连接超时（毫秒，默认 4000）
- `TDX_KLINE_FAVOR_LOCAL` `1` 表示日线优先用本地文件（默认 1）
"""

from __future__ import annotations

import logging
import os
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 公共常量
# ---------------------------------------------------------------------------

# 通达信公网行情服务器（按地理位置和稳定性排序）
DEFAULT_TDX_HQ_SERVERS: list[tuple[str, int]] = [
    ("119.147.212.81", 7709),  # 招商证券 — 长期稳定
    ("180.153.18.170", 7709),  # 上海电信主站
    ("180.153.39.51", 7709),
    ("114.80.63.12", 7709),
    ("60.191.117.167", 7709),
    ("218.108.50.178", 7709),
    ("218.108.98.244", 7709),
    ("221.231.141.60", 7709),
    ("110.80.110.235", 7709),
    ("60.190.118.68", 7709),
    ("221.194.181.176", 7709),
    ("125.39.85.25", 7709),
]

# pytdx 周期编码：5/15/30/60 分钟、日、周、月、1 分钟、季度、年
_PYTDX_CATEGORY = {
    "5min": 0,
    "15min": 1,
    "30min": 2,
    "60min": 3,
    "daily": 4,
    "weekly": 5,
    "monthly": 6,
    "1min": 8,
    "quarterly": 11,
    "yearly": 12,
}


def _normalize_period(period: str) -> str:
    raw = (period or "daily").strip().lower()
    aliases = {
        "d": "daily", "1d": "daily", "day": "daily",
        "w": "weekly", "1w": "weekly", "week": "weekly",
        "m": "monthly", "1m_period": "monthly", "month": "monthly",
        "1m": "1min", "5m": "5min", "15m": "15min",
        "30m": "30min", "60m": "60min", "1h": "60min", "1hour": "60min",
    }
    return aliases.get(raw, raw)


def _detect_market(code: str) -> int:
    """0=深圳/北交所, 1=上海。

    上海：60/68/9（B 股）/5（指数 ETF）。
    北交所：4/8 起头（mootdx 走 sz 目录扩展前缀），
    其余按深圳处理。
    """
    code = (code or "").strip().lstrip("0").zfill(6) or "000000"
    if code.startswith(("6", "9", "5")):
        return 1
    return 0


def _exchange_dir(code: str) -> str:
    code = (code or "").strip().zfill(6)
    if code.startswith(("4", "8")):
        return "bj"
    if code.startswith(("6", "9", "5")):
        return "sh"
    return "sz"


# ---------------------------------------------------------------------------
# 本地 vipdoc 读取
# ---------------------------------------------------------------------------

@dataclass
class _LocalDayBar:
    date: str
    open: float
    high: float
    low: float
    close: float
    amount: float       # 元
    volume: int         # 股（已 *100 转换自"手"）
    pre_close: Optional[float] = None
    change_pct: Optional[float] = None


def _parse_local_day_file(path: Path, limit: Optional[int] = None) -> list[_LocalDayBar]:
    """直接解析 *.day 二进制文件。

    通达信日线 32 字节/记录：
        I 4B  YYYYMMDD（整型）
        I 4B  open  * 100（注：mootdx/部分实现里是 *1000，实测自 C:\\new_tdx_test 为 *100）
        I 4B  high  * 100
        I 4B  low   * 100
        I 4B  close * 100
        f 4B  amount（元，float）
        I 4B  volume（手）
        I 4B  reserved / 上日收盘
    为避免不同版本字段定义差异（*100 vs *1000），读取后用 mootdx 同款"价格区间合理性"
    自动识别 scale 因子，老旧文件最大支撑也能正确还原。
    """
    if not path.exists() or path.stat().st_size < 32:
        return []
    raw = path.read_bytes()
    bars: list[_LocalDayBar] = []
    n = len(raw) // 32
    if limit and limit > 0:
        start = max(0, n - int(limit))
    else:
        start = 0

    # 自动嗅探价格 scale：用最后一条收盘价判断 *100 还是 *1000
    sample_off = (n - 1) * 32
    if sample_off >= 0:
        sample = struct.unpack_from("<IIIIIfII", raw, sample_off)
        close_raw = sample[4]
        # 实证：A 股多数股票收盘价 ≤ 4000 元（*1000 ≤ 4_000_000；*100 ≤ 400_000）
        scale = 100.0 if close_raw <= 1_500_000 else 1000.0
    else:
        scale = 100.0

    for i in range(start, n):
        offset = i * 32
        date_int, op, hi, lo, cl, amt, vol, _resv = struct.unpack_from("<IIIIIfII", raw, offset)
        if date_int <= 0:
            continue
        ymd = f"{date_int // 10000:04d}-{(date_int // 100) % 100:02d}-{date_int % 100:02d}"
        bars.append(
            _LocalDayBar(
                date=ymd,
                open=round(op / scale, 4),
                high=round(hi / scale, 4),
                low=round(lo / scale, 4),
                close=round(cl / scale, 4),
                amount=float(amt),
                volume=int(vol) * 100,  # 手 → 股
            )
        )

    # 反推 pre_close 与 change_pct
    for idx in range(1, len(bars)):
        prev = bars[idx - 1].close
        cur = bars[idx]
        cur.pre_close = prev
        if prev:
            cur.change_pct = round((cur.close - prev) / prev * 100, 4)

    return bars


# 5/1 分钟线（lc5/lc1 文件）每条 32 字节
def _parse_local_minute_file(path: Path, limit: Optional[int] = None) -> list[dict]:
    """解析 *.lc5 / *.lc1 分钟线。"""
    if not path.exists() or path.stat().st_size < 32:
        return []
    raw = path.read_bytes()
    bars: list[dict] = []
    n = len(raw) // 32
    if limit and limit > 0:
        start = max(0, n - int(limit))
    else:
        start = 0
    for i in range(start, n):
        offset = i * 32
        # 头 4 字节：日期 + 时间分钟数；后续浮点价 + 量
        date_short, time_short, op, hi, lo, cl, amt, vol = struct.unpack_from(
            "<HHfffffI", raw, offset
        )
        # date_short = (year - 2004) * 2048 + month * 100 + day
        year = (date_short >> 11) + 2004
        month = (date_short % 2048) // 100
        day = (date_short % 2048) % 100
        hour = time_short // 60
        minute = time_short % 60
        ts = f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}:00"
        bars.append(
            {
                "date": ts,
                "open": float(op),
                "high": float(hi),
                "low": float(lo),
                "close": float(cl),
                "amount": float(amt),
                "volume": int(vol) * 100,
            }
        )
    return bars


# ---------------------------------------------------------------------------
# 单例客户端
# ---------------------------------------------------------------------------

class TdxLocalSource:
    """通达信本地 + 在线行情统一封装。

    线程安全（单 pytdx 连接 + Lock）；耗时操作必须由调用方放到线程池。
    """

    _instance: Optional["TdxLocalSource"] = None
    _instance_lock = threading.Lock()

    def __new__(cls) -> "TdxLocalSource":
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    inst = super().__new__(cls)
                    inst._init()
                    cls._instance = inst
        return cls._instance

    # 显式禁止 __init__ 重置状态（单例）
    def __init__(self) -> None:  # noqa: D401
        return

    def _init(self) -> None:
        self._tdx_dir = self._resolve_install_dir()
        self._mode = (os.getenv("TDX_DATA_SOURCE") or "local_first").strip().lower()
        self._local_only = os.getenv("TDX_LOCAL_ONLY", "0") == "1"
        self._connect_timeout = max(1.0, float(os.getenv("TDX_CONNECT_TIMEOUT_MS", "4000")) / 1000.0)
        self._kline_favor_local = os.getenv("TDX_KLINE_FAVOR_LOCAL", "1") != "0"

        pool = os.getenv("TDX_SERVER_POOL", "").strip()
        if pool:
            servers: list[tuple[str, int]] = []
            for token in pool.split(","):
                token = token.strip()
                if not token:
                    continue
                if ":" in token:
                    ip, port = token.split(":", 1)
                    try:
                        servers.append((ip.strip(), int(port.strip())))
                    except ValueError:
                        continue
                else:
                    servers.append((token, 7709))
            self._servers = servers or list(DEFAULT_TDX_HQ_SERVERS)
        else:
            ip = (os.getenv("TDX_SERVER_IP") or "").strip()
            port = int(os.getenv("TDX_SERVER_PORT") or "7709")
            if ip:
                self._servers = [(ip, port), *DEFAULT_TDX_HQ_SERVERS]
            else:
                self._servers = list(DEFAULT_TDX_HQ_SERVERS)

        self._mootdx_reader: Any = None
        self._hq_api: Any = None
        self._hq_server: Optional[tuple[str, int]] = None
        self._hq_lock = threading.RLock()
        self._last_hq_failure_at: float = 0.0
        self._hq_cooldown_sec = 30.0

        logger.info(
            "[TdxLocal] init tdx_dir=%s mode=%s local_only=%s pool=%d",
            self._tdx_dir,
            self._mode,
            self._local_only,
            len(self._servers),
        )

    # ------------------------------------------------------------------
    # 配置
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_install_dir() -> Optional[Path]:
        for key in ("TDX_INSTALL_DIR", "TDX_DATA_DIR", "TDX_HOME"):
            val = (os.getenv(key) or "").strip()
            if val:
                p = Path(val).expanduser()
                if (p / "vipdoc").exists():
                    return p
        # 常见默认路径
        candidates = [
            Path(r"C:\new_tdx_test"),
            Path(r"C:\new_tdx"),
            Path(r"C:\Tdx"),
            Path(r"C:\通达信"),
            Path(r"D:\new_tdx"),
        ]
        for c in candidates:
            if (c / "vipdoc").exists():
                return c
        return None

    @property
    def install_dir(self) -> Optional[Path]:
        return self._tdx_dir

    @property
    def has_local(self) -> bool:
        return self._tdx_dir is not None

    @property
    def is_local_only(self) -> bool:
        return self._local_only

    def status(self) -> dict[str, Any]:
        return {
            "tdx_dir": str(self._tdx_dir) if self._tdx_dir else None,
            "mode": self._mode,
            "local_only": self._local_only,
            "servers": list(self._servers[:3]),
            "hq_connected": self._hq_api is not None,
            "kline_favor_local": self._kline_favor_local,
        }

    # ------------------------------------------------------------------
    # mootdx Reader（本地 vipdoc）
    # ------------------------------------------------------------------

    def _get_reader(self) -> Any:
        if self._mootdx_reader is not None:
            return self._mootdx_reader
        if not self.has_local:
            return None
        try:
            from mootdx.reader import Reader  # type: ignore

            self._mootdx_reader = Reader.factory(market="std", tdxdir=str(self._tdx_dir))
            logger.info("[TdxLocal] mootdx Reader ready: %s", self._tdx_dir)
        except Exception as exc:
            logger.warning("[TdxLocal] mootdx unavailable, fallback to struct parser: %s", exc)
            self._mootdx_reader = False  # 标记不可用
        return self._mootdx_reader if self._mootdx_reader else None

    # ------------------------------------------------------------------
    # 在线 pytdx 连接
    # ------------------------------------------------------------------

    def _ensure_hq(self) -> Any:
        if self._local_only:
            return None
        if self._hq_api is not None:
            return self._hq_api
        if time.time() - self._last_hq_failure_at < self._hq_cooldown_sec:
            return None
        try:
            from pytdx.hq import TdxHq_API  # type: ignore
        except ImportError:
            logger.warning("[TdxLocal] pytdx not installed; online quotes disabled")
            self._last_hq_failure_at = time.time()
            return None
        with self._hq_lock:
            if self._hq_api is not None:
                return self._hq_api
            api = TdxHq_API(heartbeat=True, auto_retry=True)
            for ip, port in self._servers:
                try:
                    if api.connect(ip, port, time_out=self._connect_timeout):
                        self._hq_api = api
                        self._hq_server = (ip, port)
                        logger.info("[TdxLocal] HQ connected via %s:%d", ip, port)
                        return api
                except Exception as exc:
                    logger.debug("[TdxLocal] connect %s:%d failed: %s", ip, port, exc)
                    continue
            self._last_hq_failure_at = time.time()
            logger.warning("[TdxLocal] all HQ servers failed; cooldown %.1fs", self._hq_cooldown_sec)
            return None

    def reset_hq(self) -> None:
        with self._hq_lock:
            if self._hq_api is not None:
                try:
                    self._hq_api.disconnect()
                except Exception:
                    pass
            self._hq_api = None
            self._hq_server = None

    # ------------------------------------------------------------------
    # 公开 API：日线 / 周线 / 月线 K 线
    # ------------------------------------------------------------------

    def get_kline(
        self,
        code: str,
        period: str = "daily",
        limit: int = 100,
    ) -> list[dict]:
        """返回 list[dict]，字段与现有 data_source.get_kline 兼容：
        date(YYYY-MM-DD) / open / close / high / low / volume / amount /
        change_pct / source。
        """
        period = _normalize_period(period)
        # 1) 日线优先本地（mootdx Reader 或 struct 解析）
        if period == "daily" and self._kline_favor_local and self.has_local:
            rows = self._kline_local_daily(code, limit=limit)
            if rows:
                return rows

        # 2) mootdx 也能读 5/15/30/60 分钟、周、月 离线文件
        if period in {"5min", "15min", "30min", "60min", "1min", "weekly", "monthly"} and self.has_local:
            rows = self._kline_local_other(code, period=period, limit=limit)
            if rows:
                return rows

        # 3) 在线 pytdx
        if not self._local_only:
            rows = self._kline_online(code, period=period, limit=limit)
            if rows:
                return rows

        return []

    def _kline_local_daily(self, code: str, limit: int) -> list[dict]:
        if not self._tdx_dir:
            return []
        sub = _exchange_dir(code)
        path = self._tdx_dir / "vipdoc" / sub / "lday" / f"{sub}{code.zfill(6)}.day"
        # 优先 mootdx Reader（已用 pandas 处理 scale 与日期），失败回退纯 struct
        reader = self._get_reader()
        if reader is not None:
            try:
                df = reader.daily(symbol=code.zfill(6))
                if df is not None and not df.empty:
                    if hasattr(df, "tail"):
                        df = df.tail(int(limit))
                    rows: list[dict] = []
                    for ts, row in df.iterrows():
                        date_str = self._row_date(ts, row)
                        op = float(row.get("open", 0) or 0)
                        hi = float(row.get("high", 0) or 0)
                        lo = float(row.get("low", 0) or 0)
                        cl = float(row.get("close", 0) or 0)
                        amt = float(row.get("amount", 0) or 0)
                        vol = float(row.get("volume", row.get("vol", 0)) or 0)
                        rows.append(
                            {
                                "date": date_str,
                                "open": op,
                                "high": hi,
                                "low": lo,
                                "close": cl,
                                "volume": int(vol),
                                "amount": amt,
                                "source": "tdx_local",
                            }
                        )
                    self._fill_change_pct(rows)
                    return rows
            except Exception as exc:
                logger.debug("[TdxLocal] mootdx daily fail %s: %s", code, exc)
        # struct 兜底
        bars = _parse_local_day_file(path, limit=limit)
        return [
            {
                "date": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
                "amount": b.amount,
                "change_pct": b.change_pct,
                "source": "tdx_local",
            }
            for b in bars
        ]

    def _kline_local_other(self, code: str, period: str, limit: int) -> list[dict]:
        reader = self._get_reader()
        if reader is None:
            return []
        try:
            if period in {"weekly"}:
                df = getattr(reader, "weekly", None)
                df = df(symbol=code.zfill(6)) if df else None
            elif period in {"monthly"}:
                df = getattr(reader, "monthly", None)
                df = df(symbol=code.zfill(6)) if df else None
            elif period == "1min":
                df = reader.minute(symbol=code.zfill(6))
            else:
                # 5/15/30/60 分钟
                df = reader.fzline(symbol=code.zfill(6))
            if df is None or getattr(df, "empty", True):
                return []
            df = df.tail(int(limit)) if hasattr(df, "tail") else df
            rows: list[dict] = []
            for ts, row in df.iterrows():
                rows.append(
                    {
                        "date": self._row_date(ts, row),
                        "open": float(row.get("open", 0) or 0),
                        "high": float(row.get("high", 0) or 0),
                        "low": float(row.get("low", 0) or 0),
                        "close": float(row.get("close", 0) or 0),
                        "volume": int(float(row.get("volume", row.get("vol", 0)) or 0)),
                        "amount": float(row.get("amount", 0) or 0),
                        "source": "tdx_local",
                    }
                )
            self._fill_change_pct(rows)
            return rows
        except Exception as exc:
            logger.debug("[TdxLocal] mootdx %s fail %s: %s", period, code, exc)
            return []

    def _kline_online(self, code: str, period: str, limit: int) -> list[dict]:
        api = self._ensure_hq()
        if api is None:
            return []
        cat = _PYTDX_CATEGORY.get(period)
        if cat is None:
            return []
        market = _detect_market(code)
        sym = code.zfill(6)
        # pytdx 单次最多 800 条
        bar_count = min(int(limit), 800)
        try:
            with self._hq_lock:
                bars = api.get_security_bars(cat, market, sym, 0, bar_count)
        except Exception as exc:
            logger.warning("[TdxLocal] pytdx bars fail %s/%s: %s", code, period, exc)
            self.reset_hq()
            return []
        if not bars:
            return []
        rows: list[dict] = []
        for b in bars:
            dt_str = str(b.get("datetime") or "")
            ymd = dt_str[:10] if period in {"daily", "weekly", "monthly"} else dt_str
            rows.append(
                {
                    "date": ymd,
                    "open": float(b.get("open") or 0),
                    "high": float(b.get("high") or 0),
                    "low": float(b.get("low") or 0),
                    "close": float(b.get("close") or 0),
                    "volume": int(float(b.get("vol") or 0) * 100),
                    "amount": float(b.get("amount") or 0),
                    "source": "tdx_online",
                }
            )
        self._fill_change_pct(rows)
        return rows

    @staticmethod
    def _row_date(ts: Any, row: Any) -> str:
        # mootdx 把日期作为 index（pandas Timestamp 或字符串）
        try:
            if hasattr(ts, "strftime"):
                return ts.strftime("%Y-%m-%d %H:%M:%S") if getattr(ts, "hour", 0) or getattr(ts, "minute", 0) else ts.strftime("%Y-%m-%d")
        except Exception:
            pass
        text = str(ts or row.get("date", "") or "")
        return text[:19] if " " in text else text[:10]

    @staticmethod
    def _fill_change_pct(rows: list[dict]) -> None:
        for i in range(1, len(rows)):
            prev_close = rows[i - 1].get("close")
            cur_close = rows[i].get("close")
            if prev_close and cur_close is not None:
                rows[i]["pre_close"] = float(prev_close)
                if prev_close:
                    rows[i]["change_pct"] = round((cur_close - prev_close) / prev_close * 100, 4)

    # ------------------------------------------------------------------
    # 实时行情
    # ------------------------------------------------------------------

    def get_realtime_quote(self, code: str) -> Optional[dict]:
        """单只股票实时报价，输出与 data_source.get_realtime_quote 兼容的字典。"""
        if self._local_only:
            # 本地模式下用最新两根日线推算"快照"
            return self._snapshot_from_local(code)
        api = self._ensure_hq()
        if api is None:
            return self._snapshot_from_local(code)
        try:
            sym = code.zfill(6)
            with self._hq_lock:
                quotes = api.get_security_quotes([(_detect_market(sym), sym)])
        except Exception as exc:
            logger.warning("[TdxLocal] pytdx quote fail %s: %s", code, exc)
            self.reset_hq()
            return self._snapshot_from_local(code)
        if not quotes:
            return self._snapshot_from_local(code)
        q = quotes[0]
        price = float(q.get("price") or 0) or None
        last_close = float(q.get("last_close") or 0) or None
        change = (price - last_close) if (price is not None and last_close) else None
        change_pct = (change / last_close * 100) if change is not None and last_close else None
        return {
            "code": sym,
            "name": "",  # pytdx 不返回中文名（需走 stock_basic 缓存）
            "price": price,
            "change": change,
            "changePercent": change_pct,
            "open": float(q.get("open") or 0) or None,
            "high": float(q.get("high") or 0) or None,
            "low": float(q.get("low") or 0) or None,
            "preClose": last_close,
            "volume": int(float(q.get("vol") or 0) * 100) or None,
            "amount": float(q.get("amount") or 0) or None,
            "turnoverRate": None,
            "source": "tdx_online",
        }

    def _snapshot_from_local(self, code: str) -> Optional[dict]:
        rows = self._kline_local_daily(code, limit=2)
        if not rows:
            return None
        last = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else None
        prev_close = prev.get("close") if prev else None
        price = last.get("close")
        change = (price - prev_close) if (price is not None and prev_close is not None) else None
        change_pct = (change / prev_close * 100) if change is not None and prev_close else None
        return {
            "code": code.zfill(6),
            "name": "",
            "price": price,
            "change": change,
            "changePercent": change_pct,
            "open": last.get("open"),
            "high": last.get("high"),
            "low": last.get("low"),
            "preClose": prev_close,
            "volume": last.get("volume"),
            "amount": last.get("amount"),
            "turnoverRate": None,
            "source": "tdx_local_snapshot",
        }

    # ------------------------------------------------------------------
    # 交易日历（基于本地任一活跃股票的日线日期反推）
    # ------------------------------------------------------------------

    def get_trading_dates(
        self,
        *,
        start_date: str = "",
        end_date: str = "",
        count: int = -1,
    ) -> list[str]:
        """返回 YYYYMMDD 格式列表，升序。

        优先级：本地"sh000001（上证指数）"日线 → pytdx K 线接口。
        """
        rows: list[dict] = []
        if self.has_local:
            # 上证综指代码 sh000001，本地必有
            rows = self._kline_local_daily("000001", limit=10000)
            if not rows:
                # 退回深证成指
                rows = self._kline_local_daily("399001", limit=10000)
        if not rows and not self._local_only:
            rows = self._kline_online("000001", "daily", 4000)

        dates = [str(r.get("date") or "")[:10].replace("-", "") for r in rows]
        dates = [d for d in dates if len(d) == 8]
        if start_date:
            dates = [d for d in dates if d >= start_date]
        if end_date:
            dates = [d for d in dates if d <= end_date]
        dates.sort()
        if count and count > 0:
            dates = dates[-int(count):]
        return dates

    # ------------------------------------------------------------------
    # 股票列表（可选）— 通过 vipdoc/sh,sz,bj/lday/*.day 文件名汇总
    # ------------------------------------------------------------------

    def list_local_stocks(self) -> list[dict]:
        """列出本地 vipdoc 中的 A 股个股代码（排除指数/ETF/可转债/B股/板块指数）。"""
        if not self.has_local:
            return []
        result: list[dict] = []
        for sub in ("sh", "sz", "bj"):
            d = self._tdx_dir / "vipdoc" / sub / "lday"
            if not d.exists():
                continue
            for fp in d.glob(f"{sub}*.day"):
                code = fp.stem.replace(sub, "", 1)
                if not code.isdigit() or len(code) != 6:
                    continue
                # 只保留 A 股个股
                if sub == "sh":
                    # 6 开头 = 沪市主板/科创板个股
                    if not code.startswith("6"):
                        continue
                elif sub == "sz":
                    # 0 开头 = 深市主板，3 开头 = 创业板（排除 399xxx 指数）
                    if not (code.startswith("0") or code.startswith("3")):
                        continue
                    if code.startswith("399"):
                        continue
                elif sub == "bj":
                    # 4/8 开头 = 北交所个股
                    if not (code.startswith("4") or code.startswith("8")):
                        continue
                result.append({"code": code, "market": sub})
        return result

    # ------------------------------------------------------------------
    # 财务数据（pytdx get_finance_info）
    # ------------------------------------------------------------------

    def get_finance_info(self, code: str) -> dict:
        api = self._ensure_hq()
        if api is None:
            return {}
        try:
            with self._hq_lock:
                info = api.get_finance_info(_detect_market(code), code.zfill(6))
        except Exception as exc:
            logger.warning("[TdxLocal] pytdx finance fail %s: %s", code, exc)
            self.reset_hq()
            return {}
        if not info:
            return {}
        return dict(info)


# 模块级单例 + 便捷访问器
tdx_local_source = TdxLocalSource()


def get_tdx_local_source() -> TdxLocalSource:
    """返回全局 TdxLocalSource 单例。"""
    return tdx_local_source
