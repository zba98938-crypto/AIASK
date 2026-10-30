import importlib.util
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[3] / "scripts/check_paper_evidence_independence.py"
    spec = importlib.util.spec_from_file_location("_paper_evidence_independence", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_duplicate_trade_rows_collapse_into_one_independent_cluster():
    summary = _module().cluster_summary(
        [
            {"code": "000002", "day": "2026-10-20", "side": "buy", "price": 3.66},
            {"code": "000002", "day": "2026-10-20", "side": "buy", "price": 3.66},
            {"code": "000001", "day": "2026-10-20", "side": "buy", "price": 10.0},
        ],
        key_fields=("code", "day", "side", "price"),
    )
    assert summary["raw_count"] == 3
    assert summary["independent_cluster_count"] == 2
    assert summary["max_cluster_count"] == 2
    assert summary["max_cluster_share"] == 2 / 3
