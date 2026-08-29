"""Shared artifact helpers for quant_manager submodules."""

from __future__ import annotations

from typing import Awaitable, Callable

QuantManagerCall = Callable[..., Awaitable[dict]]


def _payload_from_artifact_row(artifact: dict | None) -> dict:
    if not isinstance(artifact, dict):
        return {}
    payload = artifact.get("payload") if isinstance(artifact.get("payload"), dict) else artifact
    return payload if isinstance(payload, dict) else {}
