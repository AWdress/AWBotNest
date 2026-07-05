"""
kernel/account_manager.py
账号生命周期管理：创建、启动、停止 用户账号 与 Bot 账号 的 Pyrogram Client。

与旧 app.py 的区别：
- 不再使用 Pyrogram 的 plugins=dict(root=...) 自动发现机制。
  插件由 kernel.PluginRuntime 通过 add_handler 动态挂载，以支持热插拔。
- 只负责连接与生命周期，不含任何业务启动逻辑（发版本消息、菜单注册等已下沉为插件）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from core import API_HASH, API_ID
from libs.log import logger
from libs.custom_client import Client
from libs.session_cleaner import clean_corrupted_sessions
from core import manager
from infra.config import get_settings

config = get_settings()

# 默认 Bot 的固定 id（由 config 的 BOT_TOKEN 表示，session 文件名保持 bot_account 向后兼容）
DEFAULT_BOT_ID = "default"


def _load_bots_config() -> list[dict]:
    """读取「默认 Bot + 额外 Bot」的统一列表：[{id, name, token}]。
    默认 Bot 恒在首位（id=default，token=BOT_TOKEN）；额外 Bot 取自 config 的 BOTS。
    token 为空的额外 Bot 会被跳过。"""
    import config.config as _cfg
    _cfg.reload()
    bots: list[dict] = [{
        "id": DEFAULT_BOT_ID,
        "name": "默认 Bot",
        "token": str(getattr(_cfg, "BOT_TOKEN", "") or ""),
    }]
    for b in (getattr(_cfg, "BOTS", None) or []):
        if not isinstance(b, dict):
            continue
        bid = str(b.get("id") or "").strip()
        token = str(b.get("token") or "").strip()
        if not bid or bid == DEFAULT_BOT_ID or not token:
            continue
        bots.append({"id": bid, "name": str(b.get("name") or bid), "token": token})
    return bots


# 兼容旧 config 读取
try:
    from config.config import ACCOUNTS as _ACCOUNTS
except Exception:  # noqa: BLE001
    _ACCOUNTS = []


class AccountManager:
    """管理所有 Telegram 账号客户端的连接与生命周期。"""

    def __init__(self, workdir: str = "sessions"):
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.user_apps: list[Client] = []
        # 多 Bot：id -> Client。默认 Bot 的 id 为 DEFAULT_BOT_ID。
        self.bot_apps: dict[str, Client] = {}
        # 各 Bot 的显示名（id -> name），供通知/列表展示
        self._bot_names: dict[str, str] = {}

        # 登录流程的临时会话状态：{session_name: {client, phone, phone_code_hash}}
        self._login_sessions: dict[str, dict] = {}

        # 代理：复用 manager 的解析结果
        self.proxy = manager.proxy

    # ──────────────────────────────────────────────
    # 便捷访问
    # ──────────────────────────────────────────────
    @property
    def primary_user_app(self) -> Optional[Client]:
        """主用户账号（第一个已连接的）"""
        return next((a for a in self.user_apps if a and a.is_connected), None)

    @property
    def connected_user_apps(self) -> list[Client]:
        return [a for a in self.user_apps if a and a.is_connected]

    @property
    def bot_app(self) -> Optional[Client]:
        """默认 Bot（向后兼容旧代码：notifier / DI 容器 / manager 等仍用 bot_app）。"""
        return self.bot_apps.get(DEFAULT_BOT_ID)

    def get_bot(self, bot_id: str | None = None) -> Optional[Client]:
        """按 id 取 Bot；未指定 / 不存在 / 未连接则回退默认 Bot。"""
        if bot_id and bot_id != DEFAULT_BOT_ID:
            app = self.bot_apps.get(bot_id)
            if app is not None:
                return app
        return self.bot_apps.get(DEFAULT_BOT_ID)

    async def list_bots(self) -> list[dict]:
        """返回所有已配置 Bot 及在线状态，供前端「通知」页与推送路由用。
        性能：用已连接 client 缓存的 .me（启动时已填充），不发网络请求。"""
        result: list[dict] = []
        for b in _load_bots_config():
            bid = b["id"]
            app = self.bot_apps.get(bid)
            online = bool(app and getattr(app, "is_connected", False))
            username = None
            if online and getattr(app, "me", None):
                username = getattr(app.me, "username", None)
            result.append({
                "id": bid,
                "name": b.get("name") or bid,
                "online": online,
                "username": username,
                "is_default": bid == DEFAULT_BOT_ID,
            })
        return result


    # ──────────────────────────────────────────────
    # 客户端构建
    # ──────────────────────────────────────────────
    def _build_user_client(self, session_name: str) -> Client:
        """构建用户账号 Client（不挂载自动发现插件）"""
        return Client(
            session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            workdir=str(self.workdir.resolve()),
            proxy=self.proxy,
        )

    def _build_bot_client(self, bot_id: str, bot_token: str) -> Client:
        """构建 Bot 账号 Client。默认 Bot 的 session 沿用 bot_account（向后兼容），
        额外 Bot 用 bot_<id> 独立 session 文件。"""
        session_name = "bot_account" if bot_id == DEFAULT_BOT_ID else f"bot_{bot_id}"
        return Client(
            session_name,
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=bot_token,
            workdir=str(self.workdir.resolve()),
            proxy=self.proxy,
        )

    # ──────────────────────────────────────────────
    # 启动 / 停止
    # ──────────────────────────────────────────────
    async def start_bots(self) -> None:
        """启动所有已配置 Bot（默认 Bot + 额外 Bot），每个带重试。
        未配置凭据时直接跳过（不进重试、不打 traceback）。单个 Bot 失败不影响其它 Bot。"""
        import config.config as _cfg
        _cfg.reload()
        self.bot_apps.clear()
        self._bot_names.clear()

        if not (getattr(_cfg, "API_ID", 0) and getattr(_cfg, "API_HASH", "")):
            raise RuntimeError("未配置 Telegram 凭据（API_ID/API_HASH），请在系统设置填写后重启")

        bots = _load_bots_config()
        # 默认 Bot 无 token 时，视为「未配置 Bot」——与旧逻辑一致，交由 start_all 兜底跳过
        if not bots or not bots[0].get("token"):
            raise RuntimeError("未配置默认 Bot 的 BOT_TOKEN，请在系统设置填写后重启")

        for b in bots:
            bid, name, token = b["id"], b.get("name") or b["id"], b["token"]
            self._bot_names[bid] = name
            app = self._build_bot_client(bid, token)
            started = False
            last_error = None
            for attempt in range(1, 4):
                try:
                    await app.start()
                    logger.info("Bot [%s] 启动成功", name)
                    started = True
                    break
                except Exception as e:  # noqa: BLE001
                    last_error = e
                    logger.warning("Bot [%s] 启动失败（第 %s/3 次）: %r", name, attempt, e)
                    if attempt < 3:
                        await asyncio.sleep(2 * attempt)
            if started:
                self.bot_apps[bid] = app
            else:
                logger.error("Bot [%s] 无法启动，已跳过: %r", name, last_error)

        if DEFAULT_BOT_ID not in self.bot_apps:
            # 默认 Bot 都没起来 → 抛出，交由 start_all 记 warning（与旧行为一致）
            raise RuntimeError("默认 Bot 无法启动（检查 BOT_TOKEN / 网络 / 代理）")

    async def start_users(self) -> None:
        """启动所有用户账号（无 session 或已暂停的跳过）"""
        # 从 JSON 配置实时读取（不要用 import 期捕获的 _ACCOUNTS，登录后才会写入）
        import config.config as _cfg
        accounts = _cfg.load().get("ACCOUNTS") or []
        self.user_apps.clear()
        for acc in accounts:
            name = acc["session"] if isinstance(acc, dict) else acc
            if name:
                self.user_apps.append(self._build_user_client(name))
        if not accounts:
            logger.info("配置中无用户账号，等待前端登录")

        # 设置有效群组（PT_GROUP_ID 属业务数据，已移出平台 config；
        # 若某插件在运行时注入了它则沿用，否则为空——平台不依赖它，空则跳过不打日志）
        try:
            import config.config as _cfg
            pt_group = getattr(_cfg, "PT_GROUP_ID", {}) or {}
            valid_group_ids = list(pt_group.values())
            if valid_group_ids:
                for app in self.user_apps:
                    app.set_valid_group_ids(valid_group_ids)
                for bot in self.bot_apps.values():
                    bot.set_valid_group_ids(valid_group_ids)
        except Exception as e:  # noqa: BLE001
            logger.warning("设置有效群组失败: %r", e)

        # 暂停列表
        paused: set[str] = set()
        paused_file = self.workdir / ".paused"
        if paused_file.exists():
            paused = set(paused_file.read_text(encoding="utf-8").splitlines())

        for app in self.user_apps:
            session_file = self.workdir / f"{app.name}.session"
            if not session_file.exists():
                logger.info("user_app [%s] 无 session 文件，跳过（请通过前端/Bot 登录）", app.name)
                continue
            if app.name in paused:
                logger.info("user_app [%s] 已手动下线，跳过", app.name)
                continue
            try:
                await app.start()
                logger.info("user_app [%s] 启动成功", app.name)
            except Exception as e:  # noqa: BLE001
                logger.warning("user_app [%s] 启动失败（可能未登录）: %r", app.name, e)
                try:
                    if app.is_connected:
                        await app.stop()
                except Exception:  # noqa: BLE001
                    pass

    async def start_all(self) -> None:
        """完整启动流程：清理会话 → 启动 Bot → 启动用户账号 → 同步 manager"""
        logger.debug("清理损坏的会话文件...")
        clean_corrupted_sessions(str(self.workdir))
        await asyncio.sleep(1)  # 给上次异常退出的 SQLite 锁释放留时间

        try:
            await self.start_bots()
        except RuntimeError as e:
            logger.warning("Bot 启动跳过（可在 WebUI 配置凭据后重启）: %s", e)
        await self.start_users()

        # 同步到全局 manager，保持旧代码（services/adapters）可用
        manager.user_apps = self.user_apps
        manager.bot_app = self.bot_app

    async def stop_all(self) -> None:
        """停止所有账号连接"""
        for bot in list(self.bot_apps.values()):
            if bot and bot.is_connected:
                await bot.stop()
        for app in self.user_apps:
            if app and app.is_connected:
                await app.stop()
        logger.info("所有账号连接已关闭")

    # ──────────────────────────────────────────────
    # 账号列表 / 上下线
    # ──────────────────────────────────────────────
    async def list_accounts(self) -> list[dict]:
        """
        返回所有账号（配置中的 + 运行中的）及其状态，供前端展示。
        性能：不调 get_me()（网络往返），用已连接 client 缓存的 .me；
              配置直接 config.load() 读 JSON，不做 importlib.reload。
        """
        import config.config as _cfg
        data = _cfg.load()
        raw_accounts = data.get("ACCOUNTS") or []
        configured = [(a["session"] if isinstance(a, dict) else a) for a in raw_accounts]
        extra = [a.name for a in self.user_apps if a.name not in configured]
        names = configured + extra

        result: list[dict] = []
        for sname in names:
            app = next((a for a in self.user_apps if a.name == sname), None)
            online = bool(app and app.is_connected)
            entry = {"session": sname, "online": online, "name": sname, "tgid": None}
            acc = next(
                (a for a in raw_accounts
                 if (a["session"] if isinstance(a, dict) else a) == sname),
                None,
            )
            if isinstance(acc, dict):
                entry["name"] = acc.get("name") or sname
                entry["tgid"] = acc.get("tgid")
            # 用缓存的 me（client.me，启动时已填充），不发网络请求
            if online and getattr(app, "me", None):
                me = app.me
                entry["name"] = me.first_name or entry["name"]
                entry["tgid"] = me.id
            result.append(entry)
        return result

    async def set_online(self, session_name: str) -> bool:
        """上线一个已有 session 的账号（需已登录过，有 .session 文件）"""
        session_file = self.workdir / f"{session_name}.session"
        if not session_file.exists():
            raise FileNotFoundError(f"账号 [{session_name}] 尚未登录，请先登录")

        app = next((a for a in self.user_apps if a.name == session_name), None)
        if app and app.is_connected:
            return True
        if app is None:
            app = self._build_user_client(session_name)
            self.user_apps.append(app)
        try:
            await app.start()
        except Exception as e:  # noqa: BLE001
            # session 失效（AUTH_KEY_UNREGISTERED 等）→ 清理并提示重新登录
            self.user_apps = [a for a in self.user_apps if a.name != session_name]
            msg = str(e)
            if "AUTH_KEY_UNREGISTERED" in msg or "Unauthorized" in type(e).__name__:
                raise RuntimeError(f"账号 [{session_name}] 的登录已失效，请删除后重新登录")
            raise RuntimeError(f"账号 [{session_name}] 上线失败: {e}")
        _unpause_account(session_name, self.workdir)
        manager.user_apps = self.user_apps
        self._rebind_primary()
        logger.info("账号 [%s] 已上线", session_name)
        return True

    async def set_offline(self, session_name: str) -> bool:
        """下线一个账号（保留 session 文件，标记暂停，重启不自动上线）"""
        app = next((a for a in self.user_apps if a.name == session_name), None)
        if app and app.is_connected:
            await app.stop()
        _pause_account(session_name, self.workdir)
        manager.user_apps = self.user_apps
        logger.info("账号 [%s] 已下线", session_name)
        return True

    async def remove_account(self, session_name: str) -> bool:
        """彻底删除账号：停连接 → 删 session 文件 → 从 config.json 的 ACCOUNTS 移除。"""
        # 1) 停止并从内存移除
        app = next((a for a in self.user_apps if a.name == session_name), None)
        if app:
            if app.is_connected:
                try:
                    await app.stop()
                except Exception:  # noqa: BLE001
                    pass
            self.user_apps = [a for a in self.user_apps if a.name != session_name]
            manager.user_apps = self.user_apps

        # 2) 删 session 文件
        for suffix in (".session", ".session-journal"):
            f = self.workdir / f"{session_name}{suffix}"
            if f.exists():
                try:
                    f.unlink()
                except OSError as e:
                    logger.warning("删除 session 文件失败 [%s]: %r", f, e)

        # 3) 从 config.json 的 ACCOUNTS 移除
        try:
            import config.config as _cfg
            accounts = [
                a for a in (_cfg.load().get("ACCOUNTS") or [])
                if (a.get("session") if isinstance(a, dict) else a) != session_name
            ]
            _cfg.save({"ACCOUNTS": accounts})
        except Exception as e:  # noqa: BLE001
            logger.warning("从 config 移除账号失败 [%s]: %r", session_name, e)

        # 4) 清暂停标记
        _unpause_account(session_name, self.workdir)
        # 清理该账号在所有插件里的「应用账号范围」，避免 scope 指向死 session
        try:
            from kernel.registry import registry
            _aff = registry.purge_account(session_name)
            if _aff:
                logger.info("已从 %d 个插件的账号范围移除 [%s]", len(_aff), session_name)
        except Exception as _e:  # noqa: BLE001
            logger.warning("清理插件账号范围失败 [%s]: %r", session_name, _e)
        logger.info("账号 [%s] 已删除", session_name)
        return True

    # ──────────────────────────────────────────────
    # 登录流程（多步：手机号 → 验证码 → 可选两步密码）
    # 状态保存在 _login_sessions，键为 session_name
    # ──────────────────────────────────────────────
    async def login_send_code(self, session_name: str, phone: str) -> dict:
        """第一步：连接并发送验证码。返回 {need: 'code'} 或抛异常。"""
        phone = phone.strip().replace("＋", "+").replace(" ", "")
        if phone.startswith("00"):
            phone = "+" + phone[2:]
        if not phone.startswith("+"):
            phone = "+" + phone
        import re as _re
        if not _re.fullmatch(r"\+\d{6,15}", phone):
            raise ValueError("手机号格式无效（示例：+8615012345678）")

        # 复用已有 client 或新建（仅连接，不挂插件）
        client = next((a for a in self.user_apps if a.name == session_name), None)
        if client is None:
            client = self._build_user_client(session_name)
        if not client.is_connected:
            await client.connect()
        code_info = await client.send_code(phone)
        self._login_sessions[session_name] = {
            "client": client,
            "phone": phone,
            "phone_code_hash": code_info.phone_code_hash,
        }
        logger.info("已向 [%s] 发送验证码", phone)
        return {"need": "code"}

    async def login_submit_code(self, session_name: str, code: str) -> dict:
        """第二步：提交验证码。返回 {need: 'password'} 或 {ok: True}。"""
        sess = self._login_sessions.get(session_name)
        if not sess:
            raise RuntimeError("登录会话已过期，请重新开始")
        client = sess["client"]
        try:
            await client.sign_in(sess["phone"], sess["phone_code_hash"], code.strip())
        except Exception as e:  # noqa: BLE001
            if "SESSION_PASSWORD_NEEDED" in str(e):
                return {"need": "password"}
            raise
        return await self._login_finalize(session_name)

    async def login_submit_password(self, session_name: str, password: str) -> dict:
        """第三步（可选）：提交两步验证密码。"""
        sess = self._login_sessions.get(session_name)
        if not sess:
            raise RuntimeError("登录会话已过期，请重新开始")
        client = sess["client"]
        await client.check_password(password.strip())
        return await self._login_finalize(session_name)

    async def _login_finalize(self, session_name: str) -> dict:
        """登录成功收尾：断开临时连接 → 以正式 client 启动 → 持久化 → 重绑容器。"""
        sess = self._login_sessions.pop(session_name, None)
        if sess:
            try:
                await sess["client"].disconnect()
            except Exception:  # noqa: BLE001
                pass

        # 用正式 client 启动（session 文件已生成）
        app = next((a for a in self.user_apps if a.name == session_name), None)
        if app is None:
            app = self._build_user_client(session_name)
            self.user_apps.append(app)
        if not app.is_connected:
            await app.start()

        me = None
        try:
            me = await app.get_me()
        except Exception:  # noqa: BLE001
            pass

        if me:
            _persist_account(session_name, me.first_name or session_name, me.id)
        _unpause_account(session_name, self.workdir)
        manager.user_apps = self.user_apps
        self._rebind_primary()
        logger.info("账号 [%s] 登录并启动成功", session_name)
        return {
            "ok": True,
            "session": session_name,
            "name": me.first_name if me else session_name,
            "tgid": me.id if me else None,
        }

    def _rebind_primary(self) -> None:
        """主账号变化后重绑 DI 容器的 user_client"""
        try:
            from infra.container import rebind_user_client
            primary = self.primary_user_app
            if primary:
                rebind_user_client(primary)
        except Exception as e:  # noqa: BLE001
            logger.warning("重绑 DI 容器 user_client 失败: %r", e)


# =============================================================================
# 模块级辅助：账号持久化 / 暂停标记
# （登录流程完全在前端完成：webui /api/accounts/login/* + Accounts.vue 向导）
# =============================================================================
def importlib_reload_cfg():
    """重载 config.config 以拿到最新 ACCOUNTS"""
    import importlib
    import config.config as _cfg
    importlib.reload(_cfg)


def _persist_account(session_name: str, name: str, tgid: int) -> None:
    """登录成功后将账号写入/更新 data/config.json 的 ACCOUNTS 列表（JSON 配置）"""
    import config.config as _cfg
    try:
        accounts = list(_cfg.load().get("ACCOUNTS") or [])
        entry = {"session": session_name, "name": name, "tgid": tgid}
        # 已存在则更新，否则追加
        for i, a in enumerate(accounts):
            sname = a.get("session") if isinstance(a, dict) else a
            if sname == session_name:
                accounts[i] = entry
                break
        else:
            accounts.append(entry)
        _cfg.save({"ACCOUNTS": accounts})
        logger.info("已将账号 [%s] 写入 config.json", session_name)
    except Exception as e:  # noqa: BLE001
        logger.warning("持久化账号 [%s] 失败: %r", session_name, e)


def _paused_file(workdir: Path | str = "sessions") -> Path:
    return Path(workdir) / ".paused"


def _paused_set(workdir: Path | str = "sessions") -> set:
    f = _paused_file(workdir)
    return set(f.read_text(encoding="utf-8").splitlines()) if f.exists() else set()


def _pause_account(sname: str, workdir: Path | str = "sessions") -> None:
    s = _paused_set(workdir)
    s.add(sname)
    _paused_file(workdir).write_text("\n".join(s), encoding="utf-8")


def _unpause_account(sname: str, workdir: Path | str = "sessions") -> None:
    s = _paused_set(workdir)
    s.discard(sname)
    _paused_file(workdir).write_text("\n".join(s), encoding="utf-8")

