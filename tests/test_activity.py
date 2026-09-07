"""Activity counts actual operations, not idle callbacks or configuration reads."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from awbotnest.activity import ActivityTracker, install_client_hooks, set_current, reset_current, track_call


class ActivityOperations(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.tracker = ActivityTracker(Path(self.temp.name) / 'activity.json')
        self.patcher = patch('awbotnest.activity.activity', self.tracker)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def counts(self):
        data = self.tracker.timeline()
        return data['totals'].get('test', 0), data['successes'].get('test', 0)

    async def test_sends_idle_nested_failure_and_persistence(self):
        client = SimpleNamespace(send_message=AsyncMock(return_value='ok'), send_file=AsyncMock())
        original = client.send_message
        install_client_hooks(client)
        install_client_hooks(client)
        await client.send_message('not a plugin')
        self.assertEqual(self.counts(), (0, 0))
        token = set_current('test', 'same-message')
        try:
            await client.send_message('one')
            await client.send_message('two')
            self.assertEqual(self.counts(), (2, 2))
            original.side_effect = ValueError('send failed')
            with self.assertRaises(ValueError):
                await client.send_message('fail')
            self.assertEqual(self.counts(), (3, 2))
            original.side_effect = None
            await track_call('test', lambda: client.send_message('nested'))
            self.assertEqual(self.counts(), (4, 3))
        finally:
            reset_current(token)
        restored = ActivityTracker(self.tracker.path).timeline()
        self.assertEqual(restored['totals'], {'test': 4})
        self.assertEqual(restored['successes'], {'test': 3})
        self.assertEqual(self.tracker.timeline(168)['totals'], {'test': 4})

    async def test_failed_result_cancellation_and_concurrent_runs(self):
        await track_call('test', lambda: {'ok': False})
        await track_call('test', lambda: False)
        await asyncio.gather(*(track_call('test', AsyncMock(return_value={'ok': True})) for _ in range(3)))
        async def cancel():
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await track_call('test', cancel)
        self.assertEqual(self.counts(), (6, 3))

    async def test_storage_failure_does_not_break_plugin(self):
        with patch.object(self.tracker, 'flush', side_effect=OSError('read only')):
            self.assertEqual(await track_call('test', lambda: 'ok'), 'ok')
