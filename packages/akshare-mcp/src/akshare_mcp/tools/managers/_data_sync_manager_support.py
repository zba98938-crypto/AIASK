"""数据同步管理器 - MCP 工具层，供用户/AI 按需触发同步任务。

与 DataSyncScheduler (services/data_sync_scheduler.py) 的区别：
- DataSyncScheduler 是后台自动调度器（启动时 + 每日 15:30）
- 本模块是 MCP 工具，通过 ``data_sync_manager(action=...)`` 按需执行
- sync_daily/sync_init.py 是独立脚本，用于深度历史全量回填
"""

from typing import Any
import argparse
import contextlib
import importlib.util
import json
import logging
from io import StringIO
from pathlib import Path
from datetime import datetime, timedelta
from ...storage import get_db
from ...utils import ok, fail
from ..manager_protocol import normalize_manager_payload

logger = logging.getLogger(__name__)

from ._data_sync_manager_support_core import *
from ._data_sync_manager_support_sync import *
