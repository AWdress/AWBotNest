from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


API_FORMATS = ("auto", "chat_completions", "responses", "anthropic_messages")
API_FORMAT_LABELS = {
    "auto": "自动识别",
    "chat_completions": "OpenAI Chat Completions",
    "responses": "OpenAI Responses",
    "anthropic_messages": "Anthropic Messages",
}


def _safe_detail(value: object) -> str:
    detail = re.sub(r"\s+", " ", str(value or "")).strip()[:200]
    detail = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", detail)
    detail = re.sub(r"(?i)\bsk-[A-Za-z0-9_-]{8,}", "sk-***", detail)
    detail = re.sub(
        r"(?i)(api[_ -]?key|authorization|token)(\s*[:=]\s*)[^\s,;}]+",
        r"\1\2***", detail,
    )
    return detail


class AIRequestError(RuntimeError):
    def __init__(self, message: str, *, category: str = "unknown",
                 status_code: int | None = None, retryable: bool = False,
                 protocol_mismatch: bool = False) -> None:
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retryable = retryable
        self.protocol_mismatch = protocol_mismatch


@dataclass(slots=True)
class ProtocolRequest:
    api_format: str
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]


def normalize_api_format(value: object) -> str:
    normalized = str(value or "auto").strip().lower().replace("-", "_")
    aliases = {
        "chat": "chat_completions",
        "openai": "chat_completions",
        "response": "responses",
        "anthropic": "anthropic_messages",
        "messages": "anthropic_messages",
    }
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in API_FORMATS else "auto"


def protocol_candidates(configured: object, base_url: str, model: str,
                        cached: str = "") -> list[str]:
    selected = normalize_api_format(configured)
    if selected != "auto":
        return [selected]
    if cached in API_FORMATS and cached != "auto":
        return [cached]
    hint = f"{base_url} {model}".lower()
    if "anthropic" in hint or "claude" in model.lower():
        return ["anthropic_messages", "chat_completions", "responses"]
    if "/responses" in base_url.lower():
        return ["responses", "chat_completions", "anthropic_messages"]
    return ["chat_completions", "responses", "anthropic_messages"]


def _api_root(base_url: str) -> str:
    return re.sub(r"/(?:chat/completions|responses|messages|images/generations)/?$", "",
                  base_url.rstrip("/"), flags=re.I)


def _responses_content(content: object) -> object:
    if not isinstance(content, list):
        return content
    values: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            values.append({"type": "input_text", "text": str(item.get("text") or "")})
        elif item.get("type") == "image_url":
            image = item.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if url:
                values.append({"type": "input_image", "image_url": str(url)})
        else:
            values.append(dict(item))
    return values


def _anthropic_content(content: object) -> object:
    if not isinstance(content, list):
        return content
    values: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            values.append({"type": "text", "text": str(item.get("text") or "")})
            continue
        if item.get("type") != "image_url":
            continue
        image = item.get("image_url")
        url = str(image.get("url") if isinstance(image, dict) else image or "")
        if not url:
            continue
        match = re.match(r"^data:([^;,]+);base64,(.+)$", url, re.S)
        source = ({"type": "base64", "media_type": match.group(1), "data": match.group(2)}
                  if match else {"type": "url", "url": url})
        values.append({"type": "image", "source": source})
    return values


def build_request(api_format: str, base_url: str, api_key: str, model: str,
                  messages: list[dict[str, object]], *, temperature: float | None = None,
                  max_tokens: int | None = None) -> ProtocolRequest:
    root = _api_root(base_url)
    if api_format == "chat_completions":
        payload: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max(1, int(max_tokens))
        return ProtocolRequest(api_format, f"{root}/chat/completions",
                               {"Authorization": f"Bearer {api_key}"}, payload)

    if api_format == "responses":
        response_messages = [
            {"role": str(item.get("role") or "user"),
             "content": _responses_content(item.get("content", ""))}
            for item in messages
        ]
        payload = {"model": model, "input": response_messages}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_output_tokens"] = max(1, int(max_tokens))
        return ProtocolRequest(api_format, f"{root}/responses",
                               {"Authorization": f"Bearer {api_key}"}, payload)

    if api_format == "anthropic_messages":
        system_parts: list[str] = []
        anthropic_messages: list[dict[str, Any]] = []
        for item in messages:
            role = str(item.get("role") or "user")
            content = item.get("content", "")
            if role == "system":
                if isinstance(content, str):
                    system_parts.append(content)
                continue
            anthropic_messages.append({
                "role": "assistant" if role == "assistant" else "user",
                "content": _anthropic_content(content),
            })
        payload = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max(1, int(max_tokens or 1024)),
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if temperature is not None:
            payload["temperature"] = temperature
        return ProtocolRequest(api_format, f"{root}/messages", {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }, payload)
    raise AIRequestError("不支持的 AI 接口格式", category="configuration")


def decode_json(response: httpx.Response, api_format: str) -> dict[str, Any]:
    try:
        value = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        excerpt = _safe_detail(response.text)[:160]
        if excerpt.startswith("<"):
            detail = "接口返回了 HTML 页面"
        elif not excerpt:
            detail = "接口返回内容为空"
        else:
            detail = f"接口返回的不是有效 JSON（{content_type or '未知类型'}）"
        raise AIRequestError(
            f"协议不匹配：{detail}，当前尝试 {API_FORMAT_LABELS.get(api_format, api_format)}",
            category="invalid_response", protocol_mismatch=True,
        ) from exc
    if not isinstance(value, dict):
        raise AIRequestError("协议响应格式不匹配：顶层内容不是对象",
                             category="invalid_response", protocol_mismatch=True)
    return value


def raise_for_status(response: httpx.Response, api_format: str) -> None:
    if response.is_success:
        return
    status = response.status_code
    detail = ""
    try:
        body = response.json()
        error = body.get("error", body) if isinstance(body, dict) else {}
        if isinstance(error, dict):
            detail = _safe_detail(error.get("message") or error.get("detail"))
        elif isinstance(error, str):
            detail = _safe_detail(error)
    except (ValueError, json.JSONDecodeError):
        detail = _safe_detail(response.text)[:160]
    suffix = f"：{detail[:200]}" if detail else ""
    if status == 401:
        raise AIRequestError(f"API Key 认证失败（HTTP 401）{suffix}", category="authentication",
                             status_code=status)
    if status == 403:
        raise AIRequestError(f"服务商拒绝访问（HTTP 403）{suffix}", category="permission",
                             status_code=status)
    if status == 404:
        model_missing = "model" in detail.lower() or "模型" in detail
        raise AIRequestError(
            f"{'模型不存在或无权使用' if model_missing else '接口路径不存在'}（HTTP 404）{suffix}",
            category="model_not_found" if model_missing else "endpoint_not_found",
            status_code=status, protocol_mismatch=not model_missing,
        )
    if status == 429:
        raise AIRequestError(f"服务商限流（HTTP 429）{suffix}", category="rate_limit",
                             status_code=status, retryable=True)
    if status in {405, 415}:
        raise AIRequestError(f"接口不支持当前请求格式（HTTP {status}）{suffix}",
                             category="protocol_mismatch", status_code=status,
                             protocol_mismatch=True)
    if status in {400, 422}:
        mismatch = bool(re.search(
            r"(?i)(unsupported|not support|unknown endpoint|invalid endpoint|chat.?completions|responses api|messages api|协议|接口)",
            detail,
        ))
        raise AIRequestError(f"请求参数或接口格式不兼容（HTTP {status}）{suffix}",
                             category="protocol_mismatch" if mismatch else "invalid_request",
                             status_code=status, protocol_mismatch=mismatch)
    if status >= 500:
        raise AIRequestError(f"上游服务异常（HTTP {status}）{suffix}", category="upstream",
                             status_code=status, retryable=True)
    raise AIRequestError(f"AI 请求失败（HTTP {status}）{suffix}", category="http_error",
                         status_code=status)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return ""


def parse_text_response(data: dict[str, Any], api_format: str) -> str:
    try:
        if api_format == "chat_completions":
            text = _content_text(data["choices"][0]["message"]["content"])
        elif api_format == "responses":
            text = str(data.get("output_text") or "")
            if not text:
                text = "".join(
                    _content_text(item.get("content"))
                    for item in data.get("output", []) if isinstance(item, dict)
                )
        elif api_format == "anthropic_messages":
            text = _content_text(data.get("content"))
        else:
            text = ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AIRequestError(
            f"协议响应格式不匹配：未找到文字内容（{API_FORMAT_LABELS.get(api_format, api_format)}）",
            category="invalid_response", protocol_mismatch=True,
        ) from exc
    if not text:
        error = data.get("error")
        detail = str(error.get("message") if isinstance(error, dict) else error or "")
        suffix = f"：{detail[:160]}" if detail else ""
        raise AIRequestError(
            f"协议响应格式不匹配：没有可读取的文字内容{suffix}",
            category="invalid_response", protocol_mismatch=True,
        )
    return text
