"""Model registry and retrain governance handlers for quant_manager."""

from __future__ import annotations

import time
from copy import deepcopy
from datetime import datetime
from typing import Any, Callable
from uuid import uuid4

import numpy as np

from ...services import get_artifact_async, list_artifacts_async, register_artifact_async
from ...services import model_retrain_scheduler as model_retrain_scheduler_module
from ...services.rolling_model_registry import default_rolling_registry
from .quant_mgr_artifact_common import QuantManagerCall, _payload_from_artifact_row
from .quant_mgr_helpers import _as_code_list, _safe_float
from .quant_mgr_registry import _list_factor_candidate_registry_items

MODEL_REGISTRY_STRATEGY = "quant_model_registry"
MODEL_REGISTRY_VERSION = "p2.v1"
MODEL_FEEDBACK_STRATEGY = "quant_model_feedback"
MODEL_FEEDBACK_VERSION = "p2.v1"
MODEL_RETRAIN_PLAN_STRATEGY = "quant_model_retrain_plan"
MODEL_RETRAIN_PLAN_VERSION = "p2.v3"
MODEL_RETRAIN_RUN_STRATEGY = "quant_model_retrain_run"
MODEL_RETRAIN_RUN_VERSION = "p2.v3"

DEFAULT_STABILITY_FLOOR = 0.35
DEFAULT_DEGRADATION_CEILING = 0.08
DEFAULT_TIGHT_RACE_GAP = 5.0
DEFAULT_REPLAY_SUCCESS_FLOOR = 0.60

from ._quant_mgr_model_registry_catalog import *
from ._quant_mgr_model_registry_feedback import *


__all__ = [name for name in globals() if name.startswith("_") or name.isupper()]
