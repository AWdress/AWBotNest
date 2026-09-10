from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from awbotnest.api import create_app
from awbotnest.backup import BACKUP_FORMAT, BACKUP_VERSION, BackupManager
from awbotnest.config import Settings
from awbotnest.routing import PluginRoutes
from awbotnest.scheduler import PluginScheduler
from awbotnest.services.ai import AIService
from awbotnest.services.ai_protocol import AIRequestError, decode_json


def response(data: object, url: str = "https://example.test/v1") -> httpx.Response:
    return httpx.Response(200, json=data, request=httpx.Request("POST", url))


class AIProtocolTests(unittest.IsolatedAsyncioTestCase):
    def service(self, api_format: str, reply: httpx.Response) -> tuple[AIService, AsyncMock]:
        settings = Settings(ai_settings={
            "providers": [{
                "id": "provider", "name": "测试服务", "api_key": "secret",
                "base_url": "https://example.test/v1", "api_format": api_format,
            }],
            "models": [{
                "id": "model", "provider_id": "provider", "model": "model-name",
                "capabilities": ["text"],
            }],
            "capabilities": {"text": {"default_model": "model"}},
        })
        post = AsyncMock(return_value=reply)
        return AIService(settings, SimpleNamespace(post=post)), post

    async def test_responses_protocol_request_and_response(self):
        service, post = self.service("responses", response({
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 1, "total_tokens": 4},
        }))
        self.assertEqual(await service.chat([{"role": "user", "content": "hello"}]), "OK")
        self.assertTrue(post.call_args.args[0].endswith("/responses"))
        self.assertIn("input", post.call_args.kwargs["json"])
        self.assertNotIn("messages", post.call_args.kwargs["json"])

    async def test_anthropic_protocol_request_and_response(self):
        service, post = self.service("anthropic_messages", response({
            "content": [{"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }))
        self.assertEqual(await service.chat([
            {"role": "system", "content": "Be brief"},
            {"role": "user", "content": "hello"},
        ]), "OK")
        self.assertTrue(post.call_args.args[0].endswith("/messages"))
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "secret")
        self.assertEqual(post.call_args.kwargs["json"]["system"], "Be brief")

    async def test_auto_protocol_recovers_from_html_and_caches_responses(self):
        html = httpx.Response(200, text="<html>not chat completions</html>",
                              headers={"content-type": "text/html"},
                              request=httpx.Request("POST", "https://example.test/v1/chat/completions"))
        ok = response({"output_text": "OK"}, "https://example.test/v1/responses")
        service, post = self.service("auto", html)
        post.side_effect = [html, ok, ok]

        self.assertEqual(await service.chat([{"role": "user", "content": "hello"}]), "OK")
        self.assertEqual(service.detected_protocols(), {"provider": "responses"})
        self.assertEqual(await service.chat([{"role": "user", "content": "again"}]), "OK")
        self.assertTrue(post.call_args.args[0].endswith("/responses"))
        self.assertEqual(len(service.usage.recent()), 3)

    def test_html_response_has_actionable_safe_error(self):
        reply = httpx.Response(200, text="<html>token=secret-value sk-1234567890</html>",
                               headers={"content-type": "text/html"})
        with self.assertRaises(AIRequestError) as captured:
            decode_json(reply, "chat_completions")
        self.assertEqual(captured.exception.category, "invalid_response")
        self.assertIn("HTML", str(captured.exception))
        self.assertNotIn("secret-value", str(captured.exception))
        self.assertNotIn("1234567890", str(captured.exception))


class UsageAndBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def bundle(config: dict[str, object]) -> bytes:
        stream = io.BytesIO()
        system = dict(config)
        plugins = system.pop("plugin_config", {})
        with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps({
                "format": BACKUP_FORMAT, "version": BACKUP_VERSION,
            }))
            archive.writestr("config/system.json", json.dumps(system))
            archive.writestr("config/plugins.json", json.dumps(plugins))
        return stream.getvalue()

    def test_usage_recent_persists_and_aggregates_plugins(self):
        from awbotnest.services.ai_usage import AIUsageTracker
        path = self.root / "usage.json"
        tracker = AIUsageTracker(path)
        tracker.record_attempt(
            source="插件:多站签到", plugin_id="pt_multi_checkin", capability="vision",
            provider_id="one", provider="服务一", model="vision", protocol="responses",
            succeeded=True, latency_ms=120, response={"usage": {"total_tokens": 9}},
        )
        restored = AIUsageTracker(path)
        self.assertEqual(restored.recent()[0]["provider"], "服务一")
        self.assertEqual(restored.plugin_summary()[0]["name"], "多站签到")
        self.assertEqual(restored.plugin_summary()[0]["total_tokens"], 9)

    def test_backup_preview_and_stale_confirmation(self):
        import awbotnest.backup as backup
        current = {"web_port": 18001, "theme": "dark", "plugin_config": {"old": {"on": True}}}
        incoming = {"web_port": 19001, "new_setting": True,
                    "plugin_config": {"new": {"on": True}}}
        (self.root / "config.json").write_text(json.dumps(current), encoding="utf-8")
        content = self.bundle(incoming)
        with patch.object(backup, "DATA_DIR", self.root), \
             patch.object(backup, "BACKUP_DIR", self.root / "backups"), \
             patch.object(backup, "PENDING_RESTORE", self.root / ".restore-pending.zip"):
            preview = BackupManager.preview(content)
            self.assertEqual(preview["summary"]["system_changed"], 1)
            self.assertEqual(preview["summary"]["plugins_added"], 1)
            self.assertEqual(preview["summary"]["plugins_removed"], 1)
            BackupManager.stage(content, expected_digest=preview["digest"],
                                expected_current_digest=preview["current_digest"])
            self.assertTrue((self.root / ".restore-pending.zip").exists())

            (self.root / "config.json").write_text(json.dumps({**current, "theme": "light"}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "当前配置"):
                BackupManager.stage(content, expected_digest=preview["digest"],
                                    expected_current_digest=preview["current_digest"])


class HealthContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_liveness_and_readiness_are_separate(self):
        scheduler = PluginScheduler()
        app = create_app(
            Settings(admin_token="test-token"), SimpleNamespace(), SimpleNamespace(), scheduler,
            PluginRoutes(), market=SimpleNamespace(clear_cache=lambda: None),
        )
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                         base_url="http://test") as client:
                self.assertEqual((await client.get("/healthz")).status_code, 200)
                self.assertEqual((await client.get("/readyz")).status_code, 503)
                app.state.platform_ready = True
                ready = await client.get("/readyz")
                self.assertEqual(ready.status_code, 200)
                self.assertTrue(ready.json()["ok"])
        finally:
            scheduler.stop()


if __name__ == "__main__":
    unittest.main()
