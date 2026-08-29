from __future__ import annotations

from akshare_mcp.services.strategy_dsl import compile_strategy_blueprint


def test_compile_strategy_blueprint_emits_semantic_contract_artifact():
    blueprint = {
        "name": "semantic-contract-blueprint",
        "description": "compile-time semantic contract smoke test",
        "trade_plan": {
            "entry": {
                "node_id": "entry_node",
                "phase": "entry",
                "claim_ids": ["claim_up"],
                "evidence_ids": ["ev_price"],
                "summary": "enter on aligned evidence",
            },
            "exit": {
                "node_id": "exit_node",
                "phase": "exit",
                "claim_ids": ["claim_up"],
                "evidence_ids": ["ev_price"],
                "summary": "exit when thesis breaks",
            },
        },
        "prediction_contract": {
            "claims": [
                {
                    "claim_id": "claim_up",
                    "expected_move": "up",
                    "evidence_ids": ["ev_price"],
                    "failure_condition": "close below trigger",
                }
            ]
        },
        "confidence_contract": {
            "support_samples": 128,
            "calibration_method": "sigmoid",
        },
        "evidence_chain": {
            "evidences": [
                {
                    "evidence_id": "ev_price",
                    "source_type": "technical",
                    "direction": "up",
                    "support_metric": "rsi",
                }
            ]
        },
        "dsl": {
            "entry": {
                "op": "gt",
                "left": {"field": "close"},
                "right": {"value": 0},
                "trade_plan_node_id": "entry_node",
            },
            "exit": {
                "op": "lt",
                "left": {"field": "close"},
                "right": {"value": 0},
                "trade_plan_node_id": "exit_node",
            },
            "risk_rules": {"stop_loss_pct": 0.05},
        },
        "risk_rules": {"stop_loss_pct": 0.05},
    }

    compiled = compile_strategy_blueprint(blueprint, tune_for_factory=False)
    metadata = dict(compiled.get("metadata") or {})
    audit = dict(metadata.get("evidence_alignment_audit") or {})
    claim_map = dict(metadata.get("claim_to_trade_plan_map") or {})

    assert claim_map["mapped_claim_count"] == 1
    assert claim_map["claim_to_trade_step_ids"]["claim_up"] == ["entry_node", "exit_node"]
    assert audit["using_new_contract"] is True
    assert metadata["evidence_alignment_score"] > 0
    assert metadata["semantic_integrity_score"] > 0
    assert metadata["hard_fail_reasons"] == []
    assert metadata["compile_failure_reasons"] == []
