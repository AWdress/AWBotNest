import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from awbotnest import main
from awbotnest.config import Settings


class StartupRestoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_configuration_startup_rolls_back(self):
        with patch.object(main.BackupManager, "apply_pending", return_value=True), \
             patch.object(main.BackupManager, "rollback_restore", return_value=True) as rollback, \
             patch.object(main, "reset_cloakbrowser_runtime"), \
             patch.object(main, "_run_configured_once", AsyncMock(side_effect=ValueError("invalid test setting"))):
            with self.assertRaisesRegex(ValueError, "invalid test setting"):
                await main.run_once()
            rollback.assert_called_once()

    async def test_restore_waits_for_successful_http_listener(self):
        for failed_bind in (False, True):
            with self.subTest(failed_bind=failed_bind):
                bind = asyncio.Event()
                close = asyncio.Event()
                app = SimpleNamespace(state=SimpleNamespace(platform_ready=False))
                server = SimpleNamespace(started=False, should_exit=False, force_exit=False)

                async def serve():
                    await bind.wait()
                    if failed_bind:
                        raise RuntimeError("test port already in use")
                    server.started = True
                    await close.wait()

                server.serve = serve
                accounts = SimpleNamespace(stop_recovery=AsyncMock(), stop=AsyncMock())
                scheduler = SimpleNamespace(close=AsyncMock(), stop=Mock())
                runtime = SimpleNamespace(stop=AsyncMock(), services=SimpleNamespace(close=AsyncMock()))
                with patch.object(main, "create_app", return_value=app), \
                     patch.object(main.uvicorn, "Config"), patch.object(main.uvicorn, "Server", return_value=server), \
                     patch.object(main, "start_platform", AsyncMock()), \
                     patch.object(main.BackupManager, "commit_restore") as commit, \
                     patch.object(main.activity, "flush"):
                    task = asyncio.create_task(main.serve_platform(
                        Settings(), accounts, runtime, scheduler, None, SimpleNamespace(),
                    ))
                    await asyncio.sleep(0.03)
                    self.assertFalse(app.state.platform_ready)
                    commit.assert_not_called()
                    bind.set()
                    if failed_bind:
                        with self.assertRaisesRegex(RuntimeError, "test port already"):
                            await asyncio.wait_for(task, 1)
                        commit.assert_not_called()
                    else:
                        for _ in range(20):
                            if app.state.platform_ready:
                                break
                            await asyncio.sleep(0.01)
                        self.assertTrue(app.state.platform_ready)
                        commit.assert_called_once()
                        close.set()
                        self.assertFalse(await asyncio.wait_for(task, 1))
                    self.assertFalse(app.state.platform_ready)
                    accounts.stop_recovery.assert_awaited_once()
