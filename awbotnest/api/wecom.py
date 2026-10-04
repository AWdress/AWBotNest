"""Authenticated callbacks for self-built Enterprise WeChat applications."""
from __future__ import annotations

import re
import secrets
import time
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, Response

from ..wecom_config import callback_member_allowed, channel_config, validate_callback_config
from ..wecom_crypto import MAX_CALLBACK_BYTES, WeComCrypto, WeComCryptoError, message_signature, parse_xml_fields


_MEMBER_ID = re.compile(r"[A-Za-z0-9_.@-]{1,64}")
MAX_REPLY_BYTES = 2048
CALLBACK_TIME_WINDOW = 300


def callback_channel(settings, channel_id: str) -> dict:
    """Use the same flat-over-nested channel convention as notification sending."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", channel_id):
        raise HTTPException(status_code=404, detail="回调渠道不存在")
    config = channel_config(settings, channel_id)
    if config is None:
        raise HTTPException(status_code=404, detail="回调渠道不存在")
    if (config.get("type") not in {"wecom", "wechat"} or config.get("enabled", True) is not True
            or config.get("callback_enabled") is not True
            or config.get("url") or config.get("webhook")):
        raise HTTPException(status_code=404, detail="回调渠道未启用")
    try:
        validate_callback_config(config)
        config = dict(config)
        for key in ("corpid", "agentid", "secret", "callback_token", "callback_aes_key", "callback_users"):
            config[key] = str(config[key]).strip()
        config["agentid"] = str(int(config["agentid"]))
        WeComCrypto(config["callback_token"], config["callback_aes_key"], config["corpid"])
    except (ValueError, KeyError):
        raise HTTPException(status_code=404, detail="回调渠道配置不完整") from None
    return config


def _parameters(request: Request, *, challenge: bool = False) -> tuple[str, str, str, str]:
    keys = ("msg_signature", "timestamp", "nonce", "echostr") if challenge else ("msg_signature", "timestamp", "nonce")
    values = {}
    for key in keys:
        items = request.query_params.getlist(key)
        if len(items) != 1 or not items[0]:
            raise HTTPException(status_code=400, detail="回调参数不完整")
        values[key] = items[0]
    signature, timestamp, nonce = values["msg_signature"], values["timestamp"], values["nonce"]
    if (not re.fullmatch(r"[0-9a-fA-F]{40}", signature)
            or not re.fullmatch(r"[0-9]{1,12}", timestamp)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", nonce)):
        raise HTTPException(status_code=400, detail="回调参数格式不正确")
    if abs(time.time() - int(timestamp)) > CALLBACK_TIME_WINDOW:
        raise HTTPException(status_code=403, detail="回调已过期")
    return signature, timestamp, nonce, values.get("echostr", "")


async def _body(request: Request) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,12}", length):
            raise HTTPException(status_code=400, detail="回调请求大小不正确")
        if int(length) > MAX_CALLBACK_BYTES:
            raise HTTPException(status_code=413, detail="回调消息过大")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_CALLBACK_BYTES:
            raise HTTPException(status_code=413, detail="回调消息过大")
        body.extend(chunk)
    if not body:
        raise HTTPException(status_code=400, detail="回调消息为空")
    return bytes(body)


def _matches_application(fields: dict[str, str], config: dict, *, required: bool,
                         require_agent: bool = True) -> None:
    recipient, agent = fields.get("ToUserName"), fields.get("AgentID")
    if ((required and (recipient is None or (require_agent and agent is None)))
            or (recipient is not None and recipient != config["corpid"])
            or (agent is not None and agent != config["agentid"])):
        raise HTTPException(status_code=403, detail="回调应用身份不匹配")


def _reply_xml(config: dict, sender: str, text: str) -> bytes:
    text = "".join(char for char in text if char in "\t\n\r" or 0x20 <= ord(char) <= 0xD7FF
                   or 0xE000 <= ord(char) <= 0xFFFD or 0x10000 <= ord(char) <= 0x10FFFF)
    raw = text.encode("utf-8")
    if len(raw) > MAX_REPLY_BYTES:
        text = raw[:MAX_REPLY_BYTES - len("\n…".encode("utf-8"))].decode("utf-8", errors="ignore") + "\n…"
    root = ET.Element("xml")
    for key, value in {"ToUserName": sender, "FromUserName": config["corpid"],
                       "CreateTime": str(int(time.time())), "MsgType": "text", "Content": text}.items():
        ET.SubElement(root, key).text = value
    return ET.tostring(root, encoding="utf-8")


def create_router(deps) -> APIRouter:
    router = APIRouter(tags=["企业微信回调"])

    @router.get("/api/wecom/callback/{channel_id}", include_in_schema=False)
    async def verify_callback(channel_id: str, request: Request):
        config = callback_channel(deps.settings, channel_id)
        signature, timestamp, nonce, ciphertext = _parameters(request, challenge=True)
        crypto = WeComCrypto(config["callback_token"], config["callback_aes_key"], config["corpid"])
        try:
            crypto.verify_signature(signature, timestamp, nonce, ciphertext)
            challenge = crypto.decrypt(ciphertext).decode("utf-8")
        except (WeComCryptoError, UnicodeError):
            raise HTTPException(status_code=403, detail="回调验证失败") from None
        return PlainTextResponse(challenge, headers={"Cache-Control": "no-store"})

    @router.post("/api/wecom/callback/{channel_id}", include_in_schema=False)
    async def receive_callback(channel_id: str, request: Request):
        config = callback_channel(deps.settings, channel_id)
        signature, timestamp, nonce, _ = _parameters(request)
        try:
            envelope = parse_xml_fields(await _body(request))
        except WeComCryptoError:
            raise HTTPException(status_code=400, detail="回调消息格式不正确") from None
        _matches_application(envelope, config, required=False)
        ciphertext = envelope.get("Encrypt")
        if not ciphertext:
            raise HTTPException(status_code=400, detail="回调缺少加密消息")
        crypto = WeComCrypto(config["callback_token"], config["callback_aes_key"], config["corpid"])
        try:
            crypto.verify_signature(signature, timestamp, nonce, ciphertext)
            message = parse_xml_fields(crypto.decrypt(ciphertext), allow_event_details=True)
        except WeComCryptoError:
            raise HTTPException(status_code=403, detail="回调验证失败") from None
        # Some official non-text events omit AgentID. They can only be ignored,
        # never dispatched; text commands always require the application ID.
        _matches_application(message, config, required=True,
                             require_agent=message.get("MsgType") == "text")
        sender = message.get("FromUserName", "")
        if not _MEMBER_ID.fullmatch(sender):
            raise HTTPException(status_code=400, detail="回调发送成员格式不正确")
        if not message.get("MsgType") or not re.fullmatch(r"[0-9]{1,12}", message.get("CreateTime", "")):
            raise HTTPException(status_code=400, detail="回调消息字段不完整")
        if message.get("MsgType") != "text":
            return PlainTextResponse("success", headers={"Cache-Control": "no-store"})
        if not callback_member_allowed(config, sender):
            raise HTTPException(status_code=403, detail="回调成员未获授权")
        if ("Content" not in message or not re.fullmatch(r"[0-9]{1,20}", message.get("MsgId", ""))
                or not re.fullmatch(r"[0-9]{1,12}", message.get("CreateTime", ""))):
            raise HTTPException(status_code=400, detail="回调文字消息格式不正确")
        try:
            reply = await request.app.state.wecom_commands.handle(channel_id, message)
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=503, detail="回调服务暂不可用") from None
        if not reply:
            return PlainTextResponse("success", headers={"Cache-Control": "no-store"})
        if not isinstance(reply, str):
            raise HTTPException(status_code=503, detail="回调服务暂不可用")
        encrypted = crypto.encrypt(_reply_xml(config, sender, reply))
        reply_time, reply_nonce = str(int(time.time())), secrets.token_hex(16)
        root = ET.Element("xml")
        for key, value in {"Encrypt": encrypted,
                           "MsgSignature": message_signature(config["callback_token"], reply_time, reply_nonce, encrypted),
                           "TimeStamp": reply_time, "Nonce": reply_nonce}.items():
            ET.SubElement(root, key).text = value
        return Response(ET.tostring(root, encoding="utf-8"), media_type="application/xml",
                        headers={"Cache-Control": "no-store"})

    return router


def register_wecom_callback(app, deps) -> None:
    from ..wecom_commands import WeComCommandService

    app.state.wecom_commands = WeComCommandService(deps.settings, deps.runtime, deps.routes)
    app.include_router(create_router(deps))
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def callback_lifespan(asgi_app):
        try:
            async with previous_lifespan(asgi_app) as state:
                yield state
        finally:
            await asgi_app.state.wecom_commands.close()

    app.router.lifespan_context = callback_lifespan
