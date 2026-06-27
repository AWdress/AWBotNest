"""
infra/container.py
依赖注入容器 - python-dependency-injector 4.x

统一管理所有服务和适配器的生命周期，
替代散落在各模块中的全局变量和 import 时初始化。
"""
from __future__ import annotations

from dependency_injector import containers, providers

from adapters.ocr.ddddocr_adapter import DdddOcrAdapter
from adapters.storage.toml_state import TomlStateRepository
from adapters.storage.sqlalchemy.transfer_repo import (
    SqlAlchemyTransferRepository,
    SqlAlchemyRaidRepository,
)
from adapters.storage.sqlalchemy.redpocket_repo import SqlAlchemyRedpocketRepository
from adapters.storage.sqlalchemy.ydx_repo import SqlAlchemyYdxRepository
from adapters.storage.sqlalchemy.ai_repo import SqlAlchemyAiRepository
from adapters.telegram.sender import PyrogramMessageSender, PyrogramNotifier
from adapters.leaderboard.imgkit_adapter import ImgkitLeaderboardGenerator
from adapters.ai.openai_adapter import OpenAIAdapter
from core.services.trap_service import TrapService, TrapDetectionConfig
from core.services.red_packet_service import RedPacketService
from core.services.lottery_service import LotteryService, LotteryConfig
from core.services.transfer_service import TransferService
from core.services.prize_service import PrizeService
from core.services.redpocket_record_service import RedpocketRecordService
from core.services.ydx_service import YdxService
from core.services.ai_service import AiService


class Container(containers.DeclarativeContainer):
    """
    应用 DI 容器

    使用方式（在 app.py 中）：
        container = Container()
        container.config.from_pydantic(get_settings())
        container.wire(modules=[__name__, "user_scripts.universal.auto_lottery_for_xiaocai"])

    获取服务：
        trap_svc = container.trap_service()
        lottery_svc = container.lottery_service()
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

    # OCR
    ocr_adapter = providers.Singleton(DdddOcrAdapter)

    # 排行榜图片生成
    leaderboard_generator = providers.Singleton(ImgkitLeaderboardGenerator)

    # AI 引擎
    ai_engine = providers.Singleton(
        OpenAIAdapter,
        api_key=config.ai.api_key,
        base_url=config.ai.base_url,
    )

    # 状态存储（TOML，过渡期）
    # state_manager 由外部传入（libs.state.state_manager）
    _state_manager_raw = providers.Object(None)
    state_repo = providers.Factory(
        TomlStateRepository,
        state_manager=_state_manager_raw,
    )

    # SQLAlchemy 会话工厂（由 models/__init__.py 的 async_session_maker 提供）
    _session_maker_raw = providers.Object(None)

    # 存储仓库
    transfer_repo = providers.Factory(
        SqlAlchemyTransferRepository,
        session_maker=_session_maker_raw,
    )
    raid_repo = providers.Factory(
        SqlAlchemyRaidRepository,
        session_maker=_session_maker_raw,
    )
    ai_repo = providers.Factory(
        SqlAlchemyAiRepository,
        session_maker=_session_maker_raw,
    )

    # Telegram 适配器
    message_sender = providers.Factory(
        PyrogramMessageSender,
        client=user_client,
    )
    notifier = providers.Factory(
        PyrogramNotifier,
        bot_client=bot_client,
    )

    # ------------------------------------------------------------------ #
    # 业务服务                                                              #
    # ------------------------------------------------------------------ #

    trap_config = providers.Factory(
        TrapDetectionConfig,
        case_sensitive=config.trap.case_sensitive,
        enable_prize_pattern_check=config.trap.enable_prize_pattern_check,
        enable_creator_blacklist=config.trap.enable_creator_blacklist,
        enable_participant_check=config.trap.enable_participant_check,
        max_participants=config.trap.max_participants,
        blacklist_creator_ids=config.trap.blacklist_creator_ids,
        suspicious_keywords=config.trap.suspicious_keywords,
        min_prize_amount=config.trap.min_prize_amount,
    )

    trap_service = providers.Singleton(
        TrapService,
        config=trap_config,
        state=state_repo,
    )

    red_packet_service = providers.Singleton(
        RedPacketService,
        ocr=ocr_adapter,
        sender=message_sender,
        notifier=notifier,
        state=state_repo,
        trap=trap_service,
        notify_chat_id=config.notify_chat_id,
    )

    lottery_config = providers.Factory(
        LotteryConfig,
        target_groups=config.lottery.target_groups,
        prize_list=config.lottery.prize_list,
        case_sensitive=config.lottery.case_sensitive,
    )

    lottery_service = providers.Singleton(
        LotteryService,
        config=lottery_config,
        state=state_repo,
        sender=message_sender,
        notifier=notifier,
        trap=trap_service,
        notify_chat_id=config.notify_chat_id,
    )

    transfer_service = providers.Singleton(
        TransferService,
        transfer_repo=transfer_repo,
        raid_repo=raid_repo,
        state=state_repo,
        notifier=notifier,
        notify_chat_id=config.notify_chat_id,
        leaderboard_generator=leaderboard_generator,
        sender=message_sender,
    )

    # 红包记录仓库 + 服务
    redpocket_repo = providers.Factory(SqlAlchemyRedpocketRepository)
    redpocket_record_service = providers.Singleton(
        RedpocketRecordService,
        repo=redpocket_repo,
    )

    # YDX 游戏记录仓库 + 服务
    ydx_repo = providers.Factory(SqlAlchemyYdxRepository)
    ydx_service = providers.Singleton(
        YdxService,
        repo=ydx_repo,
    )

    ai_service = providers.Singleton(
        AiService,
        engine=ai_engine,
        state_repo=state_repo,
        ai_repo=ai_repo,
    )

    # 发奖状态服务（内存状态，Singleton 以保持状态）
    prize_service = providers.Singleton(PrizeService)


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
            "trap": {
                "case_sensitive": False,
                "enable_prize_pattern_check": True,
                "enable_creator_blacklist": True,
                "enable_participant_check": True,
                "max_participants": 1,
                "blacklist_creator_ids": [],
                "suspicious_keywords": [],
                "min_prize_amount": 500,
            },
            "lottery": {
                "target_groups": cfg.lottery_target_groups,
                "prize_list": cfg.prize_list,
                "case_sensitive": False,
            },
            "ai": {
                "enabled": cfg.ai.enabled,
                "provider": cfg.ai.provider,
                "api_key": cfg.ai.api_key.get_secret_value(),
                "base_url": cfg.ai.base_url,
                "model": cfg.ai.model,
                "system_prompt": cfg.ai.system_prompt,
                "max_history": cfg.ai.max_history,
                "white_list_chats": cfg.ai.white_list_chats,
            },
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
    将容器中的 user_client 重新绑定为新的 Pyrogram 实例，
    并重置依赖它的 Singleton 服务，使其下次取用时使用新客户端。

    供 manager.start_userbot 在重新创建 user_app 后调用。
    """
    if _container_instance is None:
        return
    _container_instance.user_client.override(providers.Object(user_client))
    # 重置依赖 message_sender(user_client) 的 Singleton，强制下次重建
    for svc in (
        _container_instance.lottery_service,
        _container_instance.red_packet_service,
        _container_instance.transfer_service,
    ):
        try:
            svc.reset()
        except Exception:  # noqa: BLE001 - 重置失败不应阻断登录流程
            pass
