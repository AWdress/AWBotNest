"""平台 Cookie 仓库与 CookieCloud 兼容能力。

浏览器扩展通过 CookieCloud 协议读写加密快照；插件只通过 ``ctx.cookies``
读取自己在元数据中声明的域名，不接触同步凭据和原始快照。
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


DATA_DIR = Path("data") / "cookiecloud"
SETTINGS_FILE = DATA_DIR / "settings.bin"
KEY_FILE = DATA_DIR / ".key"
SNAPSHOT_FILE = DATA_DIR / "snapshot.json"
HISTORY_FILE = DATA_DIR / "history.json"
MASK = "********"
ALLOWED_CRYPTO_TYPES = {"legacy", "aes-128-cbc-fixed"}
MAX_ENCRYPTED_BYTES = 16 * 1024 * 1024
MAX_COOKIE_DOMAINS = 64
MAX_SYNC_HISTORY = 50

_LOCK = threading.RLock()
_CACHE_FINGERPRINT: tuple[int, int] | None = None
_CACHE_DATA: dict[str, Any] | None = None
_LAST_ERROR = ""
_SYNC_REQUEST_COOLDOWN = 30 * 60
_SYNC_REQUESTED_AT: dict[tuple[str, str], float] = {}


class CookieServiceError(RuntimeError):
    """Cookie 同步配置、数据或解密错误。"""


class CookiePermissionError(PermissionError):
    """插件申请了未声明的 Cookie 域名。"""


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _fernet() -> Fernet:
    with _LOCK:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not KEY_FILE.exists():
            _atomic_write(KEY_FILE, Fernet.generate_key())
        try:
            return Fernet(KEY_FILE.read_bytes().strip())
        except (OSError, ValueError) as exc:
            raise CookieServiceError("Cookie 同步密钥文件不可用") from exc


def default_settings() -> dict[str, Any]:
    return {
        "enabled": False,
        "uuid": "",
        "password": "",
        "crypto_type": "aes-128-cbc-fixed",
        "remote_enabled": False,
        "remote_url": "",
        "remote_uuid": "",
        "remote_password": "",
        "remote_crypto_type": "auto",
        "remote_interval_minutes": 60,
        "remote_domains": [],
    }


def load_settings() -> dict[str, Any]:
    settings = default_settings()
    if not SETTINGS_FILE.exists():
        return settings
    try:
        payload = _fernet().decrypt(SETTINGS_FILE.read_bytes())
        stored = json.loads(payload.decode("utf-8"))
    except (InvalidToken, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CookieServiceError("Cookie 同步设置损坏或无法解密") from exc
    if not isinstance(stored, dict):
        raise CookieServiceError("Cookie 同步设置格式无效")
    settings.update({key: stored.get(key, value) for key, value in settings.items()})
    settings["enabled"] = bool(settings["enabled"])
    settings["uuid"] = _normalize_uuid(settings["uuid"])
    settings["password"] = str(settings["password"] or "")[:256]
    if settings["crypto_type"] not in ALLOWED_CRYPTO_TYPES:
        settings["crypto_type"] = "aes-128-cbc-fixed"
    settings["remote_enabled"] = bool(settings["remote_enabled"])
    settings["remote_url"] = _normalize_remote_url(settings["remote_url"])
    settings["remote_uuid"] = _normalize_uuid(settings["remote_uuid"])
    settings["remote_password"] = str(settings["remote_password"] or "")[:256]
    if settings["remote_crypto_type"] not in {*ALLOWED_CRYPTO_TYPES, "auto"}:
        settings["remote_crypto_type"] = "auto"
    settings["remote_interval_minutes"] = _normalize_remote_interval(
        settings["remote_interval_minutes"]
    )
    settings["remote_domains"] = normalize_declared_domains(settings["remote_domains"])
    return settings


def save_settings(value: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        current = load_settings()
        password = str(value.get("password", "") or "")
        if password == MASK:
            password = current["password"]
        remote_password = str(value.get("remote_password", "") or "")
        if remote_password == MASK:
            remote_password = current["remote_password"]
        uuid = _normalize_uuid(value.get("uuid"))
        remote_uuid = _normalize_uuid(value.get("remote_uuid"))
        crypto_type = str(value.get("crypto_type") or "aes-128-cbc-fixed")
        if crypto_type not in ALLOWED_CRYPTO_TYPES:
            raise CookieServiceError("不支持的 CookieCloud 加密算法")
        remote_crypto_type = str(value.get("remote_crypto_type") or "auto")
        if remote_crypto_type not in {*ALLOWED_CRYPTO_TYPES, "auto"}:
            raise CookieServiceError("不支持的远程 CookieCloud 加密算法")
        if len(password) > 256 or len(remote_password) > 256:
            raise CookieServiceError("端到端加密密码不能超过 256 个字符")
        settings = {
            "enabled": bool(value.get("enabled")),
            "uuid": uuid,
            "password": password,
            "crypto_type": crypto_type,
            "remote_enabled": bool(value.get("remote_enabled")),
            "remote_url": _normalize_remote_url(value.get("remote_url")),
            "remote_uuid": remote_uuid,
            "remote_password": remote_password,
            "remote_crypto_type": remote_crypto_type,
            "remote_interval_minutes": _normalize_remote_interval(
                value.get("remote_interval_minutes")
            ),
            "remote_domains": normalize_declared_domains(value.get("remote_domains")),
        }
        if settings["enabled"] and (not uuid or not password):
            raise CookieServiceError("启用 Cookie 同步前请生成 UUID 和端到端加密密码")
        if not settings["enabled"]:
            settings["remote_enabled"] = False
        if settings["remote_enabled"]:
            if not all((settings["remote_url"], remote_uuid, remote_password)):
                raise CookieServiceError("启用远程同步前请填写服务器地址、UUID 和加密密码")
        encoded = json.dumps(settings, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        _atomic_write(SETTINGS_FILE, _fernet().encrypt(encoded))
        credentials_changed = any(
            current.get(key) != settings.get(key)
            for key in ("uuid", "password", "crypto_type")
        )
        if credentials_changed:
            clear_snapshot()
        else:
            _invalidate_cache()
        return settings


def generate_credentials() -> dict[str, str]:
    return {
        "uuid": secrets.token_urlsafe(18).replace("-", "").replace("_", "")[:24],
        "password": secrets.token_urlsafe(24),
    }


def masked_settings() -> dict[str, Any]:
    settings = load_settings()
    settings["password"] = MASK if settings["password"] else ""
    settings["remote_password"] = MASK if settings["remote_password"] else ""
    return settings


def _normalize_uuid(value: Any) -> str:
    uuid = str(value or "").strip()
    if not uuid:
        return ""
    if len(uuid) > 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", uuid):
        raise CookieServiceError("UUID 只允许字母、数字、下划线和短横线")
    return uuid


def _normalize_remote_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        return ""
    if len(url) > 2048:
        raise CookieServiceError("远程 CookieCloud 地址过长")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise CookieServiceError("远程 CookieCloud 地址格式不正确") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise CookieServiceError("远程 CookieCloud 地址必须使用 http 或 https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CookieServiceError("远程 CookieCloud 地址不能包含账号、密码、查询参数或片段")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme}://{netloc}{path}"


def _normalize_remote_interval(value: Any) -> int:
    try:
        interval = int(value or 60)
    except (TypeError, ValueError) as exc:
        raise CookieServiceError("远程同步间隔必须是整数") from exc
    return max(5, min(interval, 10080))


def _pkcs7_unpad(value: bytes) -> bytes:
    unpadder = padding.PKCS7(128).unpadder()
    return unpadder.update(value) + unpadder.finalize()


def _evp_bytes_to_key(
    password: bytes, salt: bytes, key_length: int = 32, iv_length: int = 16,
) -> tuple[bytes, bytes]:
    output = b""
    previous = b""
    while len(output) < key_length + iv_length:
        # CookieCloud legacy payloads use OpenSSL's historical MD5 key derivation.
        previous = hashlib.md5(previous + password + salt).digest()  # noqa: S324
        output += previous
    return output[:key_length], output[key_length:key_length + iv_length]


def decrypt_payload(encrypted: str, uuid: str, password: str, crypto_type: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(encrypted, validate=True)
        passphrase = hashlib.md5(f"{uuid}-{password}".encode()).hexdigest()[:16]  # noqa: S324
        if crypto_type == "aes-128-cbc-fixed":
            key = passphrase.encode("utf-8")
            iv = b"\0" * 16
            ciphertext = raw
        elif crypto_type == "legacy":
            if len(raw) < 16 or raw[:8] != b"Salted__":
                raise ValueError("legacy payload is missing the OpenSSL salt")
            key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), raw[8:16])
            ciphertext = raw[16:]
        else:
            raise ValueError("unsupported crypto type")
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plaintext = _pkcs7_unpad(decryptor.update(ciphertext) + decryptor.finalize())
        result = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CookieServiceError("Cookie 数据解密失败，请检查 UUID、密码和加密算法") from exc
    if not isinstance(result, dict) or not isinstance(result.get("cookie_data", {}), dict):
        raise CookieServiceError("CookieCloud 数据格式无效")
    return result


def encrypt_payload(data: dict[str, Any], uuid: str, password: str, crypto_type: str) -> str:
    plaintext = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    passphrase = hashlib.md5(f"{uuid}-{password}".encode()).hexdigest()[:16]  # noqa: S324
    if crypto_type == "aes-128-cbc-fixed":
        key = passphrase.encode("utf-8")
        iv = b"\0" * 16
        prefix = b""
    elif crypto_type == "legacy":
        salt = secrets.token_bytes(8)
        key, iv = _evp_bytes_to_key(passphrase.encode("utf-8"), salt)
        prefix = b"Salted__" + salt
    else:
        raise CookieServiceError("不支持的 CookieCloud 加密算法")
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(prefix + ciphertext).decode("ascii")


def save_snapshot(
    encrypted: str,
    crypto_type: str,
    *,
    history_message: str = "浏览器 Cookie 已同步",
) -> dict[str, Any]:
    global _LAST_ERROR
    if crypto_type not in ALLOWED_CRYPTO_TYPES:
        raise CookieServiceError("不支持的 CookieCloud 加密算法")
    if not isinstance(encrypted, str) or not encrypted:
        raise CookieServiceError("缺少加密 Cookie 数据")
    if len(encrypted.encode("utf-8")) > MAX_ENCRYPTED_BYTES:
        raise CookieServiceError("Cookie 数据超过平台允许的大小")
    with _LOCK:
        settings = load_settings()
        if not settings["enabled"]:
            raise CookieServiceError("Cookie 同步尚未启用")
        decoded = decrypt_payload(encrypted, settings["uuid"], settings["password"], crypto_type)
        payload = {
            "encrypted": encrypted,
            "crypto_type": crypto_type,
            "saved_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        _atomic_write(SNAPSHOT_FILE, json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        _invalidate_cache()
        _LAST_ERROR = ""
        _set_cache(decoded)
        status = snapshot_status(decoded)
        _append_sync_history(
            "success",
            history_message,
            cookie_count=status["cookie_count"],
            domain_count=status["domain_count"],
        )
    return status


def load_snapshot() -> dict[str, Any]:
    if not SNAPSHOT_FILE.exists():
        raise CookieServiceError("浏览器还没有同步 Cookie")
    try:
        data = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CookieServiceError("Cookie 快照损坏") from exc
    if not isinstance(data, dict) or not data.get("encrypted"):
        raise CookieServiceError("Cookie 快照格式无效")
    return data


def encrypted_snapshot(uuid: str) -> dict[str, str]:
    with _LOCK:
        settings = load_settings()
        if not settings["enabled"] or not secrets.compare_digest(uuid, settings["uuid"]):
            raise FileNotFoundError("Cookie 同步配置不存在")
        snapshot = load_snapshot()
        return {
            "encrypted": str(snapshot["encrypted"]),
            "crypto_type": str(snapshot.get("crypto_type") or "legacy"),
        }


def clear_snapshot() -> None:
    global _LAST_ERROR
    with _LOCK:
        try:
            SNAPSHOT_FILE.unlink()
        except FileNotFoundError:
            pass
        _invalidate_cache()
        _LAST_ERROR = ""


def _invalidate_cache() -> None:
    global _CACHE_DATA, _CACHE_FINGERPRINT
    _CACHE_DATA = None
    _CACHE_FINGERPRINT = None


def _set_cache(data: dict[str, Any]) -> None:
    global _CACHE_DATA, _CACHE_FINGERPRINT
    _CACHE_DATA = deepcopy(data)
    try:
        stat = SNAPSHOT_FILE.stat()
        _CACHE_FINGERPRINT = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _CACHE_FINGERPRINT = None


def _decoded_snapshot_ref() -> dict[str, Any]:
    with _LOCK:
        snapshot = load_snapshot()
        stat = SNAPSHOT_FILE.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size)
        if _CACHE_DATA is not None and _CACHE_FINGERPRINT == fingerprint:
            return _CACHE_DATA
        settings = load_settings()
        decoded = decrypt_payload(
            str(snapshot["encrypted"]), settings["uuid"], settings["password"],
            str(snapshot.get("crypto_type") or "legacy"),
        )
        _set_cache(decoded)
        return _CACHE_DATA or {}


def decoded_snapshot() -> dict[str, Any]:
    return deepcopy(_decoded_snapshot_ref())


def _cookie_count(data: dict[str, Any]) -> tuple[int, int]:
    domains = data.get("cookie_data") or {}
    count = sum(len(items) for items in domains.values() if isinstance(items, list))
    return count, sum(1 for items in domains.values() if isinstance(items, list) and items)


def snapshot_status(decoded: dict[str, Any] | None = None) -> dict[str, Any]:
    settings = load_settings()
    status = {
        "configured": bool(settings["uuid"] and settings["password"]),
        "has_data": SNAPSHOT_FILE.exists(),
        "last_sync": "",
        "cookie_count": 0,
        "domain_count": 0,
        "last_error": _LAST_ERROR,
    }
    if SNAPSHOT_FILE.exists():
        try:
            snapshot = load_snapshot()
            status["last_sync"] = str(snapshot.get("saved_at") or "")
            decoded = decoded or decoded_snapshot()
            status["cookie_count"], status["domain_count"] = _cookie_count(decoded)
        except (CookieServiceError, OSError) as exc:
            status["last_error"] = str(exc)
    return status


def sync_history(limit: int = 20) -> list[dict[str, Any]]:
    """读取最近的同步结果，只保存统计信息，不记录 Cookie 内容。"""
    safe_limit = max(1, min(int(limit or 20), MAX_SYNC_HISTORY))
    with _LOCK:
        if not HISTORY_FILE.exists():
            return []
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(history, list):
            return []
        result = []
        for item in history[-safe_limit:]:
            if not isinstance(item, dict):
                continue
            try:
                cookie_count = max(0, int(item.get("cookie_count") or 0))
                domain_count = max(0, int(item.get("domain_count") or 0))
            except (TypeError, ValueError):
                cookie_count = 0
                domain_count = 0
            result.append({
                "time": str(item.get("time") or ""),
                "status": "success" if item.get("status") == "success" else "error",
                "message": str(item.get("message") or "")[:300],
                "cookie_count": cookie_count,
                "domain_count": domain_count,
            })
        result.reverse()
        return result


def _append_sync_history(
    status: str,
    message: str,
    *,
    cookie_count: int = 0,
    domain_count: int = 0,
) -> None:
    entry = {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "success" if status == "success" else "error",
        "message": str(message or "")[:300],
        "cookie_count": max(0, int(cookie_count or 0)),
        "domain_count": max(0, int(domain_count or 0)),
    }
    with _LOCK:
        history = list(reversed(sync_history(MAX_SYNC_HISTORY)))
        history.append(entry)
        try:
            _atomic_write(
                HISTORY_FILE,
                json.dumps(history[-MAX_SYNC_HISTORY:], ensure_ascii=False).encode("utf-8"),
            )
        except OSError:
            # 同步记录是辅助信息，写入失败不能让已经成功的 Cookie 上传失败。
            return


def record_error(message: str) -> None:
    global _LAST_ERROR
    _LAST_ERROR = str(message or "")[:300]
    _append_sync_history("error", _LAST_ERROR or "浏览器 Cookie 同步失败")


def _detect_crypto_type(encrypted: str, configured: str) -> str:
    if configured in ALLOWED_CRYPTO_TYPES:
        return configured
    try:
        raw = base64.b64decode(encrypted, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise CookieServiceError("远程 CookieCloud 返回的加密数据无效") from exc
    return "legacy" if raw.startswith(b"Salted__") else "aes-128-cbc-fixed"


def _filter_remote_data(data: dict[str, Any], domains: list[str]) -> dict[str, Any]:
    if not domains:
        return data
    filtered = deepcopy(data)
    for key in ("cookie_data", "local_storage_data"):
        source = data.get(key) or {}
        if not isinstance(source, dict):
            filtered[key] = {}
            continue
        filtered[key] = {
            source_domain: values
            for source_domain, values in source.items()
            if (domain := normalize_domain(source_domain))
            and _domain_is_allowed(domain, domains)
        }
    return filtered


def _proxy_for_remote(url: str) -> str | None:
    """本机和局域网 CookieCloud 直连，其余地址遵循平台代理。"""
    hostname = urlsplit(url).hostname or ""
    if hostname.casefold() == "localhost":
        return None
    try:
        if ipaddress.ip_address(hostname).is_private:
            return None
    except ValueError:
        pass
    from libs.proxy import proxy_url

    return proxy_url()


async def pull_remote_snapshot() -> dict[str, Any]:
    """从外部 CookieCloud 拉取数据，转换为平台自己的加密快照。"""
    settings = load_settings()
    if not settings["enabled"] or not settings["remote_enabled"]:
        raise CookieServiceError("远程 CookieCloud 同步尚未启用")

    remote_url = settings["remote_url"]
    remote_uuid = settings["remote_uuid"]
    endpoint = f"{remote_url}/get/{remote_uuid}"
    try:
        timeout = httpx.Timeout(20, connect=8)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            proxy=_proxy_for_remote(remote_url),
            trust_env=False,
        ) as client:
            async with client.stream(
                "GET", endpoint, headers={"Accept": "application/json"}
            ) as response:
                if response.status_code != 200:
                    raise CookieServiceError(
                        f"远程 CookieCloud 返回 HTTP {response.status_code}"
                    )
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_ENCRYPTED_BYTES + 1024 * 1024:
                        raise CookieServiceError("远程 CookieCloud 返回的数据过大")
        try:
            payload = json.loads(bytes(content))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CookieServiceError("远程 CookieCloud 返回的不是有效数据") from exc
        if not isinstance(payload, dict) or not payload.get("encrypted"):
            raise CookieServiceError("远程 CookieCloud 中还没有可用数据")

        encrypted = str(payload["encrypted"])
        remote_crypto_type = _detect_crypto_type(
            encrypted,
            settings["remote_crypto_type"],
        )
        decoded = decrypt_payload(
            encrypted,
            remote_uuid,
            settings["remote_password"],
            remote_crypto_type,
        )
        decoded = _filter_remote_data(decoded, settings["remote_domains"])
        local_encrypted = encrypt_payload(
            decoded,
            settings["uuid"],
            settings["password"],
            settings["crypto_type"],
        )
        return save_snapshot(
            local_encrypted,
            settings["crypto_type"],
            history_message="已从远程 CookieCloud 同步",
        )
    except httpx.TimeoutException as exc:
        error = CookieServiceError("连接远程 CookieCloud 超时")
        record_error(f"远程 CookieCloud 同步失败：{error}")
        raise error from exc
    except httpx.HTTPError as exc:
        error = CookieServiceError("无法连接远程 CookieCloud")
        record_error(f"远程 CookieCloud 同步失败：{error}")
        raise error from exc
    except CookieServiceError as exc:
        record_error(f"远程 CookieCloud 同步失败：{exc}")
        raise


def normalize_domain(value: Any, allow_wildcard: bool = False) -> str:
    source = str(value or "").strip().lower()
    wildcard = allow_wildcard and source.startswith("*.")
    if wildcard:
        source = source[2:]
    if "://" in source:
        source = urlsplit(source).hostname or ""
    else:
        source = source.split("/", 1)[0].split(":", 1)[0]
    source = source.strip(".")
    if not source or len(source) > 253:
        return ""
    try:
        source = source.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    labels = source.split(".")
    if any(
        not label or len(label) > 63 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", label)
        for label in labels
    ):
        return ""
    return f"*.{source}" if wildcard else source


def normalize_declared_domains(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[:MAX_COOKIE_DOMAINS]:
        domain = normalize_domain(value, allow_wildcard=True)
        if domain and domain not in result:
            result.append(domain)
    return result


def _domain_is_allowed(requested: str, declared: Iterable[str]) -> bool:
    for pattern in declared:
        if pattern.startswith("*."):
            base = pattern[2:]
            if requested == base or requested.endswith(f".{base}"):
                return True
        elif requested == pattern:
            return True
    return False


def _cookie_applies(
    cookie: dict[str, Any], domain: str, path: str, now: float, source_domain: str,
) -> bool:
    cookie_domain = normalize_domain(cookie.get("domain") or source_domain)
    if not cookie_domain:
        return False
    host_only = bool(cookie.get("hostOnly"))
    if host_only:
        if domain != cookie_domain:
            return False
    elif domain != cookie_domain and not domain.endswith(f".{cookie_domain}"):
        return False
    cookie_path = str(cookie.get("path") or "/")
    path_prefix = cookie_path.rstrip("/") or "/"
    if not path.startswith(path_prefix):
        return False
    if path_prefix != "/" and len(path) > len(path_prefix) and path[len(path_prefix)] != "/":
        return False
    expires = cookie.get("expirationDate", cookie.get("expires"))
    try:
        if expires is not None and float(expires) > 0 and float(expires) <= now:
            return False
    except (TypeError, ValueError):
        pass
    return bool(str(cookie.get("name") or ""))


class PluginCookies:
    """绑定插件身份的 Cookie 只读接口。"""

    def __init__(
        self,
        plugin_id: str,
        request_notifier: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.plugin_id = plugin_id
        self._request_notifier = request_notifier

    @property
    def domains(self) -> list[str]:
        from kernel.registry import registry

        meta = registry.get_meta(self.plugin_id)
        return list(meta.cookie_domains) if meta else []

    @property
    def available(self) -> bool:
        try:
            settings = load_settings()
            return bool(settings["enabled"] and self.domains and SNAPSHOT_FILE.exists())
        except CookieServiceError:
            return False

    def _authorize(self, domain: Any) -> str:
        normalized = self._authorize_domain(domain)
        if not load_settings()["enabled"]:
            raise CookieServiceError("平台 Cookie 同步尚未启用")
        return normalized

    async def get(
        self, domain: str, *, path: str = "/", names: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        requested = self._authorize(domain)
        if names is None:
            wanted = None
        elif isinstance(names, str):
            wanted = {names}
        else:
            wanted = {str(name) for name in names}
        path = path if path.startswith("/") else f"/{path}"
        data = _decoded_snapshot_ref()
        now = datetime.now().timestamp()
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for group_domain, items in (data.get("cookie_data") or {}).items():
            if not isinstance(items, list):
                continue
            source_domain = normalize_domain(group_domain)
            if not source_domain:
                continue
            for item in items:
                if (not isinstance(item, dict)
                        or not _cookie_applies(item, requested, path, now, source_domain)):
                    continue
                name = str(item.get("name") or "")
                if wanted is not None and name not in wanted:
                    continue
                identity = (name, str(item.get("domain") or ""), str(item.get("path") or "/"))
                if identity in seen:
                    continue
                seen.add(identity)
                matched = deepcopy(item)
                matched.setdefault("domain", source_domain)
                result.append(matched)
        result.sort(key=lambda item: len(str(item.get("path") or "/")), reverse=True)
        return result

    async def header(
        self, domain: str, *, path: str = "/", names: Iterable[str] | None = None,
    ) -> str:
        cookies = await self.get(domain, path=path, names=names)
        values: dict[str, str] = {}
        for cookie in cookies:
            values.setdefault(str(cookie["name"]), str(cookie.get("value") or ""))
        return "; ".join(f"{name}={value}" for name, value in values.items())

    async def playwright(self, domain: str, *, path: str = "/") -> list[dict[str, Any]]:
        cookies = await self.get(domain, path=path)
        allowed = {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite"}
        result = []
        for cookie in cookies:
            item = {key: value for key, value in cookie.items() if key in allowed}
            if "expirationDate" in cookie and "expires" not in item:
                item["expires"] = cookie["expirationDate"]
            same_site = str(item.get("sameSite") or "").lower()
            if same_site in {"strict", "lax", "none"}:
                item["sameSite"] = same_site.title()
            else:
                item.pop("sameSite", None)
            result.append(item)
        return result

    async def request_sync(self, domain: str) -> bool:
        """Cookie 不可用时提醒管理员同步；已有可用 Cookie 时返回 True。"""
        requested = self._authorize_domain(domain)
        try:
            if self._has_cookie_for_domain(requested):
                return True
        except CookieServiceError:
            pass

        key = (self.plugin_id, requested)
        now = time.monotonic()
        with _LOCK:
            last_requested = _SYNC_REQUESTED_AT.get(key, 0.0)
            if now - last_requested < _SYNC_REQUEST_COOLDOWN:
                return False
            _SYNC_REQUESTED_AT[key] = now
            if len(_SYNC_REQUESTED_AT) > 1024:
                expired = [
                    item for item, requested_at in _SYNC_REQUESTED_AT.items()
                    if now - requested_at >= _SYNC_REQUEST_COOLDOWN
                ]
                for item in expired:
                    _SYNC_REQUESTED_AT.pop(item, None)

        if self._request_notifier is not None:
            try:
                await self._request_notifier(requested)
            except Exception:
                with _LOCK:
                    _SYNC_REQUESTED_AT.pop(key, None)
                raise
        return False

    def _has_cookie_for_domain(self, requested: str) -> bool:
        if not load_settings()["enabled"]:
            return False
        data = _decoded_snapshot_ref()
        now = datetime.now().timestamp()
        for group_domain, items in (data.get("cookie_data") or {}).items():
            if not isinstance(items, list):
                continue
            source_domain = normalize_domain(group_domain)
            if not source_domain:
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                cookie_path = str(item.get("path") or "/")
                if _cookie_applies(item, requested, cookie_path, now, source_domain):
                    return True
        return False

    def _authorize_domain(self, domain: Any) -> str:
        normalized = normalize_domain(domain)
        if not normalized:
            raise ValueError("Cookie 域名格式无效")
        if not _domain_is_allowed(normalized, self.domains):
            raise CookiePermissionError(f"插件未声明 Cookie 域名权限: {normalized}")
        return normalized
