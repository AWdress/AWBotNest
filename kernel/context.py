"""
kernel/context.py
PlatformContext —— 平台传给每个插件的「能力上下文」。

设计原则（见 SPEC.md）：
- 插件只能通过 ctx 访问平台能力，禁止直接 import pyrogram / 全局 config / 内核内部模块。
- ctx.on_message / ctx.on_callback 注册的处理器由平台自动登记句柄，停用插件时自动注销，
  这是「真热插拔」的关键。
- 每个插件拿到独立的 ctx 实例，配置、KV、日志均带本插件命名空间，互不污染。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

# 通过 core 统一出口引用 Telegram 能力（插件不直接 import pyrogram）
from core import filters as _filters
from core import logger as _root_logger
from pyrogram.handlers import MessageHandler, CallbackQueryHandler


class _PluginLoggerAdapter:
    """
    给插件日志统一加「插件名」前缀，并在日志记录上带 plugin 字段，
    让前端「运行日志」页的 source 列显示插件名。
    插件用 ctx.log.info/warning/error/debug(...)，效果：[插件id] 你的消息
    """

    def __init__(self, plugin_id: str):
        self._pid = plugin_id

    def _emit(self, level: str, msg, *args, **kwargs):
        fn = getattr(_root_logger, level)
        text = str(msg) % args if args else str(msg)
        # extra.plugin 供 log_stream 读取，前缀供文件/控制台可读
        fn(f"[{self._pid}] {text}", extra={"plugin": self._pid}, **kwargs)

    def debug(self, msg, *a, **k):   self._emit("debug", msg, *a, **k)
    def info(self, msg, *a, **k):    self._emit("info", msg, *a, **k)
    def warning(self, msg, *a, **k): self._emit("warning", msg, *a, **k)
    def error(self, msg, *a, **k):   self._emit("error", msg, *a, **k)
    def exception(self, msg, *a, **k): self._emit("error", msg, *a, **k)


def _make_plugin_logger(plugin_id: str):
    return _PluginLoggerAdapter(plugin_id)

if TYPE_CHECKING:
    from kernel.account_manager import AccountManager
    from kernel.registry import PluginRegistry


class _ClientProxy:
    """
    对单个 Pyrogram Client 的发送能力封装。
    插件用 ctx.bot / ctx.user 拿到，避免直接操作裸 Client。
    若对应账号未连接，属性为 None。
    """

    def __init__(self, client: object | None):
        self._client = client

    @property
    def raw(self) -> object | None:
        """需要高级用法时可取裸 client（不鼓励）"""
        return self._client

    @property
    def connected(self) -> bool:
        return bool(self._client and getattr(self._client, "is_connected", False))

    async def send(self, chat_id: int | str, text: str, **kwargs) -> Any:
        """发送文本消息"""
        if not self._client:
            raise RuntimeError("目标账号未连接，无法发送消息")
        return await self._client.send_message(chat_id, text, **kwargs)

    async def send_photo(self, chat_id: int | str, photo: Any, **kwargs) -> Any:
        if not self._client:
            raise RuntimeError("目标账号未连接，无法发送图片")
        return await self._client.send_photo(chat_id, photo, **kwargs)


class _KVStore:
    """
    插件专属键值存储，基于 sqlitedict，每插件独立命名空间（data/kv/<plugin_id>.sqlite）。
    用法：ctx.kv.get(key, default) / ctx.kv.set(key, value) / ctx.kv.delete(key) / ctx.kv.keys()
    """

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path

    def _open(self):
        from sqlitedict import SqliteDict
        # autocommit 保证写入即落盘；tablename 隔离命名空间
        return SqliteDict(str(self._path), tablename="kv", autocommit=True)

    def get(self, key: str, default: Any = None) -> Any:
        with self._open() as d:
            return d.get(key, default)

    def set(self, key: str, value: Any) -> None:
        with self._open() as d:
            d[key] = value

    def delete(self, key: str) -> None:
        with self._open() as d:
            if key in d:
                del d[key]

    def keys(self) -> list[str]:
        with self._open() as d:
            return list(d.keys())


class PlatformContext:
    """
    传给插件 setup/teardown 的能力上下文。

    一个插件一个实例。注册的 handler 句柄保存在 self._handles 中，
    供 PluginRuntime 在停用时统一注销。
    """

    def __init__(
        self,
        plugin_id: str,
        accounts: "AccountManager",
        registry: "PluginRegistry",
        kv_dir: Path = Path("data/kv"),
        data_root: Path = Path("data/plugin_data"),
    ):
        self.plugin_id = plugin_id
        self._accounts = accounts
        self._registry = registry
        self._data_root = data_root

        # 已注册的处理器句柄列表：(client, handler, group)
        self._handles: list[tuple[object, object, int]] = []
        # teardown 时要调用的清理回调（如取消定时任务）
        self._cleanups: list[Callable[[], Any]] = []

        # 暴露给插件的能力
        self.filters = _filters
        self.log = _make_plugin_logger(plugin_id)
        self.kv = _KVStore(kv_dir / f"{plugin_id}.sqlite")

    # ──────────────────────────────────────────────
    # 账号能力
    # ──────────────────────────────────────────────
    @property
    def bot(self) -> _ClientProxy:
        """Bot 账号发送代理"""
        return _ClientProxy(self._accounts.bot_app)

    @property
    def user(self) -> _ClientProxy:
        """主用户账号发送代理（多账号取第一个已连接）"""
        return _ClientProxy(self._accounts.primary_user_app)

    @property
    def user_apps(self) -> list[object]:
        """所有已连接用户账号（多账号场景）"""
        return self._accounts.connected_user_apps

    @property
    def owner_id(self) -> int:
        """
        平台所有者的 Telegram 数字 ID（取自主账号 ACCOUNTS[0].tgid）。
        插件不直接读 config，需要给「平台主人」推送时用它。无主账号时为 0。
        """
        try:
            import config.config as _cfg
            return int(getattr(_cfg, "MY_TGID", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0

    async def notify(self, text: str, level: str = "info", category: str | None = None,
                     account: Any = None, **kwargs) -> Any:
        """
        提交一条通知给平台通知中心。插件只管「内容 + 级别 + 分类 + 哪个账号」，
        平台统一分类（打级别标签 + 插件名 + 账号名）、套格式，再通过 Bot 发给平台主人
        （Bot 不可用时回退主账号收藏夹）。同时记入运行日志。

        level: info | success | warning | error
        category: 可选业务分类（如「订单」「签到」），显示在标签里
        account: 多账号场景下标明「这条是哪个账号的」。在 handler 里直接传 client：
                 `await ctx.notify("...", account=client)`，平台会显示该账号名。
        """
        from kernel import notifier
        meta = self._registry.get_meta(self.plugin_id)
        plugin_name = meta.name if meta else self.plugin_id
        return await notifier.submit(
            self._accounts, self.plugin_id, plugin_name, text,
            level=level, category=category, account=account, **kwargs,
        )

    # ──────────────────────────────────────────────
    # 配置
    # ──────────────────────────────────────────────
    @property
    def config(self) -> dict[str, Any]:
        """本插件当前配置（config_schema 默认值叠加用户保存值）"""
        return self._registry.get_config(self.plugin_id)

    # ──────────────────────────────────────────────
    # 可写目录（SPEC §5.5）
    # ──────────────────────────────────────────────
    @property
    def data_dir(self) -> Path:
        """
        本插件独立的可写数据目录 data/plugin_data/<id>/（Path，首次访问自动建）。
        存实际文件（头像图片池、下载素材等）用它；ctx.kv 只存键值。
        """
        d = self._data_root / self.plugin_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ──────────────────────────────────────────────
    # 处理器注册（核心：自动登记句柄以支持热卸载）
    # ──────────────────────────────────────────────
    def on_message(self, filter_=None, group: int = 0, target: str = "auto"):
        """
        注册消息处理器。装饰器用法：
            @ctx.on_message(ctx.filters.text, group=-10)
            async def handler(client, message): ...

        target: 'user' | 'bot' | 'auto'(按插件 scope 自动选择，默认 user)
        """
        def decorator(func: Callable):
            wrapped = self._track(func)
            handler = MessageHandler(wrapped, filter_)
            for client in self._resolve_targets(target):
                client.add_handler(handler, group)
                self._handles.append((client, handler, group))
            return func
        return decorator

    def on_callback(self, filter_=None, group: int = 0, target: str = "auto"):
        """注册回调查询处理器（按钮点击）"""
        def decorator(func: Callable):
            wrapped = self._track(func)
            handler = CallbackQueryHandler(wrapped, filter_)
            for client in self._resolve_targets(target):
                client.add_handler(handler, group)
                self._handles.append((client, handler, group))
            return func
        return decorator

    def _track(self, func: Callable) -> Callable:
        """包一层：进入 handler 时把「当前插件」设进 contextvar，
        使该 handler 内部的出站发送（send/reply/edit）能归属到本插件并计入活跃。
        注意：不再对「每条收到的消息」计数——只统计插件真正发出的动作。"""
        import functools

        pid = self.plugin_id

        @functools.wraps(func)
        async def wrapper(client, update, *args, **kwargs):
            from kernel import activity
            token = activity.set_current(pid)
            try:
                return await func(client, update, *args, **kwargs)
            finally:
                activity.reset_current(token)

        return wrapper

    def _resolve_targets(self, target: str) -> list[object]:
        """根据 target / 插件 scope 决定把 handler 注册到哪些 client。
        user/both 插件会按「应用账号范围」过滤用户账号（空范围=全部）。"""
        meta = self._registry.get_meta(self.plugin_id)
        scope = meta.scope if meta else "user"
        if target == "auto":
            target = scope

        clients: list[object] = []
        if target in ("user", "both"):
            user_apps = self._accounts.connected_user_apps
            scope_sessions = self._registry.get_account_scope(self.plugin_id)
            if scope_sessions:
                # 仅挂到勾选的账号（按 session 名匹配）
                user_apps = [a for a in user_apps if getattr(a, "name", None) in scope_sessions]
            clients.extend(user_apps)
        if target in ("bot", "both"):
            bot = self._accounts.bot_app
            if bot and getattr(bot, "is_connected", False):
                clients.append(bot)
        return clients

    # ──────────────────────────────────────────────
    # 定时任务
    # ──────────────────────────────────────────────
    def schedule(self, func: Callable, trigger: str = "interval", **trigger_args):
        """
        注册定时任务，停用插件时自动移除。
        例：ctx.schedule(fn, trigger="interval", seconds=60)

        job id 自动加上 "<插件id>::" 前缀，状态页据此归属到本插件并展示。
        可传 id=... 自定义后半段；不传则用函数名。冲突时自动追加 #n。
        """
        from schedulers import scheduler

        raw_id = str(trigger_args.pop("id", None) or getattr(func, "__name__", "job"))
        job_id = f"{self.plugin_id}::{raw_id}"
        if scheduler.get_job(job_id):
            n = 1
            while scheduler.get_job(f"{job_id}#{n}"):
                n += 1
            job_id = f"{job_id}#{n}"

        job = scheduler.add_job(func, trigger, id=job_id, **trigger_args)
        self._cleanups.append(lambda jid=job_id: self._safe_remove_job(jid))
        return job

    @staticmethod
    def _safe_remove_job(job_id: str) -> None:
        """移除定时任务，job 已不存在时静默忽略。"""
        from schedulers import scheduler
        try:
            scheduler.remove_job(job_id)
        except Exception:  # noqa: BLE001 - 任务可能已被移除
            pass

    def add_cleanup(self, fn: Callable[[], Any]) -> None:
        """登记一个 teardown 时调用的清理回调"""
        self._cleanups.append(fn)

    # ──────────────────────────────────────────────
    # 内部：供 PluginRuntime 调用
    # ──────────────────────────────────────────────
    def _unregister_all(self) -> None:
        """注销本插件所有处理器并执行清理回调"""
        for client, handler, group in self._handles:
            try:
                client.remove_handler(handler, group)
            except Exception as e:  # noqa: BLE001 - 卸载尽量不抛
                _root_logger.warning("注销 handler 失败 [%s]: %r", self.plugin_id, e)
        self._handles.clear()

        for fn in self._cleanups:
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                _root_logger.warning("执行清理回调失败 [%s]: %r", self.plugin_id, e)
        self._cleanups.clear()
