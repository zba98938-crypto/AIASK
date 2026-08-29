from __future__ import annotations

import base64
import mimetypes
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import uuid4

from .env_config import load_project_env
from .native_web_utils import _fetch_binary_url
from .numeric import bounded_float, bounded_int
from .paths import aiask_agent_home


def media_provider_catalog(env: dict[str, str] | None = None) -> dict[str, Any]:
    values = dict(os.environ if env is None else env)

    def configured(required: list[str]) -> bool:
        return all(str(values.get(key) or "").strip() for key in required)

    def row(
        *,
        name: str,
        modality: str,
        provider_type: str,
        required_env: list[str],
        capabilities: list[str],
        default_model_env: str | None = None,
        default_model: str | None = None,
        local_dependency: str | None = None,
    ) -> dict[str, Any]:
        ready = configured(required_env)
        dependency_ready = True
        if local_dependency:
            dependency_ready = bool(shutil.which(local_dependency))
        if local_dependency and not dependency_ready:
            status = "skipped_missing_dependency"
        else:
            status = "live_unverified" if ready else "skipped_missing_credentials"
        if not required_env and not local_dependency:
            status = "available"
        return {
            "name": name,
            "modality": modality,
            "provider_type": provider_type,
            "configured": bool(ready and dependency_ready) if required_env or local_dependency else True,
            "status": status,
            "required_env": required_env,
            "capabilities": capabilities,
            "default_model": str(values.get(default_model_env) or default_model or "") if default_model_env or default_model else None,
            "local_dependency": local_dependency,
            "secrets_redacted": True,
        }

    providers = [
        row(
            name="openai_vision",
            modality="vision",
            provider_type="openai",
            required_env=["OPENAI_API_KEY", "AIASK_AGENT_VISION_MODEL"],
            capabilities=["image_understanding"],
            default_model_env="AIASK_AGENT_VISION_MODEL",
        ),
        row(
            name="openai_image",
            modality="image",
            provider_type="openai",
            required_env=["OPENAI_API_KEY"],
            capabilities=["image_generate"],
            default_model_env="AIASK_AGENT_IMAGE_MODEL",
            default_model="gpt-image-1",
        ),
        row(
            name="aiask_video_endpoint",
            modality="video",
            provider_type="openai_compatible",
            required_env=["AIASK_VIDEO_API_URL", "AIASK_VIDEO_API_KEY"],
            capabilities=["video_status", "video_create", "video_status_check"],
            default_model_env="AIASK_VIDEO_MODEL",
            default_model="video",
        ),
        row(
            name="openai_tts",
            modality="tts",
            provider_type="openai",
            required_env=["OPENAI_API_KEY"],
            capabilities=["text_to_speech"],
            default_model_env="AIASK_AGENT_TTS_MODEL",
            default_model="gpt-4o-mini-tts",
        ),
        row(
            name="edge_tts",
            modality="tts",
            provider_type="local_dependency",
            required_env=[],
            capabilities=["text_to_speech"],
            local_dependency="edge-tts",
        ),
        row(
            name="openai_stt",
            modality="stt",
            provider_type="openai",
            required_env=["OPENAI_API_KEY"],
            capabilities=["transcribe_audio"],
            default_model_env="AIASK_AGENT_STT_MODEL",
            default_model="gpt-4o-mini-transcribe",
        ),
        row(
            name="iflytek_voice",
            modality="voice",
            provider_type="iflytek",
            required_env=["IFLYTEK_APP_ID", "IFLYTEK_API_KEY"],
            capabilities=["speech_to_text", "text_to_speech"],
        ),
        row(
            name="local_whisper",
            modality="stt",
            provider_type="local_dependency",
            required_env=[],
            capabilities=["transcribe_audio"],
            local_dependency="whisper",
        ),
    ]
    by_modality: dict[str, int] = {}
    configured_count = 0
    for item in providers:
        by_modality[str(item["modality"])] = by_modality.get(str(item["modality"]), 0) + 1
        configured_count += 1 if item.get("configured") else 0
    return {
        "object": "aiask.media_provider_catalog",
        "status": "implemented",
        "providers": providers,
        "provider_count": len(providers),
        "configured_count": configured_count,
        "by_modality": by_modality,
        "secrets_redacted": True,
        "catalog_semantics": "configured providers are reported as live_unverified until a real provider call succeeds",
    }


async def _generate_image(arguments: dict[str, Any]) -> dict[str, Any]:
    load_project_env()
    prompt = str(arguments.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    api_key = str(os.getenv("OPENAI_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is required for native image generation")
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key)
    response = await client.images.generate(
        model=str(arguments.get("model") or os.getenv("AIASK_AGENT_IMAGE_MODEL", "gpt-image-1")),
        prompt=prompt,
        size=str(arguments.get("size") or "1024x1024"),
    )
    item = response.data[0]
    output_dir = aiask_agent_home() / "generated" / "images"
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"image_{uuid4().hex}.png"
    b64 = getattr(item, "b64_json", None)
    if b64:
        path.write_bytes(base64.b64decode(b64))
        return {"path": str(path), "url": None, "model": response.model if hasattr(response, "model") else None}
    return {"path": None, "url": getattr(item, "url", None)}


async def _text_to_speech(arguments: dict[str, Any]) -> dict[str, Any]:
    load_project_env()
    text = str(arguments.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")
    provider = str(arguments.get("provider") or os.getenv("AIASK_AGENT_TTS_PROVIDER", "openai")).strip().lower() or "openai"
    output_dir = aiask_agent_home() / "generated" / "audio"
    output_dir.mkdir(parents=True, exist_ok=True)
    audio_format = str(arguments.get("format") or os.getenv("AIASK_AGENT_TTS_FORMAT", "mp3")).strip().lower() or "mp3"
    path = output_dir / f"speech_{uuid4().hex}.{audio_format}"
    if provider == "edge_tts":
        voice = str(arguments.get("voice") or os.getenv("AIASK_AGENT_TTS_VOICE", "en-US-AriaNeural"))
        try:
            import edge_tts
        except Exception:
            return {"configured": False, "provider": "edge_tts", "path": None, "error": "edge-tts is not installed"}
        try:
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(path))
            return {"configured": True, "provider": "edge_tts", "path": str(path), "voice": voice, "bytes": path.stat().st_size}
        except Exception as exc:
            return {"configured": True, "provider": "edge_tts", "path": None, "error": str(exc)}
    api_key = str(os.getenv("OPENAI_API_KEY", "")).strip()
    if not api_key:
        return {"configured": False, "provider": "openai", "path": None, "error": "OPENAI_API_KEY is required"}
    from openai import AsyncOpenAI

    model = str(arguments.get("model") or os.getenv("AIASK_AGENT_TTS_MODEL", "gpt-4o-mini-tts"))
    voice = str(arguments.get("voice") or os.getenv("AIASK_AGENT_TTS_VOICE", "alloy"))
    kwargs: dict[str, Any] = {"model": model, "voice": voice, "input": text, "response_format": audio_format}
    if arguments.get("speed") is not None:
        kwargs["speed"] = bounded_float(arguments.get("speed"), default=1.0, minimum=0.25, maximum=4.0)
    try:
        response = await AsyncOpenAI(api_key=api_key).audio.speech.create(**kwargs)
        if hasattr(response, "aread"):
            raw = await response.aread()
        elif hasattr(response, "read"):
            raw = response.read()
        else:
            raw = getattr(response, "content", b"")
        path.write_bytes(bytes(raw or b""))
        return {
            "configured": True,
            "provider": "openai",
            "model": model,
            "voice": voice,
            "format": audio_format,
            "path": str(path),
            "bytes": path.stat().st_size,
        }
    except Exception as exc:
        return {"configured": True, "provider": "openai", "model": model, "voice": voice, "path": None, "error": str(exc)}


async def _transcribe_audio(arguments: dict[str, Any]) -> dict[str, Any]:
    load_project_env()
    provider = str(arguments.get("provider") or os.getenv("AIASK_AGENT_STT_PROVIDER", "openai")).strip().lower() or "openai"
    if provider != "openai":
        return {"configured": False, "provider": provider, "text": None, "error": f"unsupported STT provider: {provider}"}
    api_key = str(os.getenv("OPENAI_API_KEY", "")).strip()
    if not api_key:
        return {"configured": False, "provider": "openai", "text": None}
    audio = str(arguments.get("audio_path") or "").strip()
    audio_url = str(arguments.get("audio_url") or "").strip()
    if not audio and not audio_url:
        return {"configured": False, "provider": "openai", "text": None, "error": "audio_path or audio_url is required"}
    from openai import AsyncOpenAI

    downloaded: Path | None = None
    if audio_url:
        raw, content_type, _ = _fetch_binary_url(
            audio_url,
            max_bytes=bounded_int(arguments.get("max_bytes"), default=25 * 1024 * 1024, minimum=1, maximum=100 * 1024 * 1024),
            timeout=bounded_float(arguments.get("timeout_seconds"), default=60.0, minimum=1.0, maximum=300.0),
        )
        suffix = mimetypes.guess_extension(content_type) or ".audio"
        input_dir = aiask_agent_home() / "generated" / "audio-input"
        input_dir.mkdir(parents=True, exist_ok=True)
        downloaded = input_dir / f"audio_{uuid4().hex}{suffix}"
        downloaded.write_bytes(raw)
        path = downloaded
    else:
        path = Path(audio).expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(str(path))
    client = AsyncOpenAI(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": str(arguments.get("model") or os.getenv("AIASK_AGENT_STT_MODEL", "gpt-4o-mini-transcribe")),
    }
    for key in ("language", "prompt", "response_format"):
        if arguments.get(key):
            kwargs[key] = arguments[key]
    try:
        with path.open("rb") as fh:
            result = await client.audio.transcriptions.create(file=fh, **kwargs)
        return {
            "configured": True,
            "provider": "openai",
            "model": kwargs["model"],
            "text": getattr(result, "text", "") if not isinstance(result, str) else result,
            "audio_path": str(path),
            "downloaded": bool(downloaded),
        }
    except Exception as exc:
        return {"configured": True, "provider": "openai", "model": kwargs["model"], "text": None, "audio_path": str(path), "error": str(exc)}


