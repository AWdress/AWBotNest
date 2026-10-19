"""Fixed, bounded broker contract shared with the isolated runner."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

MAX_OUTPUT = 16 * 1024
MAX_FILE = 64 * 1024
MAX_REQUEST = 512 * 1024
MAX_WIRE = 2 * 1024 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def path_parts(value, *, root=False):
    if value == "." and root:
        return []
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 240
            or any(char in value for char in "\\:\x00") or value.startswith("/")
            or any(ord(char) < 32 for char in value)):
        raise ValueError("路径必须是工作目录内的相对路径")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("路径不能包含空段、. 或 ..")
    return parts


def _text(value, maximum, *, empty=False, byte_limit=False):
    if (not isinstance(value, str) or not empty and not value.strip()
            or len(value.encode("utf-8") if byte_limit else value) > maximum or "\x00" in value):
        raise ValueError("操作内容为空、过长或格式不正确")
    return value


def arguments_for(operation, value):
    if not isinstance(value, dict):
        raise ValueError("操作参数必须是对象")
    value = dict(value)
    if operation in {"python", "shell"}:
        key, maximum = ("code", 12000) if operation == "python" else ("command", 8000)
        if set(value) != {key}:
            raise ValueError("执行参数不正确")
        _text(value[key], maximum)
    elif operation == "read_file":
        if set(value) - {"path", "offset", "limit"} or "path" not in value:
            raise ValueError("读取参数不正确")
        path_parts(value["path"])
        for key, default, low, high in (("offset", 0, 0, MAX_FILE), ("limit", 2000, 1, 4000)):
            value.setdefault(key, default)
            if type(value[key]) is not int or not low <= value[key] <= high:
                raise ValueError("读取位置不正确")
    elif operation == "write_file":
        if set(value) != {"path", "content"}:
            raise ValueError("写入参数不正确")
        path_parts(value["path"])
        _text(value["content"], MAX_FILE, empty=True, byte_limit=True)
    elif operation == "list_files":
        if set(value) - {"path"}:
            raise ValueError("目录参数不正确")
        value.setdefault("path", ".")
        path_parts(value["path"], root=True)
    elif operation == "browser":
        action = value.get("action")
        fields = {"goto": {"url"}, "snapshot": set(), "click": {"selector"},
                  "fill": {"selector", "text"}, "select": {"selector", "value"},
                  "evaluate": {"script"}, "screenshot": set(), "set_content": {"html"}}
        if not isinstance(action, str) or action not in fields or set(value) != {"action", *fields[action]}:
            raise ValueError("浏览器动作或参数不正确")
        for key in fields[action]:
            maximum = MAX_FILE if key == "html" else 12000 if key == "script" else 4000
            _text(value[key], maximum, empty=key in {"html", "text", "value"}, byte_limit=key == "html")
        if action == "goto":
            parsed = urlsplit(value["url"])
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None or "\\" in value["url"]):
                raise ValueError("浏览器只能访问不含账号的 HTTP 或 HTTPS 地址")
    else:
        raise ValueError("不支持此操作")
    return value


def bounded_result(value):
    if len(json_bytes(value)) > MAX_OUTPUT:
        return {"ok": False, "error": "执行结果超过 16 KiB，请缩小查询或分段读取"}
    return value
