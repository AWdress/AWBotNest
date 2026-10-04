"""Enterprise WeChat's documented callback encryption, without SDK side effects.

Protocol: https://developer.work.weixin.qq.com/document/path/90968
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import struct
import xml.etree.ElementTree as ET
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAX_CALLBACK_BYTES = 64 * 1024
MAX_CIPHERTEXT_CHARS = 4 * ((MAX_CALLBACK_BYTES + 512 + 52 + 2) // 3)
_XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_XML_SCALAR_FIELDS = {
    "ToUserName", "FromUserName", "CreateTime", "MsgType", "AgentID", "MsgId", "Content", "Event",
    "MediaId", "PicUrl", "FileName", "FileSize", "EventKey", "ThumbMediaId", "TaskId", "ChangeType",
}
_XML_LIST_ITEMS = {
    "SelectedItems": frozenset({"SelectedItem"}),
    "OptionIds": frozenset({"OptionId"}),
    "PicList": frozenset({"item"}),
    "ApprovalNodes": frozenset({"ApprovalNode"}),
    "Items": frozenset({"Item"}),
}


class WeComCryptoError(ValueError):
    """An invalid configuration or authenticated callback envelope."""


def message_signature(token: str, timestamp: str, nonce: str, ciphertext: str) -> str:
    if any(not isinstance(value, str) for value in (token, timestamp, nonce, ciphertext)):
        raise WeComCryptoError("企业微信回调参数格式不正确")
    return hashlib.sha1("".join(sorted((token, timestamp, nonce, ciphertext))).encode("utf-8")).hexdigest()


def _validate_xml_tree(root: ET.Element) -> None:
    """Apply resource and shape limits once to the whole callback document."""
    pending = [(root, 0)]
    count = 0
    while pending:
        current, depth = pending.pop()
        count += 1
        if (depth > 32 or count > 4096 or not isinstance(current.tag, str)
                or not _XML_FIELD.fullmatch(current.tag) or current.attrib
                or (current.tail and current.tail.strip())
                or (len(current) and current.text and current.text.strip())):
            raise WeComCryptoError("企业微信回调事件字段格式不正确")
        pending.extend((child, depth + 1) for child in current)


def _callback_xml_root(content: bytes | str) -> ET.Element:
    try:
        if isinstance(content, bytes):
            if len(content) > MAX_CALLBACK_BYTES:
                raise WeComCryptoError("企业微信回调消息过大")
            text = content.decode("utf-8")
        elif isinstance(content, str):
            if len(content.encode("utf-8")) > MAX_CALLBACK_BYTES:
                raise WeComCryptoError("企业微信回调消息过大")
            text = content
        else:
            raise WeComCryptoError("企业微信回调消息格式不正确")
        if _XML_DECLARATION.search(text):
            raise WeComCryptoError("企业微信回调不接受 XML 声明实体")
        root = ET.fromstring(text)
        if root.tag != "xml" or root.attrib or (root.text and root.text.strip()):
            raise WeComCryptoError("企业微信回调 XML 格式不正确")
        _validate_xml_tree(root)
        return root
    except (ET.ParseError, UnicodeError) as exc:
        raise WeComCryptoError("企业微信回调 XML 格式不正确") from exc


def _event_fields(parent: ET.Element) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    list_items = _XML_LIST_ITEMS.get(parent.tag, frozenset())
    for node in parent:
        if len(node) and node.tag in _XML_SCALAR_FIELDS:
            raise WeComCryptoError("企业微信回调包含不合法嵌套字段")
        value = _event_fields(node) if len(node) else (node.text or "")
        if node.tag in list_items:
            fields.setdefault(node.tag, []).append(value)
        else:
            if node.tag in fields:
                raise WeComCryptoError("企业微信回调包含重复或不合法字段")
            fields[node.tag] = value
    return fields


def _callback_fields(root: ET.Element, *, allow_event_details: bool) -> dict[str, Any]:
    event_details = allow_event_details and root.findtext("MsgType") == "event"
    for node in root:
        if len(node) and (not event_details or node.tag in _XML_SCALAR_FIELDS):
            raise WeComCryptoError("企业微信回调包含不合法嵌套字段")
    fields = _event_fields(root)
    if not fields:
        raise WeComCryptoError("企业微信回调缺少消息字段")
    return fields


def parse_xml_fields(content: bytes | str, *, allow_event_details: bool = False) -> dict[str, str]:
    """Keep the existing flat envelope API, optionally validating event details."""
    fields = _callback_fields(_callback_xml_root(content), allow_event_details=allow_event_details)
    return {name: value if isinstance(value, str) else "" for name, value in fields.items()}


def parse_callback_message(content: bytes | str) -> dict[str, Any]:
    """Preserve nested event objects and protocol lists; ordinary messages stay flat."""
    return _callback_fields(_callback_xml_root(content), allow_event_details=True)


class WeComCrypto:
    def __init__(self, token: str, encoding_aes_key: str, corp_id: str) -> None:
        if (not isinstance(token, str) or not token or not token.isascii() or len(token) > 128
                or any(ord(char) < 33 or ord(char) == 127 for char in token)):
            raise WeComCryptoError("企业微信回调 Token 格式不正确")
        if not isinstance(encoding_aes_key, str) or not re.fullmatch(r"[A-Za-z0-9+/]{43}", encoding_aes_key):
            raise WeComCryptoError("企业微信回调 AESKey 必须为 43 位 Base64 字符串")
        if not isinstance(corp_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", corp_id):
            raise WeComCryptoError("企业微信企业 ID 格式不正确")
        try:
            key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WeComCryptoError("企业微信回调 AESKey 格式不正确") from exc
        # Officially generated 43-character keys may have nonzero Base64 pad
        # bits (including the published protocol example). Match the official
        # SDK's decode behavior rather than requiring a canonical re-encoding.
        if len(key) != 32:
            raise WeComCryptoError("企业微信回调 AESKey 格式不正确")
        self.token = token
        self._key = key
        self._corp_id = corp_id.encode("utf-8")

    def verify_signature(self, signature: str, timestamp: str, nonce: str, ciphertext: str) -> None:
        if (not isinstance(signature, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", signature)
                or not isinstance(ciphertext, str) or len(ciphertext) > MAX_CIPHERTEXT_CHARS
                or not isinstance(timestamp, str) or not re.fullmatch(r"[0-9]{1,12}", timestamp)
                or not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", nonce)):
            raise WeComCryptoError("企业微信回调签名不正确")
        expected = message_signature(self.token, timestamp, nonce, ciphertext)
        if not hmac.compare_digest(expected, signature.lower()):
            raise WeComCryptoError("企业微信回调签名不正确")

    def encrypt(self, payload: bytes | str) -> str:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, bytes) or len(payload) > MAX_CALLBACK_BYTES:
            raise WeComCryptoError("企业微信回调消息格式或大小不正确")
        plain = secrets.token_bytes(16) + struct.pack("!I", len(payload)) + payload + self._corp_id
        padding = 32 - len(plain) % 32
        plain += bytes((padding,)) * padding
        worker = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).encryptor()
        return base64.b64encode(worker.update(plain) + worker.finalize()).decode("ascii")

    def decrypt(self, ciphertext: str) -> bytes:
        if (not isinstance(ciphertext, str) or not ciphertext
                or len(ciphertext) > MAX_CIPHERTEXT_CHARS):
            raise WeComCryptoError("企业微信回调密文格式不正确")
        try:
            encrypted = base64.b64decode(ciphertext, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WeComCryptoError("企业微信回调密文格式不正确") from exc
        if (not encrypted or len(encrypted) % 32
                or base64.b64encode(encrypted).decode("ascii") != ciphertext):
            raise WeComCryptoError("企业微信回调密文格式不正确")
        worker = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).decryptor()
        plain = worker.update(encrypted) + worker.finalize()
        padding = plain[-1]
        if not 1 <= padding <= 32 or not hmac.compare_digest(plain[-padding:], bytes((padding,)) * padding):
            raise WeComCryptoError("企业微信回调密文校验失败")
        plain = plain[:-padding]
        if len(plain) < 20:
            raise WeComCryptoError("企业微信回调密文校验失败")
        length = struct.unpack("!I", plain[16:20])[0]
        if length > MAX_CALLBACK_BYTES or 20 + length > len(plain):
            raise WeComCryptoError("企业微信回调消息长度不正确")
        receiver = plain[20 + length:]
        if not hmac.compare_digest(receiver, self._corp_id):
            raise WeComCryptoError("企业微信回调企业身份不匹配")
        return plain[20:20 + length]
