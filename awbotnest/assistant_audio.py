"""In-memory, bounded native audio input; no paths, URLs, codecs, or executables."""
from __future__ import annotations

import base64
import binascii
import unicodedata

MAX_AUDIO_BYTES = 8 * 1024 * 1024
MIME_BY_EXTENSION = {
    ".wav": "audio/wav", ".wave": "audio/wav", ".mp3": "audio/mpeg",
    ".mpga": "audio/mpeg", ".mpeg": "audio/mpeg", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
    ".webm": "audio/webm", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".amr": "audio/amr", ".aac": "audio/aac",
}


def is_audio_filename(value):
    return isinstance(value, str) and any(value.lower().endswith(ext) for ext in MIME_BY_EXTENSION)


def parse_audio(name, content: bytes, content_type=""):
    if (not isinstance(name, str) or not name or name != name.strip() or len(name.encode("utf-8")) > 200
            or any(char in name for char in '/\\:<>"|?*') or name.startswith(".") or name.endswith((".", " "))
            or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in name)):
        raise ValueError("语音名称不正确，不能包含路径或隐藏文件名")
    extension = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in MIME_BY_EXTENSION:
        raise ValueError("请选择 WAV、MP3、M4A、MP4、WebM、Ogg、AAC 或 AMR 语音文件")
    if not isinstance(content, bytes) or not content or len(content) > MAX_AUDIO_BYTES:
        raise ValueError("单条语音不能超过 8 MiB")
    if extension in {".wav", ".wave"}:
        valid = len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WAVE"
    elif extension in {".m4a", ".mp4"}:
        valid = len(content) >= 12 and content[4:8] == b"ftyp"
    elif extension in {".ogg", ".oga", ".opus"}:
        valid = content.startswith(b"OggS")
    elif extension == ".amr":
        valid = content.startswith((b"#!AMR\n", b"#!AMR-WB\n"))
    elif extension == ".webm":
        valid = content.startswith(b"\x1aE\xdf\xa3")
    elif extension == ".aac":
        valid = len(content) > 2 and content[0] == 0xff and content[1] & 0xf6 == 0xf0
    else:
        valid = (content.startswith(b"ID3") or len(content) > 2 and content[0] == 0xff
                 and content[1] & 0xe0 == 0xe0 and content[1] & 0x06 != 0)
    if not valid:
        raise ValueError("语音内容与文件格式不符，请重新导出音频后发送")
    return {"name": name, "data": content, "content_type": MIME_BY_EXTENSION[extension]}


def validate_audio(value):
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() - {"name", "data", "content_type"}:
        raise ValueError("一次只能发送一条语音")
    return parse_audio(value.get("name"), value.get("data"), value.get("content_type", ""))


def normalize_audio(value):
    """HTTP object with standard-base64 data; native adapters call parse_audio instead."""
    if value is None:
        return None
    if not isinstance(value, dict) or value.keys() - {"name", "data", "content_type"}:
        raise ValueError("一次只能发送一条语音")
    encoded = value.get("data")
    if not isinstance(encoded, str) or not encoded or len(encoded) > ((MAX_AUDIO_BYTES + 2) // 3) * 4:
        raise ValueError("单条语音不能超过 8 MiB")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("语音编码不正确") from None
    return parse_audio(value.get("name"), content, value.get("content_type", ""))
