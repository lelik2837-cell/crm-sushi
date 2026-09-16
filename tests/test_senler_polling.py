"""Durability, migration and worker isolation; no production data or network."""
import fcntl
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'crm'))
from flask import Flask
from senler import register_senler
from senler_api import BotAPI, DeliveryError, telegram_uses_polling
from senler_core import now
from senler_telegram import TelegramPoller
from test_senler import FakeAPI


class TelegramPollingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='senler-poll-test-')
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'crm.db')
        def get_db():
            conn = sqlite3.connect(self.path, timeout=1)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            return conn
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        self.app.testing = True
        self.service = register_senler(self.app, get_db, self.path, lambda _: True)
        token = self.service.cipher().encrypt(b'private-token').decode()
        with self.service.db() as conn:
            self.channel_id = conn.execute('''INSERT INTO senler_channels
                (owner_id,kind,name,external_id,token,webhook_secret,status,created_at)
                VALUES (1,'telegram','Test','7',?,'test-secret','connected',?)''', (token, now())).lastrowid
        self.api = Mock()
        self.service.api_factory = lambda *_: self.api
        self.poller = TelegramPoller(self.service)
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=1, role='owner', senler_csrf='test-csrf')
        for context in (patch.dict(os.environ, {'SENLER_TELEGRAM_RECEIVE_MODE': 'polling'}),
                        patch('senler_api.requests.request', side_effect=AssertionError('Unexpected network call'))):
            context.start()
            self.addCleanup(context.stop)

    def row(self, table):
        with self.service.db() as conn:
            row = conn.execute('SELECT * FROM ' + table + ' LIMIT 1').fetchone()
            return dict(row) if row else None

    def message(self, update_id, text='/start'):
        return {'update_id': update_id, 'message': {'message_id': update_id, 'text': text,
            'chat': {'id': 42, 'type': 'private'}, 'from': {'id': 42, 'first_name': 'Test'}}}

    def receive(self, batches, poller=None):
        receiver = poller or self.poller
        batches = iter(batches)
        def updates(offset):
            batch = next(batches, None)
            if batch is None:
                receiver.stopped.set()
                return []
            return batch
        self.api.get_updates.side_effect = updates
        receiver.run(self.channel_id)

    def test_default_mode_connects_without_public_https_and_preserves_pending_updates(self):
        with patch.dict(os.environ):
            os.environ.pop('SENLER_TELEGRAM_RECEIVE_MODE', None)
            self.assertTrue(telegram_uses_polling())
            api = BotAPI({'kind': 'telegram'}, 'private-token')
            with patch.object(api, 'telegram', return_value=True) as call:
                self.assertEqual(api.connect('http://internal'), '')
            call.assert_called_once_with('deleteWebhook', {'drop_pending_updates': False})

    def test_long_poll_uses_proxy_and_timeout_longer_than_telegram_wait(self):
        response = Mock(ok=True, status_code=200, headers={})
        response.json.return_value = {'ok': True, 'result': []}
        with patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': 'socks5h://xray:1080'}), \
                patch('senler_api.requests.request', return_value=response) as call:
            self.assertEqual(BotAPI({'kind': 'telegram'}, 'private-token').get_updates(87), [])
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs['proxies']['https'], 'socks5h://xray:1080')
        self.assertEqual(call.call_args.kwargs['timeout'], (5, 35))
        self.assertEqual(call.call_args.kwargs['json'], {'offset': 87, 'limit': 100, 'timeout': 25,
                          'allowed_updates': ['message', 'callback_query', 'my_chat_member']})

    def test_http_connect_switches_to_polling_with_no_public_url(self):
        self.service.api_factory = BotAPI
        identity = Mock(ok=True, status_code=200, headers={})
        identity.json.return_value = {'ok': True, 'result': {'id': 7, 'is_bot': True, 'username': 'test_bot'}}
        removed = Mock(ok=True, status_code=200, headers={})
        removed.json.return_value = {'ok': True, 'result': True}
        with patch.dict(os.environ, {'SENLER_PUBLIC_URL': ''}), \
                patch('senler_api.requests.request', side_effect=[identity, removed]) as call:
            response = self.client.post('/reports/senler/api/channels/{}/connect'.format(self.channel_id),
                                        json={}, headers={'X-CSRF-Token': 'test-csrf'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([c.args[1].rsplit('/', 1)[-1] for c in call.call_args_list], ['getMe', 'deleteWebhook'])
        self.assertEqual(self.row('senler_channels')['status'], 'connected')

    def test_failed_webhook_removal_does_not_start_polling_or_drop_queue(self):
        def fail():
            self.poller.stopped.set()
            raise DeliveryError('Не удалось переключить приём.', retry_after=30)
        self.api.start_polling.side_effect = fail
        self.poller.run(self.channel_id)
        self.api.get_updates.assert_not_called()
        self.assertEqual(self.row('senler_telegram_polling')['next_offset'], 0)
        self.assertEqual(self.row('senler_telegram_polling')['last_error'], 'Не удалось переключить приём.')

    def test_restart_acknowledges_only_committed_cursor_and_deduplicates_webhook(self):
        update = self.message(10)
        self.receive([[update]])
        self.assertEqual([c.args[0] for c in self.api.get_updates.call_args_list], [0, 11])
        self.assertEqual(self.row('senler_telegram_polling')['next_offset'], 11)
        response = self.client.post('/api/senler/webhook/telegram/{}'.format(self.channel_id), json=update,
                                    headers={'X-Telegram-Bot-Api-Secret-Token': 'test-secret'})
        self.assertEqual(response.status_code, 200)
        self.api.reset_mock()
        self.receive([[update, self.message(11, 'hello')]], TelegramPoller(self.service))
        self.assertEqual([c.args[0] for c in self.api.get_updates.call_args_list], [11, 12])
        with self.service.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM senler_events').fetchone()[0], 2)
        self.assertEqual(self.row('senler_telegram_polling')['next_offset'], 12)
        self.api.start_polling.assert_called_once()

    def test_failed_batch_rolls_back_events_and_cursor(self):
        channel = self.poller.channel(self.channel_id)
        with self.service.db() as conn:
            conn.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON senler_events WHEN NEW.event_key='2'
                            BEGIN SELECT RAISE(ABORT,'simulated storage failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.poller.save_updates(channel, [self.message(1), self.message(2)])
        self.assertIsNone(self.row('senler_events'))
        self.assertIsNone(self.row('senler_telegram_polling'))
        with self.service.db() as conn:
            conn.execute('DROP TRIGGER fail_event')
        self.receive([[self.message(1), self.message(2)]])
        self.assertEqual(self.api.get_updates.call_args_list[0].args[0], 0)
        self.assertEqual(self.row('senler_telegram_polling')['next_offset'], 3)

    def test_unknown_and_group_updates_advance_cursor_without_subscribers(self):
        group = self.message(8)
        group['message']['chat']['type'] = 'group'
        self.receive([[group, {'update_id': 9, 'edited_message': {}}]])
        self.assertIsNone(self.row('senler_events'))
        self.assertEqual(self.row('senler_telegram_polling')['next_offset'], 10)
        self.assertIsNone(self.row('senler_telegram_polling')['received_at'])

    def test_invalid_batch_does_not_advance_cursor(self):
        for batch in (None, {}, [self.message(1), {'update_id': '2'}], [{'update_id': True}]):
            with self.subTest(batch=batch), self.assertRaises(DeliveryError):
                self.poller.save_updates(self.poller.channel(self.channel_id), batch)
        self.assertIsNone(self.row('senler_events'))
        self.assertIsNone(self.row('senler_telegram_polling'))

    def test_inflight_response_is_not_confirmed_after_pause_or_token_change(self):
        for field, value in [('status', 'paused'), ('token', 'changed')]:
            with self.subTest(field=field):
                channel = self.poller.channel(self.channel_id)
                with self.service.db() as conn:
                    conn.execute('UPDATE senler_channels SET ' + field + '=? WHERE id=?', (value, self.channel_id))
                self.assertFalse(self.poller.save_updates(channel, [self.message(1)]))
                self.assertIsNone(self.row('senler_events'))
                self.assertIsNone(self.row('senler_telegram_polling'))
                with self.service.db() as conn:
                    conn.execute('UPDATE senler_channels SET ' + field + '=? WHERE id=?', (channel[field], self.channel_id))

    def test_receive_does_not_hold_database_transaction_during_network(self):
        def network(offset):
            with self.service.db() as conn:
                conn.execute("UPDATE senler_channels SET name='Edited while polling' WHERE id=?", (self.channel_id,))
            self.poller.stopped.set()
            return [self.message(1)]
        self.api.get_updates.side_effect = network
        self.poller.run(self.channel_id)
        self.assertEqual(self.row('senler_channels')['name'], 'Edited while polling')
        self.assertEqual(self.row('senler_telegram_polling')['next_offset'], 2)

    def test_competing_workers_do_not_poll_until_lock_is_released(self):
        with self.poller.lock_path(self.channel_id).open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.poller.run(self.channel_id)
            self.api.start_polling.assert_not_called()
            self.api.get_updates.assert_not_called()
        self.receive([[self.message(1)]])
        self.api.start_polling.assert_called_once()
        self.assertEqual(self.row('senler_telegram_polling')['next_offset'], 2)

    def test_error_backoff_survives_restart_and_is_visible_to_owner(self):
        def failed(offset):
            self.poller.stopped.set()
            raise DeliveryError('Telegram временно недоступен.', retry_after=61)
        self.api.get_updates.side_effect = failed
        self.poller.run(self.channel_id)
        state = self.row('senler_telegram_polling')
        self.assertEqual(state['next_offset'], 0)
        self.assertGreaterEqual(state['retry_at'], now() + 60)
        receiver = TelegramPoller(self.service)
        self.api.reset_mock()
        with patch.object(receiver.stopped, 'wait', side_effect=lambda _: receiver.stopped.set()):
            receiver.run(self.channel_id)
        self.api.start_polling.assert_not_called()
        self.api.get_updates.assert_not_called()
        data = self.client.get('/reports/senler/api/bootstrap').get_json()['channels'][0]
        self.assertEqual(data['receive_mode'], 'polling')
        self.assertEqual(data['telegram_poll_error'], 'Telegram временно недоступен.')
        self.assertNotIn('token', data)

    def test_poller_handles_start_subscribe_and_stop_through_existing_queue(self):
        self.receive([[self.message(1)]])
        self.service.api_factory = FakeAPI
        FakeAPI.sent, FakeAPI.failure, FakeAPI.conversations = [], None, {}
        with patch('senler_core.time.sleep'):
            self.service.tick(seconds=0.2)
        self.assertEqual(self.row('senler_subscribers')['status'], 'pending')
        self.assertEqual(FakeAPI.sent[0][2]['buttons'][0]['value'], 'subscribe')
        callback = {'update_id': 2, 'callback_query': {'id': 'cb1', 'data': 'subscribe',
            'from': {'id': 42, 'first_name': 'Test'}, 'message': {'chat': {'id': 42, 'type': 'private'}}}}
        self.poller.save_updates(self.poller.channel(self.channel_id), [callback])
        with patch('senler_core.time.sleep'):
            self.service.tick(seconds=0.2)
        self.assertEqual(self.row('senler_subscribers')['status'], 'active')
        self.poller.save_updates(self.poller.channel(self.channel_id), [self.message(3, '/stop')])
        with patch('senler_core.time.sleep'):
            self.service.tick(seconds=0.2)
        self.assertEqual(self.row('senler_subscribers')['status'], 'unsubscribed')

    def test_supervisor_starts_only_connected_telegram_and_replaces_dead_threads(self):
        with self.service.db() as conn:
            for kind, status in [('vk', 'connected'), ('max', 'connected'), ('telegram', 'paused')]:
                conn.execute('''INSERT INTO senler_channels(owner_id,kind,name,token,webhook_secret,status,created_at)
                    VALUES (1,?,'Other','fake','fake',?,?)''', (kind, status, now()))
        with patch('senler_telegram.threading.Thread') as thread:
            thread.return_value.is_alive.return_value = True
            self.poller.refresh()
            self.poller.refresh()
            thread.assert_called_once()
            self.assertEqual(thread.call_args.kwargs['args'], (self.channel_id,))
            thread.return_value.is_alive.return_value = False
            self.poller.refresh()
            self.assertEqual(thread.call_count, 2)
        self.assertEqual(len(self.poller.threads), 1)


if __name__ == '__main__':
    unittest.main()
