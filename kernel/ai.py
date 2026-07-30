"""
平台统一 AI 能力。

插件通过 ctx.ai 调用文字、识图和生图，不直接接触服务商密钥。
第一版面向 OpenAI 兼容接口，并把模型选择、超时、并发和结果落盘收敛到平台。
"""
from __future__ import annotations

import asyncio
import base64
import copy
import ipaddress
import logging
import mimetypes
import re
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

AI_MASK = "********"
AI_CAPABILITIES = ("text", "vision", "image")
MODEL_ALIAS_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_INPUT_IMAGE_BYTES = 20 * 1024 * 1024
MAX_OUTPUT_IMAGE_BYTES = 30 * 1024 * 1024
MAX_IMAGE_REDIRECTS = 5
IMAGE_SUFFIXES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

DEFAULT_AI_SETTINGS: dict[str, Any] = {
    "providers": [
        {
            "id": "default",
            "name": "默认 AI 服务",
            "enabled": True,
            "base_url": "https://api.openai.com/v1",
            "api_key": "",
        }
    ],
    "models": [],
    "capabilities": {
        capability: {"default_model": "", "fallback_model": ""}
        for capability in AI_CAPABILITIES
    },
    "timeout_seconds": 60,
    "image_timeout_seconds": 300,
    "max_concurrency": 3,
    "plugin_permissions": {},
}


class AIServiceError(RuntimeError):
    """平台 AI 配置或调用失败。"""


@dataclass(slots=True)
class AIStatus:
    total: int = 0
    succeeded: int = 0
    failed: int = 0
    active: int = 0
    last_error: str = ""


_status = AIStatus()
_semaphore: asyncio.Semaphore | None = None
_semaphore_size = 0
_semaphore_lock = threading.Lock()


def _config_module():
    import config.config as cfg

    return cfg


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _capability_list(value: Any, default: Iterable[str] = ()) -> list[str]:
    source = value if isinstance(value, (list, tuple, set)) else default
    return [item for item in source if item in AI_CAPABILITIES]


def _valid_provider_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(
        parsed.scheme in ("http", "https")
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def normalize_ai_settings(value: Any) -> dict[str, Any]:
    """清洗配置并补齐旧版本缺少的字段。"""
    raw = value if isinstance(value, dict) else {}
    result = copy.deepcopy(DEFAULT_AI_SETTINGS)
    result["timeout_seconds"] = _bounded_int(raw.get("timeout_seconds"), 60, 5, 300)
    result["image_timeout_seconds"] = _bounded_int(
        raw.get("image_timeout_seconds"), 300, 30, 300,
    )
    result["max_concurrency"] = _bounded_int(raw.get("max_concurrency"), 3, 1, 20)

    providers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw.get("providers") or []:
        if not isinstance(item, dict):
            continue
        provider_id = str(item.get("id") or "").strip()
        if not provider_id or provider_id in seen:
            continue
        seen.add(provider_id)
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        providers.append({
            "id": provider_id,
            "name": str(item.get("name") or provider_id).strip() or provider_id,
            "enabled": bool(item.get("enabled", True)),
            "base_url": base_url,
            "api_key": str(item.get("api_key") or ""),
        })
    if not providers:
        providers = copy.deepcopy(DEFAULT_AI_SETTINGS["providers"])
    result["providers"] = providers

    provider_ids = {item["id"] for item in providers}
    models: list[dict[str, Any]] = []
    model_ids: set[str] = set()
    model_aliases: set[str] = set()
    for item in raw.get("models") or []:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        provider_id = str(item.get("provider_id") or "").strip()
        if not model_id or model_id in model_ids or provider_id not in provider_ids:
            continue
        alias = str(item.get("alias") or model_id).strip() or model_id
        if alias in model_aliases:
            continue
        allowed = _capability_list(item.get("capabilities"))
        model_ids.add(model_id)
        model_aliases.add(alias)
        models.append({
            "id": model_id,
            "alias": alias,
            "name": str(item.get("name") or model_id).strip() or model_id,
            "enabled": bool(item.get("enabled", True)),
            "provider_id": provider_id,
            "model": str(item.get("model") or "").strip(),
            "capabilities": allowed,
        })
    result["models"] = models

    capabilities = raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {}
    result["capabilities"] = {}
    for capability in AI_CAPABILITIES:
        item = capabilities.get(capability) if isinstance(capabilities.get(capability), dict) else {}
        default_model = str(item.get("default_model") or "").strip()
        fallback_model = str(item.get("fallback_model") or "").strip()
        if default_model not in model_ids:
            default_model = ""
        if fallback_model not in model_ids or fallback_model == default_model:
            fallback_model = ""
        result["capabilities"][capability] = {
            "default_model": default_model,
            "fallback_model": fallback_model,
        }

    permissions = raw.get("plugin_permissions")
    result["plugin_permissions"] = {}
    if isinstance(permissions, dict):
        for plugin_id, permission in permissions.items():
            if not isinstance(permission, dict):
                continue
            allowed = _capability_list(
                permission.get("capabilities"), AI_CAPABILITIES,
            )
            assigned = permission.get("models")
            assigned = assigned if isinstance(assigned, dict) else {}
            assigned_models: dict[str, str] = {}
            for capability in AI_CAPABILITIES:
                model_id = str(assigned.get(capability) or "").strip()
                model = next((item for item in models if item["id"] == model_id), None)
                assigned_models[capability] = (
                    model_id
                    if model and capability in model["capabilities"]
                    else ""
                )
            result["plugin_permissions"][str(plugin_id)] = {
                "enabled": bool(permission.get("enabled", True)),
                "capabilities": allowed,
                "models": assigned_models,
            }
    return result


def load_ai_settings() -> dict[str, Any]:
    cfg = _config_module()
    return normalize_ai_settings(cfg.load().get("AI_SERVICES"))


def masked_ai_settings() -> dict[str, Any]:
    result = load_ai_settings()
    for provider in result["providers"]:
        if provider.get("api_key"):
            provider["api_key"] = AI_MASK
    return result


def save_ai_settings(value: Any) -> dict[str, Any]:
    """保存 AI 配置；打码密钥表示保留原值。"""
    raw = value if isinstance(value, dict) else {}
    aliases: set[str] = set()
    for index, item in enumerate(raw.get("models") or [], start=1):
        if not isinstance(item, dict):
            raise AIServiceError(f"第 {index} 个模型配置不正确")
        alias = str(item.get("alias") or "").strip()
        if not alias or not MODEL_ALIAS_RE.fullmatch(alias):
            raise AIServiceError(f"第 {index} 个模型的调用别名只能使用英文、数字、点、横线或下划线")
        if alias in aliases:
            raise AIServiceError(f"模型调用别名重复：{alias}")
        aliases.add(alias)
        if not str(item.get("model") or "").strip():
            raise AIServiceError(f"模型“{alias}”没有填写真实模型名")
        if not _capability_list(item.get("capabilities")):
            raise AIServiceError(f"模型“{alias}”至少要选择一种能力")
    incoming = normalize_ai_settings(value)
    for provider in incoming["providers"]:
        if provider["enabled"] and not _valid_provider_url(provider["base_url"]):
            raise AIServiceError(f"AI 服务“{provider['name']}”的服务地址不正确")
    current = load_ai_settings()
    current_keys = {item["id"]: item.get("api_key", "") for item in current["providers"]}
    for provider in incoming["providers"]:
        if provider.get("api_key") == AI_MASK:
            provider["api_key"] = current_keys.get(provider["id"], "")
    cfg = _config_module()
    cfg.save({"AI_SERVICES": incoming})
    return masked_ai_settings()


def _provider(settings: dict[str, Any], provider_id: str) -> dict[str, Any]:
    for item in settings["providers"]:
        if item["id"] == provider_id:
            if not item["enabled"]:
                raise AIServiceError(f"AI 服务“{item['name']}”未启用")
            if not item["base_url"]:
                raise AIServiceError(f"AI 服务“{item['name']}”未填写服务地址")
            if not _valid_provider_url(item["base_url"]):
                raise AIServiceError(f"AI 服务“{item['name']}”的服务地址不正确")
            return item
    raise AIServiceError("AI 服务不存在")


def _client(provider: dict[str, Any], timeout: int) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=provider.get("api_key") or "not-needed",
        base_url=provider["base_url"],
        timeout=float(timeout),
        max_retries=2,
    )


def _semaphore_for(size: int) -> asyncio.Semaphore:
    global _semaphore, _semaphore_size
    with _semaphore_lock:
        if _semaphore is None or _semaphore_size != size:
            _semaphore = asyncio.Semaphore(size)
            _semaphore_size = size
        return _semaphore


def _permission(settings: dict[str, Any], plugin_id: str, capability: str) -> None:
    permission = settings["plugin_permissions"].get(plugin_id)
    if not permission:
        return
    if not permission["enabled"]:
        raise AIServiceError("这个插件的 AI 权限已关闭")
    if capability not in permission["capabilities"]:
        raise AIServiceError(f"这个插件没有 {capability} 能力权限")


def _model_candidates(
    settings: dict[str, Any],
    capability: str,
    requested: str | None,
    preferred: str | None = None,
) -> list[tuple[dict[str, Any], str, str]]:
    target = settings["capabilities"][capability]
    selected_ids = [requested] if requested else [
        preferred, target["default_model"], target["fallback_model"],
    ]
    selected_ids = list(dict.fromkeys(item for item in selected_ids if item))
    if not selected_ids:
        raise AIServiceError(f"尚未配置 {capability} 模型")
    models_by_id = {item["id"]: item for item in settings["models"]}
    models_by_alias = {item["alias"]: item for item in settings["models"]}
    candidates: list[tuple[dict[str, Any], str, str]] = []
    provider_error: AIServiceError | None = None
    for model_id in selected_ids:
        item = models_by_id.get(model_id) or (models_by_alias.get(model_id) if requested else None)
        if not item or not item["enabled"] or not item["model"]:
            if requested:
                raise AIServiceError("插件指定的模型不存在或未启用")
            continue
        if capability not in item["capabilities"]:
            if requested:
                raise AIServiceError("插件指定的模型不支持当前能力")
            continue
        try:
            provider = _provider(settings, item["provider_id"])
        except AIServiceError as exc:
            if requested:
                raise
            provider_error = exc
            continue
        candidates.append((provider, item["model"], item["id"]))
    if not candidates:
        if provider_error:
            raise provider_error
        raise AIServiceError(f"没有可用的 {capability} 模型")
    return candidates


def _plugin_model(
    settings: dict[str, Any],
    plugin_id: str,
    capability: str,
) -> str:
    permission = settings["plugin_permissions"].get(plugin_id) or {}
    models = permission.get("models")
    if not isinstance(models, dict):
        return ""
    return str(models.get(capability) or "")


async def list_provider_models(provider: dict[str, Any], timeout: int = 30) -> list[str]:
    """从服务商 /models 读取模型名。"""
    normalized = {
        "id": str(provider.get("id") or "preview"),
        "name": str(provider.get("name") or "AI 服务"),
        "enabled": True,
        "base_url": str(provider.get("base_url") or "").strip().rstrip("/"),
        "api_key": str(provider.get("api_key") or ""),
    }
    if not normalized["base_url"]:
        raise AIServiceError("请先填写服务地址")
    if not _valid_provider_url(normalized["base_url"]):
        raise AIServiceError("AI 服务地址不正确")
    client: AsyncOpenAI | None = None
    try:
        client = _client(normalized, max(5, min(120, int(timeout or 30))))
        page = await client.models.list()
        return sorted({str(item.id) for item in page.data if getattr(item, "id", None)})
    except Exception as exc:  # noqa: BLE001
        raise AIServiceError(_friendly_error(exc)) from exc
    finally:
        await _close_client(client)


def _friendly_error(exc: Exception) -> str:
    message = str(exc).strip()
    if len(message) > 300:
        message = message[:300] + "…"
    return message or exc.__class__.__name__


def _mime_for_image(path: Path | None, data: bytes) -> str:
    if path:
        guessed = mimetypes.guess_type(path.name)[0]
        if guessed and guessed.startswith("image/"):
            return guessed
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise AIServiceError("不支持的图片格式，请使用 PNG、JPEG、GIF 或 WebP")


def _image_data_url(image: str | Path | bytes | bytearray) -> str:
    path: Path | None = None
    if isinstance(image, (bytes, bytearray)):
        data = bytes(image)
    else:
        text = str(image)
        parsed = urlparse(text)
        if parsed.scheme in ("http", "https"):
            raise AIServiceError("请先把网络图片下载到本地，再交给图片识别")
        path = Path(text)
        if not path.is_file():
            raise AIServiceError("要识别的图片不存在")
        data = path.read_bytes()
    if not data:
        raise AIServiceError("要识别的图片是空文件")
    if len(data) > MAX_INPUT_IMAGE_BYTES:
        raise AIServiceError("图片不能超过 20MB")
    mime = _mime_for_image(path, data)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _is_safe_image_url(value: str, provider_base_url: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    provider_host = (urlparse(provider_base_url).hostname or "").lower()
    if parsed.hostname.lower() == provider_host:
        return True
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
        return bool(addresses) and all(ipaddress.ip_address(item).is_global for item in addresses)
    except (OSError, ValueError):
        return False


async def _download_generated_image(
    url: str,
    target_stem: Path,
    timeout: int,
    provider_base_url: str,
) -> Path:
    current_url = url
    async with httpx.AsyncClient(
        timeout=float(timeout), follow_redirects=False, trust_env=True,
    ) as client:
        for _ in range(MAX_IMAGE_REDIRECTS + 1):
            if not _is_safe_image_url(current_url, provider_base_url):
                raise AIServiceError("生图服务返回了不安全的图片地址")
            async with client.stream("GET", current_url) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise AIServiceError("生图服务返回了无效的图片跳转地址")
                    current_url = urljoin(str(response.url), location)
                    continue
                response.raise_for_status()
                content_type = str(response.headers.get("content-type", "")).lower()
                if not content_type.startswith("image/"):
                    raise AIServiceError("生图服务返回的不是图片")
                try:
                    content_length = int(response.headers.get("content-length", "0") or 0)
                except ValueError:
                    content_length = 0
                if content_length > MAX_OUTPUT_IMAGE_BYTES:
                    raise AIServiceError("生成的图片超过 30MB")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_OUTPUT_IMAGE_BYTES:
                        raise AIServiceError("生成的图片超过 30MB")
                data = bytes(content)
                mime = _mime_for_image(None, data)
                target = target_stem.with_suffix(IMAGE_SUFFIXES[mime])
                target.write_bytes(data)
                return target
    raise AIServiceError("生图服务的图片跳转次数过多")


def _status_started() -> None:
    _status.total += 1
    _status.active += 1


def _status_finished(error: Exception | None = None) -> None:
    _status.active = max(0, _status.active - 1)
    if error is None:
        _status.succeeded += 1
        return
    _status.failed += 1
    _status.last_error = _friendly_error(error)


async def _close_client(client: AsyncOpenAI | None) -> None:
    if client is None:
        return
    try:
        await client.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("关闭 AI 客户端失败: %s", _friendly_error(exc))


class PluginAI:
    """绑定插件身份的数据面接口，作为 ctx.ai 暴露。"""

    def __init__(self, plugin_id: str, data_dir: Path):
        self.plugin_id = plugin_id
        self.data_dir = data_dir

    @property
    def available(self) -> bool:
        return self.is_available("text")

    def is_available(self, capability: str = "text") -> bool:
        try:
            if capability not in AI_CAPABILITIES:
                return False
            settings = load_ai_settings()
            _permission(settings, self.plugin_id, capability)
            assigned = _plugin_model(settings, self.plugin_id, capability)
            return bool(_model_candidates(settings, capability, None, assigned))
        except AIServiceError:
            return False

    def available_models(self, capability: str | None = None) -> list[dict[str, Any]]:
        """返回当前插件可选的模型别名，不包含服务地址、密钥或真实模型名。"""
        if capability is not None and capability not in AI_CAPABILITIES:
            return []
        settings = load_ai_settings()
        permission = settings["plugin_permissions"].get(self.plugin_id)
        if permission and not permission["enabled"]:
            return []
        allowed_capabilities = set(
            permission["capabilities"] if permission else AI_CAPABILITIES
        )
        def effective_default(name: str) -> str:
            assigned = _plugin_model(settings, self.plugin_id, name)
            try:
                candidates = _model_candidates(settings, name, None, assigned)
                return candidates[0][2] if candidates else ""
            except (AIServiceError, IndexError):
                return ""

        if capability is None:
            defaults = {
                model_id
                for name in allowed_capabilities
                if (model_id := effective_default(name))
            }
        else:
            default_id = effective_default(capability)
            defaults = {default_id} if default_id else set()
        usable_providers = {
            item["id"]
            for item in settings["providers"]
            if item["enabled"] and _valid_provider_url(item["base_url"])
        }
        result = []
        for item in settings["models"]:
            if (
                not item["enabled"]
                or not item["model"]
                or item["provider_id"] not in usable_providers
            ):
                continue
            allowed = [
                name for name in item["capabilities"]
                if name in allowed_capabilities and (capability is None or name == capability)
            ]
            if not allowed:
                continue
            result.append({
                "alias": item["alias"],
                "name": item["name"],
                "capabilities": allowed,
                "default": item["id"] in defaults,
            })
        return result

    async def chat(
        self,
        prompt: str,
        *,
        system: str | None = None,
        images: Iterable[str | Path | bytes | bytearray] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        settings = load_ai_settings()
        image_items = list(images or [])
        capability = "vision" if image_items else "text"
        _permission(settings, self.plugin_id, capability)
        assigned = _plugin_model(settings, self.plugin_id, capability)
        candidates = _model_candidates(
            settings, capability, None if assigned else model, assigned,
        )
        content: Any = str(prompt)
        if image_items:
            content = [{"type": "text", "text": str(prompt)}]
            content.extend(
                {"type": "image_url", "image_url": {"url": _image_data_url(item)}}
                for item in image_items
            )
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": str(system)})
        messages.append({"role": "user", "content": content})
        return await self._chat_models(settings, candidates, messages, temperature, max_tokens)

    async def vision(
        self,
        image: str | Path | bytes | bytearray,
        prompt: str = "请识别并说明图片内容。",
        *,
        model: str | None = None,
        system: str | None = None,
    ) -> str:
        settings = load_ai_settings()
        _permission(settings, self.plugin_id, "vision")
        assigned = _plugin_model(settings, self.plugin_id, "vision")
        candidates = _model_candidates(
            settings, "vision", None if assigned else model, assigned,
        )
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": str(system)})
        messages.append({
            "role": "user",
            "content": [
                {"type": "text", "text": str(prompt)},
                {"type": "image_url", "image_url": {"url": _image_data_url(image)}},
            ],
        })
        return await self._chat_models(settings, candidates, messages, None, None)

    async def _chat_models(
        self,
        settings: dict[str, Any],
        candidates: list[tuple[dict[str, Any], str, str]],
        messages: list[dict[str, Any]],
        temperature: float | None,
        max_tokens: int | None,
    ) -> str:
        timeout = settings["timeout_seconds"]
        semaphore = _semaphore_for(settings["max_concurrency"])
        last_error: Exception | None = None
        async with semaphore:
            for provider, selected, model_id in candidates:
                client: AsyncOpenAI | None = None
                attempt_error: Exception | None = None
                _status_started()
                try:
                    client = _client(provider, timeout)
                    kwargs: dict[str, Any] = {"model": selected, "messages": messages}
                    if temperature is not None:
                        kwargs["temperature"] = float(temperature)
                    if max_tokens is not None:
                        kwargs["max_tokens"] = int(max_tokens)
                    response = await client.chat.completions.create(**kwargs)
                    if not response.choices:
                        raise AIServiceError("模型没有返回有效响应")
                    text = response.choices[0].message.content or ""
                    if not text:
                        raise AIServiceError("模型没有返回文字内容")
                    return str(text)
                except Exception as exc:  # noqa: BLE001
                    attempt_error = exc
                    last_error = exc
                    logger.warning(
                        "AI 模型调用失败 [%s/%s/%s]: %s",
                        provider["id"], model_id, selected, _friendly_error(exc),
                    )
                finally:
                    _status_finished(attempt_error)
                    await _close_client(client)
        raise AIServiceError(_friendly_error(last_error or RuntimeError("AI 调用失败")))

    async def generate_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        size: str = "1024x1024",
        quality: str | None = None,
    ) -> Path:
        settings = load_ai_settings()
        _permission(settings, self.plugin_id, "image")
        assigned = _plugin_model(settings, self.plugin_id, "image")
        candidates = _model_candidates(
            settings, "image", None if assigned else model, assigned,
        )
        timeout = settings["image_timeout_seconds"]
        semaphore = _semaphore_for(settings["max_concurrency"])
        last_error: Exception | None = None
        self.data_dir.mkdir(parents=True, exist_ok=True)
        async with semaphore:
            for provider, selected, model_id in candidates:
                client: AsyncOpenAI | None = None
                attempt_error: Exception | None = None
                _status_started()
                try:
                    client = _client(provider, timeout)
                    kwargs: dict[str, Any] = {
                        "model": selected,
                        "prompt": str(prompt),
                        "size": size,
                        "n": 1,
                    }
                    if quality:
                        kwargs["quality"] = quality
                    response = await client.images.generate(**kwargs)
                    if not response.data:
                        raise AIServiceError("生图模型没有返回图片")
                    item = response.data[0]
                    target_stem = self.data_dir / f"ai_{uuid4().hex}"
                    if getattr(item, "b64_json", None):
                        data = base64.b64decode(item.b64_json, validate=True)
                        if len(data) > MAX_OUTPUT_IMAGE_BYTES:
                            raise AIServiceError("生成的图片超过 30MB")
                        mime = _mime_for_image(None, data)
                        target = target_stem.with_suffix(IMAGE_SUFFIXES[mime])
                        target.write_bytes(data)
                    elif getattr(item, "url", None):
                        target = await _download_generated_image(
                            str(item.url), target_stem, timeout, provider["base_url"],
                        )
                    else:
                        raise AIServiceError("生图服务没有返回可用图片")
                    return target
                except Exception as exc:  # noqa: BLE001
                    attempt_error = exc
                    last_error = exc
                    logger.warning(
                        "AI 生图失败 [%s/%s/%s]: %s",
                        provider["id"], model_id, selected, _friendly_error(exc),
                    )
                finally:
                    _status_finished(attempt_error)
                    await _close_client(client)
        raise AIServiceError(_friendly_error(last_error or RuntimeError("AI 生图失败")))


def status_snapshot() -> dict[str, Any]:
    return {
        "total": _status.total,
        "succeeded": _status.succeeded,
        "failed": _status.failed,
        "active": _status.active,
        "last_error": _status.last_error,
    }
