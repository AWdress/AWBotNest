from __future__ import annotations

import asyncio
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import awbotnest.cloak_proxy as cloak_proxy
from awbotnest.cloak_proxy import configure_cloakbrowser, requirements_use_cloakbrowser


def _module(name: str) -> ModuleType:
    return ModuleType(name)


def test_requirement_detection_handles_versions_and_extras():
    assert requirements_use_cloakbrowser(["httpx", "CloakBrowser[geoip]>=0.5"])
    assert not requirements_use_cloakbrowser(["playwright>=1.55"])


def test_cloak_launch_and_download_inherit_live_platform_proxy():
    calls: list[dict] = []

    class SyncTarget:
        def __init__(self, kwargs):
            self.kwargs = kwargs
        def close(self):
            return None
        def on(self, event, callback):
            return None

    class AsyncTarget:
        def __init__(self, kwargs):
            self.kwargs = kwargs
        async def close(self):
            return None
        def on(self, event, callback):
            return None

    def launch(**kwargs):
        download.httpx.get("https://downloads.test/browser.zip")
        return SyncTarget(kwargs)

    async def launch_async(**kwargs):
        return AsyncTarget(kwargs)

    fake_httpx = SimpleNamespace(
        get=lambda url, **kwargs: calls.append({"url": url, **kwargs}),
        Timeout=object,
    )
    package = _module("cloakbrowser")
    browser = _module("cloakbrowser.browser")
    download = _module("cloakbrowser.download")
    license_module = _module("cloakbrowser.license")
    geoip = _module("cloakbrowser.geoip")
    for module in (package, browser):
        module.launch = launch
        module.launch_async = launch_async
    download.httpx = fake_httpx
    license_module.httpx = fake_httpx

    modules = {
        "cloakbrowser": package,
        "cloakbrowser.browser": browser,
        "cloakbrowser.download": download,
        "cloakbrowser.license": license_module,
        "cloakbrowser.geoip": geoip,
    }
    settings = SimpleNamespace(
        proxy_url="socks5://proxy.test:1080",
        cloakbrowser_use_free_key=True,
        cloakbrowser_license_key="cb_platform_secret",
    )
    with patch.dict(sys.modules, modules):
        configure_cloakbrowser(settings)

        target = package.launch()
        assert target.kwargs["proxy"] == "socks5://proxy.test:1080"
        assert target.kwargs["license_key"] == "cb_platform_secret"
        target.close()
        assert calls[-1]["proxy"] == "socks5://proxy.test:1080"
        async def async_launch():
            value = await package.launch_async()
            assert value.kwargs["proxy"] == "socks5://proxy.test:1080"
            await value.close()
        asyncio.run(async_launch())
        target = package.launch(proxy="http://custom.test:8080")
        assert target.kwargs["proxy"] == "http://custom.test:8080"
        target.close()
        assert calls[-1]["proxy"] == "http://custom.test:8080"
        target = package.launch(proxy=None)
        assert target.kwargs["proxy"] is None
        target.close()
        assert "proxy" not in calls[-1]
        target = package.launch(proxy=False)
        assert target.kwargs["proxy"] is None
        target.close()
        assert "proxy" not in calls[-1]
        custom = {"server": "http://dict-proxy.test:3128", "username": "user@name", "password": "p:ss"}
        target = package.launch(proxy=custom)
        assert target.kwargs["proxy"] is custom
        target.close()
        assert calls[-1]["proxy"] == "http://user%40name:p%3Ass@dict-proxy.test:3128"

        settings.proxy_url = "http://new-proxy.test:7890"
        target = browser.launch()
        assert target.kwargs["proxy"] == "http://new-proxy.test:7890"
        target.close()
        target = browser.launch(license_key="cb_plugin_secret")
        assert target.kwargs["license_key"] == "cb_plugin_secret"
        target.close()
        license_module.httpx.get("https://license.test/check")
        assert calls[-1]["proxy"] == "http://new-proxy.test:7890"


def test_free_key_serializes_sessions_until_graceful_close_and_keyless_does_not():
    class Target:
        def __init__(self, kwargs):
            self.kwargs = kwargs
        def close(self):
            return None
        def on(self, event, callback):
            return None

    package = _module("cloakbrowser")
    browser = _module("cloakbrowser.browser")
    download = _module("cloakbrowser.download")
    license_module = _module("cloakbrowser.license")
    geoip = _module("cloakbrowser.geoip")
    package.launch = lambda **kwargs: Target(kwargs)
    browser.launch = package.launch
    fake_httpx = SimpleNamespace(get=lambda *args, **kwargs: None)
    download.httpx = fake_httpx
    license_module.httpx = fake_httpx
    modules = {
        "cloakbrowser": package,
        "cloakbrowser.browser": browser,
        "cloakbrowser.download": download,
        "cloakbrowser.license": license_module,
        "cloakbrowser.geoip": geoip,
    }
    settings = SimpleNamespace(
        proxy_url="", cloakbrowser_use_free_key=True,
        cloakbrowser_license_key="cb_free",
    )
    with patch.dict(sys.modules, modules):
        configure_cloakbrowser(settings)
        first = package.launch()
        assert first.kwargs["license_key"] == "cb_free"
        finished = threading.Event()
        result = []
        worker = threading.Thread(
            target=lambda: (result.append(package.launch()), finished.set()), daemon=True,
        )
        worker.start()
        time.sleep(0.05)
        assert not finished.is_set()
        first.close()
        assert finished.wait(1)
        result[0].close()
        worker.join(1)

        # 关闭开关时即使保留已保存的 Key，也不注入 Key、不进入队列。
        settings.cloakbrowser_use_free_key = False
        first_keyless = package.launch()
        second_keyless = package.launch()
        assert "license_key" not in first_keyless.kwargs
        first_keyless.close()
        second_keyless.close()


def test_close_failure_enters_remote_seat_cooldown():
    class BrokenTarget:
        def close(self):
            raise RuntimeError("browser transport failed")
        def on(self, event, callback):
            return None

    gate = cloak_proxy._FreeSessionGate()
    lease = cloak_proxy._SessionLease(gate)
    gate._active = True
    with patch.object(cloak_proxy, "_platform_license_key", return_value="cb_free"), \
         patch.object(cloak_proxy.threading, "Thread") as recovery:
        target = cloak_proxy._attach_session_lease(BrokenTarget(), lease)
        try:
            target.close()
        except RuntimeError:
            pass
        else:
            raise AssertionError("close() failure must remain visible to the plugin")
        status = gate.snapshot()
    assert status["active"] is False
    assert status["cooldown_seconds"] > 0
    recovery.assert_called_once()


def test_cancelled_async_waiter_leaves_no_queue_entry():
    async def scenario():
        original_gate = cloak_proxy._SESSION_GATE
        gate = cloak_proxy._FreeSessionGate()
        cloak_proxy._SESSION_GATE = gate
        try:
            with patch.object(cloak_proxy, "_platform_license_key", return_value="cb_free"):
                first = gate.acquire()
                waiter = asyncio.create_task(cloak_proxy._acquire_session_async())
                await asyncio.sleep(0.05)
                waiter.cancel()
                try:
                    await waiter
                except asyncio.CancelledError:
                    pass
                first.release()
                await asyncio.sleep(0.05)
                assert gate.snapshot()["waiting"] == 0
        finally:
            cloak_proxy._SESSION_GATE = original_gate

    asyncio.run(scenario())


def test_hot_reload_clears_maintenance_and_discards_old_cloak_modules():
    gate = cloak_proxy._FreeSessionGate()
    original_gate = cloak_proxy._SESSION_GATE
    cloak_proxy._SESSION_GATE = gate
    fake_package = ModuleType("cloakbrowser")
    fake_browser = ModuleType("cloakbrowser.browser")
    try:
        gate.begin_maintenance()
        with patch.dict(sys.modules, {
            "cloakbrowser": fake_package,
            "cloakbrowser.browser": fake_browser,
        }):
            cloak_proxy.reset_cloakbrowser_runtime()
            assert "cloakbrowser" not in sys.modules
            assert "cloakbrowser.browser" not in sys.modules
        assert gate.snapshot()["maintenance"] is False
    finally:
        cloak_proxy._SESSION_GATE = original_gate
