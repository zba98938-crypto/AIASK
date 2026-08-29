"""Runtime path helpers for factory maintenance scripts."""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from _integration_paths import default_sqlite_path, ensure_runtime_dirs


ensure_runtime_dirs()
DB_PATH = default_sqlite_path()
