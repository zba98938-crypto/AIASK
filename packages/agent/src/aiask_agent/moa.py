from __future__ import annotations

import asyncio
import os
from typing import Any

from .env_config import load_project_env
from .model_client import ModelClient


DEFAULT_REFERENCE_MODELS = ("gpt-4.1-mini", "gpt-4.1-mini")


def reference_models() -> list[str]:
    load_project_env()
    raw = str(os.getenv("AIASK_MOA_REFERENCE_MODELS", "")).strip()
    if raw:
        return [item.strip() for item in raw.split(",") if item.strip()]
    return list(DEFAULT_REFERENCE_MODELS)


def aggregator_model(default_model: str) -> str:
    load_project_env()
    return str(os.getenv("AIASK_MOA_AGGREGATOR_MODEL", "")).strip() or default_model


async def run_moa(
    *,
    model_client: ModelClient,
    user_prompt: str,
    default_model: str,
    max_reference_tokens: int | None = None,
) -> dict[str, Any]:
    prompt = str(user_prompt or "").strip()
    if not prompt:
        raise ValueError("user_prompt is required")
    refs = reference_models()

    async def call_reference(model: str) -> dict[str, Any]:
        try:
            response = await model_client.complete(messages=[{"role": "user", "content": prompt}], tools=[], model=model)
            return {"model": model, "success": True, "content": response.content, "usage": response.usage}
        except Exception as exc:
            return {"model": model, "success": False, "error": str(exc), "content": ""}

    reference_results = await asyncio.gather(*(call_reference(model) for model in refs))
    successful = [item for item in reference_results if item.get("success") and item.get("content")]
    if not successful:
        return {"configured": False, "reference_models": refs, "reference_results": reference_results, "content": ""}
    responses = "\n\n".join(f"{idx + 1}. [{item['model']}]\n{item['content']}" for idx, item in enumerate(successful))
    system_prompt = (
        "Synthesize the reference responses into one accurate, concise answer. "
        "Do not copy errors from references; reconcile disagreements explicitly when needed.\n\n"
        f"Reference responses:\n{responses}"
    )
    agg_model = aggregator_model(default_model)
    aggregate = await model_client.complete(
        messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt}],
        tools=[],
        model=agg_model,
    )
    return {
        "configured": True,
        "reference_models": refs,
        "aggregator_model": agg_model,
        "reference_results": reference_results,
        "content": aggregate.content,
        "usage": aggregate.usage,
    }
