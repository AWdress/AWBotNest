from __future__ import annotations

import asyncio
import base64
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from ..config import DATA_DIR, Settings

from .http import HttpService

class AIService:
    def __init__(self, settings: Settings, http: HttpService) -> None:
        self.settings = settings
        self.http = http
        self._limit = 0
        self._semaphore = None

    async def _post(self, *args, **kwargs):
        try:
            limit = max(1, min(20, int(self.settings.ai_settings.get("max_concurrency", 3))))
        except (TypeError, ValueError):
            limit = 3
        if self._limit != limit:
            self._limit = limit
            self._semaphore = asyncio.Semaphore(limit)
        async with self._semaphore:
            return await self.http.post(*args, **kwargs)

    def _timeout(self, capability: str) -> int:
        config = self.settings.ai_settings if isinstance(self.settings.ai_settings, dict) else {}
        default = 300 if capability == "image" else 60
        maximum = 600 if capability == "image" else 300
        try:
            return max(5, min(int(config.get("image_timeout_seconds" if capability == "image" else "timeout_seconds", default)), maximum))
        except (TypeError, ValueError):
            return default

    def _resolve(self, capability: str, model: str = "", plugin_id: str = "") -> tuple[str, str, str]:
        config = self.settings.ai_settings if isinstance(self.settings.ai_settings, dict) else {}
        providers = {str(item.get("id")): item for item in config.get("providers", [])
                     if isinstance(item, dict) and item.get("enabled", True)}
        models = {str(item.get("id")): item for item in config.get("models", [])
                  if isinstance(item, dict) and item.get("enabled", True)}
        model_aliases = {str(item.get("alias")): item for item in models.values()
                         if str(item.get("alias") or "")}
        permissions = config.get("plugin_permissions", {})
        permission = permissions.get(plugin_id, {}) if plugin_id and isinstance(permissions, dict) else {}
        if permission:
            if permission.get("enabled") is False or capability not in permission.get("capabilities", []):
                raise PermissionError(f"插件未获准使用 {capability} AI 能力")
            model = model or str((permission.get("models") or {}).get(capability) or "")
        assignment = (config.get("capabilities") or {}).get(capability, {})
        model = model or str(assignment.get("default_model") or "")
        selected = models.get(model) or model_aliases.get(model)
        if selected:
            if capability not in selected.get("capabilities", []):
                raise RuntimeError(f"模型不支持 {capability} 能力")
            provider = providers.get(str(selected.get("provider_id") or ""))
            if not provider:
                raise RuntimeError("模型对应的 AI 服务不可用")
            key = str(provider.get("api_key") or "")
            if not key or key == "********":
                raise RuntimeError("尚未配置 AI 服务密钥")
            return (str(provider.get("base_url") or self.settings.ai_base_url).rstrip("/"), key,
                    str(selected.get("model") or selected.get("alias") or ""))
        if config.get("models") or config.get("providers"):
            raise RuntimeError(f"指定的 {capability} 模型不存在或未启用")
        if not self.settings.ai_api_key:
            raise RuntimeError(f"尚未配置 {capability} 模型")
        return self.settings.ai_base_url.rstrip("/"), self.settings.ai_api_key, model or self.settings.ai_model

    def _fallback(self, capability: str, plugin_id: str = "") -> str:
        config = self.settings.ai_settings if isinstance(self.settings.ai_settings, dict) else {}
        permission = (config.get("plugin_permissions") or {}).get(plugin_id, {}) if plugin_id else {}
        return str(((config.get("capabilities") or {}).get(capability) or {}).get("fallback_model") or "")

    async def chat(self, messages: list[dict[str, object]], *, model: str = "",
                   temperature: float | None = None, max_tokens: int | None = None,
                   plugin_id: str = "", _allow_fallback: bool = True) -> str:
        requested_model = model
        payload: dict[str, object] = {
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max(1, int(max_tokens))
        try:
            base_url, api_key, resolved_model = self._resolve("text", model, plugin_id)
            payload["model"] = resolved_model
            response = await self._post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
                timeout=self._timeout("text"),
            )
            response.raise_for_status()
            data = response.json()
            return str(data["choices"][0]["message"]["content"])
        except Exception:
            fallback = self._fallback("text", plugin_id) if not requested_model else ""
            if _allow_fallback and fallback:
                return await self.chat(messages, model=fallback, temperature=temperature,
                                       max_tokens=max_tokens, plugin_id=plugin_id, _allow_fallback=False)
            raise

    async def vision(self, prompt: str, image: str, *, model: str = "", plugin_id: str = "",
                     _allow_fallback: bool = True) -> str:
        requested_model = model
        payload = {"messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            *({"type": "image_url", "image_url": {"url": url}}
              for url in (image if isinstance(image, list) else [image])),
        ]}]}
        try:
            base_url, api_key, resolved_model = self._resolve("vision", model, plugin_id)
            payload["model"] = resolved_model
            response = await self._post(f"{base_url}/chat/completions",
                                            headers={"Authorization": f"Bearer {api_key}"},
                                            json=payload, timeout=self._timeout("vision"))
            response.raise_for_status()
            return str(response.json()["choices"][0]["message"]["content"])
        except Exception:
            fallback = self._fallback("vision", plugin_id) if not requested_model else ""
            if _allow_fallback and fallback:
                return await self.vision(prompt, image, model=fallback, plugin_id=plugin_id,
                                         _allow_fallback=False)
            raise

    async def generate_image(self, prompt: str, *, model: str = "", size: str = "1024x1024",
                             plugin_id: str = "", quality: str | None = None,
                             _allow_fallback: bool = True) -> dict[str, object]:
        requested_model = model
        try:
            base_url, api_key, resolved_model = self._resolve("image", model, plugin_id)
            response = await self._post(f"{base_url}/images/generations",
                                            headers={"Authorization": f"Bearer {api_key}"},
                                            json={"model": resolved_model, "prompt": prompt, "size": size,
                                                  "n": 1, **({"quality": quality} if quality else {})},
                                            timeout=self._timeout("image"))
            response.raise_for_status()
            data = response.json().get("data", [])
            if not data or not isinstance(data[0], dict):
                raise RuntimeError("AI 服务未返回图片")
            return dict(data[0])
        except Exception:
            fallback = self._fallback("image", plugin_id) if not requested_model else ""
            if _allow_fallback and fallback:
                return await self.generate_image(prompt, model=fallback, size=size,
                                                 plugin_id=plugin_id, quality=quality, _allow_fallback=False)
            raise

    def available_models(self, capability: str, plugin_id: str = "") -> list[dict[str, object]]:
        config = self.settings.ai_settings if isinstance(self.settings.ai_settings, dict) else {}
        result = []
        for item in config.get("models", []):
            if not isinstance(item, dict) or not item.get("enabled", True) or capability not in item.get("capabilities", []):
                continue
            try:
                self._resolve(capability, str(item.get("id") or ""), plugin_id)
            except (RuntimeError, PermissionError):
                continue
            result.append({"alias": str(item.get("alias") or item.get("model") or ""),
                           "name": str(item.get("name") or item.get("alias") or ""),
                           "capabilities": list(item.get("capabilities") or [])})
        return result


class PluginAI:
    """绑定插件身份的 AI 数据面，避免插件绕过管理员的能力授权。"""

    def __init__(self, service: AIService, plugin_id: str, data_dir: Path) -> None:
        self.service, self.plugin_id, self.data_dir = service, plugin_id, data_dir

    @property
    def available(self) -> bool:
        return self.is_available("text")

    def is_available(self, capability: str = "text") -> bool:
        try:
            self.service._resolve(capability, plugin_id=self.plugin_id)
            return True
        except (RuntimeError, PermissionError):
            return False

    def available_models(self, capability: str | None = None) -> list[dict[str, object]]:
        names = (capability,) if capability else ("text", "vision", "image")
        values: list[dict[str, object]] = []
        seen: set[str] = set()
        for name in names:
            for item in self.service.available_models(name, self.plugin_id):
                key = str(item.get("alias") or "")
                if key not in seen:
                    seen.add(key)
                    values.append(item)
        return values

    async def chat(self, prompt: str | list[dict[str, object]], *, system: str | None = None,
                   images: list[str] | None = None, model: str = "",
                   temperature: float | None = None, max_tokens: int | None = None) -> str:
        if isinstance(prompt, list):
            messages = prompt
            return await self.service.chat(messages, model=model, temperature=temperature,
                                           max_tokens=max_tokens, plugin_id=self.plugin_id)
        if images:
            return await self.vision(images, prompt, model=model, system=system)
        messages: list[dict[str, object]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": str(prompt)})
        return await self.service.chat(messages, model=model, temperature=temperature,
                                       max_tokens=max_tokens, plugin_id=self.plugin_id)

    @staticmethod
    def _image_url(image):
        if isinstance(image, str) and image.startswith(("http://", "https://", "data:")):
            return image
        data = bytes(image) if isinstance(image, (bytes, bytearray)) else Path(image).read_bytes()
        mime = "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif data.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            mime = "image/webp"
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")

    async def vision(self, image: str | Path | bytes | bytearray | list,
                     prompt: str = "请识别并说明图片内容。", *, model: str = "",
                     system: str | None = None) -> str:
        image_url = ([self._image_url(item) for item in image] if isinstance(image, list)
                     else self._image_url(image))
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        return await self.service.vision(full_prompt, image_url, model=model, plugin_id=self.plugin_id)

    async def generate_image(self, prompt: str, *, model: str = "", size: str = "1024x1024",
                             quality: str | None = None) -> Path:
        item = await self.service.generate_image(prompt, model=model, size=size,
                                                quality=quality, plugin_id=self.plugin_id)
        if item.get("b64_json"):
            data = base64.b64decode(str(item["b64_json"]), validate=True)
        elif item.get("url"):
            response = await self.service.http.get(str(item["url"]), timeout=180)
            response.raise_for_status()
            data = response.content
        else:
            raise RuntimeError("AI 服务未返回图片内容")
        if len(data) > 30 * 1024 * 1024:
            raise RuntimeError("生成的图片超过 30MB")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / f"ai_{uuid4().hex}.png"
        target.write_bytes(data)
        return target
