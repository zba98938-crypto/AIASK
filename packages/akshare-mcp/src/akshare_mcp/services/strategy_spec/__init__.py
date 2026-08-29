"""Strategy spec facade package."""

from . import constants as _constants
from . import defaults as _defaults
from . import dsl_builder as _dsl_builder
from . import model as _model
from . import normalizers as _normalizers
from . import runtime_contracts as _runtime_contracts

for _module in (
    _constants,
    _normalizers,
    _defaults,
    _dsl_builder,
    _runtime_contracts,
    _model,
):
    globals().update(
        {
            name: getattr(_module, name)
            for name in dir(_module)
            if not name.startswith("__")
        }
    )
