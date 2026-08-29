from __future__ import annotations

import importlib.util

from akshare_mcp.services.adapters.mapie_adapter import get_conformal_adapter


def test_conformal_adapter_exposes_requested_backend_and_runtime_state():
    adapter = get_conformal_adapter(prefer_mapie=True)
    result = adapter.predict_set(
        calibration_scores=[
            0.05, 0.08, 0.12, 0.18, 0.22, 0.28,
            0.34, 0.39, 0.44, 0.49, 0.53, 0.58,
            0.62, 0.68, 0.73, 0.79, 0.84, 0.88,
        ],
        calibration_labels=[0, 0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1],
        test_scores=[0.2, 0.8],
        alpha=0.2,
        n_classes=2,
    )

    mapie_installed = importlib.util.find_spec("mapie") is not None
    payload = result.to_dict()
    assert payload["backend_requested"] == "mapie"
    assert payload["backend_used"] in {"mapie", "builtin"}
    assert len(payload["prediction_sets"]) == 2
    assert len(payload["prediction_intervals"]) == 2

    if mapie_installed:
        assert payload["backend_used"] == "mapie"
        assert payload["fallback_used"] is False
    else:
        assert payload["backend_used"] == "builtin"
        assert payload["fallback_used"] is True
        assert str(payload["fallback_reason"] or "").startswith("mapie_")


def test_conformal_adapter_supports_multiclass_when_mapie_is_available():
    adapter = get_conformal_adapter(prefer_mapie=True)
    result = adapter.predict_set(
        calibration_scores=[
            0.05, 0.06, 0.07, 0.08, 0.09, 0.10, 0.11, 0.12,
            0.35, 0.36, 0.37, 0.38, 0.39, 0.40, 0.41, 0.42,
            0.70, 0.72, 0.74, 0.76, 0.78, 0.80, 0.82, 0.84,
        ],
        calibration_labels=[
            0, 0, 0, 0, 0, 0, 0, 0,
            1, 1, 1, 1, 1, 1, 1, 1,
            2, 2, 2, 2, 2, 2, 2, 2,
        ],
        test_scores=[0.07, 0.39, 0.79],
        alpha=0.1,
        n_classes=3,
    )

    mapie_installed = importlib.util.find_spec("mapie") is not None
    payload = result.to_dict()
    assert payload["backend_requested"] == "mapie"
    assert len(payload["prediction_sets"]) == 3

    if mapie_installed:
        assert payload["backend_used"] == "mapie"
        assert payload["fallback_used"] is False
        assert payload["prediction_sets"][0] == [0]
        assert payload["prediction_sets"][1] == [1]
        assert payload["prediction_sets"][2] == [2]
    else:
        assert payload["backend_used"] == "builtin"
        assert payload["fallback_used"] is True
