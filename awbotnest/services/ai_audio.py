"""Single-attempt OpenAI-compatible transcription through the system HTTP service."""
from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit

import httpx

from ..assistant_audio import parse_audio
from .ai_protocol import AIRequestError
from .ai_usage import AIUsageTracker

MAX_RESPONSE_BYTES = 64 * 1024
AUDIO_RESERVATION = 8192


def _target(ai):
    config = ai.settings.ai_settings if isinstance(ai.settings.ai_settings, dict) else {}
    capabilities = config.get("capabilities")
    assignment = capabilities.get("audio", {}) if isinstance(capabilities, dict) else {}
    model = assignment.get("default_model") if isinstance(assignment, dict) else ""
    models = config.get("models") if isinstance(config.get("models"), list) else []
    if not model or not any(isinstance(item, dict) and item.get("enabled", True)
                            and (model == item.get("id") or model == item.get("alias"))
                            and isinstance(item.get("capabilities"), list)
                            and "audio" in item["capabilities"] for item in models):
        raise AIRequestError("请先在 AI 服务中配置语音转写模型", category="configuration")
    try:
        target = ai._resolve_details("audio")
    except (ValueError, RuntimeError, PermissionError):
        raise AIRequestError("语音转写模型或对应服务不可用，请检查 AI 服务设置", category="configuration") from None
    if target.get("api_format") == "anthropic_messages":
        raise AIRequestError("语音转写需要支持 OpenAI 音频接口的服务", category="configuration")
    base = target["base_url"].rstrip("/")
    try:
        parsed = urlsplit(base)
        valid = (parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None
                 and parsed.password is None and not parsed.query and not parsed.fragment and "\\" not in base
                 and not any(ord(char) <= 32 or ord(char) == 127 for char in base))
        parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise AIRequestError("语音转写服务地址不正确", category="configuration")
    root = re.sub(r"/(?:audio/transcriptions|chat/completions|responses|messages|images/generations)$", "", base, flags=re.I)
    return target, root + "/audio/transcriptions"


def _status(code):
    if 200 <= code < 300:
        return
    if code in {401, 403}:
        message, category = "语音服务认证失败，请检查服务密钥与权限", "authentication"
    elif code == 429:
        message, category = "语音服务限流或额度不足，请稍后再试", "rate_limit"
    elif code in {400, 415, 422}:
        message, category = "语音服务未接受此音频或模型，请检查配置，或改用 WAV、MP3、M4A、WebM 格式", "invalid_request"
    elif code >= 500:
        message, category = "语音服务暂不可用，请稍后再试", "server"
    else:
        message, category = "语音服务接口不可用，请检查服务地址", "protocol_mismatch"
    raise AIRequestError(message, category=category, status_code=code)


async def transcribe(ai, name, data, mime="", *, budget_tokens: int | None = 32000):
    audio = parse_audio(name, data, mime)
    target, url = _target(ai)
    if budget_tokens is not None and (type(budget_tokens) is not int or budget_tokens < AUDIO_RESERVATION):
        raise AIRequestError("语音转写预算不足，未发送请求", category="budget")
    started, response_data = time.perf_counter(), None
    ai.usage.begin()
    succeeded = False
    ai._log_started("语音", "", target["model"], target["base_url"])
    try:
        try:
            async with ai._request_slot():
                async with ai.http.stream("POST", url,
                    headers={"Authorization": f"Bearer {target['api_key']}", "Accept-Encoding": "identity"},
                    data={"model": target["model"]},
                    files={"file": (audio["name"], audio["data"], audio["content_type"])},
                    timeout=ai._timeout("audio"), follow_redirects=False) as response:
                    _status(response.status_code)
                    if response.headers.get("content-encoding", "").strip().lower() not in {"", "identity"}:
                        raise AIRequestError("语音服务返回了不支持的压缩内容", category="invalid_response")
                    length = response.headers.get("content-length")
                    if length is not None and (not re.fullmatch(r"[0-9]{1,20}", length) or int(length) > MAX_RESPONSE_BYTES):
                        raise AIRequestError("语音服务返回内容过长", category="invalid_response")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise AIRequestError("语音服务返回内容过长", category="invalid_response")
                        body.extend(chunk)
            try:
                result = json.loads(body)
            except (ValueError, RecursionError, UnicodeError):
                raise AIRequestError("语音服务未返回有效的 JSON 转写结果", category="invalid_response") from None
            text = result.get("text") if isinstance(result, dict) else None
            if (not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 10000
                    or any(ord(char) < 32 and char not in "\t\n\r" for char in text)):
                raise AIRequestError("语音转写为空、格式不正确或超过 10000 字节，请缩短语音后重试", category="invalid_response")
            response_data = result
            counts = AIUsageTracker._tokens(result)
            usage = dict(zip(("input_tokens", "output_tokens", "total_tokens"), counts))
            ai.usage.record_tokens(result)
            ai._log_succeeded("语音", "", target["model"], target["base_url"], started, result)
            ai._record_attempt(capability="audio", plugin_id="", target=target, protocol="audio_transcriptions",
                               started=started, succeeded=True, data=result)
            ai.usage.succeed()
            succeeded = True
            return {"text": text.strip(), "usage": usage, "budget_used": max(AUDIO_RESERVATION, usage["total_tokens"])}
        except (httpx.TimeoutException, TimeoutError):
            raise AIRequestError("语音服务请求超时，未自动重试", category="timeout") from None
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise AIRequestError("语音服务连接或响应异常，未自动重试", category="network") from None
    except AIRequestError as exc:
        ai._log_failed("语音", "", target["model"], target["base_url"], started, exc, False)
        ai._record_attempt(capability="audio", plugin_id="", target=target, protocol="audio_transcriptions",
                           started=started, succeeded=False, data=response_data, exc=exc)
        raise
    finally:
        if not succeeded:
            ai.usage.fail()
