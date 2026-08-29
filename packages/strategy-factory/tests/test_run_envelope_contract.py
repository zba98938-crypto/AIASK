"""Conformance tests for the governed ``--json`` envelope.

These pin the contract the CLI, MCP surface and Hermes adapter all parse. A
change that breaks any assertion here is a protocol break and needs a
``PROTOCOL_VERSION`` bump, not a test edit.
"""

from __future__ import annotations

import json

import pytest

from strategy_factory.contracts.run_envelope import (
    FAILED_STATUSES,
    PROTOCOL_VERSION,
    REQUIRED_ENVELOPE_FIELDS,
    RunEnvelope,
    SUCCESSFUL_STATUSES,
    envelope_from_runtime_result,
    new_run_id,
    normalize_status,
)


def test_envelope_always_carries_every_governed_field() -> None:
    payload = RunEnvelope.start("strategy_factory").finish("success").to_dict()

    for field_name in REQUIRED_ENVELOPE_FIELDS:
        assert field_name in payload, f"governed field {field_name} is missing"

    assert payload["protocol_version"] == PROTOCOL_VERSION
    assert payload["runtime"] == "strategy_factory"
    assert payload["run_id"].startswith("strategy_factory-")
    assert payload["started_at"] and payload["finished_at"]


def test_envelope_is_json_serialisable_with_stable_key_order() -> None:
    payload = RunEnvelope.start("incubation").finish().to_dict()
    encoded = json.dumps(payload)
    assert json.loads(encoded) == payload
    assert list(payload) == list(REQUIRED_ENVELOPE_FIELDS)


def test_run_id_is_prefixed_and_unique() -> None:
    first = new_run_id("factor_mining")
    second = new_run_id("factor_mining")
    assert first.startswith("factor_mining-")
    assert first != second


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ok", "success"),
        ("completed", "success"),
        ("SUCCESS", "success"),
        ("success_observe_only", "partial"),
        ("success_no_submission", "partial"),
        ("warning", "degraded"),
        ("error", "failed"),
        ("cancelled", "blocked"),
        ("disabled", "skipped"),
        ("", "failed"),
        (None, "failed"),
        ("something_unheard_of", "failed"),
    ],
)
def test_status_normalization_fails_closed(raw, expected) -> None:
    assert normalize_status(raw) == expected


def test_derived_status_prefers_errors_then_degradation() -> None:
    clean = RunEnvelope.start("signal_tracker").finish()
    assert clean.status == "success"

    degraded = RunEnvelope.start("signal_tracker")
    degraded.degrade("stale_prices")
    assert degraded.finish().status == "degraded"

    failed = RunEnvelope.start("signal_tracker")
    failed.degrade("stale_prices")
    failed.fail("db unreachable")
    assert failed.finish().status == "failed"


def test_explicit_success_is_downgraded_when_errors_exist() -> None:
    """A runner claiming success while reporting errors must not read as clean."""
    envelope = RunEnvelope.start("market_event_ingest")
    envelope.fail("one source failed")
    envelope.finish("success")

    assert envelope.status == "partial"
    assert envelope.ok is False


def test_ok_is_reserved_for_clean_success() -> None:
    assert RunEnvelope.start("x").finish("success").ok is True
    for status in ("partial", "degraded", "skipped", "failed", "blocked"):
        assert RunEnvelope.start("x").finish(status).ok is False, status


def test_exit_codes_distinguish_failure_from_incomplete() -> None:
    assert RunEnvelope.start("x").finish("success").exit_code() == 0
    assert RunEnvelope.start("x").finish("failed").exit_code() == 1
    assert RunEnvelope.start("x").finish("blocked").exit_code() == 1
    # Completed but not clean is neither 0 nor 1, so schedulers can tell them apart.
    assert RunEnvelope.start("x").finish("partial").exit_code() == 2
    assert RunEnvelope.start("x").finish("degraded").exit_code() == 2


def test_status_vocabularies_do_not_overlap() -> None:
    assert not SUCCESSFUL_STATUSES & FAILED_STATUSES


def test_collections_deduplicate_and_ignore_empties() -> None:
    envelope = RunEnvelope.start("x")
    envelope.degrade("same")
    envelope.degrade("same")
    envelope.degrade("")
    envelope.fail("boom")
    envelope.fail("boom")
    envelope.add_evidence("lvke://a")
    envelope.add_evidence("lvke://a")

    assert envelope.degraded_reasons == ["same"]
    assert envelope.errors == ["boom"]
    assert envelope.evidence_refs == ["lvke://a"]


def test_counts_ignore_non_integer_values() -> None:
    envelope = RunEnvelope.start("x")
    envelope.add_count("good", "12")
    envelope.add_count("bad", None)
    envelope.add_count("worse", "not-a-number")

    assert envelope.counts == {"good": 12}


def test_wrapping_a_runtime_result_lifts_governed_fields() -> None:
    raw = {
        "status": "completed",
        "run_id": "incubation-abc123",
        "errors": ["source timeout"],
        "quality_flags": ["provisional_metrics"],
        "totals": {"accepted": 3, "rejected": "1", "junk": None},
        "extra": {"kept": True},
    }

    envelope = envelope_from_runtime_result("incubation", raw)
    payload = envelope.to_dict()

    # run_id supplied by the runtime is preserved rather than regenerated.
    assert payload["run_id"] == "incubation-abc123"
    # "completed" would be success, but an error downgrades it.
    assert payload["status"] == "partial"
    assert payload["errors"] == ["source timeout"]
    assert payload["degraded_reasons"] == ["provisional_metrics"]
    assert payload["counts"] == {"accepted": 3, "rejected": 1}
    # The original payload is retained untouched.
    assert payload["data"]["extra"] == {"kept": True}


def test_wrapping_uses_success_boolean_when_status_is_absent() -> None:
    assert envelope_from_runtime_result("x", {"success": True}).status == "success"
    assert envelope_from_runtime_result("x", {"success": False}).status == "failed"


def test_wrapping_a_non_dict_result_still_yields_a_valid_envelope() -> None:
    payload = envelope_from_runtime_result("x", None).to_dict()

    for field_name in REQUIRED_ENVELOPE_FIELDS:
        assert field_name in payload
    assert payload["status"] == "failed"
    assert payload["data"] == {}
    assert any("non_mapping" in error for error in payload["errors"])


def test_wrapping_an_empty_payload_fails_closed() -> None:
    """An empty dict is not evidence of a successful run."""
    envelope = envelope_from_runtime_result("x", {})

    assert envelope.status == "failed"
    assert "runtime_returned_empty_payload" in envelope.errors


def test_support_missing_runtime_payload_reads_as_failed() -> None:
    """The shape the four runtimes return when providers are absent."""
    envelope = envelope_from_runtime_result(
        "factor_mining",
        {
            "success": False,
            "error": "factor_mining_support_missing",
            "trigger": "scheduled",
        },
    )

    assert envelope.status == "failed"
    assert "factor_mining_support_missing" in envelope.errors
    assert envelope.exit_code() == 1
