from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from .config import DATA_DIR, Settings


class HttpService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        timeout = kwargs.pop("timeout", 30)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True,
                                     proxy=self.settings.proxy_url or None) as client:
            return await client.request(method, url, **kwargs)

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def download(self, url: str, destination: str | Path, *,
                       max_bytes: int = 100 * 1024 * 1024) -> Path:
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".part")
        total = 0
        try:
            async with httpx.AsyncClient(timeout=120, follow_redirects=True,
                                         proxy=self.settings.proxy_url or None) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with temporary.open("wb") as output:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError("下载内容超过插件允许的大小")
                            output.write(chunk)
            temporary.replace(target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


class CookieService:
    def __init__(self) -> None:
        self.path = DATA_DIR / "cookies.json"
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    async def get(self, domain: str) -> dict[str, str]:
        async with self._lock:
            return dict(self._read().get(domain.lower().strip(), {}))

    async def set(self, domain: str, values: dict[str, str]) -> None:
        async with self._lock:
            data = self._read()
            data[domain.lower().strip()] = {str(key): str(value) for key, value in values.items()}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    async def replace(self, values: dict[str, dict[str, str]]) -> None:
        async with self._lock:
            clean = {
                str(domain).lower().strip(): {str(key): str(value) for key, value in cookies.items()}
                for domain, cookies in values.items() if str(domain).strip() and isinstance(cookies, dict)
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)

    async def domains(self) -> list[dict[str, object]]:
        async with self._lock:
            return [{"domain": key, "count": len(value)} for key, value in sorted(self._read().items())]

    async def delete(self, domain: str) -> bool:
        async with self._lock:
            data = self._read()
            removed = data.pop(domain.lower().strip(), None) is not None
            if removed:
                temp = self.path.with_suffix(".tmp")
                temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                temp.replace(self.path)
            return removed

    async def header(self, domain: str) -> str:
        values = await self.get(domain)
        return "; ".join(f"{key}={value}" for key, value in values.items())

    async def playwright(self, domain: str) -> list[dict[str, object]]:
        values = await self.get(domain)
        return [{"name": key, "value": value, "domain": domain, "path": "/"}
                for key, value in values.items()]


class BrowserService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, url: str, action: Callable[[Any], Any], *, headless: bool = True,
                  timeout: int = 60, cookies: list[dict[str, object]] | None = None,
                  user_agent: str = "") -> Any:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise RuntimeError("浏览器能力未安装，请安装 playwright") from exc
        async with async_playwright() as playwright:
            launch_args: dict[str, object] = {"headless": headless}
            if self.settings.proxy_url:
                launch_args["proxy"] = {"server": self.settings.proxy_url}
            browser = await playwright.chromium.launch(**launch_args)
            context_args = {"user_agent": user_agent} if user_agent else {}
            context = await browser.new_context(**context_args)
            try:
                if cookies:
                    await context.add_cookies(cookies)
                page = await context.new_page()
                page.set_default_timeout(timeout * 1000)
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                value = action(page)
                return await asyncio.wait_for(value, timeout=timeout) if isinstance(value, Awaitable) else value
            finally:
                await context.close()
                await browser.close()

    async def page_source(self, url: str, **kwargs: Any) -> str:
        return await self.run(url, lambda page: page.content(), **kwargs)


class AIService:
    def __init__(self, settings: Settings, http: HttpService) -> None:
        self.settings = settings
        self.http = http

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
        permissions = config.get("plugin_permissions", {})
        permission = permissions.get(plugin_id, {}) if plugin_id and isinstance(permissions, dict) else {}
        if permission:
            if permission.get("enabled") is False or capability not in permission.get("capabilities", []):
                raise PermissionError(f"插件未获准使用 {capability} AI 能力")
            model = model or str((permission.get("models") or {}).get(capability) or "")
        assignment = (config.get("capabilities") or {}).get(capability, {})
        model = model or str(assignment.get("default_model") or "")
        selected = models.get(model)
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
        if not self.settings.ai_api_key:
            raise RuntimeError(f"尚未配置 {capability} 模型")
        return self.settings.ai_base_url.rstrip("/"), self.settings.ai_api_key, model or self.settings.ai_model

    def _fallback(self, capability: str, plugin_id: str = "") -> str:
        config = self.settings.ai_settings if isinstance(self.settings.ai_settings, dict) else {}
        permission = (config.get("plugin_permissions") or {}).get(plugin_id, {}) if plugin_id else {}
        if permission and str((permission.get("models") or {}).get(capability) or ""):
            return ""
        return str(((config.get("capabilities") or {}).get(capability) or {}).get("fallback_model") or "")

    async def chat(self, messages: list[dict[str, object]], *, model: str = "",
                   temperature: float | None = None, max_tokens: int | None = None,
                   plugin_id: str = "", _allow_fallback: bool = True) -> str:
        requested_model = model
        base_url, api_key, resolved_model = self._resolve("text", model, plugin_id)
        payload: dict[str, object] = {
            "model": resolved_model,
            "messages": messages,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = max(1, int(max_tokens))
        try:
            response = await self.http.post(
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
        base_url, api_key, resolved_model = self._resolve("vision", model, plugin_id)
        payload = {"model": resolved_model, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": image}},
        ]}]}
        try:
            response = await self.http.post(f"{base_url}/chat/completions",
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
                             plugin_id: str = "", _allow_fallback: bool = True) -> dict[str, object]:
        requested_model = model
        base_url, api_key, resolved_model = self._resolve("image", model, plugin_id)
        try:
            response = await self.http.post(f"{base_url}/images/generations",
                                            headers={"Authorization": f"Bearer {api_key}"},
                                            json={"model": resolved_model, "prompt": prompt, "size": size,
                                                  "n": 1}, timeout=self._timeout("image"))
            response.raise_for_status()
            data = response.json().get("data", [])
            if not data or not isinstance(data[0], dict):
                raise RuntimeError("AI 服务未返回图片")
            return dict(data[0])
        except Exception:
            fallback = self._fallback("image", plugin_id) if not requested_model else ""
            if _allow_fallback and fallback:
                return await self.generate_image(prompt, model=fallback, size=size,
                                                 plugin_id=plugin_id, _allow_fallback=False)
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
            return await self.vision(images[0], prompt, model=model, system=system)
        messages: list[dict[str, object]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": str(prompt)})
        return await self.service.chat(messages, model=model, temperature=temperature,
                                       max_tokens=max_tokens, plugin_id=self.plugin_id)

    async def vision(self, image: str | Path | bytes | bytearray,
                     prompt: str = "请识别并说明图片内容。", *, model: str = "",
                     system: str | None = None) -> str:
        if isinstance(image, (bytes, bytearray)):
            image_url = "data:image/png;base64," + base64.b64encode(bytes(image)).decode("ascii")
        elif isinstance(image, Path) or (isinstance(image, str) and not image.startswith(("http://", "https://", "data:"))):
            data = Path(image).read_bytes()
            image_url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        else:
            image_url = str(image)
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        return await self.service.vision(full_prompt, image_url, model=model, plugin_id=self.plugin_id)

    async def generate_image(self, prompt: str, *, model: str = "", size: str = "1024x1024",
                             quality: str | None = None) -> Path:
        item = await self.service.generate_image(prompt, model=model, size=size, plugin_id=self.plugin_id)
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


class PlatformServices:
    def __init__(self, settings: Settings) -> None:
        self.http = HttpService(settings)
        self.cookies = CookieService()
        self.browser = BrowserService(settings)
        self.ai = AIService(settings, self.http)
