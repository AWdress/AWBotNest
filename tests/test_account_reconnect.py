from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest

import config.config as config_module
import kernel.account_manager as account_manager_module
from kernel.account_manager import AccountManager, _pause_account


class FakeSession:
    def __init__(self, started: bool = False) -> None:
        self.is_started = asyncio.Event()
        if started:
            self.is_started.set()
        self.restart_calls = 0

    async def restart(self) -> None:
        self.restart_calls += 1
        self.is_started.set()


class FakeClient:
    def __init__(self, name: str, *, connected: bool, started: bool) -> None:
        self.name = name
        self.is_connected = connected
        self.session = FakeSession(started)
        self.start_calls = 0
        self.stop_calls = 0

    async def start(self) -> None:
        self.start_calls += 1
        self.is_connected = True
        self.session.is_started.set()

    async def stop(self) -> None:
        self.stop_calls += 1
        self.is_connected = False
        self.session.is_started.clear()


@pytest.fixture
def account_manager(tmp_path, monkeypatch) -> AccountManager:
    accounts = AccountManager(str(tmp_path))

    async def no_missing_bots(_now: float) -> bool:
        return False

    monkeypatch.setattr(accounts, "_recover_missing_bots", no_missing_bots)
    accounts._RECONNECT_GRACE_PERIOD = 0
    return accounts


def test_connection_ready_checks_the_underlying_session() -> None:
    connected = FakeClient("ready", connected=True, started=True)
    stalled = FakeClient("stalled", connected=True, started=False)

    assert AccountManager.connection_ready(connected)
    assert not AccountManager.connection_ready(stalled)


@pytest.mark.asyncio
async def test_stalled_session_restarts_without_plugin_resync(account_manager) -> None:
    app = FakeClient("bot", connected=True, started=False)
    account_manager.bot_apps["default"] = app
    account_manager._bot_names["default"] = "主 Bot"
    account_manager._disconnect_since["bot:default"] = 0

    await account_manager._run_reconnect_cycle()

    assert app.session.restart_calls == 1
    assert "bot:default" not in account_manager._disconnect_since


@pytest.mark.asyncio
async def test_first_recovery_resyncs_plugins(account_manager) -> None:
    app = FakeClient("user", connected=False, started=False)
    account_manager.user_apps.append(app)
    (account_manager.workdir / "user.session").touch()
    account_manager._disconnect_since["user:user"] = 0
    resync_calls = 0

    async def resync() -> None:
        nonlocal resync_calls
        resync_calls += 1

    account_manager._reconnect_callback = resync
    await account_manager._run_reconnect_cycle()

    assert app.start_calls == 1
    assert resync_calls == 1
    assert AccountManager.connection_ready(app)


@pytest.mark.asyncio
async def test_manually_paused_account_is_not_restarted(account_manager) -> None:
    app = FakeClient("paused", connected=False, started=False)
    account_manager.user_apps.append(app)
    (account_manager.workdir / "paused.session").touch()
    _pause_account("paused", account_manager.workdir)
    account_manager._disconnect_since["user:paused"] = 0

    await account_manager._run_reconnect_cycle()

    assert app.start_calls == 0
    assert "user:paused" not in account_manager._disconnect_since


@pytest.mark.asyncio
async def test_manual_online_repairs_a_stalled_session(account_manager) -> None:
    app = FakeClient("user", connected=True, started=False)
    account_manager.user_apps.append(app)
    (account_manager.workdir / "user.session").touch()

    assert await account_manager.set_online("user")
    assert app.session.restart_calls == 1
    assert AccountManager.connection_ready(app)


@pytest.mark.asyncio
async def test_bot_that_failed_during_startup_is_created_later(tmp_path, monkeypatch) -> None:
    accounts = AccountManager(str(tmp_path))
    accounts._RECONNECT_GRACE_PERIOD = 0
    specs = [{"id": "default", "name": "主 Bot", "token": "token"}]
    monkeypatch.setattr(account_manager_module, "_load_bots_config", lambda: specs)
    app = FakeClient("bot", connected=True, started=True)
    start_calls = 0
    resync_calls = 0

    async def start_bot(_bot_id: str, _name: str, _token: str):
        nonlocal start_calls
        start_calls += 1
        return app, ""

    async def resync() -> None:
        nonlocal resync_calls
        resync_calls += 1

    monkeypatch.setattr(accounts, "_start_bot_client", start_bot)
    accounts._reconnect_callback = resync

    await accounts._run_reconnect_cycle()
    await accounts._run_reconnect_cycle()

    assert start_calls == 1
    assert accounts.bot_apps["default"] is app
    assert resync_calls == 1


@pytest.mark.asyncio
async def test_account_avatar_uses_cached_profile_photo(account_manager) -> None:
    app = FakeClient("user", connected=True, started=True)
    app.me = SimpleNamespace(photo=SimpleNamespace(big_file_id="photo-file"))
    downloaded = []

    async def download_media(file_id, *, in_memory=False):
        downloaded.append((file_id, in_memory))
        return BytesIO(b"jpeg-data")

    app.download_media = download_media
    account_manager.user_apps.append(app)

    avatar = await account_manager.account_avatar("user")

    assert avatar.getvalue() == b"jpeg-data"
    assert downloaded == [("photo-file", True)]


@pytest.mark.asyncio
async def test_account_list_exposes_telegram_avatar_version(account_manager, monkeypatch) -> None:
    app = FakeClient("user", connected=True, started=True)
    app.me = SimpleNamespace(
        first_name="测试账号",
        id=12345,
        is_premium=True,
        photo=SimpleNamespace(big_photo_unique_id="stable-photo-id"),
    )
    account_manager.user_apps.append(app)
    monkeypatch.setattr(config_module, "load", lambda: {"ACCOUNTS": [{"session": "user"}]})

    result = await account_manager.list_accounts()

    assert result[0]["avatar_id"] == "stable-photo-id"
    assert result[0]["is_premium"] is True


@pytest.mark.asyncio
async def test_account_list_refreshes_stale_profile(account_manager, monkeypatch) -> None:
    app = FakeClient("user", connected=True, started=True)
    app.me = SimpleNamespace(
        first_name="旧名称",
        id=12345,
        is_premium=True,
        photo=SimpleNamespace(big_photo_unique_id="old-photo"),
    )
    refreshed = SimpleNamespace(
        first_name="新名称",
        id=12345,
        is_premium=False,
        photo=SimpleNamespace(big_photo_unique_id="new-photo"),
    )

    async def get_me():
        return refreshed

    app.get_me = get_me
    account_manager.user_apps.append(app)
    account_manager._profile_refreshed_at = 0
    monkeypatch.setattr(config_module, "load", lambda: {"ACCOUNTS": [{"session": "user"}]})

    result = await account_manager.list_accounts()

    assert result[0]["name"] == "新名称"
    assert result[0]["avatar_id"] == "new-photo"
    assert result[0]["is_premium"] is False


@pytest.mark.asyncio
async def test_account_avatar_is_absent_for_offline_account(account_manager) -> None:
    account_manager.user_apps.append(FakeClient("offline", connected=False, started=False))

    assert await account_manager.account_avatar("offline") is None


@pytest.mark.asyncio
async def test_offline_account_keeps_last_profile_and_cached_avatar(account_manager, monkeypatch) -> None:
    app = FakeClient("user", connected=True, started=True)
    app.me = SimpleNamespace(
        first_name="稳定名称",
        id=12345,
        is_premium=True,
        photo=SimpleNamespace(
            big_photo_unique_id="stable-photo-id",
            big_file_id="photo-file",
        ),
    )

    async def download_media(_file_id, *, in_memory):
        assert in_memory is True
        return BytesIO(b"cached-avatar")

    app.download_media = download_media
    account_manager.user_apps.append(app)
    monkeypatch.setattr(config_module, "load", lambda: {
        "ACCOUNTS": [{"session": "user", "name": "旧配置名称", "tgid": 12345}],
    })

    online = await account_manager.list_accounts()
    await account_manager.account_avatar("user")
    app.is_connected = False
    app.session.is_started.clear()
    offline = await account_manager.list_accounts()
    cached_avatar = await account_manager.account_avatar("user")

    assert online[0]["name"] == "稳定名称"
    assert offline[0]["name"] == "稳定名称"
    assert offline[0]["avatar_id"] == "stable-photo-id"
    assert offline[0]["is_premium"] is True
    assert cached_avatar.getvalue() == b"cached-avatar"
