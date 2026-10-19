from __future__ import annotations

import json
import re
from copy import deepcopy
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

MAX_TOOL_CALLS = 16
MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_TOOL_HISTORY_BYTES = 2 * 1024 * 1024


def _tool_error(message: str, *, configuration: bool = False) -> AIRequestError:
    return AIRequestError(message, category="configuration" if configuration else "invalid_tool_call")


def _bounded_json(value: object, limit: int, *, configuration: bool = False) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > limit:
            raise _tool_error("AI 工具数据超过大小限制", configuration=configuration)
        return encoded
    except (TypeError, ValueError, RecursionError, UnicodeError) as exc:
        raise _tool_error("AI 工具数据不是有效 JSON", configuration=configuration) from exc


def _tool_name(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise _tool_error("AI 工具名称无效")
    return value


def _call_id(value: object) -> str:
    if (not isinstance(value, str) or not value or len(value) > 200
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise _tool_error("AI 工具调用标识无效")
    return value


def _argument_object(value: object) -> dict[str, Any]:
    if isinstance(value, str):
        if len(value.encode("utf-8", errors="replace")) > MAX_TOOL_ARGUMENT_BYTES:
            raise _tool_error("AI 工具参数超过大小限制")

        def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = item
            return result

        def constant(_: str) -> None:
            raise ValueError("non-finite number")

        try:
            value = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
        except (ValueError, RecursionError) as exc:
            raise _tool_error("AI 工具参数不是有效 JSON 对象") from exc
    if not isinstance(value, dict):
        raise _tool_error("AI 工具参数必须是 JSON 对象")
    _bounded_json(value, MAX_TOOL_ARGUMENT_BYTES)
    # Bound nesting as well as bytes before passing the object to plugin code.
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > 32:
            raise _tool_error("AI 工具参数嵌套过深")
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return deepcopy(value)


def normalize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Accept function definitions, not provider-hosted or executable text tools."""
    if not isinstance(tools, list) or len(tools) > 64:
        raise _tool_error("AI 工具列表无效或数量过多", configuration=True)
    _bounded_json(tools, 256 * 1024, configuration=True)
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type", "function") != "function":
            raise _tool_error("仅支持已声明的函数工具", configuration=True)
        function = tool.get("function", tool)
        if not isinstance(function, dict):
            raise _tool_error("AI 工具定义无效", configuration=True)
        name = _tool_name(function.get("name"))
        if name in names:
            raise _tool_error("AI 工具名称重复", configuration=True)
        names.add(name)
        schema = function.get("parameters", function.get("input_schema", {"type": "object", "properties": {}}))
        if not isinstance(schema, dict) or schema.get("type", "object") != "object":
            raise _tool_error("AI 工具参数定义必须是对象", configuration=True)
        value = {"name": name, "description": str(function.get("description") or ""),
                 "parameters": deepcopy(schema)}
        if isinstance(function.get("strict"), bool):
            value["strict"] = function["strict"]
        result.append(value)
    return result


def _normalize_calls(calls: object, allowed: set[str] | None = None) -> list[dict[str, Any]]:
    if calls is None:
        calls = []
    if not isinstance(calls, list) or len(calls) > MAX_TOOL_CALLS:
        raise _tool_error("AI 工具调用列表无效或数量过多")
    _bounded_json(calls, 256 * 1024)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for call in calls:
        if not isinstance(call, dict) or call.get("type", "function") != "function":
            raise _tool_error("AI 返回了不支持的工具调用")
        function = call.get("function", call)
        if not isinstance(function, dict):
            raise _tool_error("AI 工具调用格式无效")
        name, identifier = _tool_name(function.get("name")), _call_id(call.get("id"))
        if identifier in seen:
            raise _tool_error("AI 工具调用标识重复")
        if allowed is not None and name not in allowed:
            raise _tool_error("AI 调用了未提供的工具")
        seen.add(identifier)
        result.append({"id": identifier, "name": name,
                       "arguments": _argument_object(function.get("arguments"))})
    return result


def _chat_calls(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"id": call["id"], "type": "function", "function": {
        "name": call["name"], "arguments": _bounded_json(call["arguments"], MAX_TOOL_ARGUMENT_BYTES),
    }} for call in calls]


def validate_tool_messages(messages: list[dict[str, Any]]) -> None:
    if not isinstance(messages, list) or len(messages) > 512:
        raise _tool_error("AI 工具对话记录无效", configuration=True)
    _bounded_json(messages, MAX_TOOL_HISTORY_BYTES, configuration=True)
    seen: set[str] = set()
    pending: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            raise _tool_error("AI 对话消息无效", configuration=True)
        role = message.get("role")
        if role not in {"system", "developer", "user", "assistant", "tool"}:
            raise _tool_error("AI 对话消息角色无效", configuration=True)
        if role == "tool":
            identifier = _call_id(message.get("tool_call_id"))
            if identifier not in pending or not isinstance(message.get("content"), str):
                raise _tool_error("AI 工具结果没有对应的待处理调用", configuration=True)
            pending.remove(identifier)
            continue
        if pending:
            raise _tool_error("AI 工具调用缺少结果", configuration=True)
        if not isinstance(message.get("content", ""), (str, list, type(None))):
            raise _tool_error("AI 对话内容无效", configuration=True)
        if role == "assistant":
            for call in _normalize_calls(message.get("tool_calls", [])):
                if call["id"] in seen:
                    raise _tool_error("AI 对话中的工具调用标识重复", configuration=True)
                seen.add(call["id"])
                pending.add(call["id"])
    if pending:
        raise _tool_error("AI 工具调用缺少结果", configuration=True)


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


def _responses_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role == "tool":
            result.append({"type": "function_call_output", "call_id": message["tool_call_id"],
                           "output": message["content"]})
            continue
        calls = _normalize_calls(message.get("tool_calls", [])) if role == "assistant" else []
        previous_output = message.get("_responses_output") if role == "assistant" else None
        if previous_output is not None:
            if (not isinstance(previous_output, list)
                    or any(not isinstance(item, dict) or item.get("type") not in {
                        "reasoning", "message", "function_call",
                    } for item in previous_output)):
                raise _tool_error("Responses 历史输出无效", configuration=True)
            if any(item.get("type") == "message" and item.get("role", "assistant") != "assistant"
                   for item in previous_output):
                raise _tool_error("Responses 历史消息角色无效", configuration=True)
            raw_calls = _normalize_calls([
                {"id": item.get("call_id"), "name": item.get("name"),
                 "arguments": item.get("arguments")}
                for item in previous_output if item["type"] == "function_call"
            ])
            if raw_calls != calls:
                raise _tool_error("Responses 历史调用不一致", configuration=True)
            # Reasoning (including encrypted content) must be replayed unchanged.
            result.extend(deepcopy(previous_output))
            continue
        content = message.get("content") or ""
        if content or not calls:
            result.append({"role": role, "content": (
                _content_text(content) if role == "assistant" else _responses_content(content)
            )})
        result.extend({"type": "function_call", "call_id": call["id"], "name": call["name"],
                       "arguments": _bounded_json(call["arguments"], MAX_TOOL_ARGUMENT_BYTES)}
                      for call in calls)
    return result


def _anthropic_history(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    system: list[str] = []
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        if role in {"system", "developer"}:
            system.append(_content_text(message.get("content")))
            continue
        if role == "tool":
            blocks = [{"type": "tool_result", "tool_use_id": message["tool_call_id"],
                       "content": message["content"]}]
            if message.get("is_error") is True:
                blocks[0]["is_error"] = True
            role = "user"
        elif role == "assistant":
            calls = _normalize_calls(message.get("tool_calls", []))
            previous = message.get("_anthropic_content")
            if previous is not None:
                if (not isinstance(previous, list)
                        or any(not isinstance(item, dict) or item.get("type") not in {
                            "text", "thinking", "redacted_thinking", "tool_use",
                        } for item in previous)):
                    raise _tool_error("Anthropic 历史输出无效", configuration=True)
                raw_calls = _normalize_calls([
                    {"id": item.get("id"), "name": item.get("name"), "arguments": item.get("input")}
                    for item in previous if item["type"] == "tool_use"
                ])
                if raw_calls != calls:
                    raise _tool_error("Anthropic 历史调用不一致", configuration=True)
                blocks = deepcopy(previous)
            else:
                text = _content_text(message.get("content"))
                blocks = [{"type": "text", "text": text}] if text else []
                blocks.extend({"type": "tool_use", "id": call["id"], "name": call["name"],
                               "input": call["arguments"]} for call in calls)
        else:
            converted = _anthropic_content(message.get("content") or "")
            blocks = converted if isinstance(converted, list) else [{"type": "text", "text": converted}]
        if not blocks:
            continue
        # Parallel results belong in one user turn, immediately after tool_use.
        if result and result[-1]["role"] == role:
            result[-1]["content"].extend(blocks)
        else:
            result.append({"role": role, "content": blocks})
    return "\n\n".join(part for part in system if part), result


def build_tool_request(api_format: str, base_url: str, api_key: str, model: str,
                       messages: list[dict[str, Any]], tools: list[dict[str, Any]], *,
                       max_tokens: int | None = 2048) -> ProtocolRequest:
    validate_tool_messages(messages)
    definitions = normalize_tools(tools)
    request = build_request(api_format, base_url, api_key, model, [], max_tokens=max_tokens)
    if api_format == "chat_completions":
        history: list[dict[str, Any]] = []
        for message in messages:
            item = {"role": message["role"], "content": message.get("content") or ""}
            if message["role"] == "tool":
                item["tool_call_id"] = message["tool_call_id"]
            elif message["role"] == "assistant":
                calls = _normalize_calls(message.get("tool_calls", []))
                if calls:
                    item["tool_calls"] = _chat_calls(calls)
                if isinstance(message.get("reasoning_content"), str):
                    item["reasoning_content"] = message["reasoning_content"]
            history.append(item)
        request.payload["messages"] = history
        request.payload["tools"] = [{"type": "function", "function": item} for item in definitions]
    elif api_format == "responses":
        request.payload["input"] = _responses_history(messages)
        # Do not silently force strict mode onto ordinary plugin JSON schemas.
        request.payload["tools"] = [{"type": "function", "strict": False, **item} for item in definitions]
        request.payload["store"] = False
        request.payload["include"] = ["reasoning.encrypted_content"]
    elif api_format == "anthropic_messages":
        system, history = _anthropic_history(messages)
        request.payload["messages"] = history
        if system:
            request.payload["system"] = system
        request.payload["tools"] = [{"name": item["name"], "description": item["description"],
                                     "input_schema": item["parameters"]} for item in definitions]
    if not definitions:
        request.payload.pop("tools", None)
    return request


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


def parse_tool_response(data: dict[str, Any], api_format: str, *,
                        allowed_tools: set[str] | None = None,
                        model: str = "") -> dict[str, Any]:
    """Decode one model turn. This function never executes a tool."""
    _bounded_json(data, 4 * 1024 * 1024)
    raw_calls: object = []
    message: dict[str, Any] = {"role": "assistant"}
    try:
        if api_format == "chat_completions":
            choice = data["choices"][0]
            assistant = choice["message"]
            text = _content_text(assistant.get("content"))
            if not text and isinstance(assistant.get("refusal"), str):
                text = assistant["refusal"]
            raw_calls = assistant.get("tool_calls", [])
            finish = str(choice.get("finish_reason") or "")
            if isinstance(assistant.get("reasoning_content"), str):
                message["reasoning_content"] = assistant["reasoning_content"]
        elif api_format == "responses":
            output = data.get("output", [])
            if (not isinstance(output, list)
                    or any(not isinstance(item, dict) or item.get("type") not in {
                        "message", "reasoning", "function_call",
                    } for item in output)):
                raise _tool_error("Responses 返回了不支持的输出类型")
            if any(item.get("type") == "message" and item.get("role", "assistant") != "assistant"
                   for item in output):
                raise _tool_error("Responses 返回了无效的消息角色")
            raw_calls = [{"id": item.get("call_id"), "name": item.get("name"),
                          "arguments": item.get("arguments")}
                         for item in output if item["type"] == "function_call"]
            text = str(data.get("output_text") or "") or "".join(
                _content_text(item.get("content")) for item in output if item["type"] == "message"
            )
            if not text:
                text = "".join(
                    str(block.get("refusal") or "")
                    for item in output if item["type"] == "message"
                    for block in item.get("content", [])
                    if isinstance(block, dict) and block.get("type") == "refusal"
                )
            finish = str(data.get("status") or "completed")
            if output:
                message["_responses_output"] = deepcopy(output)
        elif api_format == "anthropic_messages":
            content = data.get("content", [])
            if (not isinstance(content, list)
                    or any(not isinstance(item, dict) or item.get("type") not in {
                        "text", "thinking", "redacted_thinking", "tool_use",
                    } for item in content)):
                raise _tool_error("Anthropic 返回了不支持的输出类型")
            raw_calls = [{"id": item.get("id"), "name": item.get("name"), "arguments": item.get("input")}
                         for item in content if item["type"] == "tool_use"]
            text = _content_text(content)
            finish = str(data.get("stop_reason") or "")
            message["_anthropic_content"] = deepcopy(content)
        else:
            raise AIRequestError("不支持的 AI 接口格式", category="configuration")
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AIRequestError("AI 工具响应格式不匹配", category="invalid_response",
                             protocol_mismatch=True) from exc
    calls = _normalize_calls(raw_calls, allowed_tools)
    if calls and finish in {"length", "max_tokens", "content_filter", "incomplete", "failed", "cancelled"}:
        raise _tool_error("AI 工具响应未完整生成，请调整输出预算后重试")
    if not calls and not text:
        raise AIRequestError("AI 服务未返回文字或工具调用", category="invalid_response",
                             protocol_mismatch=True)
    message["content"] = text
    if calls:
        message["tool_calls"] = _chat_calls(calls)
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    def tokens(value: object) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

    input_tokens = tokens(usage.get("prompt_tokens", usage.get("input_tokens")))
    output_tokens = tokens(usage.get("completion_tokens", usage.get("output_tokens")))
    total_value = usage.get("total_tokens")
    total_tokens = tokens(total_value) if total_value is not None else input_tokens + output_tokens
    return {"text": text, "tool_calls": calls, "message": message,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens},
            "finish_reason": finish, "model": str(data.get("model") or model)}
