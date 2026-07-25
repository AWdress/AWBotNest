"""
infra/container.py
依赖注入容器 - python-dependency-injector 4.x

统一管理所有服务和适配器的生命周期，
替代散落在各模块中的全局变量和 import 时初始化。
"""
from __future__ import annotations

from dependency_injector import containers, providers

from adapters.storage.toml_state import TomlStateRepository


class Container(containers.DeclarativeContainer):
    """
    应用 DI 容器

    使用方式（在 app.py 中）：
        container = Container()
        container.config.from_pydantic(get_settings())

    获取服务：
        state = container.state_repo()
    """

    # ------------------------------------------------------------------ #
    # 配置                                                                  #
    # ------------------------------------------------------------------ #
    config = providers.Configuration()

    # ------------------------------------------------------------------ #
    # 外部客户端（由 app.py 在容器初始化后注入）                          #
    # ------------------------------------------------------------------ #
    # 这些是 Pyrogram Client 对象，不在容器内创建，而是由外部传入
    user_client = providers.Object(None)   # user_app
    bot_client = providers.Object(None)    # bot_app

    # ------------------------------------------------------------------ #
    # 基础设施适配器                                                       #
    # ------------------------------------------------------------------ #

    # 状态存储（TOML，过渡期）
    # state_manager 由外部传入（libs.state.state_manager）
    _state_manager_raw = providers.Object(None)
    state_repo = providers.Factory(
        TomlStateRepository,
        state_manager=_state_manager_raw,
    )

    # SQLAlchemy 会话工厂（由 models/__init__.py 的 async_session_maker 提供）
    _session_maker_raw = providers.Object(None)


def build_container(
    user_client: object,
    bot_client: object,
    state_manager: object,
    session_maker: object,
    settings: object,
) -> Container:
    """
    工厂函数：构建并配置完整的 DI 容器

    Args:
        user_client: Pyrogram user_app Client
        bot_client: Pyrogram bot_app Client
        state_manager: libs.state.StateManager 实例
        session_maker: SQLAlchemy async_sessionmaker 实例
        settings: infra.config.AppSettings 实例

    Returns:
        已配置的 Container 实例
    """
    container = Container()

    # 注入外部依赖
    container.user_client.override(providers.Object(user_client))
    container.bot_client.override(providers.Object(bot_client))
    container._state_manager_raw.override(providers.Object(state_manager))
    container._session_maker_raw.override(providers.Object(session_maker))

    # 从 pydantic settings 加载配置
    container.config.from_dict(_settings_to_dict(settings))

    return container


def _settings_to_dict(settings: object) -> dict:
    """将 AppSettings 转为 dependency-injector config 可接受的 dict"""
    try:
        cfg = settings  # type: ignore[attr-defined]
        return {
            "notify_chat_id": cfg.notify_chat_id,
        }
    except AttributeError:
        return {}


# 模块级单例，由 app.py 在启动时设置
_container_instance: "Container | None" = None


def get_container() -> "Container":
    """获取全局 DI 容器单例（插件层使用）"""
    if _container_instance is None:
        raise RuntimeError("DI 容器尚未初始化，请确保 app.py 启动完成后再使用")
    return _container_instance


def get_container_or_none() -> "Container | None":
    """获取全局 DI 容器单例，未初始化时返回 None（供可降级的监听器使用）"""
    return _container_instance


def rebind_user_client(user_client: object) -> None:
    """
    将容器中的 user_client 重新绑定为新的 Pyrogram 实例。

    供 manager.start_userbot 在重新创建 user_app 后调用。
    """
    if _container_instance is None:
        return
    _container_instance.user_client.override(providers.Object(user_client))
