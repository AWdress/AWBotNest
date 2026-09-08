from pydantic import BaseModel, Field

class LoginStartBody(BaseModel):
    session: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=3, max_length=32)

class AdminLoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=512)

class CredentialBody(BaseModel):
    old_password: str = Field(default="", max_length=512)
    new_username: str = Field(default="", max_length=64)
    new_password: str = Field(default="", max_length=512)

class LoginCompleteBody(BaseModel):
    session: str = Field(min_length=1, max_length=64)
    code: str = Field(default="", max_length=16)
    password: str = Field(default="", max_length=256)

class SettingsBody(BaseModel):
    api_id: int = Field(default=0, ge=0)
    api_hash: str = Field(default="", max_length=128)
    bot_token: str = Field(default="", max_length=256)
    bot_name: str = Field(default="主要 Bot", max_length=64)
    default_bot_id: str = Field(default="default", max_length=64)
    default_bot_chat_id: str = Field(default="", max_length=64)
    web_host: str = Field(default="0.0.0.0", max_length=255)
    web_port: int = Field(default=18001, ge=1, le=65535)
    bots: list[dict[str, str]] = Field(default_factory=list)
    ai_base_url: str = Field(default="https://api.openai.com/v1", max_length=512)
    ai_api_key: str = Field(default="", max_length=512)
    ai_model: str = Field(default="gpt-4.1-mini", max_length=128)
    plugin_repos: list[str] = Field(default_factory=list)
    notification_channels: list[dict[str, object]] = Field(default_factory=list)
    proxy_url: str = Field(default="", max_length=512)
    webhook_secret: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=512)
    pip_index_url: str = Field(default="", max_length=1024)
    github_token: str = Field(default="", max_length=512, pattern=r"^[^\s]*$")
    log_cleaner: dict[str, object] = Field(default_factory=dict)

class PluginConfigBody(BaseModel):
    values: dict[str, object]

class MarketInstallBody(BaseModel):
    plugin: dict[str, object]

class PluginActionBody(BaseModel):
    payload: dict[str, object] = Field(default_factory=dict)

class NotificationTestBody(BaseModel):
    channel: str = Field(min_length=1, max_length=64)
    text: str = Field(default="AWBotNest 通知渠道测试成功", max_length=2000)

class CookieBody(BaseModel):
    values: dict[str, str]
