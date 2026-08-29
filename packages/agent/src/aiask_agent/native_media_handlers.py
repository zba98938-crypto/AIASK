from __future__ import annotations

import asyncio
import base64
import mimetypes
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from .env_config import load_project_env
from .native_media import _generate_image, _text_to_speech, _transcribe_audio, media_provider_catalog
from .native_web_utils import _json_request, _response_text, _validate_public_url
from .numeric import bounded_int


def build_media_handlers(_envelope: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    async def vision_analyze(arguments: dict[str, Any]) -> dict[str, Any]:
        load_project_env()
        tool = "agent_vision_analyze"
        image = str(arguments.get("image_path") or arguments.get("image_url") or "").strip()
        if not image:
            return _envelope(False, error="image_path or image_url is required", tool_name=tool)
        try:
            provider = str(arguments.get("provider") or os.getenv("AIASK_AGENT_VISION_PROVIDER", "openai")).strip().lower() or "openai"
            data: dict[str, Any] = {"image": image, "prompt": arguments.get("prompt"), "provider": provider}
            if image.startswith("http"):
                _validate_public_url(image)
                data["source"] = "url"
                image_url = image
            else:
                path = Path(image).expanduser().resolve()
                if not path.exists() or not path.is_file():
                    raise FileNotFoundError(str(path))
                data.update({"source": "file", "path": str(path), "bytes": path.stat().st_size})
                mime = mimetypes.guess_type(str(path))[0] or "image/png"
                image_url = f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"
                try:
                    from PIL import Image

                    with Image.open(path) as img:
                        data.update({"width": img.width, "height": img.height, "format": img.format})
                except Exception:
                    data["metadata_only"] = True
            if provider != "openai":
                data["configured"] = False
                return _envelope(False, data=data, error=f"unsupported vision provider: {provider}", tool_name=tool, level="read_only", target=image)
            model = str(arguments.get("model") or os.getenv("AIASK_AGENT_VISION_MODEL", "")).strip()
            if not os.getenv("OPENAI_API_KEY") or not model:
                data["configured"] = False
                return _envelope(False, data=data, error="vision provider is not configured", tool_name=tool, level="read_only", target=image)
            from openai import AsyncOpenAI

            data["configured"] = True
            data["model"] = model
            prompt = str(arguments.get("prompt") or "Analyze the image and describe the visible evidence relevant to the user's task.").strip()
            try:
                response = await AsyncOpenAI(api_key=str(os.getenv("OPENAI_API_KEY"))).responses.create(
                    model=model,
                    input=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": prompt},
                                {"type": "input_image", "image_url": image_url},
                            ],
                        }
                    ],
                )
                data["analysis"] = _response_text(response)
                return _envelope(True, data=data, tool_name=tool, level="read_only", target=image)
            except Exception as exc:
                data["error"] = str(exc)
                return _envelope(False, data=data, error=str(exc), tool_name=tool, level="read_only", target=image)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def media_provider_catalog_tool(_: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_media_provider_catalog"
        try:
            return _envelope(True, data=media_provider_catalog(), tool_name=tool, level="read_only")
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="read_only")

    async def image_generate(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_image_generate"
        try:
            data = await _generate_image(arguments)
            return _envelope(True, data=data, tool_name=tool, level="external_generation", target=data.get("path"), idempotent=False)
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="external_generation", idempotent=False)

    async def video_generate(arguments: dict[str, Any]) -> dict[str, Any]:
        load_project_env()
        tool = "agent_video_generate"
        action = str(arguments.get("action") or "status").strip().lower()
        provider = str(arguments.get("provider") or os.getenv("AIASK_VIDEO_PROVIDER") or "openai_compatible").strip()
        endpoint = str(os.getenv("AIASK_VIDEO_API_URL") or os.getenv("AIASK_VIDEO_BASE_URL") or "").strip()
        api_key = str(os.getenv("AIASK_VIDEO_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip()
        configured = bool(endpoint and api_key)
        base = {
            "configured": configured,
            "provider": provider,
            "required_env": ["AIASK_VIDEO_API_URL", "AIASK_VIDEO_API_KEY"],
            "secrets_redacted": True,
            "actions": ["status", "create", "status_check"],
        }
        if action == "status":
            return _envelope(True, data=base, tool_name=tool, level="read_only")
        if not configured:
            return _envelope(
                False,
                data=base,
                error="video generation provider is not configured",
                tool_name=tool,
                level="external_generation",
                idempotent=False,
            )
        try:
            if action == "create":
                prompt = str(arguments.get("prompt") or "").strip()
                if not prompt:
                    raise ValueError("prompt is required")
                payload = {
                    "prompt": prompt,
                    "model": arguments.get("model") or os.getenv("AIASK_VIDEO_MODEL") or "video",
                    "size": arguments.get("size") or os.getenv("AIASK_VIDEO_SIZE") or "1280x720",
                    "duration_seconds": bounded_int(
                        arguments.get("duration_seconds") or os.getenv("AIASK_VIDEO_DURATION_SECONDS"),
                        default=5,
                        minimum=1,
                        maximum=300,
                    ),
                    "metadata": dict(arguments.get("metadata") or {}),
                }
                result = await asyncio.to_thread(
                    _json_request,
                    "POST",
                    endpoint.rstrip("/") + "/videos",
                    payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=60,
                )
                body = result.get("body") if isinstance(result.get("body"), dict) else {}
                data = {**base, "job": body, "response": {key: value for key, value in result.items() if key != "body"}}
                return _envelope(bool(result.get("ok")), data=data, error=None if result.get("ok") else str(result.get("error") or "video generation request failed"), tool_name=tool, level="external_generation", target=str(body.get("id") or ""), idempotent=False)
            if action == "status_check":
                job_id = str(arguments.get("job_id") or "").strip()
                if not job_id:
                    raise ValueError("job_id is required")
                result = await asyncio.to_thread(
                    _json_request,
                    "GET",
                    endpoint.rstrip("/") + f"/videos/{quote_plus(job_id)}",
                    None,
                    headers={"Authorization": f"Bearer {api_key}"},
                    timeout=30,
                )
                data = {**base, "job_id": job_id, "job": result.get("body"), "response": {key: value for key, value in result.items() if key != "body"}}
                return _envelope(bool(result.get("ok")), data=data, error=None if result.get("ok") else str(result.get("error") or "video status request failed"), tool_name=tool, level="read_only", target=job_id)
            raise ValueError(f"unsupported video_generate action: {action}")
        except Exception as exc:
            return _envelope(False, data=base, error=str(exc), tool_name=tool, level="external_generation", idempotent=False)

    async def text_to_speech(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_text_to_speech"
        try:
            data = await _text_to_speech(arguments)
            success = bool(data.get("configured")) and not data.get("error")
            return _envelope(
                success,
                data=data,
                error=None if success else str(data.get("error") or "text-to-speech provider is not configured"),
                tool_name=tool,
                level="external_generation",
                target=data.get("path"),
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="external_generation", idempotent=False)

    async def transcribe_audio(arguments: dict[str, Any]) -> dict[str, Any]:
        tool = "agent_transcribe_audio"
        try:
            data = await _transcribe_audio(arguments)
            success = bool(data.get("configured")) and not data.get("error")
            return _envelope(
                success,
                data=data,
                error=None if success else str(data.get("error") or "speech-to-text provider is not configured"),
                tool_name=tool,
                level="external_generation",
                target=arguments.get("audio_path") or arguments.get("audio_url"),
                idempotent=False,
            )
        except Exception as exc:
            return _envelope(False, error=str(exc), tool_name=tool, level="external_generation", idempotent=False)

    return {
        "agent_vision_analyze": vision_analyze,
        "agent_media_provider_catalog": media_provider_catalog_tool,
        "agent_image_generate": image_generate,
        "agent_video_generate": video_generate,
        "agent_text_to_speech": text_to_speech,
        "agent_transcribe_audio": transcribe_audio,
    }
