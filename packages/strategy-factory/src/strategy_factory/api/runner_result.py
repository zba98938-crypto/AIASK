"""Atomic result channel between a one-shot runner and its factory worker.

Logs are for people; a worker must not infer success from a truncated log tail.
The path is supplied by the worker in its private temporary directory.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

RESULT_PATH_ENV = "AIASK_FACTORY_RESULT_PATH"
RESULT_CONTRACT_VERSION = "aiask.factory_result.v1"


def write_runner_result(factory: str, result: dict[str, Any]) -> None:
    configured = os.environ.get(RESULT_PATH_ENV)
    if not configured:
        return
    path = Path(configured)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(
                {"contract_version": RESULT_CONTRACT_VERSION, "factory": factory, "result": result},
                ensure_ascii=False,
                default=str,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
