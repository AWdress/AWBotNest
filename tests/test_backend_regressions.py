"""Offline regression checks for V1 behavior retained by the V2 backend."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from awbotnest.config import Settings, BotSettings
from awbotnest.scheduler import PluginScheduler
from awbotnest.notifier import NotificationService
from awbotnest.context import PluginContext
from awbotnest.routing import PluginRoutes
from awbotnest.services import PlatformServices
from awbotnest.telegram import TelegramAccounts


class BackendRegressions(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        from awbotnest.activity import ActivityTracker
        self.activity = ActivityTracker(self.root / 'activity.json')
        self.activity_patch = patch('awbotnest.activity.activity', self.activity)
        self.activity_patch.start()
        self.addCleanup(self.activity_patch.stop)
        self.scheduler = PluginScheduler()

    async def asyncTearDown(self):
        self.scheduler.stop()
        await asyncio.sleep(0)
        self.temp.cleanup()

    def context(self):
        settings = Settings()
        with patch('awbotnest.services.DATA_DIR', self.root):
            services = PlatformServices(settings)
        with patch('awbotnest.context.DATA_DIR', self.root), patch('awbotnest.storage.DATA_DIR', self.root):
            return PluginContext('example', 'standalone', SimpleNamespace(), self.scheduler,
                                 settings, services, PluginRoutes(), SimpleNamespace())

    async def test_notify_table_routes_through_platform(self):
        ctx = self.context()
        ctx.notify = AsyncMock(return_value='sent')
        result = await ctx.notify_table(['账号', '结果'], [['A', '成功']], caption='签到', level='success')
        self.assertEqual(result, 'sent')
        self.assertIn('<table', ctx.notify.call_args.args[0])
        self.assertEqual(ctx.notify.call_args.kwargs['format'], 'rich')
        self.assertEqual(ctx.notify.call_args.kwargs['level'], 'success')
        await ctx.close()

    async def test_scheduled_manual_and_action_activity(self):
        ctx = self.context()
        callback = AsyncMock(return_value={'ok': True})
        job = ctx.schedule_interval('run', callback, seconds=3600)
        for _ in range(2):
            self.scheduler.run_now(job)
            await self.scheduler._manual[job]
        callback.return_value = {'ok': False}
        self.scheduler.run_now(job)
        await self.scheduler._manual[job]
        await ctx.execute('action:run', AsyncMock(return_value=True))
        await ctx.execute('setup', AsyncMock())
        await ctx.execute('callback:config', AsyncMock(return_value={}))
        self.assertEqual(self.activity.timeline()['totals'], {'example': 4})
        self.assertEqual(self.activity.timeline()['successes'], {'example': 3})
        await ctx.close()

    async def test_cookie_permissions_paths_and_sync_reminder(self):
        from awbotnest.plugin_cookies import PluginCookies
        ctx = self.context()
        ctx.settings.cookie_settings = {'enabled': True}
        notify = AsyncMock()
        cookies = PluginCookies(ctx.services.cookies, ctx.settings, ['*.example.org'], notify)
        await ctx.services.cookies.replace({'example.org': [
            {'name': 'sid', 'value': 'root', 'path': '/', 'domain': '.example.org'},
            {'name': 'sid', 'value': 'account', 'path': '/account', 'domain': '.example.org'},
            {'name': 'expired', 'value': 'x', 'expires': 1, 'domain': '.example.org'},
        ]})
        self.assertTrue(cookies.available)
        self.assertEqual(await cookies.header('sub.example.org', path='/account/login'), 'sid=account')
        self.assertEqual(await cookies.header('example.org', path='/accounts'), 'sid=root')
        with self.assertRaises(PermissionError):
            await cookies.get('notexample.org')
        self.assertTrue(await cookies.request_sync('example.org'))
        notify.assert_not_awaited()
        await ctx.services.cookies.replace({})
        self.assertFalse(await cookies.request_sync('example.org'))
        self.assertFalse(await cookies.request_sync('example.org'))
        notify.assert_awaited_once()
        ctx.settings.cookie_settings['enabled'] = False
        self.assertFalse(cookies.available)
        with self.assertRaises(RuntimeError):
            await cookies.header('example.org')
        await ctx.close()

    async def test_generic_schedule_date_args_and_duplicate_names(self):
        from datetime import datetime, timedelta
        ctx = self.context()
        callback = AsyncMock(return_value={'ok': True})
        later = datetime.now() + timedelta(days=1)
        first = ctx.schedule(callback, 'date', id='once', run_date=later, args=[42])
        second = ctx.schedule(callback, 'date', id='once', run_date=later, args=[43])
        self.assertNotEqual(first, second)
        self.scheduler.run_now(first)
        await self.scheduler._manual[first]
        callback.assert_awaited_once_with(42)
        await ctx.close()
        self.assertEqual(self.scheduler.jobs(), [])

    async def test_ai_multiple_images_and_mime(self):
        ctx = self.context()
        ctx.services.ai.vision = AsyncMock(return_value='seen')
        self.assertEqual(await ctx.ai.chat('describe', images=[b'\xff\xd8\xfftest', 'https://example.org/a.png']), 'seen')
        images = ctx.services.ai.vision.call_args.args[1]
        self.assertEqual(len(images), 2)
        self.assertTrue(images[0].startswith('data:image/jpeg;base64,'))
        self.assertEqual(images[1], 'https://example.org/a.png')
        await ctx.close()

    async def test_same_timestamp_plugin_update_uses_new_source(self):
        import os
        from awbotnest.plugins import PluginRuntime
        folder = self.root / 'plugins'
        folder.mkdir()
        entry = folder / 'fresh.py'
        entry.write_text('value = 1\n')
        runtime = object.__new__(PluginRuntime)
        runtime.plugins_dir = folder
        stamp = entry.stat().st_mtime
        self.assertEqual(runtime._import(entry, 'fresh').value, 1)
        entry.write_text('value = 2\n')
        os.utime(entry, (stamp, stamp))
        self.assertEqual(runtime._import(entry, 'fresh').value, 2)

    async def test_backup_keeps_sqlite_data_and_rejects_traversal(self):
        import sqlite3
        import zipfile
        from awbotnest.backup import BackupManager
        data, plugins, sessions = (self.root / name for name in ('data', 'plugins', 'sessions'))
        data.mkdir()
        with sqlite3.connect(data / 'state.db') as connection:
            connection.execute('create table example (value text)')
            connection.execute("insert into example values ('preserved')")
        connection.close()
        with patch.multiple('awbotnest.backup', APP_ROOT=self.root, DATA_DIR=data,
                            PLUGINS_DIR=plugins, SESSIONS_DIR=sessions, BACKUP_DIR=data / 'backups'):
            archive = BackupManager.create()
            BackupManager.validate(archive)
            with zipfile.ZipFile(archive) as zipped:
                destination = self.root / 'snapshot.db'
                destination.write_bytes(zipped.read('data/state.db'))
                with sqlite3.connect(destination) as connection:
                    self.assertEqual(connection.execute('select value from example').fetchone()[0], 'preserved')
                connection.close()
            invalid = self.root / 'invalid.zip'
            with zipfile.ZipFile(invalid, 'w') as zipped:
                zipped.writestr('data/../../outside', 'bad')
            with self.assertRaises(ValueError):
                BackupManager.validate(invalid)

    async def test_stop_propagation_does_not_open_circuit(self):
        from telethon.events import StopPropagation
        ctx = self.context()
        def stop():
            raise StopPropagation()
        for _ in range(6):
            with self.assertRaises(StopPropagation):
                await ctx.execute('event', stop)
        states = ctx.governor.status(ctx.plugin_id)['circuits']
        self.assertTrue(all(not item['open'] and item['failures'] == 0 for item in states))
        await ctx.close()

    async def test_replay_is_cancelled_when_context_closes(self):
        ctx = self.context()
        entered = asyncio.Event()
        @ctx.on_replay('slow')
        async def replay(payload):
            entered.set()
            await asyncio.Event().wait()
        event = ctx.record_event('slow', {'value': 1})
        task = asyncio.create_task(ctx.governor.replay(ctx.plugin_id, event['id']))
        await asyncio.wait_for(entered.wait(), 1)
        await ctx.close()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_scheduler_grace_and_pending_snapshot(self):
        key = self.scheduler.add_interval('p', 'job', AsyncMock(), seconds=100)
        self.assertIsNone(self.scheduler.jobs()[0]['next_run'])
        self.scheduler.start()
        self.assertEqual(self.scheduler.scheduler.get_job(key).misfire_grace_time, 300)

    async def test_empty_cron_rejected(self):
        with self.assertRaises(ValueError):
            self.scheduler.add_cron('p', 'job', AsyncMock())

    async def test_failure_result_survives_context(self):
        ctx = self.context()
        key = ctx.schedule_interval('failure', AsyncMock(return_value={'ok': False}), seconds=100)
        await self.scheduler.scheduler.get_job(key).func()
        self.assertEqual(self.scheduler.snapshot(key)['progress']['status'], 'failed')
        await ctx.close()

    async def test_progress_from_callback(self):
        ctx = self.context()
        captured = []
        async def callback():
            self.assertTrue(ctx.report_progress(25, 'one of four'))
            captured.append(self.scheduler.snapshot(key)['progress'])
        key = ctx.schedule_interval('progress', callback, seconds=100)
        await self.scheduler.scheduler.get_job(key).func()
        self.assertEqual(captured[0]['percent'], 25)
        await ctx.close()

    async def test_stop_cancels_running_scheduled_callback(self):
        ctx = self.context()
        entered = asyncio.Event()
        finished = asyncio.Event()
        async def callback():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        key = ctx.schedule_interval('long', callback, seconds=100)
        task = asyncio.create_task(self.scheduler.scheduler.get_job(key).func())
        await entered.wait()
        await ctx.close()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(finished.is_set())
        self.assertEqual(self.scheduler.snapshot(key)['progress']['status'], 'cancelled')

    async def test_cleanup_idempotent_and_isolated(self):
        ctx = self.context()
        calls = []
        ctx.add_cleanup(lambda: calls.append('first'))
        def fails():
            raise ValueError('cleanup failure')
        ctx.add_cleanup(fails)
        ctx.add_cleanup(lambda: calls.append('last'))
        await ctx.close()
        await ctx.close()
        self.assertEqual(calls, ['last', 'first'])

    async def test_closed_api_cannot_execute(self):
        ctx = self.context()
        callback = AsyncMock()
        ctx.on_api('run', callback)
        handler = ctx.routes._apis[('example', 'run')]
        await ctx.close()
        with self.assertRaises(RuntimeError):
            await handler(None)
        callback.assert_not_called()

    async def test_multi_channel_failure_continues(self):
        settings = Settings(bot_routing={'p': 'bad,good'}, notification_channels=[
            {'id': 'bad', 'type': 'webhook', 'url': 'https://example.invalid/a'},
            {'id': 'good', 'type': 'webhook', 'url': 'https://example.invalid/b'},
        ])
        import httpx
        http = SimpleNamespace(post=AsyncMock(side_effect=[RuntimeError('failure'),
            httpx.Response(200, json={'ok': True}, request=httpx.Request('POST', 'https://example.invalid'))]))
        service = NotificationService(settings, SimpleNamespace(), http)
        service.history_path = self.root / 'history.json'
        await service.send('hello', plugin_id='p')
        self.assertEqual(http.post.await_count, 2)

    async def test_default_non_telegram_channel(self):
        import httpx
        settings = Settings(notification_channels=[{'id': 'bark', 'type': 'bark',
            'url': 'https://example.invalid', 'is_default': True}])
        http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200,
            json={'code': 200}, request=httpx.Request('POST', 'https://example.invalid'))))
        await NotificationService(settings, SimpleNamespace(), http).send('hello', _record=False)
        http.post.assert_awaited_once()

    async def test_reconnect_disconnected_user(self):
        settings = Settings(user_sessions=['example'])
        accounts = TelegramAccounts(settings, self.root)
        previous = SimpleNamespace(is_connected=lambda: False, disconnect=AsyncMock())
        replacement = SimpleNamespace(connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                                      disconnect=AsyncMock(), is_connected=lambda: True)
        accounts.users['example'] = previous
        (self.root / 'example.session').touch()
        with patch.object(accounts, '_client', return_value=replacement), patch.object(accounts, '_cache_profile', AsyncMock()):
            self.assertTrue(await accounts.connect_user('example'))
        previous.disconnect.assert_awaited_once()
        self.assertIs(accounts.users['example'], replacement)

    async def settings_request(self, settings, path, payload):
        import httpx
        from awbotnest.app import create_app
        app = create_app(settings, SimpleNamespace(), SimpleNamespace(), self.scheduler,
                         PluginRoutes(), market=SimpleNamespace(clear_cache=lambda: None))
        with patch('awbotnest.app.save_settings'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://test') as client:
                return await client.put(path, json=payload, headers={'Authorization': 'Bearer ' + settings.admin_token})

    async def test_partial_settings_preserve_credentials_and_routes(self):
        settings = Settings(bot_token='kept', api_id=123, api_hash='kept',
            bot_routing={'p': 'bark'}, notification_channels=[{'id': 'bark', 'type': 'bark'}])
        response = await self.settings_request(settings, '/api/settings', {'bot_name': 'changed'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(settings.bot_token, 'kept')
        self.assertEqual(settings.api_id, 123)
        self.assertEqual(settings.bot_routing, {'p': 'bark'})

    async def test_invalid_settings_do_not_mutate(self):
        settings = Settings(bot_token='kept')
        response = await self.settings_request(settings, '/api/settings',
            {'bot_token': 'changed', 'pip_index_url': 'invalid'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(settings.bot_token, 'kept')

    async def test_channel_save_preserves_omitted_token_and_bark_route(self):
        settings = Settings(bots=[BotSettings('b', 'bot', 'kept')], bot_routing={'p': 'bark'})
        response = await self.settings_request(settings, '/api/settings/notification-channels',
            {'channels': [{'id': 'b', 'type': 'telegram'}, {'id': 'bark', 'type': 'bark'}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(settings.bots[0].token, 'kept')
        self.assertEqual(settings.bot_routing, {'p': 'bark'})

    async def test_market_keeps_failed_repository_and_refreshes_local_version(self):
        from awbotnest.market import PluginMarket, OFFICIAL_REPO
        settings = Settings(plugin_repos=['other/repo'])
        with patch('awbotnest.market.PLUGINS_DIR', self.root / 'plugins'):
            market = PluginMarket(settings)
            market._cache = {'plugins': [{'id': 'old', 'repo': OFFICIAL_REPO,
                'version': '2', 'official': True}]}
            market._cache_until = 0
            async def listing(repo):
                if repo == OFFICIAL_REPO:
                    raise RuntimeError('offline')
                return {'plugins': [{'id': 'new', 'repo': repo, 'version': '1'}]}
            with patch.object(market, 'list_repo', side_effect=listing), \
                 patch.object(market, '_install_counts', AsyncMock(return_value={})), \
                 patch.object(market, '_installed_version', return_value='2'):
                result = await market.list_all()
                self.assertEqual([item['id'] for item in result['plugins']], ['old', 'new'])
                self.assertFalse(result['plugins'][0]['update_available'])
                self.assertEqual(result['plugins'][0]['installed_version'], '2')

    async def test_ai_disabled_primary_uses_fallback(self):
        import httpx
        from awbotnest.services import AIService
        settings = Settings(ai_settings={
            'providers': [{'id': 'provider', 'api_key': 'test', 'base_url': 'https://example.invalid'}],
            'models': [
                {'id': 'disabled', 'model': 'bad', 'enabled': False, 'capabilities': ['text']},
                {'id': 'fallback', 'model': 'good', 'provider_id': 'provider', 'capabilities': ['text']}],
            'capabilities': {'text': {'default_model': 'disabled', 'fallback_model': 'fallback'}}})
        http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200,
            json={'choices': [{'message': {'content': 'ok'}}]},
            request=httpx.Request('POST', 'https://example.invalid'))))
        self.assertEqual(await AIService(settings, http).chat([]), 'ok')
        self.assertEqual(http.post.call_args.kwargs['json']['model'], 'good')

    async def test_ai_disabled_model_cannot_use_legacy_key(self):
        from awbotnest.services import AIService
        settings = Settings(ai_api_key='legacy', ai_settings={'models': [
            {'id': 'disabled', 'enabled': False}]})
        with self.assertRaises(RuntimeError):
            AIService(settings, SimpleNamespace())._resolve('text', 'disabled')

    async def test_password_schema_is_secret(self):
        from awbotnest.plugins import PluginRuntime
        self.assertTrue(PluginRuntime.secret_field({'type': 'password'}))
        self.assertTrue(PluginRuntime.secret_field({'type': 'string', 'secret': True}))
        self.assertFalse(PluginRuntime.secret_field({'type': 'string'}))

    async def test_browser_sync_callback_uses_worker_and_v1_arguments(self):
        from awbotnest.services import BrowserService
        service = BrowserService(Settings())
        import threading
        owner = threading.get_ident()
        def worker(url, action, **kwargs):
            self.assertNotEqual(threading.get_ident(), owner)
            self.assertEqual(kwargs['user_agent'], 'test-agent')
            self.assertEqual(kwargs['cookies'], 'key=value')
            return action(SimpleNamespace(content=lambda: 'html'))
        with patch.object(service, '_run_sync', side_effect=worker):
            value = await service.run('https://example.invalid', lambda page: page.content(),
                                      ua='test-agent', cookies='key=value')
        self.assertEqual(value, 'html')

    async def test_browser_proxy_credentials_split(self):
        from awbotnest.services import BrowserService
        self.assertEqual(BrowserService._proxy('http://name:p%40ss@localhost:8080'),
            {'server': 'http://localhost:8080', 'username': 'name', 'password': 'p@ss'})

    async def test_ai_concurrency_setting_enforced(self):
        from awbotnest.services import AIService
        active = peak = 0
        async def request(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.01)
            active -= 1
        service = AIService(Settings(ai_settings={'max_concurrency': 1}), SimpleNamespace(post=request))
        await asyncio.gather(*(service._post('test') for _ in range(4)))
        self.assertEqual(peak, 1)

    async def test_cookie_domain_filter_boundary(self):
        from awbotnest.cookiecloud import filter_domains
        values = {'example.org': [], 'sub.example.org': [], 'notexample.org': []}
        self.assertEqual(set(filter_domains(values, ['*.example.org'])), {'example.org', 'sub.example.org'})

    async def test_logs_are_inside_persisted_data(self):
        from awbotnest.config import DATA_DIR
        from awbotnest.logs import LOG_FILE
        self.assertTrue(LOG_FILE.is_relative_to(DATA_DIR))

    async def test_http_methods_do_not_overwrite(self):
        ctx = self.context()
        ctx.on_api('/config', AsyncMock(return_value='get'), methods=['GET'])
        ctx.on_api('/config', AsyncMock(return_value='post'), methods=['POST'])
        self.assertEqual(await ctx.routes.dispatch_api('example', 'config', SimpleNamespace(method='GET')), 'get')
        self.assertEqual(await ctx.routes.dispatch_api('example', 'config', SimpleNamespace(method='POST')), 'post')
        await ctx.close()

    async def test_action_decorator_with_no_arguments(self):
        ctx = self.context()
        @ctx.action('test')
        async def action():
            return 'done'
        self.assertEqual(await ctx.routes.dispatch_action('example', 'test', {}), 'done')
        await ctx.close()

    async def test_capability_fallback_and_cleanup(self):
        ctx = self.context()
        ctx.provide_capability('answer', AsyncMock(side_effect=ValueError('bad')), priority=200)
        ctx.provide_capability('answer', AsyncMock(return_value='ok'), priority=100)
        self.assertEqual(await ctx.call_capability('answer'), 'ok')
        await ctx.close()
        self.assertEqual(ctx.governor.capabilities.names(), [])

    async def test_replay_scoped_and_sensitive_payload_masked(self):
        ctx = self.context()
        @ctx.on_replay('run')
        async def replay(payload):
            return payload['number']
        event = ctx.record_event('record', {'number': 4, 'password': 'private'}, replay_type='run')
        self.assertEqual(event['payload']['password'], '***')
        self.assertEqual(await ctx.governor.replay('example', event['id']), 4)
        with self.assertRaises(LookupError):
            await ctx.governor.replay('other', event['id'])
        await ctx.close()
        with self.assertRaises(LookupError):
            await ctx.governor.replay('example', event['id'])

    async def test_circuit_recovers_after_cooldown(self):
        ctx = self.context()
        ctx.governor.configure('example', {'failure_threshold': 1})
        with self.assertRaises(ValueError):
            await ctx.execute('test', AsyncMock(side_effect=ValueError('bad')))
        with self.assertRaises(RuntimeError):
            await ctx.execute('test', AsyncMock(return_value='ok'))
        for state in ctx.governor._circuits.values():
            state.opened_until = 0
        self.assertEqual(await ctx.execute('test', AsyncMock(return_value='ok')), 'ok')
        await ctx.close()

    async def test_account_instances_isolate_storage_jobs_and_cleanup(self):
        from awbotnest.plugins import PluginRuntime
        settings = Settings(api_id=1, api_hash='test', enabled_plugins=[])
        accounts = SimpleNamespace(users={name: SimpleNamespace(is_connected=lambda: True)
                                         for name in ('alice', 'bob')}, bots={})
        with patch('awbotnest.services.DATA_DIR', self.root), patch('awbotnest.deps.DATA_DIR', self.root):
            services = PlatformServices(settings)
            runtime = PluginRuntime(settings, accounts, self.scheduler, services, PluginRoutes(), SimpleNamespace(), self.root)
        async def setup(ctx):
            ctx.kv.set('account', ctx.account_name)
            ctx.schedule_interval('same-name', AsyncMock(), seconds=100)
        runtime.entry_file = lambda plugin_id: self.root / 'example.py'
        runtime._metadata = lambda path: {'id': 'example', 'scope': 'user', 'instance_mode': 'account'}
        runtime._import = lambda *args: SimpleNamespace(setup=setup)
        runtime.deps.ensure = AsyncMock()
        with patch('awbotnest.context.DATA_DIR', self.root), patch('awbotnest.storage.DATA_DIR', self.root), \
             patch('awbotnest.plugins.save_settings'):
            meta = await runtime.enable('example')
            self.assertEqual(meta.error, '')
            contexts = runtime.loaded['example'].contexts
            self.assertEqual([ctx.kv.get('account') for ctx in contexts], ['alice', 'bob'])
            self.assertNotEqual(contexts[0].data_dir, contexts[1].data_dir)
            self.assertEqual(len(self.scheduler.jobs()), 2)
            await runtime.disable('example')
            self.assertEqual(self.scheduler.jobs(), [])


if __name__ == '__main__':
    unittest.main()
