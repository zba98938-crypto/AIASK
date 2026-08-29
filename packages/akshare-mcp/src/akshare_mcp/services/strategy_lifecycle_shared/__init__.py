"""Strategy lifecycle facade package."""

from . import closure_review as _closure_review
from . import confidence as _confidence
from . import execution_audit_snapshot as _execution_audit_snapshot
from . import execution_quality as _execution_quality
from . import incubation as _incubation
from . import overview as _overview
from . import presentation as _presentation
from . import prediction_trace as _prediction_trace
from . import state_machine as _state_machine

for _module in (
    _state_machine,
    _closure_review,
    _confidence,
    _execution_audit_snapshot,
    _execution_quality,
    _incubation,
    _prediction_trace,
    _presentation,
    _overview,
):
    globals().update(
        {
            name: getattr(_module, name)
            for name in dir(_module)
            if not name.startswith("__")
        }
    )
