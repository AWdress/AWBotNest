"""Headless smoke test for the built administration interface."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright


def main() -> None:
    token = os.environ.get("AWBOTNEST_TEST_TOKEN", "")
    if not token:
        raise SystemExit("AWBOTNEST_TEST_TOKEN is required")

    errors: list[str] = []
    with sync_playwright() as playwright:
        candidates = (
            shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        )
        executable = next((path for path in candidates if path and Path(path).is_file()), None)
        browser = playwright.chromium.launch(headless=True, executable_path=executable)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.on("console", lambda message: errors.append(f"console: {message.text}")
                if message.type == "error" else None)
        page.on("pageerror", lambda error: errors.append(f"page: {error}"))
        page.on("response", lambda response: errors.append(f"HTTP {response.status}: {response.url}")
                if response.status >= 400 else None)
        page.goto("http://127.0.0.1:18001", wait_until="networkidle")
        page.evaluate("token => localStorage.setItem('awbotnest_token', token)", token)
        page.reload(wait_until="networkidle")

        routes = ("status", "accounts", "plugins", "logs", "settings")
        for route in routes:
            page.goto(f"http://127.0.0.1:18001/#/{route}", wait_until="networkidle")
            page.wait_for_timeout(300)
            if page.locator("body").inner_text().strip() == "":
                errors.append(f"empty page: {route}")
            if route == "logs":
                try:
                    page.locator(".logs-page").wait_for(state="visible", timeout=15_000)
                    page.locator(".conn.on").wait_for(state="visible", timeout=5_000)
                except Exception:
                    outcome = page.evaluate("""() => new Promise(resolve => {
                      const scheme = location.protocol === 'https:' ? 'wss' : 'ws'
                      const token = localStorage.getItem('awbotnest_token') || ''
                      const socket = new WebSocket(`${scheme}://${location.host}/api/logs/ws`,
                        ['awbotnest', `auth.${token}`])
                      socket.onopen = () => { socket.close(); resolve('opened') }
                      socket.onclose = event => resolve(`closed:${event.code}:${event.reason}`)
                      socket.onerror = () => resolve('error')
                      setTimeout(() => resolve('timeout'), 3000)
                    })""")
                    visible_text = " ".join(page.locator("body").inner_text().split())[:160]
                    errors.append(f"log WebSocket did not connect ({outcome}; page={visible_text})")

        mobile = browser.new_page(viewport={"width": 390, "height": 844})
        mobile.goto("http://127.0.0.1:18001", wait_until="domcontentloaded")
        mobile.evaluate("token => localStorage.setItem('awbotnest_token', token)", token)
        mobile.reload(wait_until="networkidle")
        mobile.goto("http://127.0.0.1:18001/#/status", wait_until="networkidle")
        overflow = mobile.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        if overflow > 1:
            errors.append(f"mobile horizontal overflow: {overflow}px")
        browser.close()

    if errors:
        raise SystemExit("\n".join(errors))
    print(f"UI smoke passed: {len(routes)} routes, desktop + mobile")


if __name__ == "__main__":
    main()
