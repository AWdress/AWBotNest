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

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


MAX_CALLBACK_BYTES = 64 * 1024
MAX_CIPHERTEXT_CHARS = 4 * ((MAX_CALLBACK_BYTES + 512 + 52 + 2) // 3)
_XML_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_XML_SCALAR_FIELDS = {"ToUserName", "FromUserName", "CreateTime", "MsgType", "AgentID", "MsgId", "Content", "Event"}


class WeComCryptoError(ValueError):
    """An invalid configuration or authenticated callback envelope."""


def message_signature(token: str, timestamp: str, nonce: str, ciphertext: str) -> str:
    if any(not isinstance(value, str) for value in (token, timestamp, nonce, ciphertext)):
        raise WeComCryptoError("企业微信回调参数格式不正确")
    return hashlib.sha1("".join(sorted((token, timestamp, nonce, ciphertext))).encode("utf-8")).hexdigest()


def _validate_event_details(node: ET.Element) -> None:
    """Validate ignored event trees; repeated list items are part of the protocol."""
    pending = [(node, 1)]
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


def parse_xml_fields(content: bytes | str, *, allow_event_details: bool = False) -> dict[str, str]:
    """Keep command/envelope fields flat, optionally ignoring valid event trees."""
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
        event_details = allow_event_details and root.findtext("MsgType") == "event"
        fields: dict[str, str] = {}
        for node in root:
            if (not isinstance(node.tag, str) or not _XML_FIELD.fullmatch(node.tag)
                    or node.tag in fields or node.attrib
                    or (node.tail and node.tail.strip())):
                raise WeComCryptoError("企业微信回调包含重复或不合法字段")
            if len(node):
                if not event_details or node.tag in _XML_SCALAR_FIELDS:
                    raise WeComCryptoError("企业微信回调包含不合法嵌套字段")
                _validate_event_details(node)
                fields[node.tag] = ""
            else:
                fields[node.tag] = node.text or ""
        if not fields:
            raise WeComCryptoError("企业微信回调缺少消息字段")
        return fields
    except (ET.ParseError, UnicodeError) as exc:
        raise WeComCryptoError("企业微信回调 XML 格式不正确") from exc


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
