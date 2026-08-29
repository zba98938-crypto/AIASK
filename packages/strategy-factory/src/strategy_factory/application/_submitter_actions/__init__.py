"""Submitter-actions facade package."""

from . import runner as _runner

globals().update(
    {
        name: getattr(_runner, name)
        for name in dir(_runner)
        if not name.startswith("__")
    }
)
