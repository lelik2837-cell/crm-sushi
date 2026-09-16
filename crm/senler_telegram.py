"""Telegram long polling, independent of the Senler delivery scheduler."""
import fcntl
import hashlib
import logging
from pathlib import Path
import tempfile
import threading

import requests

from senler_api import DeliveryError, TRANSIENT_NETWORK_ERRORS, parse_event, telegram_uses_polling
from senler_core import dumps, now

log = logging.getLogger(__name__)


class TelegramPoller:
    def __init__(self, service):
        self.service = service
        self.threads = {}
        self.stopped = threading.Event()

    def refresh(self):
        """Called every five seconds in each gunicorn worker; no network here."""
        if self.stopped.is_set() or not telegram_uses_polling():
            return
        with self.service.db() as conn:
            channels = conn.execute("SELECT id FROM senler_channels WHERE kind='telegram' AND status='connected'").fetchall()
        self.threads = {key: thread for key, thread in self.threads.items() if thread.is_alive()}
        for channel in channels:
            channel_id = channel['id']
            if channel_id not in self.threads:
                thread = threading.Thread(target=self.run, args=(channel_id,),
                                          name='senler-telegram-{}'.format(channel_id), daemon=True)
                self.threads[channel_id] = thread
                thread.start()

    def lock_path(self, channel_id):
        database = str(Path(self.service.database_path).resolve())
        key = hashlib.sha256(database.encode()).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / 'senler-telegram-{}-{}.lock'.format(key, channel_id)

    def channel(self, channel_id):
        with self.service.db() as conn:
            row = conn.execute("SELECT * FROM senler_channels WHERE id=? AND kind='telegram' AND status='connected'",
                               (channel_id,)).fetchone()
            return dict(row) if row else None

    def save_updates(self, channel, updates):
        if not isinstance(updates, list):
            raise DeliveryError('Telegram вернул неожиданный список сообщений.')
        parsed = []
        for update in updates:
            if (not isinstance(update, dict) or type(update.get('update_id')) is not int
                    or update['update_id'] < 0):
                raise DeliveryError('Telegram вернул сообщение без номера события.')
            try:
                event = parse_event('telegram', update)
            except (KeyError, TypeError, ValueError, AttributeError):
                raise DeliveryError('Не удалось прочитать входящее событие Telegram.') from None
            parsed.append((update['update_id'], event))
        with self.service.db() as conn:
            conn.execute('BEGIN IMMEDIATE')
            current = conn.execute('SELECT token,status FROM senler_channels WHERE id=?', (channel['id'],)).fetchone()
            if not current or current['token'] != channel['token'] or current['status'] != 'connected':
                return False
            conn.execute('INSERT OR IGNORE INTO senler_telegram_polling(channel_id) VALUES (?)', (channel['id'],))
            # Events and cursor commit together. The NEXT getUpdates request may
            # acknowledge only this durable cursor, never an in-memory response.
            for update_id, event in sorted(parsed, key=lambda item: item[0]):
                if event:
                    conn.execute('INSERT OR IGNORE INTO senler_events(channel_id,event_key,body_json,created_at) VALUES (?,?,?,?)',
                                 (channel['id'], str(update_id), dumps(event), now()))
            conn.execute("UPDATE senler_telegram_polling SET next_offset=MAX(next_offset,?),polled_at=?,last_error='',retry_at=0 WHERE channel_id=?",
                         (max((item[0] + 1 for item in parsed), default=0), now(), channel['id']))
            if any(event for _, event in parsed):
                conn.execute('UPDATE senler_telegram_polling SET received_at=? WHERE channel_id=?', (now(), channel['id']))
        return True

    def record_error(self, channel_id, message, delay):
        with self.service.db() as conn:
            conn.execute('INSERT OR IGNORE INTO senler_telegram_polling(channel_id) VALUES (?)', (channel_id,))
            conn.execute('UPDATE senler_telegram_polling SET last_error=?,retry_at=? WHERE channel_id=?',
                         (message, now() + delay, channel_id))

    def run(self, channel_id):
        # The kernel releases the lock even after SIGKILL. The other worker's
        # supervisor can then take over, using the cursor saved in SQLite.
        with self.lock_path(channel_id).open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            try:
                self._receive(channel_id)
            except Exception:
                # Never log exception details: API URLs contain the bot token.
                log.error('Senler Telegram receiver stopped for channel %s', channel_id)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _receive(self, channel_id):
        # One pool per receiver thread: reuse the TCP/TLS tunnel between polls.
        # Sending and callbacks use separate connections, never a busy long poll.
        with requests.Session() as session:
            self._receive_session(channel_id, session.request)

    def _receive_session(self, channel_id, requester):
        prepared_token = None
        network_failures = 0
        while not self.stopped.is_set() and telegram_uses_polling():
            channel = self.channel(channel_id)
            if not channel:
                return
            with self.service.db() as conn:
                state = conn.execute('SELECT * FROM senler_telegram_polling WHERE channel_id=?', (channel_id,)).fetchone()
            if state and state['retry_at'] > now():
                self.stopped.wait(min(5, state['retry_at'] - now()))
                continue
            try:
                api = self.service.api(channel, requester=requester)
                if prepared_token != channel['token']:
                    api.start_polling()
                    prepared_token = channel['token']
                updates = api.get_updates(state['next_offset'] if state else 0)
                if not self.save_updates(channel, updates):
                    return
                network_failures = 0
            except DeliveryError as exc:
                # A dropped getUpdates connection does not reinstall a webhook.
                # Repeating deleteWebhook here added another slow VPN round trip.
                if exc.api_code == 409:
                    prepared_token = None
                if exc.network_code in TRANSIENT_NETWORK_ERRORS or exc.network_code in ('DNS', 'CONNECTION_REFUSED'):
                    network_failures += 1
                    delay = min(10, 2 ** min(network_failures - 1, 4))
                else:
                    # Keep Telegram's explicit Retry-After (especially HTTP 429).
                    delay = max(1, exc.retry_after or 30)
                self.record_error(channel_id, str(exc), delay)
            except ValueError:
                prepared_token = None
                self.record_error(channel_id, 'Не удалось прочитать ключ бота Telegram. Проверьте настройки канала.', 60)
            except Exception:
                # A failed DB commit leaves the cursor unchanged. Retry the same
                # batch; never confirm data that did not reach durable storage.
                self.record_error(channel_id, 'Не удалось сохранить сообщения Telegram. Приём будет повторён.', 30)
