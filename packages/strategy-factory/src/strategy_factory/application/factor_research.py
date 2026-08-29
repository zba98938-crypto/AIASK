"""Compatibility alias for factor research.

The implementation moved to ``application.research.factor_research_builder``.
This module now aliases itself to that implementation module so legacy imports
and monkeypatch-based tests keep targeting the live globals.
"""

from __future__ import annotations

import sys

from .research import factor_research_builder as _impl

sys.modules[__name__] = _impl
