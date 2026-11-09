"""Lossless JSON archives for candidates, separate from bounded run summaries."""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Any


CANDIDATE_RECORDING_VERSION = "strategy_factory.candidate_recording.v1"


def _json_default(value: Any) -> Any:
    if hasattr(value, "tolist"):
        return value.tolist()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(f"candidate archive cannot serialize {type(value).__name__}")


def _archive_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return {"$nonfinite_float": str(value)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _archive_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_archive_value(item) for item in value]
    return _archive_value(_json_default(value))


def write_candidate_archive(run_id: str, phase: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically preserve every record; never apply preview or JSON-size caps.

    Only a small content-addressed reference goes into the database/run result.
    A serialization or disk failure must propagate to the factory audit.
    """
    document = {
        **payload,
        "contract_version": CANDIDATE_RECORDING_VERSION,
        "run_id": str(run_id),
        "phase": str(phase),
    }
    encoded = json.dumps(
        _archive_value(document), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    runtime = Path(os.getenv("AIASK_RUNTIME_HOME") or Path.home() / ".aiask").expanduser()
    directory = Path(os.getenv("AIASK_FACTORY_CANDIDATE_ARCHIVE_DIR") or runtime / "artifacts" / "strategy_factory_candidates").expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    run_key = hashlib.sha256(str(run_id).encode()).hexdigest()[:24]
    path = directory / f"{run_key}-{digest}.json.gz"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".candidate-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            with gzip.GzipFile(filename="", fileobj=handle, mode="wb", mtime=0) as compressed:
                compressed.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {
        "contract_version": CANDIDATE_RECORDING_VERSION,
        "run_id": str(run_id),
        "phase": str(phase),
        "storage_mode": "external_gzip_json",
        "path": str(path.resolve()),
        "sha256": digest,
        "uncompressed_bytes": len(encoded),
        "compressed_bytes": path.stat().st_size,
        "candidate_count": len(payload.get("candidates") or []),
        "complete": True,
    }


def read_candidate_archive(reference: dict[str, Any]) -> dict[str, Any]:
    """Read and validate an archive referenced by a persisted run artifact."""
    encoded = gzip.decompress(Path(reference["path"]).read_bytes())
    if hashlib.sha256(encoded).hexdigest() != reference.get("sha256"):
        raise ValueError("candidate_archive_hash_mismatch")
    payload = json.loads(encoded)
    if (
        payload.get("contract_version") != CANDIDATE_RECORDING_VERSION
        or payload.get("run_id") != reference.get("run_id")
        or payload.get("phase") != reference.get("phase")
        or len(payload.get("candidates") or []) != reference.get("candidate_count")
    ):
        raise ValueError("candidate_archive_contract_mismatch")
    return payload


def recording_artifact(recording: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_type": "candidate_recording",
        "artifact_version": CANDIDATE_RECORDING_VERSION,
        "payload_json": recording,
        "payload_hash": hashlib.sha256(
            json.dumps(recording, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
        "storage_mode": "external_gzip_json",
    }
