from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .config import DATA_DIR


HISTORY_PATH = DATA_DIR / "cookie_sync_history.json"


def sync_history() -> list[dict[str, Any]]:
    try:
        values = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in values[-50:] if isinstance(item, dict)][::-1] if isinstance(values, list) else []


def record_sync(source: str, status: str, message: str, domain_count: int = 0,
                cookie_count: int = 0) -> None:
    values = list(reversed(sync_history()))
    values.append({"time": time.time(), "source": source, "status": status,
                   "message": message, "domain_count": domain_count,
                   "cookie_count": cookie_count})
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = HISTORY_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(values[-50:], ensure_ascii=False), encoding="utf-8")
    temporary.replace(HISTORY_PATH)


class CookieCloudError(RuntimeError):
    pass


def _decode(value: str) -> bytes:
    value = "".join(str(value or "").split())
    value += "=" * (-len(value) % 4)
    return base64.b64decode(value, validate=True)


def _unpad(value: bytes) -> bytes:
    worker = padding.PKCS7(128).unpadder()
    return worker.update(value) + worker.finalize()


def _decrypt_cbc(value: bytes, key: bytes, iv: bytes) -> dict[str, Any]:
    worker = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    result = json.loads(_unpad(worker.update(value) + worker.finalize()).decode("utf-8"))
    if not isinstance(result, dict):
        raise ValueError("CookieCloud payload is not an object")
    return result


def _evp(password: bytes, salt: bytes) -> tuple[bytes, bytes]:
    output = previous = b""
    while len(output) < 48:
        previous = hashlib.md5(previous + password + salt).digest()  # noqa: S324
        output += previous
    return output[:32], output[32:48]


def decrypt_payload(encrypted: str, uuid: str, password: str, mode: str = "auto") -> dict[str, Any]:
    try:
        raw = _decode(encrypted)
    except ValueError as exc:
        raise CookieCloudError("Cookie 数据编码无效") from exc
    digest = hashlib.md5(f"{uuid.strip()}-{password.strip()}".encode()).hexdigest()  # noqa: S324
    candidates = [mode, "aes-128-cbc-fixed", "legacy"]
    for candidate in dict.fromkeys(candidates):
        try:
            if candidate in {"auto", "aes-128-cbc-fixed"} and not raw.startswith(b"Salted__"):
                for iv in (digest[8:24].encode(), b"\0" * 16):
                    try:
                        return _decrypt_cbc(raw, digest[:16].encode(), iv)
                    except Exception:
                        pass
            if candidate in {"auto", "legacy"} and raw.startswith(b"Salted__"):
                key, iv = _evp(digest[:16].encode(), raw[8:16])
                return _decrypt_cbc(raw[16:], key, iv)
        except Exception:
            pass
    raise CookieCloudError("Cookie 数据解密失败，请检查 UUID、密码和加密算法")


def normalize_cookie_data(payload: Any) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise CookieCloudError("远程 CookieCloud 数据格式无效")
    source = payload.get("cookie_data", payload)
    if not isinstance(source, dict):
        raise CookieCloudError("远程 CookieCloud 缺少 Cookie 数据")
    if "cookie_data" not in payload and (not source or any(not isinstance(items, list) for items in source.values())):
        raise CookieCloudError("远程 CookieCloud 未返回 Cookie 数据")
    result: dict[str, list[dict[str, Any]]] = {}
    for source_domain, items in source.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or source_domain).lstrip(".").lower().strip()
            name = str(item.get("name") or "").strip()
            if domain and name:
                result.setdefault(domain, []).append({**item, "domain": str(item.get("domain") or source_domain),
                                                     "name": name, "value": str(item.get("value") or ""),
                                                     "path": str(item.get("path") or "/")})
    return result


async def pull(url: str, uuid: str, password: str, mode: str = "auto",
               proxy: str | None = None) -> dict[str, list[dict[str, Any]]]:
    endpoint = f"{url.rstrip('/')}/get/{uuid.strip()}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20, connect=8),
                                     follow_redirects=True, proxy=proxy) as client:
            response = await client.get(endpoint, headers={"Accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("cookie_data"), dict):
                decoded = payload
            else:
                encrypted = str((payload or {}).get("encrypted") or "") if isinstance(payload, dict) else ""
                try:
                    decoded = decrypt_payload(encrypted, uuid, password, mode)
                except CookieCloudError:
                    response = await client.post(endpoint, json={"password": password.strip()},
                                                 headers={"Accept": "application/json"})
                    response.raise_for_status()
                    decoded = response.json()
            return normalize_cookie_data(decoded)
    except httpx.TimeoutException as exc:
        raise CookieCloudError("连接远程 CookieCloud 超时") from exc
    except httpx.HTTPError as exc:
        raise CookieCloudError(f"远程 CookieCloud 请求失败：{exc}") from exc
