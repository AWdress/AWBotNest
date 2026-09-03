from __future__ import annotations

import asyncio
import logging
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.errors import PasswordHashInvalidError, PhoneCodeInvalidError, SessionPasswordNeededError

from .config import DATA_DIR, SESSIONS_DIR, Settings

logger = logging.getLogger("awbotnest.telegram")


@dataclass(slots=True)
class ClientState:
    id: str
    kind: str
    connected: bool
    username: str | None = None
    display_name: str | None = None
    user_id: int | None = None
    premium: bool = False
    avatar: str | None = None


class TelegramAccounts:
    """Telethon 客户端生命周期；无应用凭据时保持独立运行模式。"""

    def __init__(self, settings: Settings, sessions_dir: Path = SESSIONS_DIR) -> None:
        self.settings = settings
        self.sessions_dir = sessions_dir
        self.bots: dict[str, TelegramClient] = {}
        self.users: dict[str, TelegramClient] = {}
        self._pending_logins: dict[str, tuple[TelegramClient, str, object, float]] = {}
        self._lock = asyncio.Lock()
        self._profiles_path = DATA_DIR / "account_profiles.json"

    def _profiles(self) -> dict[str, dict[str, object]]:
        try:
            value = json.loads(self._profiles_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def _cache_profile(self, key: str, client: TelegramClient) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError("账号标识不合法")
        me = await client.get_me()
        first = str(getattr(me, "first_name", "") or "").strip()
        last = str(getattr(me, "last_name", "") or "").strip()
        avatar_dir = DATA_DIR / "avatars"
        avatar_dir.mkdir(parents=True, exist_ok=True)
        avatar_file = avatar_dir / f"{key}.jpg"
        try:
            downloaded = await client.download_profile_photo(me, file=str(avatar_file))
            avatar = f"/api/avatars/{key}.jpg" if downloaded else None
        except Exception:
            avatar = f"/api/avatars/{key}.jpg" if avatar_file.exists() else None
        profile = {
            "username": getattr(me, "username", None),
            "display_name": " ".join(item for item in (first, last) if item) or getattr(me, "username", None),
            "user_id": getattr(me, "id", None),
            "premium": bool(getattr(me, "premium", False)),
            "avatar": avatar,
        }
        profiles = self._profiles()
        profiles[key] = profile
        self._profiles_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._profiles_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._profiles_path)
        return profile

    @property
    def telegram_available(self) -> bool:
        return self.settings.telegram_configured

    def _client(self, session: str) -> TelegramClient:
        kwargs = {}
        if self.settings.proxy_url:
            value = urlparse(self.settings.proxy_url)
            if value.scheme not in {"http", "socks4", "socks5"} or not value.hostname or not value.port:
                raise ValueError("代理地址必须是 http/socks4/socks5 URL")
            kwargs["proxy"] = (value.scheme, value.hostname, value.port, True,
                               value.username, value.password)
        return TelegramClient(session, self.settings.api_id, self.settings.api_hash, **kwargs)

    @property
    def connected_users(self) -> list[TelegramClient]:
        return [client for client in self.users.values() if client.is_connected()]

    @property
    def bot(self) -> TelegramClient | None:
        selected = self.bots.get(self.settings.default_bot_id)
        if selected and selected.is_connected():
            return selected
        return next((client for client in self.bots.values() if client.is_connected()), None)

    async def start(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        if not self.telegram_available:
            logger.info("未配置 Telegram API_ID/API_HASH，平台以独立模式启动")
            return
        async with self._lock:
            for bot_spec in self.settings.bot_specs():
                if not bot_spec.token:
                    continue
                if not re.fullmatch(r"[A-Za-z0-9_-]+", bot_spec.id):
                    logger.error("Bot ID 不合法，已跳过：%s", bot_spec.id)
                    continue
                bot = self._client(str(self.sessions_dir / f"bot_{bot_spec.id}"))
                try:
                    await bot.start(bot_token=bot_spec.token)
                    self.bots[bot_spec.id] = bot
                    try:
                        await self._cache_profile(f"bot_{bot_spec.id}", bot)
                    except Exception:
                        logger.warning("Bot [%s] 资料缓存失败", bot_spec.name, exc_info=True)
                    logger.info("Bot [%s] 启动成功", bot_spec.name)
                except asyncio.CancelledError:
                    await bot.disconnect()
                    raise
                except Exception:
                    await bot.disconnect()
                    logger.exception("Telethon Bot [%s] 连接失败，平台继续启动", bot_spec.name)

            for session_name in self.settings.user_sessions:
                await self._start_user(session_name)

    async def _start_user(self, session_name: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_]+", session_name or "") or session_name in self.users:
            return
        session_path = self.sessions_dir / session_name
        if not session_path.with_suffix(".session").exists():
            logger.info("用户会话 %s 尚未登录，已跳过", session_name)
            return
        client = self._client(str(session_path))
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                logger.warning("用户会话 %s 已失效，已跳过", session_name)
                return
            self.users[session_name] = client
            try:
                await self._cache_profile(session_name, client)
            except Exception:
                logger.warning("用户账号 %s 资料缓存失败", session_name, exc_info=True)
            logger.info("用户账号 [%s] 启动成功", session_name)
        except asyncio.CancelledError:
            await client.disconnect()
            raise
        except Exception:
            await client.disconnect()
            logger.exception("用户账号 %s 连接失败", session_name)

    async def begin_user_login(self, session_name: str, phone: str) -> dict[str, object]:
        """发送登录验证码；客户端保留在内存中等待下一步。"""
        if not self.telegram_available:
            raise RuntimeError("请先配置 Telegram API_ID/API_HASH")
        session_name = session_name.strip()
        phone = phone.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+", session_name):
            raise ValueError("会话名称只能包含字母、数字和下划线")
        if not phone:
            raise ValueError("手机号不能为空")
        if session_name in self.users:
            raise ValueError("该会话名称已在使用")
        for name, pending in list(self._pending_logins.items()):
            if time.monotonic() - pending[3] > 600:
                await self.cancel_user_login(name)
        await self.cancel_user_login(session_name)
        client = self._client(str(self.sessions_dir / session_name))
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
        except Exception:
            await client.disconnect()
            raise
        self._pending_logins[session_name] = (client, phone, sent.phone_code_hash, time.monotonic())
        return {"session": session_name, "code_type": type(sent.type).__name__}

    async def complete_user_login(
        self,
        session_name: str,
        code: str = "",
        password: str = "",
    ) -> dict[str, object]:
        pending = self._pending_logins.get(session_name)
        if pending is None:
            raise LookupError("登录会话不存在或已过期，请重新发送验证码")
        client, phone, code_hash, started = pending
        if time.monotonic() - started > 600:
            await self.cancel_user_login(session_name)
            raise LookupError("登录会话已超过 10 分钟，请重新发送验证码")
        try:
            if password:
                await client.sign_in(password=password)
            else:
                await client.sign_in(phone=phone, code=code.strip(), phone_code_hash=code_hash)
        except SessionPasswordNeededError:
            return {"session": session_name, "needs_password": True, "authorized": False}
        except PasswordHashInvalidError:
            return {"session": session_name, "needs_password": True, "authorized": False,
                    "error": "两步验证密码错误"}
        except PhoneCodeInvalidError:
            return {"session": session_name, "needs_password": False, "authorized": False,
                    "error": "验证码错误"}
        except Exception:
            await self.cancel_user_login(session_name)
            raise
        self._pending_logins.pop(session_name, None)
        self.users[session_name] = client
        me = await client.get_me()
        try:
            await self._cache_profile(session_name, client)
        except Exception:
            logger.warning("用户账号 %s 资料缓存失败", session_name, exc_info=True)
        return {
            "ok": True,
            "session": session_name,
            "needs_password": False,
            "authorized": True,
            "user_id": getattr(me, "id", None),
            "username": getattr(me, "username", None),
        }

    async def cancel_user_login(self, session_name: str) -> None:
        pending = self._pending_logins.pop(session_name, None)
        if pending is not None:
            await pending[0].disconnect()

    async def disconnect_user(self, session_name: str) -> bool:
        client = self.users.pop(session_name, None)
        if client is None:
            return False
        await client.disconnect()
        return True

    async def connect_user(self, session_name: str) -> bool:
        if session_name not in self.settings.user_sessions:
            return False
        await self._start_user(session_name)
        client = self.users.get(session_name)
        return bool(client and client.is_connected())

    async def refresh_profile(self, kind: str, account_id: str) -> bool:
        client = self.bots.get(account_id) if kind == "bot" else self.users.get(account_id)
        if client is None or not client.is_connected():
            return False
        key = f"bot_{account_id}" if kind == "bot" else account_id
        await self._cache_profile(key, client)
        return True

    async def delete_user(self, session_name: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9_]+", session_name or ""):
            raise ValueError("会话名称不合法")
        await self.cancel_user_login(session_name)
        await self.disconnect_user(session_name)
        removed = False
        for suffix in (".session", ".session-journal"):
            path = self.sessions_dir / f"{session_name}{suffix}"
            if path.exists():
                path.unlink()
                removed = True
        if session_name in self.settings.user_sessions:
            self.settings.user_sessions.remove(session_name)
            from .config import save_settings
            save_settings(self.settings)
            removed = True
        profiles = self._profiles()
        if profiles.pop(session_name, None) is not None:
            temporary = self._profiles_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self._profiles_path)
            removed = True
        avatar = DATA_DIR / "avatars" / f"{session_name}.jpg"
        if avatar.exists():
            avatar.unlink()
            removed = True
        return removed

    def clients_for_scope(self, scope: str, bot_id: str = "") -> list[TelegramClient]:
        clients: list[TelegramClient] = []
        selected_bot = self.choose_bot(bot_id)
        if scope in {"bot", "both"} and selected_bot and selected_bot.is_connected():
            clients.append(selected_bot)
        if scope in {"user", "both"}:
            clients.extend(self.connected_users)
        return clients

    def choose_bot(self, bot_id: str = "") -> TelegramClient | None:
        if bot_id:
            client = self.bots.get(bot_id)
            return client if client and client.is_connected() else None
        return self.bot

    async def states(self) -> list[ClientState]:
        result: list[ClientState] = []
        profiles = self._profiles()
        def fields(profile: dict[str, object]) -> dict[str, object]:
            return {key: profile[key] for key in ("username", "display_name", "user_id", "premium", "avatar")
                    if key in profile}
        for spec in self.settings.bot_specs():
            if not spec.token:
                continue
            bot_id = spec.id
            client = self.bots.get(bot_id)
            profile = profiles.get(f"bot_{bot_id}", {})
            result.append(ClientState(bot_id, "bot", bool(client and client.is_connected()), **fields(profile)))
        for name in self.settings.user_sessions:
            client = self.users.get(name)
            profile = profiles.get(name, {})
            result.append(ClientState(name, "user", bool(client and client.is_connected()), **fields(profile)))
        return result

    async def stop(self) -> None:
        async with self._lock:
            clients = [
                *self.users.values(),
                *(pending[0] for pending in self._pending_logins.values()),
                *self.bots.values(),
            ]
            self.users.clear()
            self._pending_logins.clear()
            self.bots.clear()
            if clients:
                await asyncio.gather(*(client.disconnect() for client in clients), return_exceptions=True)
