"""
infra/config.py
统一配置管理 - pydantic-settings 2.x

替换 config/config.py 中的直接赋值方式，提供：
- 类型安全
- 多源合并（.env 文件 > 环境变量 > 默认值）
- 运行时验证
- 不暴露敏感值到日志
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from libs.log import logger

BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"  # 运行时数据目录（卷映射），存 config.json / state.toml 等


class ProxySettings(BaseSettings):
    """代理配置"""
    proxy_enable: bool = False
    scheme: str = "http"
    hostname: str = ""
    port: int = 7890
    username: str = ""
    password: SecretStr = SecretStr("")
    proxy_url: str = ""

    model_config = SettingsConfigDict(env_prefix="PROXY_", extra="ignore")


class AiSettings(BaseSettings):
    """AI 对话配置"""
    enabled: bool = False
    provider: str = "openai"
    api_key: SecretStr = SecretStr("")
    base_url: str = "https://api.openai.com/v1"
    model: str = "gpt-3.5-turbo"
    system_prompt: str = "你是一个有用的助手。"
    max_history: int = 10
    white_list_chats: list[int] = Field(default_factory=list)
    enable_private_chat: bool = True
    enable_group_chat: bool = True
    enable_explain_command: bool = True
    enable_explain_prompt: bool = False

    model_config = SettingsConfigDict(env_prefix="AI_", extra="ignore")


class DatabaseSettings(BaseSettings):
    """数据库配置"""
    dbset: str = "SQLite"             # "SQLite" | "mySQL" | "PostgreSQL"
    address: str = "localhost"
    port: int = 3306
    user: str = "root"
    password: SecretStr = SecretStr("")
    db_name: str = "tgbot"
    # SQLite 路径
    sqlite_path: str = "db_file/SQLite/tgbot.db"

    model_config = SettingsConfigDict(env_prefix="DB_", extra="ignore")

    @property
    def async_url(self) -> str:
        from urllib.parse import quote_plus  # noqa: PLC0415
        if self.dbset == "SQLite":
            return f"sqlite+aiosqlite:///{self.sqlite_path}"
        pw = quote_plus(self.password.get_secret_value())
        if self.dbset == "PostgreSQL":
            return (
                f"postgresql+asyncpg://{self.user}:{pw}"
                f"@{self.address}:{self.port}/{self.db_name}"
            )
        return (
            f"mysql+aiomysql://{self.user}:{pw}"
            f"@{self.address}:{self.port}/{self.db_name}"
        )


class TelegramSettings(BaseSettings):
    """Telegram 连接配置"""
    api_id: int = Field(default=0, description="Telegram API ID")
    api_hash: SecretStr = Field(default=SecretStr(""), description="Telegram API Hash")
    bot_token: SecretStr = Field(default=SecretStr(""), description="Bot Token")
    my_tgid: int = Field(default=0, description="自己的 Telegram ID")
    my_name: str = Field(default="", description="TG 昵称（排行榜显示用）")
    ny_username: str = Field(default="", description="TG 用户名")
    web_ui_url: str = Field(default="", description="Web UI 的外部访问 URL")
    web_ui_port: int = Field(default=8000, description="Web UI 监听端口")
    ngrok_enable: bool = Field(default=False, description="是否自动开启 ngrok 映射")
    ngrok_token: str = Field(default="", description="ngrok authtoken")
    # 多用户账号列表，每项含 session 名称（对应 sessions/<session>.session）
    user_accounts: list[dict] = Field(
        default_factory=lambda: [{"session": "user_account"}],
        description="多用户账号配置列表",
    )

    model_config = SettingsConfigDict(env_prefix="TG_", extra="ignore")

    @field_validator("api_id", mode="before")
    @classmethod
    def validate_api_id(cls, v: Any) -> int:
        if isinstance(v, str) and v.strip():
            return int(v)
        return int(v) if v else 0


class AppSettings(BaseSettings):
    """
    应用全局配置

    加载优先级（高 → 低）：
    1. 环境变量
    2. .env 文件（项目根目录）
    3. config/config.py（兼容旧格式，通过 _load_legacy_config 填充）
    4. 默认值
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # 嵌套模型使用 __ 分隔符
        env_nested_delimiter="__",
    )

    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    proxy: ProxySettings = Field(default_factory=ProxySettings)
    ai: AiSettings = Field(default_factory=AiSettings)

    def save_to_state(self) -> None:
        """将部分可变配置持久化到 config/state.toml"""
        import toml # noqa: PLC0415
        state_path = DATA_DIR / "state.toml"
        
        # 只保存允许通过 WebUI 修改的字段
        state_data = {
            "prize_list": self.prize_list,
            "lottery_target_groups": self.lottery_target_groups,
            "prize_match_rules": self.prize_match_rules,
            "trap_lottery_detection": self.trap_lottery_detection,
            "ai": self.ai.model_dump(exclude={"api_key"}),
        }
        
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                toml.dump(state_data, f)
            logger.info(f"配置已持久化到 {state_path}")
        except Exception as e:
            logger.error(f"保存 state.toml 失败: {e}")

    # 群组配置
    pt_group_id: dict[str, int] = Field(default_factory=dict)
    notify_chat_id: int = 0          # BOT_MESSAGE_CHAT

    # 抽奖配置
    lottery_target_groups: list[int] = Field(default_factory=list)
    prize_list: dict[str, list[str]] = Field(default_factory=dict)
    
    # 奖品匹配规则
    prize_match_rules: dict[str, Any] = Field(default_factory=lambda: {"case_sensitive": False})
    
    # 陷阱检测配置
    trap_lottery_detection: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _resolve_notify_chat(self) -> "AppSettings":
        """从 pt_group_id 中解析通知频道 ID"""
        if not self.notify_chat_id and self.pt_group_id:
            self.notify_chat_id = self.pt_group_id.get(
                "BOT_MESSAGE_CHAT",
                self.telegram.my_tgid,
            )
        return self


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """
    获取全局配置单例

    优先级：环境变量 > state.toml > config/config.py
    """
    settings = AppSettings()

    # 1. 基础加载：从旧版 config.py 加载（迁移期兼容）
    if not settings.telegram.api_id:
        _load_legacy_config(settings)

    # 2. 覆盖加载：从 state.toml 加载运行时持久化的状态
    _load_state_toml(settings)

    return settings


def _load_state_toml(settings: AppSettings) -> None:
    """从 config/state.toml 读取持久化状态并覆盖到 settings"""
    import toml # noqa: PLC0415
    state_path = DATA_DIR / "state.toml"
    if not state_path.exists():
        return

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            state_data = toml.load(f)
            
        # 覆盖可变字段
        for key in ["prize_list", "lottery_target_groups", "prize_match_rules", "trap_lottery_detection", "ai"]:
            if key in state_data:
                if key == "ai" and isinstance(state_data[key], dict):
                    # AI 配置是嵌套模型，需合并
                    current_ai = settings.ai.model_dump()
                    current_ai.update(state_data[key])
                    settings.ai = AiSettings(**current_ai)
                else:
                    setattr(settings, key, state_data[key])
        logger.info(f"已从 {state_path} 加载持久化配置")
    except Exception as e:
        logger.error(f"加载 state.toml 失败: {e}")


def _migrate_config_to_accounts(cfg) -> None:
    """将旧 config.py 的 MY_NAME/NY_USERNAME/MY_TGID 原地迁移为 ACCOUNTS 格式"""
    import re  # noqa: PLC0415
    config_path = CONFIG_DIR / "config.py"
    if not config_path.exists():
        return
    content = config_path.read_text(encoding="utf-8")
    if "ACCOUNTS" in content:
        return  # 已是新格式，跳过

    session = "user_account"  # 保持旧 session 文件名兼容
    name    = getattr(cfg, "MY_NAME", "")
    tgid    = getattr(cfg, "MY_TGID", 0)

    # 删除旧的三个独立变量
    content = re.sub(r"^MY_NAME\s*=.*\n?",     "", content, flags=re.MULTILINE)
    content = re.sub(r"^NY_USERNAME\s*=.*\n?", "", content, flags=re.MULTILINE)
    content = re.sub(r"^MY_TGID\s*=.*\n?",     "", content, flags=re.MULTILINE)

    # 在 BOT_TOKEN 行后插入 ACCOUNTS 块
    accounts_block = (
        f'\n# 多账号配置：每项含 session（文件名）、name（昵称）、tgid（Telegram ID）\n'
        f'ACCOUNTS = [\n'
        f'    {{"session": "{session}", "name": "{name}", "tgid": {tgid}}},\n'
        f'    # {{"session": "Account2", "name": "Name2", "tgid": 0}},\n'
        f']\n'
        f'\n# 以下从主账号自动推导，无需手动设置\n'
        f'MY_NAME     = ACCOUNTS[0]["name"]    if ACCOUNTS else ""\n'
        f'MY_TGID     = ACCOUNTS[0]["tgid"]    if ACCOUNTS else 0\n'
        f'NY_USERNAME = ACCOUNTS[0]["session"] if ACCOUNTS else ""\n'
    )
    content = re.sub(
        r"(^BOT_TOKEN\s*=.*\n)",
        r"\1" + accounts_block,
        content, flags=re.MULTILINE,
    )

    config_path.write_text(content, encoding="utf-8")
    # 使缓存模块立即包含新写入的 ACCOUNTS
    import importlib, sys  # noqa: PLC0415, E401
    if "config.config" in sys.modules:
        importlib.reload(sys.modules["config.config"])
    logger.info("已将 config.py 迁移为 ACCOUNTS 格式，旧变量已移除")


def _load_legacy_config(settings: AppSettings) -> None:
    """从 config/config.py（JSON 垫片）读取配置填充到 settings"""
    try:
        # 配置数据现存于 config/config.json，由 config.py 垫片加载。
        # 不再对 config.py 做 Python 文本补全/迁移（那会破坏垫片）。
        import config.config as cfg  # noqa: PLC0415
        cfg.reload()  # 确保拿到磁盘最新值


        # Telegram
        settings.telegram = TelegramSettings(
            api_id=getattr(cfg, "API_ID", 0),
            api_hash=SecretStr(str(getattr(cfg, "API_HASH", ""))),
            bot_token=SecretStr(str(getattr(cfg, "BOT_TOKEN", ""))),
            my_tgid=getattr(cfg, "MY_TGID", 0),
            my_name=getattr(cfg, "MY_NAME", ""),
            ny_username=getattr(cfg, "NY_USERNAME", ""),
            web_ui_url=getattr(cfg, "WEB_UI_URL", ""),
            web_ui_port=getattr(cfg, "WEB_UI_PORT", 18000),
            ngrok_enable=getattr(cfg, "NGROK_ENABLE", False),
            ngrok_token=getattr(cfg, "NGROK_TOKEN", ""),
            user_accounts=[
                acc if isinstance(acc, dict) else {"session": acc}
                for acc in getattr(cfg, "ACCOUNTS", None)
                or [{"session": getattr(cfg, "NY_USERNAME", None) or getattr(cfg, "MY_NAME", None) or "user_account"}]
            ],
        )

        # 群组
        pt_group_id: dict = getattr(cfg, "PT_GROUP_ID", {})
        settings.pt_group_id = {k: int(v) for k, v in pt_group_id.items()}
        settings.notify_chat_id = pt_group_id.get(
            "BOT_MESSAGE_CHAT", settings.telegram.my_tgid
        )

        # 数据库
        db_info: dict = getattr(cfg, "DB_INFO", {})
        if db_info:
            settings.database = DatabaseSettings(
                dbset=db_info.get("dbset", "SQLite"),
                address=db_info.get("address", "localhost"),
                port=int(db_info.get("port", 3306)),
                user=db_info.get("user", "root"),
                password=SecretStr(str(db_info.get("password", ""))),
                db_name=db_info.get("db_name", "tgbot"),
            )

        # 代理
        proxy_set: dict = getattr(cfg, "proxy_set", {})
        if proxy_set.get("proxy_enable"):
            px = proxy_set.get("proxy", {})
            settings.proxy = ProxySettings(
                proxy_enable=True,
                scheme=px.get("scheme", "http"),
                hostname=px.get("hostname", ""),
                port=int(px.get("port", 7890)),
                proxy_url=proxy_set.get("PROXY_URL", ""),
            )

        # 奖品列表 - 处理变量 Key 兼容性
        prize_list: dict = getattr(cfg, "PRIZE_LIST", {})
        processed_prize_list = {}
        for k, v in prize_list.items():
            # 如果 key 在 PT_GROUP_ID 中，则使用对应的值作为 key
            actual_key = str(pt_group_id.get(k, k))
            processed_prize_list[actual_key] = v
        settings.prize_list = processed_prize_list

        # 抽奖群组
        lottery_groups: list = getattr(cfg, "LOTTERY_TARGET_GROUP", [])
        settings.lottery_target_groups = [int(g) for g in lottery_groups]
        
        # 新增：匹配规则与陷阱检测
        settings.prize_match_rules = getattr(cfg, "PRIZE_MATCH_RULES", {"case_sensitive": False})
        settings.trap_lottery_detection = getattr(cfg, "TRAP_LOTTERY_DETECTION", {})

        # AI 配置
        ai_info: dict = getattr(cfg, "AI_INFO", {})
        if ai_info:
            settings.ai = AiSettings(
                enabled=ai_info.get("enabled", False),
                provider=ai_info.get("provider", "openai"),
                api_key=SecretStr(str(ai_info.get("api_key", ""))),
                base_url=ai_info.get("base_url", "https://api.openai.com/v1"),
                model=ai_info.get("model", "gpt-3.5-turbo"),
                system_prompt=ai_info.get("system_prompt", "你是一个有用的助手。"),
                max_history=int(ai_info.get("max_history", 10)),
                white_list_chats=ai_info.get("white_list_chats", []),
                enable_private_chat=ai_info.get("enable_private_chat", True),
                enable_group_chat=ai_info.get("enable_group_chat", True),
                enable_explain_command=ai_info.get("enable_explain_command", True),
            )

    except ImportError:
        pass  # config.py 不存在时忽略
    except Exception as e:
        logger.error(f"从旧版 config.py 加载配置失败: {e}")


def _patch_legacy_config_file() -> None:
    """
    全面检查 config/config.py，如果缺少任何必要的配置项，则自动追加。
    """
    config_path = CONFIG_DIR / "config.py"
    if not config_path.exists():
        return

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        # 定义 必须存在的配置项 及其 默认值/注释格式
        # 格式: (变量名, 追加内容)
        # 注意：平台化后此处只补全「平台级」配置。业务配置（AI/抽奖/奖品/陷阱等）
        #       一律由各自插件自带，不再写入 config.py（见 SPEC.md）。
        patches = [
            ("API_ID", 'API_ID = 0  # Telegram API ID'),
            ("API_HASH", 'API_HASH = ""  # Telegram API Hash'),
            ("BOT_TOKEN", 'BOT_TOKEN = ""  # Bot Token'),
            ("MY_TGID", 'MY_TGID = 0  # 自己的 Telegram ID'),
            ("MY_NAME", 'MY_NAME = "" # TG 昵称'),
            ("NY_USERNAME", 'NY_USERNAME = "" # 用户名'),
            ("proxy_set", 'proxy_set = {"proxy_enable": False, "proxy": {"scheme": "http", "hostname": "127.0.0.1", "port": 7890}, "PROXY_URL": ""}'),
            ("WEB_UI_URL", 'WEB_UI_URL = "" # Web 控制面板外部地址'),
            ("WEB_UI_PORT", 'WEB_UI_PORT = 18000'),
            ("NGROK_ENABLE", 'NGROK_ENABLE = False'),
            ("NGROK_TOKEN", 'NGROK_TOKEN = ""'),
            ("DB_INFO", 'DB_INFO = {"dbset": "SQLite", "address": "127.0.0.1", "db_name": "tgbot", "port": 3306, "user": "", "password": ""}'),
        ]

        new_settings = []
        for key, line in patches:
            # 简单的关键词匹配检查是否存在该配置
            if key not in content:
                new_settings.append(line)

        if new_settings:
            with open(config_path, "a", encoding="utf-8") as f:
                # 在文件末尾追加，增加适当的分割线
                header = "\n\n# " + "="*30 + "\n# 系统自动补全的平台配置项\n# " + "="*30 + "\n"
                f.write(header + "\n".join(new_settings) + "\n")
            logger.info(f"已自动补全 {config_path} 中缺失的 {len(new_settings)} 项平台配置")
    except Exception as e:
        logger.error(f"自动补全 config.py 失败: {e}")


