"""Gateway package facade (split from single gateway.py module)."""

from . import models as _models
from . import stores as _stores
from . import http_client as _http_client
from . import adapters as _adapters
from . import router as _router
from . import runtime as _runtime

for _m in (_models, _stores, _http_client, _adapters, _router, _runtime):
    globals().update({n: getattr(_m, n) for n in dir(_m) if not n.startswith('__')})
