"""SQLite 策略超市 Mixin — CRUD / 静态工具 / 工厂 / 质量报告 / 领域事件"""

import hashlib
import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from ._strategy_crud_utils import _StrategyCrudUtilsMixin
from ._strategy_crud_core import _StrategyCrudCoreMixin
from ._strategy_crud_market import _StrategyCrudMarketMixin
from ._strategy_crud_quality import _StrategyCrudQualityMixin


class StrategyCrudMixin(_StrategyCrudUtilsMixin, _StrategyCrudCoreMixin, _StrategyCrudMarketMixin, _StrategyCrudQualityMixin):
        """静态工具方法 + 策略 CRUD + 工厂辅助 + 质量报告 + 领域事件 + 每日快照"""
