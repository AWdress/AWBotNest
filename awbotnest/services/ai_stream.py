"""Bounded protocol streaming. A partial stream is never a callable tool turn."""
from __future__ import annotations

import inspect
import json
from copy import deepcopy

import httpx

from .ai_protocol import AIRequestError, MAX_TOOL_CALLS, ProtocolRequest


MAX_STREAM_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 256 * 1024


def _error(message="AI 流式响应不完整，请重新发起任务"):
    # In particular, do not turn a partially received request into a protocol
    # mismatch: auto-detection must not replay it through another endpoint.
    return AIRequestError(message, category="incomplete_stream", retryable=False)


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("non-finite number")

    try:
        result = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise _error("AI 流式响应包含无效 JSON，未执行工具") from exc
    if not isinstance(result, dict):
        raise _error("AI 流式响应格式无效，未执行工具")
    return result


async def _emit(callback, value):
    if callback is not None and value:
        result = callback(value)
        if inspect.isawaitable(result):
            await result


def _string(value):
    if not isinstance(value, str):
        raise _error("AI 流式文本格式无效，未执行工具")
    return value


async def _events(response):
    """Parse CR, LF and CRLF framing without unbounded aiter_lines buffering."""
    buffer = bytearray()
    data, event = [], "message"
    total = event_size = 0
    skip_lf = False
    first_line = True
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_STREAM_BYTES:
            raise _error("AI 流式响应超过 2 MiB，未执行工具")
        buffer.extend(chunk)
        while buffer:
            if skip_lf:
                if buffer[0] == 10:
                    del buffer[0]
                skip_lf = False
                if not buffer:
                    break
            candidates = [index for index in (buffer.find(b"\r"), buffer.find(b"\n")) if index >= 0]
            if not candidates:
                break
            index = min(candidates)
            line = bytes(buffer[:index])
            skip_lf = buffer[index] == 13
            del buffer[:index + 1]
            event_size += len(line) + 1
            if event_size > MAX_EVENT_BYTES:
                raise _error("AI 流式单条事件超过 256 KiB，未执行工具")
            if first_line:
                line = line.removeprefix(b"\xef\xbb\xbf")
                first_line = False
            try:
                line = line.decode("utf-8", errors="strict")
            except UnicodeError as exc:
                raise _error("AI 流式响应编码无效，未执行工具") from exc
            if not line:
                if data:
                    yield event, "\n".join(data)
                data, event, event_size = [], "message", 0
            elif not line.startswith(":"):
                name, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if name == "data":
                    data.append(value)
                elif name == "event":
                    event = value
        if event_size + len(buffer) > MAX_EVENT_BYTES:
            raise _error("AI 流式单条事件超过 256 KiB，未执行工具")
    # An unterminated event is deliberately discarded. Only a framed terminal
    # event can make the aggregate available to the existing tool validator.


class _Chat:
    def __init__(self):
        self.message = {"role": "assistant", "content": ""}
        self.calls = {}
        self.model = ""
        self.usage = {}
        self.finish = None
        self.done = False

    async def feed(self, event, data, callback):
        if data == "[DONE]":
            if self.finish not in {"stop", "tool_calls"}:
                raise _error()
            self.done = True
            return
        value = _json(data)
        if value.get("error"):
            raise _error("AI 服务在流式生成时返回错误，未执行工具")
        if isinstance(value.get("model"), str):
            self.model = value["model"]
        if isinstance(value.get("usage"), dict):
            self.usage = value["usage"]
        choices = value.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 1:
            raise _error("AI 流式候选结果无效，未执行工具")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise _error("AI 流式候选结果无效，未执行工具")
            delta = choice.get("delta", {})
            if not isinstance(delta, dict) or self.finish is not None:
                raise _error()
            if delta.get("role", "assistant") != "assistant":
                raise _error("AI 流式消息角色无效，未执行工具")
            for key in ("content", "refusal", "reasoning_content"):
                fragment = delta.get(key)
                if fragment is not None:
                    fragment = _string(fragment)
                    self.message[key] = self.message.get(key, "") + fragment
                    if key in {"content", "refusal"}:
                        await _emit(callback, fragment)
            fragments = delta.get("tool_calls", [])
            if not isinstance(fragments, list) or len(fragments) > MAX_TOOL_CALLS:
                raise _error("AI 工具调用数量超过限制，未执行工具")
            for fragment in fragments:
                if not isinstance(fragment, dict):
                    raise _error("AI 流式工具格式无效，未执行工具")
                index = fragment.get("index")
                if type(index) is not int or not 0 <= index < MAX_TOOL_CALLS:
                    raise _error("AI 流式工具序号无效，未执行工具")
                call = self.calls.setdefault(index, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                if fragment.get("type", "function") != "function":
                    raise _error("AI 返回了不支持的流式工具，未执行工具")
                if fragment.get("id") is not None:
                    call["id"] += _string(fragment["id"])
                function = fragment.get("function", {})
                if not isinstance(function, dict):
                    raise _error("AI 流式工具格式无效，未执行工具")
                for key in ("name", "arguments"):
                    if function.get(key) is not None:
                        call["function"][key] += _string(function[key])
            if choice.get("finish_reason") is not None:
                self.finish = choice["finish_reason"]
                if not isinstance(self.finish, str) or self.finish not in {"stop", "tool_calls"}:
                    raise _error("AI 响应未完整生成，未执行工具，请调整输出预算后重试")

    def result(self):
        if not self.done or self.finish is None or sorted(self.calls) != list(range(len(self.calls))):
            raise _error()
        if self.calls:
            self.message["tool_calls"] = [self.calls[index] for index in sorted(self.calls)]
        return {"model": self.model, "choices": [{"index": 0, "message": self.message, "finish_reason": self.finish}], "usage": self.usage}


class _Responses:
    def __init__(self):
        self.value = None
        self.done = False
        self.calls = set()

    async def feed(self, event, data, callback):
        if data == "[DONE]":
            if not self.done:
                raise _error()
            return
        value = _json(data)
        kind = value.get("type", event)
        if not isinstance(kind, str):
            raise _error("AI 流式事件类型无效，未执行工具")
        if event not in {"message", kind}:
            raise _error("AI 流式事件类型不一致，未执行工具")
        if kind in {"error", "response.error", "response.failed", "response.incomplete"}:
            raise _error("AI 响应未完整生成，未执行工具")
        if kind == "response.output_text.delta":
            await _emit(callback, _string(value.get("delta")))
        elif kind == "response.output_item.added":
            item = value.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                index = value.get("output_index")
                if type(index) is not int or not 0 <= index < 64:
                    raise _error("AI 流式工具序号无效，未执行工具")
                self.calls.add(index)
                if len(self.calls) > MAX_TOOL_CALLS:
                    raise _error("AI 工具调用数量超过限制，未执行工具")
        elif kind == "response.completed":
            response = value.get("response")
            if self.done or not isinstance(response, dict) or response.get("status") != "completed":
                raise _error()
            output = response.get("output")
            if not isinstance(output, list) or sum(isinstance(item, dict) and item.get("type") == "function_call" for item in output) > MAX_TOOL_CALLS:
                raise _error("AI 流式输出或工具调用数量无效，未执行工具")
            self.value, self.done = response, True

    def result(self):
        if not self.done or self.value is None:
            raise _error()
        return self.value


class _Anthropic:
    def __init__(self):
        self.value = None
        self.blocks, self.partial = {}, {}
        self.closed = set()
        self.done = False

    async def feed(self, event, data, callback):
        value = _json(data)
        kind = value.get("type", event)
        if not isinstance(kind, str):
            raise _error("AI 流式事件类型无效，未执行工具")
        if event not in {"message", kind}:
            raise _error("AI 流式事件类型不一致，未执行工具")
        if kind == "ping":
            return
        if kind == "error":
            raise _error("AI 服务在流式生成时返回错误，未执行工具")
        if kind == "message_start":
            message = value.get("message")
            if self.value is not None or not isinstance(message, dict) or message.get("role", "assistant") != "assistant" or message.get("content", []) != []:
                raise _error("AI 流式消息起始内容无效，未执行工具")
            self.value = deepcopy(message)
            if message.get("usage") is not None and not isinstance(message["usage"], dict):
                raise _error("AI 流式用量格式无效，未执行工具")
            self.value["usage"] = dict(message.get("usage") or {})
            return
        if self.value is None or self.done:
            raise _error()
        if kind == "content_block_start":
            index, block = value.get("index"), value.get("content_block")
            if type(index) is not int or not 0 <= index < 64 or index in self.blocks or not isinstance(block, dict) or not isinstance(block.get("type"), str) or block.get("type") not in {"text", "thinking", "redacted_thinking", "tool_use"}:
                raise _error("AI 流式内容块无效，未执行工具")
            self.blocks[index] = deepcopy(block)
            if sum(item.get("type") == "tool_use" for item in self.blocks.values()) > MAX_TOOL_CALLS:
                raise _error("AI 工具调用数量超过限制，未执行工具")
            if block["type"] == "text":
                await _emit(callback, _string(block.get("text", "")))
        elif kind == "content_block_delta":
            index, delta = value.get("index"), value.get("delta")
            if type(index) is not int or index not in self.blocks or index in self.closed or not isinstance(delta, dict):
                raise _error("AI 流式内容块无效，未执行工具")
            block = self.blocks[index]
            part = delta.get("type")
            if not isinstance(part, str):
                raise _error("AI 流式内容类型无效，未执行工具")
            if part == "text_delta" and block["type"] == "text":
                text = _string(delta.get("text"))
                block["text"] = block.get("text", "") + text
                await _emit(callback, text)
            elif part == "input_json_delta" and block["type"] == "tool_use":
                if block.get("input") not in ({}, None):
                    raise _error("AI 流式工具参数不一致，未执行工具")
                self.partial[index] = self.partial.get(index, "") + _string(delta.get("partial_json"))
            elif part in {"thinking_delta", "signature_delta"} and block["type"] == "thinking":
                key = "thinking" if part == "thinking_delta" else "signature"
                block[key] = block.get(key, "") + _string(delta.get(key))
            else:
                raise _error("AI 返回了不支持的流式内容，未执行工具")
        elif kind == "content_block_stop":
            index = value.get("index")
            if type(index) is not int or index not in self.blocks or index in self.closed:
                raise _error("AI 流式内容块结束状态无效，未执行工具")
            if index in self.partial:
                self.blocks[index]["input"] = _json(self.partial[index])
            self.closed.add(index)
        elif kind == "message_delta":
            delta = value.get("delta")
            if not isinstance(delta, dict):
                raise _error()
            for key in ("stop_reason", "stop_sequence"):
                if key in delta:
                    self.value[key] = delta[key]
            if isinstance(value.get("usage"), dict):
                self.value["usage"].update(value["usage"])
        elif kind == "message_stop":
            stop_reason = self.value.get("stop_reason")
            if self.closed != set(self.blocks) or not isinstance(stop_reason, str) or stop_reason not in {"end_turn", "tool_use", "stop_sequence", "refusal"}:
                raise _error("AI 响应未完整生成，未执行工具")
            if sorted(self.blocks) != list(range(len(self.blocks))):
                raise _error("AI 流式内容块序号不完整，未执行工具")
            self.value["content"] = [self.blocks[index] for index in sorted(self.blocks)]
            self.done = True
        else:
            raise _error("AI 流式事件格式无效，未执行工具")

    def result(self):
        if not self.done or self.value is None:
            raise _error()
        return self.value


async def stream_tool_response(http, request: ProtocolRequest, protocol: str, *, timeout, on_delta=None) -> httpx.Response:
    """Return complete JSON as a Response for the existing protocol validators.

    No redirects, protocol fallback, tool execution or request retry happens here.
    A non-SSE JSON/error response is retained, with the same bounded body limit.
    """
    collectors = {"chat_completions": _Chat, "responses": _Responses, "anthropic_messages": _Anthropic}
    if protocol not in collectors:
        raise AIRequestError("不支持的 AI 流式协议", category="configuration")
    payload = {**request.payload, "stream": True}
    if protocol == "chat_completions":
        payload["stream_options"] = {"include_usage": True}
    received = False
    try:
        async with http.stream("POST", request.url, headers=request.headers, json=payload,
                               timeout=timeout, follow_redirects=False) as response:
            mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if not response.is_success or mime != "text/event-stream":
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=16 * 1024):
                    received = True
                    body.extend(chunk)
                    if len(body) > MAX_STREAM_BYTES:
                        raise _error("AI 响应超过 2 MiB，未执行工具")
                headers = {key: value for key, value in response.headers.items()
                           if key.lower() not in {"content-length", "content-encoding", "transfer-encoding"}}
                if response.is_success:
                    try:
                        data = json.loads(body)
                    except (ValueError, UnicodeError, RecursionError):
                        data = None  # Existing decoder classifies HTML/invalid JSON.
                    if isinstance(data, dict):
                        finish = None
                        if protocol == "chat_completions":
                            choices = data.get("choices")
                            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                                finish = choices[0].get("finish_reason")
                        elif protocol == "responses":
                            finish = data.get("status")
                        else:
                            finish = data.get("stop_reason")
                        if isinstance(finish, str) and finish in {"length", "max_tokens", "content_filter", "incomplete", "failed", "cancelled", "model_context_window_exceeded"}:
                            raise _error("AI 响应未完整生成，未执行工具，请调整输出预算后重试")
                return httpx.Response(response.status_code, headers=headers, content=bytes(body), request=response.request)
            collector = collectors[protocol]()
            async for event, data in _events(response):
                received = True
                await collector.feed(event, data, on_delta)
                if collector.done:
                    # Do not wait for an upstream socket kept alive after its
                    # terminal event. Exiting the context closes it normally.
                    break
            return httpx.Response(response.status_code, json=collector.result(), request=response.request)
    except httpx.HTTPError as exc:
        message = "AI 流式连接中断，未执行工具" if received else "AI 流式连接失败，请稍后重试"
        raise _error(message) from exc
