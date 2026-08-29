"""Cross-host contracts owned by Strategy Factory.

These are the shapes every host and every caller agrees on:

* :mod:`strategy_factory.contracts.run_envelope` — the governed ``--json``
  envelope every control-plane surface emits.
* :mod:`strategy_factory.contracts.host_adapter` — what a host must implement to
  run AIASK.
* :mod:`strategy_factory.contracts.execution_universe` — which strategies are
  executable.

This file exists for a concrete reason: without it the directory is an implicit
namespace package, which resolves during development (the test ``pythonpath``
covers it) but fails after an editable install with
``ModuleNotFoundError: strategy_factory.contracts.host_adapter``. A clean-install
run caught that; every other subpackage here already declares itself.

Nothing is re-exported. Callers outside the package should use
``strategy_factory.api.runtime``, which is the supported public surface.
"""

from __future__ import annotations

__all__: list[str] = []
