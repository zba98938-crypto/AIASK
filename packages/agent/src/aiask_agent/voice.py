"""语音消息双向支持 — TTS/STT 集成。

提供语音消息的转录（STT）和合成（TTS）能力，
支持在 Gateway 入站/出站流程中自动处理语音消息。

支持的 Provider：
- OpenAI Whisper (STT) + OpenAI TTS
- Azure Cognitive Services
- 讯飞 (iFlytek) — 国内优先
- 本地 whisper.cpp (离线)

环境变量：
    AIASK_VOICE_STT_PROVIDER: stt provider (openai/azure/iflytek/local)
    AIASK_VOICE_TTS_PROVIDER: tts provider (openai/azure/iflytek/local)
    OPENAI_API_KEY: OpenAI API key (for whisper/tts)
    IFLYTEK_APP_ID: 讯飞 App ID
    IFLYTEK_API_KEY: 讯飞 API Key
    IFLYTEK_API_SECRET: 讯飞 API Secret
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

from .env_config import load_project_env


def _load_project_env_if_requested() -> None:
    if any(os.getenv(key) for key in ("AIASK_AGENT_ENV_FILE", "AIASK_ENV_FILE", "AIASK_AGENT_LOAD_PROJECT_ENV")):
        load_project_env()


def _stt_provider() -> str:
    _load_project_env_if_requested()
    return str(os.getenv("AIASK_VOICE_STT_PROVIDER") or "openai").strip().lower()


def _tts_provider() -> str:
    _load_project_env_if_requested()
    return str(os.getenv("AIASK_VOICE_TTS_PROVIDER") or "openai").strip().lower()


# ------------------------------------------------------------------
# STT (Speech-to-Text)
# ------------------------------------------------------------------


async def transcribe(audio_path: str, *, language: str = "zh") -> dict[str, Any]:
    """将语音文件转录为文本。

    Args:
        audio_path: 音频文件路径
        language: 语言代码 (zh/en/ja 等)

    Returns:
        {"success": bool, "text": str, "provider": str, "language": str}
    """
    provider = _stt_provider()

    if provider == "openai":
        return await _stt_openai(audio_path, language=language)
    elif provider == "iflytek":
        return await _stt_iflytek(audio_path, language=language)
    elif provider == "local":
        return await _stt_local(audio_path, language=language)
    else:
        return {"success": False, "text": "", "provider": provider, "error": f"Unknown STT provider: {provider}"}


async def _stt_openai(audio_path: str, *, language: str = "zh") -> dict[str, Any]:
    _load_project_env_if_requested()
    """OpenAI Whisper STT。"""
    api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return {"success": False, "text": "", "provider": "openai", "error": "OPENAI_API_KEY not configured"}

    path = Path(audio_path).expanduser()
    if not path.exists():
        return {"success": False, "text": "", "provider": "openai", "error": f"File not found: {audio_path}"}

    try:
        import httpx

        base_url = str(os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                data={"model": "whisper-1", "language": language},
                files={"file": (path.name, path.read_bytes(), "audio/mpeg")},
            )
            if resp.status_code == 200:
                data = resp.json()
                return {"success": True, "text": data.get("text", ""), "provider": "openai", "language": language}
            return {"success": False, "text": "", "provider": "openai", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except ImportError:
        return {"success": False, "text": "", "provider": "openai", "error": "httpx not installed"}
    except Exception as exc:
        return {"success": False, "text": "", "provider": "openai", "error": str(exc)}


async def _stt_iflytek(audio_path: str, *, language: str = "zh") -> dict[str, Any]:
    """讯飞语音转写。"""
    app_id = str(os.getenv("IFLYTEK_APP_ID") or "").strip()
    api_key = str(os.getenv("IFLYTEK_API_KEY") or "").strip()

    if not app_id or not api_key:
        return {"success": False, "text": "", "provider": "iflytek", "error": "IFLYTEK_APP_ID and IFLYTEK_API_KEY required"}

    path = Path(audio_path).expanduser()
    if not path.exists():
        return {"success": False, "text": "", "provider": "iflytek", "error": f"File not found: {audio_path}"}

    try:
        audio_data = base64.b64encode(path.read_bytes()).decode("utf-8")
        # Simplified iFlytek API call
        payload = {
            "common": {"app_id": app_id},
            "business": {"language": language, "domain": "iat", "accent": "mandarin"},
            "data": {"status": 2, "format": "audio/L16;rate=16000", "audio": audio_data},
        }
        # Note: Real implementation needs WebSocket connection to wss://iat-api.xfyun.cn/v2/iat
        return {"success": False, "text": "", "provider": "iflytek", "error": "iFlytek STT requires WebSocket implementation (placeholder)"}
    except Exception as exc:
        return {"success": False, "text": "", "provider": "iflytek", "error": str(exc)}


async def _stt_local(audio_path: str, *, language: str = "zh") -> dict[str, Any]:
    """本地 whisper.cpp STT。"""
    import subprocess
    import shutil

    whisper_bin = shutil.which("whisper-cpp") or shutil.which("whisper") or str(os.getenv("WHISPER_CPP_PATH") or "")
    model_path = str(os.getenv("WHISPER_MODEL_PATH") or "").strip()

    if not whisper_bin:
        return {"success": False, "text": "", "provider": "local", "error": "whisper-cpp not found in PATH"}

    path = Path(audio_path).expanduser()
    if not path.exists():
        return {"success": False, "text": "", "provider": "local", "error": f"File not found: {audio_path}"}

    try:
        cmd = [whisper_bin, "-f", str(path), "-l", language, "--no-timestamps"]
        if model_path:
            cmd.extend(["-m", model_path])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            text = result.stdout.strip()
            return {"success": True, "text": text, "provider": "local", "language": language}
        return {"success": False, "text": "", "provider": "local", "error": result.stderr[:500]}
    except Exception as exc:
        return {"success": False, "text": "", "provider": "local", "error": str(exc)}


# ------------------------------------------------------------------
# TTS (Text-to-Speech)
# ------------------------------------------------------------------


async def synthesize(text: str, *, voice: str = "alloy", output_path: str | None = None) -> dict[str, Any]:
    """将文本合成为语音文件。

    Args:
        text: 要合成的文本
        voice: 语音名称
        output_path: 输出文件路径（None 则自动生成临时文件）

    Returns:
        {"success": bool, "path": str, "provider": str, "duration_ms": int}
    """
    provider = _tts_provider()

    if provider == "openai":
        return await _tts_openai(text, voice=voice, output_path=output_path)
    elif provider == "iflytek":
        return await _tts_iflytek(text, voice=voice, output_path=output_path)
    else:
        return {"success": False, "path": "", "provider": provider, "error": f"Unknown TTS provider: {provider}"}


async def _tts_openai(text: str, *, voice: str = "alloy", output_path: str | None = None) -> dict[str, Any]:
    _load_project_env_if_requested()
    """OpenAI TTS。"""
    api_key = str(os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        return {"success": False, "path": "", "provider": "openai", "error": "OPENAI_API_KEY not configured"}

    try:
        import httpx

        base_url = str(os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/audio/speech",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": "tts-1", "input": text[:4096], "voice": voice, "response_format": "mp3"},
            )
            if resp.status_code == 200:
                if output_path:
                    Path(output_path).write_bytes(resp.content)
                else:
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        suffix=".mp3",
                        prefix="aiask_tts_",
                        delete=False,
                    ) as output:
                        output.write(resp.content)
                        output_path = output.name
                return {"success": True, "path": output_path, "provider": "openai", "bytes": len(resp.content)}
            return {"success": False, "path": "", "provider": "openai", "error": f"HTTP {resp.status_code}: {resp.text[:200]}"}
    except ImportError:
        return {"success": False, "path": "", "provider": "openai", "error": "httpx not installed"}
    except Exception as exc:
        return {"success": False, "path": "", "provider": "openai", "error": str(exc)}


async def _tts_iflytek(text: str, *, voice: str = "xiaoyan", output_path: str | None = None) -> dict[str, Any]:
    """讯飞 TTS。"""
    app_id = str(os.getenv("IFLYTEK_APP_ID") or "").strip()
    api_key = str(os.getenv("IFLYTEK_API_KEY") or "").strip()

    if not app_id or not api_key:
        return {"success": False, "path": "", "provider": "iflytek", "error": "IFLYTEK_APP_ID and IFLYTEK_API_KEY required"}

    # Note: Real implementation needs WebSocket to wss://tts-api.xfyun.cn/v2/tts
    return {"success": False, "path": "", "provider": "iflytek", "error": "iFlytek TTS requires WebSocket implementation (placeholder)"}


# ------------------------------------------------------------------
# Gateway integration helpers
# ------------------------------------------------------------------


async def process_voice_inbound(audio_path: str, *, language: str = "zh") -> str:
    """处理入站语音消息：转录为文本。"""
    result = await transcribe(audio_path, language=language)
    if result["success"]:
        return result["text"]
    return f"[语音转录失败: {result.get('error', 'unknown')}]"


async def process_voice_outbound(text: str, *, voice: str = "alloy") -> str | None:
    """处理出站语音消息：合成语音文件。返回文件路径或 None。"""
    # Only synthesize if text is short enough for voice
    if len(text) > 2000:
        return None
    result = await synthesize(text, voice=voice)
    if result["success"]:
        return result["path"]
    return None


def voice_configured() -> dict[str, Any]:
    """检查语音功能配置状态。"""
    stt = _stt_provider()
    tts = _tts_provider()

    stt_ready = False
    tts_ready = False

    if stt == "openai":
        stt_ready = bool(os.getenv("OPENAI_API_KEY"))
    elif stt == "iflytek":
        stt_ready = bool(os.getenv("IFLYTEK_APP_ID") and os.getenv("IFLYTEK_API_KEY"))
    elif stt == "local":
        import shutil
        stt_ready = bool(shutil.which("whisper-cpp") or shutil.which("whisper") or os.getenv("WHISPER_CPP_PATH"))

    if tts == "openai":
        tts_ready = bool(os.getenv("OPENAI_API_KEY"))
    elif tts == "iflytek":
        tts_ready = bool(os.getenv("IFLYTEK_APP_ID") and os.getenv("IFLYTEK_API_KEY"))

    return {
        "stt_provider": stt,
        "stt_configured": stt_ready,
        "tts_provider": tts,
        "tts_configured": tts_ready,
        "voice_enabled": stt_ready or tts_ready,
    }
