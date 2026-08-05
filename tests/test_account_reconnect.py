from __future__ import annotations

import asyncio

import pytest

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
