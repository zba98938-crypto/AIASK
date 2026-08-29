
import io
import threading
from contextlib import redirect_stdout, redirect_stderr

try:
    import baostock as bs
    BAOSTOCK_IMPORT_ERROR: str | None = None
except ImportError as exc:
    bs = None
    BAOSTOCK_IMPORT_ERROR = str(exc)
import pandas as pd
from typing import Optional
from datetime import datetime, timedelta

class BaostockClient:
    _instance = None
    _logged_in = False
    _lock = threading.RLock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(BaostockClient, cls).__new__(cls)
        return cls._instance

    @property
    def available(self) -> bool:
        return bs is not None

    @property
    def unavailable_reason(self) -> str | None:
        return BAOSTOCK_IMPORT_ERROR

    def login(self):
        if bs is None:
            return False
        with self._lock:
            if not self._logged_in:
                stdout_buf = io.StringIO()
                stderr_buf = io.StringIO()
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    lg = bs.login()
                if lg.error_code == '0':
                    self._logged_in = True
                else:
                    import sys
                    print(f"Baostock login failed: {lg.error_msg}", file=sys.stderr)
        return self._logged_in

    def logout(self):
        with self._lock:
            if self._logged_in and bs is not None:
                stdout_buf = io.StringIO()
                stderr_buf = io.StringIO()
                with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
                    bs.logout()
                self._logged_in = False

    def normalize_code(self, code: str) -> str:
        """Ensure code is in sh.XXXXXX or sz.XXXXXX format"""
        if code.startswith(('sh.', 'sz.')):
            return code
        if code.startswith('6'):
            return f"sh.{code}"
        else:
            return f"sz.{code}"

    def get_balance_sheet(self, code: str, year: str, quarter: str) -> pd.DataFrame:
        """获取资产负债表"""
        with self._lock:
            if not self.login():
                return pd.DataFrame()
            bs_code = self.normalize_code(code)
            rs = bs.query_balance_data(code=bs_code, year=year, quarter=quarter)

            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())

            if not data_list:
                return pd.DataFrame()

            return pd.DataFrame(data_list, columns=rs.fields)

    def get_profit_statement(self, code: str, year: str, quarter: str) -> pd.DataFrame:
        """获取利润表"""
        with self._lock:
            if not self.login():
                return pd.DataFrame()
            bs_code = self.normalize_code(code)
            rs = bs.query_profit_data(code=bs_code, year=year, quarter=quarter)

            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())

            if not data_list:
                return pd.DataFrame()

            return pd.DataFrame(data_list, columns=rs.fields)

    def get_history_k_data(self, code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """获取历史K线"""
        with self._lock:
            if not self.login():
                return pd.DataFrame()
            bs_code = self.normalize_code(code)
            # frequency: d=日k, w=周, m=月
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,code,open,high,low,close,preclose,volume,amount,adjustflag,turn,tradestatus,pctChg,isST",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag="3",
            )  # 3=默认不复权，建议客户端处理，或者这里用2前复权

            data_list = []
            while (rs.error_code == '0') & rs.next():
                data_list.append(rs.get_row_data())

            if not data_list:
                return pd.DataFrame()

            return pd.DataFrame(data_list, columns=rs.fields)

# Global instance
baostock_client = BaostockClient()
