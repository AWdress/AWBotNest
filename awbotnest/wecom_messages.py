from __future__ import annotations

import copy
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


MAX_WECOM_MEDIA_BYTES = 20 * 1024 * 1024
WECOM_MESSAGE_TYPES = frozenset({
    "text", "image", "voice", "video", "shortvideo", "location", "link", "file", "event",
})
WECOM_MEDIA_TYPES = frozenset({"image", "file", "voice", "video", "shortvideo"})


def valid_wecom_media_id(value: Any) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 1024 and value.isascii()
            and all(33 <= ord(char) < 127 for char in value)
            and "://" not in value and not value.startswith("//"))


@dataclass(frozen=True, slots=True)
class WeComMedia:
    """已鉴权下载的企业微信媒体；内容与文件名不写入对象摘要。"""

    content: bytes = field(repr=False)
    filename: str = field(default="", repr=False)
    content_type: str = field(default="application/octet-stream", repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("企业微信媒体内容必须是 bytes")


@dataclass(frozen=True, slots=True)
class WeComMessage:
    """只承载已通过鉴权和插件权限检查的企业微信消息。"""

    channel_id: str = field(default="", repr=False)
    user_id: str = field(default="", repr=False)
    corp_id: str = field(default="", repr=False)
    agent_id: str = field(default="", repr=False)
    message_id: str = field(default="", repr=False)
    create_time: int = field(default=0, repr=False)
    message_type: str = field(default="", repr=False)
    text: str = field(default="", repr=False)
    media_id: str = field(default="", repr=False)
    pic_url: str = field(default="", repr=False)
    file_name: str = field(default="", repr=False)
    file_size: int | None = field(default=None, repr=False)
    event: str = field(default="", repr=False)
    event_key: str = field(default="", repr=False)
    fields: dict[str, Any] = field(default_factory=dict, repr=False)
    _reply: Callable[[str], Awaitable[Any] | Any] | None = field(
        default=None, repr=False, compare=False,
    )
    _download: Callable[[int], Awaitable[WeComMedia] | WeComMedia] | None = field(
        default=None, repr=False, compare=False,
    )

    def __post_init__(self) -> None:
        # 插件可读取嵌套 XML 字段，但修改自己的副本不能影响鉴权或其他插件。
        if not isinstance(self.fields, dict):
            raise TypeError("企业微信消息 fields 必须是字典")
        object.__setattr__(self, "fields", copy.deepcopy(self.fields))

    async def reply(self, text: str) -> Any:
        if not isinstance(text, str):
            raise TypeError("企业微信回复内容必须是字符串")
        if self._reply is None:
            raise RuntimeError("当前企业微信消息不可回复")
        value = self._reply(text)
        return await value if inspect.isawaitable(value) else value

    async def download_media(self, max_bytes: int = MAX_WECOM_MEDIA_BYTES) -> WeComMedia:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int):
            raise TypeError("企业微信媒体大小限制必须是整数")
        if not 0 < max_bytes <= MAX_WECOM_MEDIA_BYTES:
            raise ValueError("企业微信媒体大小限制必须在 1 字节到 20 MiB 之间")
        if not self.media_id or self._download is None:
            raise RuntimeError("当前企业微信消息没有可下载的媒体")
        value = self._download(max_bytes)
        media = await value if inspect.isawaitable(value) else value
        if not isinstance(media, WeComMedia):
            raise TypeError("企业微信媒体下载必须返回 WeComMedia")
        if len(media.content) > max_bytes:
            raise ValueError("企业微信媒体超过大小限制")
        return media
