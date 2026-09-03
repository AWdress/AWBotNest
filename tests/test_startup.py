import asyncio
import logging
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from awbotnest import main
from awbotnest.config import Settings


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_respects_disable_during_startup(self):
        from awbotnest.plugins import PluginRuntime

        runtime = object.__new__(PluginRuntime)
        runtime.settings = Settings(enabled_plugins=["first", "second"])
        runtime._lifecycle_locks = {}
        runtime.scan = lambda: [SimpleNamespace(id=name, error="") for name in ("first", "second")]
        entered, resume = asyncio.Event(), asyncio.Event()
        enabled = []

        async def enable(name):
            enabled.append(name)
            if name == "first":
                entered.set()
                await resume.wait()

        runtime._enable = enable
        task = asyncio.create_task(runtime.restore())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            runtime.settings.enabled_plugins.remove("second")
            resume.set()
            await asyncio.wait_for(task, 2)
            self.assertEqual(enabled, ["first"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_real_http_responds_while_initialization_is_waiting(self):
        import httpx
        from fastapi import FastAPI

        app = FastAPI()

        @app.get("/api/health")
        async def health():
            return {"ok": True}

        entered = asyncio.Event()

        async def slow_start(*args):
            entered.set()
            await asyncio.Event().wait()

        original_server = main.uvicorn.Server
        captured = {}

        def server_factory(config):
            server = original_server(config)
            captured["server"] = server
            return server

        runtime = SimpleNamespace(stop=AsyncMock())
        accounts = SimpleNamespace(stop=AsyncMock())
        scheduler = SimpleNamespace(stop=Mock())
        with (patch.object(main, "start_platform", slow_start),
              patch.object(main, "create_app", return_value=app),
              patch.object(main.uvicorn, "Server", side_effect=server_factory),
              patch.object(main.activity, "flush")):
            task = asyncio.create_task(main.serve_platform(
                Settings(web_host="127.0.0.1", web_port=0), accounts, runtime, scheduler, object(), object()))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                server = captured["server"]

                async def wait_started():
                    while not server.started:
                        if task.done():
                            await task
                            self.fail("Server exited before listening")
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_started(), 3)
                port = server.servers[0].sockets[0].getsockname()[1]
                async with httpx.AsyncClient(trust_env=False) as client:
                    result = await client.get(f"http://127.0.0.1:{port}/api/health")
                self.assertEqual(result.json(), {"ok": True})
                self.assertFalse(task.done())
            finally:
                if "server" in captured:
                    captured["server"].should_exit = True
                await asyncio.wait_for(task, 5)

    async def run_scenario(self, *, fail_startup=False, fail_web=False, restart=False):
        web_ready = asyncio.Event()
        cancelled = asyncio.Event()
        initialization_entered = asyncio.Event()
        captured = {}

        async def initialize(*args):
            initialization_entered.set()
            await web_ready.wait()
            if fail_startup:
                raise RuntimeError("startup failed")
            try:
                await asyncio.Event().wait()  # Simulate a slow Telegram connection.
            finally:
                cancelled.set()

        class Server:
            should_exit = False
            force_exit = False

            async def serve(self):
                await initialization_entered.wait()
                web_ready.set()
                if fail_web:
                    raise RuntimeError("bind failed")
                if restart:
                    captured["restart"].set()
                elif not fail_startup:
                    return
                while not self.should_exit:
                    await asyncio.sleep(0.001)

        def create_app(*args):
            captured["restart"] = args[5]
            return object()

        runtime = SimpleNamespace(stop=AsyncMock())
        accounts = SimpleNamespace(stop=AsyncMock())
        scheduler = SimpleNamespace(stop=Mock())
        with (patch.object(main, "start_platform", initialize),
              patch.object(main, "create_app", create_app),
              patch.object(main.uvicorn, "Config"),
              patch.object(main.uvicorn, "Server", return_value=Server()),
              patch.object(main.activity, "flush") as flush):
            operation = main.serve_platform(Settings(), accounts, runtime, scheduler, object(), object())
            if fail_web or fail_startup:
                with self.assertRaisesRegex(RuntimeError, "failed"):
                    await asyncio.wait_for(operation, 2)
            else:
                self.assertEqual(await asyncio.wait_for(operation, 2), restart)
            self.assertTrue(web_ready.is_set())
            if not fail_startup:
                self.assertTrue(cancelled.is_set())
            runtime.stop.assert_awaited_once()
            accounts.stop.assert_awaited_once()
            scheduler.stop.assert_called_once()
            flush.assert_called_once()

    async def test_web_runs_during_slow_telegram_start(self):
        await self.run_scenario()

    async def test_web_failure_cancels_platform_and_cleans_up(self):
        await self.run_scenario(fail_web=True)

    async def test_platform_failure_stops_web_and_cleans_up(self):
        await self.run_scenario(fail_startup=True)

    async def test_restart_works_during_telegram_start(self):
        await self.run_scenario(restart=True)


class LoggingTests(unittest.TestCase):
    def test_startup_logs_reach_stdout_and_frontend_buffer(self):
        # Isolated interpreter: do not replace the test runner's logging handlers.
        script = '''
import asyncio, logging
from awbotnest import main
logging.getLogger().addHandler(logging.NullHandler())
async def cycle():
    logging.getLogger("awbotnest.main").info("startup-test-record")
    assert any(r["message"] == "startup-test-record" for r in main.memory_logs.recent())
    return False
main.run_once = cycle
asyncio.run(main.run())
'''
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("startup-test-record", result.stdout)
        self.assertNotIn("startup-test-record", result.stderr)


if __name__ == "__main__":
    unittest.main()
