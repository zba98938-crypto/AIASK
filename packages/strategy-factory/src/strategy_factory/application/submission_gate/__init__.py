"""Submission-gate facade package."""

from . import runner as _runner

globals().update(
    {
        name: getattr(_runner, name)
        for name in dir(_runner)
        if not name.startswith("__")
    }
)

__all__ = [name for name in globals() if not name.startswith("_")]
