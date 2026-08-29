"""Optional host callbacks for shared storage.

The storage package must not import AKShare MCP or Strategy Factory modules.
Hosts can register callbacks here when storage needs domain-specific helpers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

SignalEvidenceBuilder = Callable[..., Any]
ExecutionAuditSnapshotBuilder = Callable[..., dict[str, Any]]
ExecutionAuditSnapshotMetadata = Callable[..., dict[str, Any]]
CleanupCallback = Callable[[], Awaitable[Any] | Any]

_signal_evidence_builder: SignalEvidenceBuilder | None = None
_execution_audit_snapshot_builder: ExecutionAuditSnapshotBuilder | None = None
_execution_audit_snapshot_metadata: ExecutionAuditSnapshotMetadata | None = None
_event_extractor: Callable[..., Any] | None = None
_headline_sentiment_classifier: Callable[[str], str] | None = None
_text_embedding_service_factory: Callable[[], Any] | None = None
_rejected_kline_recorder: Callable[..., Any] | None = None
_execution_audit_gate_evaluator: Callable[..., Any] | None = None
_cleanup_callbacks: list[CleanupCallback] = []


def configure_storage_runtime_hooks(
    *,
    signal_evidence_builder: SignalEvidenceBuilder | None = None,
    execution_audit_snapshot_builder: ExecutionAuditSnapshotBuilder | None = None,
    execution_audit_snapshot_metadata: ExecutionAuditSnapshotMetadata | None = None,
    event_extractor: Callable[..., Any] | None = None,
    headline_sentiment_classifier: Callable[[str], str] | None = None,
    text_embedding_service_factory: Callable[[], Any] | None = None,
    rejected_kline_recorder: Callable[..., Any] | None = None,
    execution_audit_gate_evaluator: Callable[..., Any] | None = None,
    cleanup_callbacks: list[CleanupCallback] | tuple[CleanupCallback, ...] | None = None,
) -> None:
    global _signal_evidence_builder
    global _execution_audit_snapshot_builder
    global _execution_audit_snapshot_metadata
    global _event_extractor
    global _headline_sentiment_classifier
    global _text_embedding_service_factory
    global _rejected_kline_recorder
    global _execution_audit_gate_evaluator
    if signal_evidence_builder is not None:
        _signal_evidence_builder = signal_evidence_builder
    if execution_audit_snapshot_builder is not None:
        _execution_audit_snapshot_builder = execution_audit_snapshot_builder
    if execution_audit_snapshot_metadata is not None:
        _execution_audit_snapshot_metadata = execution_audit_snapshot_metadata
    if event_extractor is not None:
        _event_extractor = event_extractor
    if headline_sentiment_classifier is not None:
        _headline_sentiment_classifier = headline_sentiment_classifier
    if text_embedding_service_factory is not None:
        _text_embedding_service_factory = text_embedding_service_factory
    if rejected_kline_recorder is not None:
        _rejected_kline_recorder = rejected_kline_recorder
    if execution_audit_gate_evaluator is not None:
        _execution_audit_gate_evaluator = execution_audit_gate_evaluator
    if cleanup_callbacks:
        for callback in cleanup_callbacks:
            if callback not in _cleanup_callbacks:
                _cleanup_callbacks.append(callback)


def clear_storage_runtime_hooks() -> None:
    global _signal_evidence_builder
    global _execution_audit_snapshot_builder
    global _execution_audit_snapshot_metadata
    global _event_extractor
    global _headline_sentiment_classifier
    global _text_embedding_service_factory
    global _rejected_kline_recorder
    global _execution_audit_gate_evaluator
    _signal_evidence_builder = None
    _execution_audit_snapshot_builder = None
    _execution_audit_snapshot_metadata = None
    _event_extractor = None
    _headline_sentiment_classifier = None
    _text_embedding_service_factory = None
    _rejected_kline_recorder = None
    _execution_audit_gate_evaluator = None
    _cleanup_callbacks.clear()


def get_signal_evidence_builder() -> SignalEvidenceBuilder | None:
    return _signal_evidence_builder


def get_execution_audit_snapshot_builder() -> ExecutionAuditSnapshotBuilder | None:
    return _execution_audit_snapshot_builder


def get_execution_audit_snapshot_metadata() -> ExecutionAuditSnapshotMetadata | None:
    return _execution_audit_snapshot_metadata


def get_event_extractor() -> Callable[..., Any] | None:
    return _event_extractor


def get_headline_sentiment_classifier() -> Callable[[str], str] | None:
    return _headline_sentiment_classifier


def get_text_embedding_service_factory() -> Callable[[], Any] | None:
    return _text_embedding_service_factory


def get_rejected_kline_recorder() -> Callable[..., Any] | None:
    return _rejected_kline_recorder


def get_execution_audit_gate_evaluator() -> Callable[..., Any] | None:
    return _execution_audit_gate_evaluator


def iter_cleanup_callbacks() -> tuple[CleanupCallback, ...]:
    return tuple(_cleanup_callbacks)


__all__ = [
    "clear_storage_runtime_hooks",
    "configure_storage_runtime_hooks",
    "get_execution_audit_snapshot_builder",
    "get_execution_audit_snapshot_metadata",
    "get_event_extractor",
    "get_execution_audit_gate_evaluator",
    "get_headline_sentiment_classifier",
    "get_rejected_kline_recorder",
    "get_signal_evidence_builder",
    "get_text_embedding_service_factory",
    "iter_cleanup_callbacks",
]
