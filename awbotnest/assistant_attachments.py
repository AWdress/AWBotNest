"""Bounded UTF-8 attachments, parsed in memory and never executed or opened by path."""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata

MAX_FILES = 3
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 128 * 1024
MAX_TEXT_BYTES = 10000
TEXT_EXTENSIONS = frozenset({".txt", ".md", ".json", ".csv", ".tsv", ".yaml", ".yml", ".toml", ".ini", ".log"})


def _filename(value):
    if (not isinstance(value, str) or not value or len(value.encode("utf-8")) > 200
            or value != value.strip() or any(char in value for char in '/\\:<>"|?*')
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)
            or value.startswith(".") or value.endswith((" ", "."))):
        raise ValueError("附件名称不正确，不能包含路径或隐藏文件名")
    suffix = "." + value.rsplit(".", 1)[-1].lower() if "." in value else ""
    if suffix not in TEXT_EXTENSIONS:
        raise ValueError("仅支持 TXT、Markdown、JSON、CSV、TSV、YAML、TOML、INI 和日志文本文件")
    if re.search(r"(?:^|[_. -])(?:secrets?|credentials?|cookies?|tokens?|passwords?|private[-_]?key)(?:[_. -]|$)", value, re.I):
        raise ValueError("请勿上传密钥、密码、Cookie 或其他凭据文件")
    return value


def is_text_filename(value):
    """Only an extension hint for native adapters; parsing still validates fully."""
    return isinstance(value, str) and any(value.lower().endswith(ext) for ext in TEXT_EXTENSIONS)


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("附件中没有可读取的文本")
    if (any(ord(char) < 32 and char not in "\t\n\r" or 0x7f <= ord(char) <= 0x9f for char in value)
            or any(unicodedata.category(char) == "Cs" for char in value)):
        raise ValueError("附件含有二进制或不支持的控制字符")
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise ValueError("附件文本合计不能超过 10000 字节，请精简后再发送")
    return value


def parse_text_attachment(name, content: bytes, content_type=""):
    name = _filename(name)
    if not isinstance(content, bytes) or not content or len(content) > MAX_FILE_BYTES:
        raise ValueError("单个文本附件不能超过 64 KiB")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("文本附件须使用 UTF-8 编码") from None
    return {"name": name, "text": _text(text), "content_type": "text/plain"}


def validate_attachments(values):
    """Revalidate internal submissions too; metadata never grants a file or URL capability."""
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_FILES:
        raise ValueError("一次最多发送 3 个文本附件")
    result = []
    for value in values:
        if (not isinstance(value, dict) or value.keys() - {"name", "text", "content_type"}
                or value.get("content_type", "text/plain") != "text/plain"):
            raise ValueError("文本附件格式不正确")
        result.append({"name": _filename(value.get("name")), "text": _text(value.get("text")),
                       "content_type": "text/plain"})
    if sum(len(item["text"].encode("utf-8")) for item in result) > MAX_TEXT_BYTES:
        raise ValueError("附件文本合计不能超过 10000 字节，请精简后再发送")
    return result


def normalize_attachments(values):
    """HTTP input: [{name, data: standard-base64, content_type?}], without data URLs."""
    if values is None:
        return []
    if not isinstance(values, list) or len(values) > MAX_FILES:
        raise ValueError("一次最多发送 3 个文本附件")
    result, total = [], 0
    for value in values:
        if not isinstance(value, dict) or value.keys() - {"name", "data", "content_type"}:
            raise ValueError("文本附件格式不正确")
        name = _filename(value.get("name"))
        encoded = value.get("data")
        if not isinstance(encoded, str) or not encoded or len(encoded) > ((MAX_FILE_BYTES + 2) // 3) * 4:
            raise ValueError("单个文本附件不能超过 64 KiB")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("文本附件编码不正确") from None
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ValueError("文本附件合计不能超过 128 KiB")
        result.append(parse_text_attachment(name, content, value.get("content_type", "")))
    return validate_attachments(result)
