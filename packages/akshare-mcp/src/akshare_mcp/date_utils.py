
from datetime import datetime, timedelta, date
import os
import pandas as pd

_latest_trade_date_cache: date = None
_latest_trade_date_ts: float = 0

def get_latest_trading_date() -> str:
    """
    Get the latest confirmed trading date (YYYYMMDD).
    Uses caching to avoid frequent API calls.
    Data source: Tushare Pro trade_cal.
    """
    global _latest_trade_date_cache, _latest_trade_date_ts
    
    # Cache for 1 hour
    if _latest_trade_date_cache and (datetime.now().timestamp() - _latest_trade_date_ts < 3600):
        return _latest_trade_date_cache.strftime("%Y%m%d")

    try:
        import tushare as ts
        token = os.getenv("TUSHARE_TOKEN", "").strip()
        if token:
            ts.set_token(token)
            pro = ts.pro_api(token)
            today_str = date.today().strftime("%Y%m%d")
            start_str = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
            df = pro.trade_cal(exchange='SSE', start_date=start_str, end_date=today_str, is_open='1')
            if df is not None and not df.empty:
                df = df.sort_values("cal_date")
                last_date_str = str(df.iloc[-1]["cal_date"])
                last_date = datetime.strptime(last_date_str, "%Y%m%d").date()
                _latest_trade_date_cache = last_date
                _latest_trade_date_ts = datetime.now().timestamp()
                return last_date.strftime("%Y%m%d")
    except Exception:
        pass

    # Fallback: try reading from local DB
    try:
        import sqlite3
        db_path = os.getenv("AKSHARE_MCP_SQLITE_PATH") or os.getenv("AIASK_SQLITE_PATH")
        if db_path and os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT MAX(trade_date) FROM trading_dates WHERE trade_date <= ?", (date.today().isoformat(),)).fetchone()
            conn.close()
            if row and row[0]:
                d = datetime.strptime(str(row[0]).replace("-", "")[:8], "%Y%m%d").date()
                _latest_trade_date_cache = d
                _latest_trade_date_ts = datetime.now().timestamp()
                return d.strftime("%Y%m%d")
    except Exception:
        pass
    
    # Fallback: Today if weekday, else previous Friday
    d = date.today()
    if d.weekday() == 5:  # Sat
        d = d - timedelta(days=1)
    elif d.weekday() == 6:  # Sun
        d = d - timedelta(days=2)
    
    return d.strftime("%Y%m%d")

def format_date_dash(date_str: str) -> str:
    """Convert YYYYMMDD to YYYY-MM-DD"""
    if len(date_str) == 8:
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    return date_str
