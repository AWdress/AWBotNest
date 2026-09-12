from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from awbotnest.cloak_proxy import configure_cloakbrowser, requirements_use_cloakbrowser


def _module(name: str) -> ModuleType:
    return ModuleType(name)


def test_requirement_detection_handles_versions_and_extras():
    assert requirements_use_cloakbrowser(["httpx", "CloakBrowser[geoip]>=0.5"])
    assert not requirements_use_cloakbrowser(["playwright>=1.55"])


def test_cloak_launch_and_download_inherit_live_platform_proxy():
    calls: list[dict] = []

    def launch(**kwargs):
        download.httpx.get("https://downloads.test/browser.zip")
        return kwargs

    async def launch_async(**kwargs):
        return kwargs

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
    settings = SimpleNamespace(proxy_url="socks5://proxy.test:1080")
    with patch.dict(sys.modules, modules):
        configure_cloakbrowser(settings)

        assert package.launch()["proxy"] == "socks5://proxy.test:1080"
        assert calls[-1]["proxy"] == "socks5://proxy.test:1080"
        assert asyncio.run(package.launch_async())["proxy"] == "socks5://proxy.test:1080"
        assert package.launch(proxy="http://custom.test:8080")["proxy"] == "http://custom.test:8080"
        assert calls[-1]["proxy"] == "http://custom.test:8080"
        assert package.launch(proxy=None)["proxy"] is None
        assert "proxy" not in calls[-1]
        assert package.launch(proxy=False)["proxy"] is None
        assert "proxy" not in calls[-1]
        custom = {"server": "http://dict-proxy.test:3128", "username": "user@name", "password": "p:ss"}
        assert package.launch(proxy=custom)["proxy"] is custom
        assert calls[-1]["proxy"] == "http://user%40name:p%3Ass@dict-proxy.test:3128"

        settings.proxy_url = "http://new-proxy.test:7890"
        assert browser.launch()["proxy"] == "http://new-proxy.test:7890"
        license_module.httpx.get("https://license.test/check")
        assert calls[-1]["proxy"] == "http://new-proxy.test:7890"
