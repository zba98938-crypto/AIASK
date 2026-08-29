from __future__ import annotations

import importlib.util

from akshare_mcp.services.adapters.data_validation_adapter import get_data_validation_adapter


def test_gx_adapter_reports_requested_backend_and_real_fallback_state():
    adapter = get_data_validation_adapter(prefer_gx=True)
    result = adapter.validate_dataset(
        records=[{"close": 10.0, "volume": 100, "regime": "bull"}],
        expectations={
            "required_fields": ["close", "volume", "regime"],
            "field_types": {"close": "float", "volume": "int", "regime": "string"},
            "allowed_values": {"regime": ["bull", "bear"]},
            "min_quality_threshold": 0.9,
        },
    )

    payload = result.to_dict()
    gx_installed = importlib.util.find_spec("great_expectations") is not None
    assert payload["backend_requested"] in {"builtin", "great_expectations"}
    assert payload["backend_used"] in {"builtin", "great_expectations"}
    if gx_installed and payload["backend_requested"] == "great_expectations":
        assert payload["backend_used"] == "great_expectations"
        assert payload["fallback_used"] is False
    else:
        assert payload["backend_used"] == "builtin"
        if payload["backend_requested"] == "great_expectations":
            assert payload["fallback_used"] is True
            assert payload["fallback_reason"] in {
                "great_expectations_not_installed",
                "great_expectations_runtime_not_configured",
            } or str(payload["fallback_reason"]).startswith("great_expectations_runtime_failed:")
        else:
            assert payload["fallback_used"] is False
