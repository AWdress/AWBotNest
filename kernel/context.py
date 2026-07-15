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
from pyrogram.handlers import MessageHandler, EditedMessageHandler, CallbackQueryHandler
from pyrogram import StopPropagation, ContinuePropagation


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


class WebhookRequest:
    """
    传给插件 webhook 处理器的入站请求封装（插件不直接接触 fastapi/starlette）。

    属性：
      method  —— HTTP 方法（GET/POST…）
      query   —— 查询串参数 dict（已剔除鉴权用的 apikey）
      headers —— 请求头 dict（键小写）
      body    —— 原始请求体 bytes
      text    —— 请求体按 UTF-8 解码的字符串（解码失败为 ""）
      json    —— 请求体解析出的 JSON（非 JSON 或解析失败为 None）
    """

    def __init__(self, method: str, query: dict, headers: dict,
                 body: bytes, json_data: Any = None, path: str = ""):
        self.method = method
        self.query = query
        self.headers = headers
        self.body = body
        self.json = json_data
        # 插件 API（ctx.on_api）用：命中的相对路径（webhook 场景为空串）
        self.path = path

    @property
    def text(self) -> str:
        try:
            return self.body.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            return ""


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
        group_base: int = 0,
    ):
        self.plugin_id = plugin_id
        self._accounts = accounts
        self._registry = registry
        self._data_root = data_root
        # 平台分配给本插件的 group 基址。插件写的相对 group 会平移到此基址上，
        # 使不同插件落在各自独立的 group 区间，互不"吃消息"（见 on_message 说明）。
        self._group_base = group_base

        # 已注册的处理器句柄列表：(client, handler, group)
        self._handles: list[tuple[object, object, int]] = []
        # teardown 时要调用的清理回调（如取消定时任务）
        self._cleanups: list[Callable[[], Any]] = []
        # webhook 处理器（ctx.on_webhook 注册；一个插件一个）。热卸载/重载时清空。
        self._webhook_handler: Optional[Callable] = None
        # 动作处理器（ctx.action 注册；name -> 处理函数）。供配置表单里的「动作按钮」触发。
        self._action_handlers: dict[str, Callable] = {}
        # 插件 API 处理器（ctx.on_api 注册；(METHOD, 相对路径) -> 处理函数）。
        # 供 vue 模式自带前端组件调用；经管理员登录态鉴权，热卸载/重载时清空。
        self._api_handlers: dict[tuple[str, str], Callable] = {}

        # 暴露给插件的能力
        self.filters = _filters
        self.log = _make_plugin_logger(plugin_id)
        self.kv = _KVStore(kv_dir / f"{plugin_id}.sqlite")
        # 主动中断消息传播的信号：handler 内 `raise ctx.StopPropagation` 可阻止
        # 后续（更大 group）的其它插件/handler 再处理这条消息。
        self.StopPropagation = StopPropagation
        self.ContinuePropagation = ContinuePropagation

    # ──────────────────────────────────────────────
    # 账号能力
    # ──────────────────────────────────────────────
    @property
    def bot(self) -> _ClientProxy:
        """Bot 账号发送代理。返回本插件在「系统设置 → 通知」里被平台分配的 Bot，
        未分配则为默认 Bot。"""
        return _ClientProxy(self._chosen_bot())

    def get_bot(self, bot_id: str | None = None) -> _ClientProxy:
        """按 id 取指定 Bot 的发送代理（高级用法）；不传/不存在则回退默认 Bot。"""
        return _ClientProxy(self._accounts.get_bot(bot_id))

    def _chosen_bot(self) -> object | None:
        """本插件被平台分配的 Bot（bot_choice）对应的 client，未分配=默认 Bot。"""
        bot_id = self._registry.get_bot_choice(self.plugin_id)
        return self._accounts.get_bot(bot_id)

    @property
    def user(self) -> _ClientProxy:
        """主用户账号发送代理（多账号取应用范围内第一个已连接）。
        遵循插件的「应用账号范围」——只勾选了某些账号时，不会取到范围外的账号。"""
        scoped = self._scoped_user_apps()
        return _ClientProxy(scoped[0] if scoped else None)

    @property
    def user_apps(self) -> list[object]:
        """本插件应用范围内、已连接的用户账号列表（多账号场景）。
        只勾选了部分账号时只返回这些账号；未勾选（=全部）时返回所有已连接用户账号。"""
        return self._scoped_user_apps()

    def _scoped_user_apps(self) -> list[object]:
        """已连接用户账号，按本插件「应用账号范围」过滤（空范围=全部）。
        handler 挂载与 ctx.user/ctx.user_apps 共用同一套过滤，保证「只勾一个账号」时
        无论插件是被动响应消息还是主动遍历账号发消息，都只作用于所选账号。"""
        user_apps = self._accounts.connected_user_apps
        scope_sessions = self._registry.get_account_scope(self.plugin_id)
        if scope_sessions:
            return [a for a in user_apps if getattr(a, "name", None) in scope_sessions]
        return list(user_apps)

    @property
    def owner_id(self) -> int:
        """
        平台管理员的 Telegram 数字 ID（取自主账号 ACCOUNTS[0].tgid）。
        插件不直接读 config，需要给「平台管理员」推送时用它。无主账号时为 0。
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
        平台统一分类（打级别标签 + 插件名 + 账号名）、套格式，再通过 Bot 发给平台管理员
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

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        """
        插件写回自己的配置（局部合并，不触发重载）。用于持久化运行状态，
        或把结果回填到 config_schema 里的 info 字段（如「上次运行时间」「已处理条数」）供前端展示。

            ctx.update_config({"last_run": "2026-07-15 10:00", "count": 5})

        - 仅合并传入的键，其它配置项保持不变（内部先读当前值再叠加）。
        - 不重载插件（避免运行中的插件把自己拆掉）；下次读 ctx.config 即为最新值。
        - 与用户在前端手动保存互不冲突：谁后写谁生效。
        """
        if not isinstance(patch, dict):
            raise TypeError("update_config 需要 dict")
        merged = {**self._registry.get_config(self.plugin_id), **patch}
        self._registry.set_config(self.plugin_id, merged)
        return merged

    # ──────────────────────────────────────────────
    # 浏览器自动化（平台托管，优先 CloakBrowser 停用浏览器，回退 Playwright）
    # ──────────────────────────────────────────────
    @property
    def browser(self):
        """
        平台托管的浏览器能力，插件无需自己装浏览器。async 用法：

            html = await ctx.browser.page_source("https://example.com")
            # 需要交互时传同步 action(page)：
            def grab(page):
                page.click("#more"); return page.inner_text("#list")
            data = await ctx.browser.run("https://example.com", grab)

        引擎优先 CloakBrowser（过反爬/指纹检测），未就绪时自动回退平台内置 Playwright。
        `ctx.browser.engine` 可查当前引擎名。
        """
        from kernel import browser as _browser
        return _browser.browser

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

        关于 group：Pyrogram 在同一 group 内只执行第一个匹配的 handler 就跳出该组，
        若所有插件都用同一 group，先注册者会"吃掉"消息。为此平台给每个插件分配独立的
        group 基址，这里传入的 group 是「插件内相对优先级」——会被平移到本插件的区间，
        既保证不同插件互不抢占，又保留插件内部用多个 group 排序的能力（数值越小越先）。
        """
        def decorator(func: Callable):
            wrapped = self._track(func)
            handler = MessageHandler(wrapped, filter_)
            real_group = self._group_base + group
            for client in self._resolve_targets(target):
                client.add_handler(handler, real_group)
                self._handles.append((client, handler, real_group))
            return func
        return decorator

    def on_edited_message(self, filter_=None, group: int = 0, target: str = "auto"):
        """注册「已编辑消息」处理器。用法同 on_message，但只在消息被编辑时触发。

        某些 bot（如 springsunday 大额转账确认）会先发一条消息再「编辑」它来送达最终结果，
        这类结果 on_message 收不到，需要本方法。group/target 语义与 on_message 相同，
        句柄同样登记进 self._handles，teardown/热重载时自动注销。
        """
        def decorator(func: Callable):
            wrapped = self._track(func)
            handler = EditedMessageHandler(wrapped, filter_)
            real_group = self._group_base + group
            for client in self._resolve_targets(target):
                client.add_handler(handler, real_group)
                self._handles.append((client, handler, real_group))
            return func
        return decorator

    def on_callback(self, filter_=None, group: int = 0, target: str = "auto"):
        """注册回调查询处理器（按钮点击）。group 语义同 on_message：插件内相对优先级，
        平台自动平移到本插件独立的 group 区间。"""
        def decorator(func: Callable):
            wrapped = self._track(func)
            handler = CallbackQueryHandler(wrapped, filter_)
            real_group = self._group_base + group
            for client in self._resolve_targets(target):
                client.add_handler(handler, real_group)
                self._handles.append((client, handler, real_group))
            return func
        return decorator

    def on_webhook(self, func: Callable) -> Callable:
        """
        注册本插件的 HTTP webhook 处理器（供外部服务回调）。用法：

            @ctx.on_webhook
            async def handle(req):
                data = req.json or {}
                await ctx.notify(f"收到事件：{data}")
                return {"ok": True}        # 返回 dict→JSON / str→文本 / None→{"ok":true}

        平台会为每个插件暴露一个入站地址（路径按插件 id 区分）：
            http(s)://<平台地址>/api/v1/plugin/<插件id>/webhook?apikey=<密钥>
        其中 apikey 是平台统一的 Webhook 密钥（在「系统设置 → 通知」生成，平台与所有插件共用）；
        本插件需在 __plugin__ 声明 "webhook": True，地址可在「插件 → 配置」弹窗查看/复制。

        req 是 WebhookRequest（.method/.query/.headers/.json/.text/.body）。
        一个插件只有一个处理器，重复注册后者覆盖前者；停用/重载时自动失效。
        """
        # webhook 处理器由路由用单参数 handler(req) 调用，不能用为消息/回调
        # 设计的 _track（其 wrapper 签名是 (client, update, ...)，单参数调用会缺参报错）。
        # 这里用单参数专用包装，同样把「当前插件」设进 contextvar 以归属出站动作。
        import functools
        from kernel import activity

        pid = self.plugin_id

        @functools.wraps(func)
        async def wrapper(req):
            token = activity.set_current(pid)
            try:
                return await func(req)
            finally:
                activity.reset_current(token)

        self._webhook_handler = wrapper
        return func

    def action(self, name: str) -> Callable:
        """
        注册一个「动作」，供插件配置表单里的动作按钮触发。用法：

            @ctx.action("test")
            async def _test():
                return {"ok": True, "message": "连接正常"}   # 返回值弹给管理员

        在 config_schema 里放一个按钮字段引用它：
            "test": {"type": "action", "label": "测试连接", "action": "test"}

        处理函数无参；返回 dict / str / None，前端以此弹提示。由已登录管理员在配置弹窗点击触发，
        经登录态鉴权（非公开）。同名重复注册后者覆盖前者；停用/重载时自动失效。
        """
        import functools
        from kernel import activity

        pid = self.plugin_id

        import asyncio

        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            async def wrapper():
                token = activity.set_current(pid)
                try:
                    result = func()
                    if asyncio.iscoroutine(result):
                        result = await result
                    return result
                finally:
                    activity.reset_current(token)

            self._action_handlers[str(name)] = wrapper
            return func

        return decorator

    @staticmethod
    def _norm_api_path(path: str) -> str:
        """规整插件 API 相对路径：确保前导 /，去掉尾部 /（根路径保留为 '/'）。"""
        p = "/" + str(path or "").strip().strip("/")
        return p

    def on_api(self, path: str, methods: list[str] | tuple[str, ...] = ("GET",)) -> Callable:
        """
        注册一个插件 API 端点，供 vue 模式自带的前端组件调用。用法：

            @ctx.on_api("/config", methods=["GET"])
            async def read_config(req):
                return {"values": ctx.config}          # dict→JSON / str→文本 / None→{"ok":true}

            @ctx.on_api("/config", methods=["POST"])
            async def save_config(req):
                ctx.update_config(req.json or {})
                return {"ok": True}

        前端组件通过平台注入的 host.api 调用，实际地址为：
            /api/plugins/<插件id>/api/<path>
        由已登录管理员经 Bearer 令牌鉴权（非公开，外部无法直接访问）。

        req 是 WebhookRequest：req.method / req.query / req.headers / req.json / req.text /
        req.body / req.path。处理函数返回 dict / str / None。
        同一 (方法, 路径) 重复注册后者覆盖前者；停用/重载时自动失效。
        """
        import asyncio
        import functools
        from kernel import activity

        pid = self.plugin_id
        norm = self._norm_api_path(path)
        # 容错：methods 传成单个字符串（如 "GET"）时按单元素处理，避免被逐字符拆成 G/E/T
        if isinstance(methods, str):
            methods = [methods]
        method_list = [str(m).upper() for m in (methods or ["GET"])]

        def decorator(func: Callable) -> Callable:
            @functools.wraps(func)
            async def wrapper(req):
                token = activity.set_current(pid)
                try:
                    result = func(req)
                    if asyncio.iscoroutine(result):
                        result = await result
                    return result
                finally:
                    activity.reset_current(token)

            for m in method_list:
                self._api_handlers[(m, norm)] = wrapper
            return func

        return decorator

    async def download(self, message: Any, subdir: str | None = None) -> Path:
        """
        下载消息里的媒体（图片/文件/视频等）到本插件目录，返回落盘 Path。

            path = await ctx.download(message, subdir="imgs")   # data/plugin_data/<id>/imgs/<file>

        - 目标目录默认 data_dir，传 subdir 则落到其子目录（自动创建）。
        - 消息无可下载媒体时抛 ValueError。
        - 实为对 Pyrogram message.download 的封装，文件名由 Telegram 决定。
        """
        if not getattr(message, "media", None):
            raise ValueError("该消息没有可下载的媒体")
        import os
        target = self.data_dir / subdir if subdir else self.data_dir
        target.mkdir(parents=True, exist_ok=True)
        # 末尾带分隔符 → Pyrogram 视为目录，文件名用 Telegram 提供的原名
        saved = await message.download(file_name=str(target) + os.sep)
        return Path(saved)

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
        user/both 插件会按「应用账号范围」过滤用户账号（空范围=全部），
        与 ctx.user / ctx.user_apps 共用 _scoped_user_apps，口径一致。"""
        meta = self._registry.get_meta(self.plugin_id)
        scope = meta.scope if meta else "user"
        if target == "auto":
            target = scope

        clients: list[object] = []
        if target in ("user", "both"):
            clients.extend(self._scoped_user_apps())
        if target in ("bot", "both"):
            bot = self._chosen_bot()
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

        # webhook / 动作 / API 处理器随插件卸载失效，避免热重载后调用到旧闭包
        self._webhook_handler = None
        self._action_handlers.clear()
        self._api_handlers.clear()
