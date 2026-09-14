"""Durable subscribers, bot runs and delivery queue for CRM's Senler section."""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet, InvalidToken

from senler_api import BotAPI, DeliveryError

KINDS = {'vk': 'ВКонтакте', 'telegram': 'Telegram', 'max': 'MAX'}
STOP_WORDS = {'/stop', 'стоп', 'отписаться', 'unsubscribe'}
START_WORDS = {'/start', 'начать', 'подписаться', 'subscribe'}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def now():
    return int(time.time())


def init_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS senler_channels (
            id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('vk','telegram','max')),
            name TEXT NOT NULL, external_id TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
            token TEXT NOT NULL, webhook_secret TEXT NOT NULL, confirmation TEXT NOT NULL DEFAULT '',
            callback_server_id TEXT NOT NULL DEFAULT '', subscribe_url TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'configured', last_error TEXT NOT NULL DEFAULT '',
            checked_at INTEGER, webhook_at INTEGER, created_at INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS senler_channel_identity ON senler_channels(kind,external_id) WHERE external_id != '';
        CREATE TABLE IF NOT EXISTS senler_subscribers (
            id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES senler_channels(id),
            external_user_id TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', username TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','active','unsubscribed','blocked')),
            source TEXT NOT NULL DEFAULT '', consent_at INTEGER, created_at INTEGER NOT NULL, last_seen INTEGER,
            bot_paused INTEGER NOT NULL DEFAULT 0, unread INTEGER NOT NULL DEFAULT 0,
            UNIQUE(channel_id,external_user_id)
        );
        CREATE INDEX IF NOT EXISTS senler_subscriber_status ON senler_subscribers(channel_id,status);
        CREATE TABLE IF NOT EXISTS senler_groups (id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(owner_id,name));
        CREATE TABLE IF NOT EXISTS senler_group_members (
            group_id INTEGER NOT NULL REFERENCES senler_groups(id), subscriber_id INTEGER NOT NULL REFERENCES senler_subscribers(id),
            PRIMARY KEY(group_id,subscriber_id)
        );
        CREATE TABLE IF NOT EXISTS senler_assets (
            id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, data BLOB NOT NULL, mime TEXT NOT NULL, extension TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS senler_asset_channels (
            asset_id INTEGER NOT NULL REFERENCES senler_assets(id), channel_id INTEGER NOT NULL REFERENCES senler_channels(id),
            payload_json TEXT NOT NULL, PRIMARY KEY(asset_id,channel_id)
        );
        CREATE TABLE IF NOT EXISTS senler_imports (
            id TEXT PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES senler_channels(id), user_id INTEGER NOT NULL,
            rows_json TEXT NOT NULL, summary_json TEXT NOT NULL, created_at INTEGER NOT NULL, applied_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS senler_campaigns (
            id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL, body_json TEXT NOT NULL, audience_json TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'draft', scheduled_at INTEGER, created_at INTEGER NOT NULL,
            started_at INTEGER, finished_at INTEGER, created_by INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS senler_bots (
            id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, name TEXT NOT NULL, channel_id INTEGER NOT NULL REFERENCES senler_channels(id),
            trigger_type TEXT NOT NULL, keywords TEXT NOT NULL DEFAULT '', priority INTEGER NOT NULL DEFAULT 10,
            draft_json TEXT NOT NULL, published_json TEXT, published_trigger TEXT, published_keywords TEXT,
            version INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft', updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS senler_runs (
            id INTEGER PRIMARY KEY, bot_id INTEGER NOT NULL REFERENCES senler_bots(id),
            subscriber_id INTEGER NOT NULL REFERENCES senler_subscribers(id), definition_json TEXT NOT NULL,
            node_id TEXT NOT NULL, nonce TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'running',
            due_at INTEGER NOT NULL, created_at INTEGER NOT NULL, error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS senler_run_due ON senler_runs(status,due_at);
        CREATE TABLE IF NOT EXISTS senler_outbox (
            id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES senler_channels(id),
            subscriber_id INTEGER NOT NULL REFERENCES senler_subscribers(id), kind TEXT NOT NULL,
            campaign_id INTEGER REFERENCES senler_campaigns(id), run_id INTEGER REFERENCES senler_runs(id), node_id TEXT,
            body_json TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending',
            due_at INTEGER NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER, sent_at INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0, external_id TEXT, error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS senler_outbox_due ON senler_outbox(status,due_at);
        CREATE INDEX IF NOT EXISTS senler_outbox_campaign ON senler_outbox(campaign_id,status);
        CREATE TABLE IF NOT EXISTS senler_messages (
            id INTEGER PRIMARY KEY, subscriber_id INTEGER NOT NULL REFERENCES senler_subscribers(id),
            direction TEXT NOT NULL, text TEXT NOT NULL, asset_id INTEGER REFERENCES senler_assets(id),
            outbox_id INTEGER UNIQUE REFERENCES senler_outbox(id), created_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS senler_message_subscriber ON senler_messages(subscriber_id,id);
        CREATE TABLE IF NOT EXISTS senler_events (
            id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL REFERENCES senler_channels(id),
            event_key TEXT NOT NULL, body_json TEXT NOT NULL, created_at INTEGER NOT NULL, processed_at INTEGER,
            UNIQUE(channel_id,event_key)
        );
        CREATE TABLE IF NOT EXISTS senler_audit (
            id INTEGER PRIMARY KEY, action TEXT NOT NULL, detail TEXT NOT NULL, user_id INTEGER, created_at INTEGER NOT NULL
        );
    ''')


def integer(value, label='Значение', minimum=1, maximum=2147483647):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError(label + ': укажите целое число.')
    if not minimum <= parsed <= maximum:
        raise ValueError('{}: допустимо от {} до {}.'.format(label, minimum, maximum))
    return parsed


def validate_body(body, allow_goto=False):
    if not isinstance(body, dict):
        raise ValueError('Сообщение не заполнено.')
    text = str(body.get('text', '')).strip()
    asset_id = integer(body['asset_id']) if body.get('asset_id') else None
    # Telegram photo captions are the shared lower limit. Room for personalization.
    limit = 900 if asset_id else 3500
    if not text or len(text) > limit:
        raise ValueError('Текст сообщения должен содержать от 1 до {} символов.'.format(limit))
    if len(text.replace('{имя}', 'x' * 40).replace('{name}', 'x' * 40)) > (1024 if asset_id else 4000):
        raise ValueError('Слишком много подстановок имени. Сократите сообщение.')
    raw_buttons = body.get('buttons', [])
    if not isinstance(raw_buttons, list) or len(raw_buttons) > 5:
        raise ValueError('В сообщении можно добавить до пяти кнопок.')
    buttons = []
    for raw in raw_buttons:
        label = str(raw.get('label', '')).strip()
        action, value = raw.get('action', 'url'), str(raw.get('value', '')).strip()
        if not label or len(label) > 40:
            raise ValueError('Подпись кнопки: от 1 до 40 символов.')
        if action == 'url':
            parsed = urlparse(value)
            if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('В кнопке нужна полная ссылка https://…')
        elif action != 'goto' or not allow_goto or not re.fullmatch(r'[a-zA-Z0-9_-]{1,24}', value):
            raise ValueError('Выберите действие кнопки.')
        buttons.append({'label': label, 'action': action, 'value': value})
    return {'text': text, 'asset_id': asset_id, 'buttons': buttons}


def validate_definition(raw, conn, owner_id=None):
    if not isinstance(raw, dict) or not isinstance(raw.get('nodes'), list) or not 1 <= len(raw['nodes']) <= 40:
        raise ValueError('Добавьте от 1 до 40 шагов сценария.')
    nodes, ids, edges, automatic = [], set(), {}, {}
    for raw_node in raw['nodes']:
        node_id = str(raw_node.get('id', ''))
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,24}', node_id) or node_id in ids:
            raise ValueError('У шагов должны быть разные идентификаторы.')
        ids.add(node_id)
        kind = raw_node.get('type')
        node = {'id': node_id, 'type': kind, 'title': str(raw_node.get('title', ''))[:80], 'next': str(raw_node.get('next') or '')}
        links, auto_links = [], []
        if kind == 'message':
            node.update(validate_body(raw_node, allow_goto=True))
            if node['asset_id'] and not conn.execute('SELECT 1 FROM senler_assets WHERE id=?' + (' AND owner_id=?' if owner_id is not None else ''),
                    (node['asset_id'], owner_id) if owner_id is not None else (node['asset_id'],)).fetchone():
                raise ValueError('Картинка шага не найдена. Загрузите её заново.')
            links = [b['value'] for b in node['buttons'] if b['action'] == 'goto']
            if links and node['next']:
                raise ValueError('В шаге с кнопками перехода продолжение выбирает подписчик. Уберите автоматический переход.')
        elif kind == 'delay':
            node['minutes'] = integer(raw_node.get('minutes'), 'Задержка в минутах', maximum=43200)
        elif kind in ('condition', 'group'):
            node['group_id'] = integer(raw_node.get('group_id'), 'Группа подписчиков')
            if not conn.execute('SELECT 1 FROM senler_groups WHERE id=?' + (' AND owner_id=?' if owner_id is not None else ''),
                    (node['group_id'], owner_id) if owner_id is not None else (node['group_id'],)).fetchone():
                raise ValueError('Выбранная группа подписчиков не существует.')
            if kind == 'condition':
                node.update(yes=str(raw_node.get('yes') or ''), no=str(raw_node.get('no') or ''), next='')
                auto_links.extend([node['yes'], node['no']])
            else:
                node['mode'] = 'remove' if raw_node.get('mode') == 'remove' else 'add'
        elif kind != 'handoff':
            raise ValueError('Неизвестный тип шага.')
        if node['next']:
            auto_links.append(node['next'])
        edges[node_id] = [link for link in links + auto_links if link]
        automatic[node_id] = [link for link in auto_links if link] if kind != 'handoff' else []
        nodes.append(node)
    if not any(n['type'] == 'message' for n in nodes):
        raise ValueError('В сценарии нужен хотя бы один шаг с сообщением.')
    if any(target not in ids for links in edges.values() for target in links):
        raise ValueError('Один из переходов ведёт к удалённому шагу.')
    entry = str(raw.get('entry') or nodes[0]['id'])
    if entry not in ids:
        raise ValueError('Выберите начальный шаг.')
    def visit(key, stack, done):
        if key in stack:
            raise ValueError('В сценарии получился автоматический цикл. Замкнуть меню можно кнопкой, которую нажимает подписчик.')
        if key in done:
            return
        for target in automatic[key]:
            visit(target, stack | {key}, done)
        done.add(key)
    done = set()
    for key in ids:
        visit(key, set(), done)
    reachable, queue = set(), [entry]
    while queue:
        key = queue.pop()
        if key not in reachable:
            reachable.add(key)
            queue.extend(edges[key])
    if reachable != ids:
        raise ValueError('Есть шаги, до которых нельзя дойти. Добавьте к ним переход или удалите их.')
    return {'entry': entry, 'nodes': nodes}


def parse_import(raw):
    if not raw or len(raw) > 4 * 1024 * 1024:
        raise ValueError('Загрузите TXT или CSV размером до 4 МБ.')
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = raw.decode('cp1251')
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError('Файл пуст.')
    delimiter = ';' if lines[0].count(';') >= lines[0].count(',') else ','
    if '\t' in lines[0]:
        delimiter = '\t'
    table = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    normalize = lambda x: re.sub(r'[^a-zа-я0-9]', '', str(x).lower())
    headers = [normalize(h) for h in table[0]]
    aliases = ['vkuserid', 'userid', 'id', 'externaluserid', 'tguserid', 'maxuserid', 'vkid', 'идентификатор', 'идпользователя']
    has_header = any(h in aliases for h in headers)
    index = next((headers.index(a) for a in aliases if a in headers), 0)
    def field(row, names):
        for name in names:
            if name in headers and headers.index(name) < len(row):
                return row[headers.index(name)].strip()
        return ''
    result, seen, invalid, duplicate, excluded = [], set(), 0, 0, 0
    for row in table[1:] if has_header else table:
        if not row or not any(row):
            continue
        user_id = row[index].strip() if index < len(row) else ''
        user_id = re.sub(r'^https?://(?:www\.)?(?:vk\.com|vk\.ru)/id', '', user_id).strip().rstrip('/')
        if not re.fullmatch(r'[1-9][0-9]{0,18}', user_id):
            invalid += 1
            continue
        if user_id in seen:
            duplicate += 1
            continue
        seen.add(user_id)
        status = field(row, ['status', 'leadstatus', 'статус']).lower() if has_header else ''
        ignore = field(row, ['ignore', 'blacklist', 'черныйсписок']).lower() if has_header else ''
        is_excluded = status in ('2', 'inactive', 'unsubscribed', 'blocked', 'неактивный', 'отписан', 'отписался') or ignore in ('1', 'true', 'да')
        if is_excluded:
            excluded += 1
        name = (field(row, ['name', 'имя', 'фио']) or ' '.join(filter(None, [field(row, ['firstname']), field(row, ['lastname'])]))) if has_header else ''
        result.append({'external_user_id': user_id, 'name': name[:160], 'excluded': is_excluded})
        if len(result) > 50000:
            raise ValueError('За один импорт можно загрузить до 50 000 подписчиков.')
    return result, {'valid': len(result) - excluded, 'invalid': invalid, 'duplicates': duplicate, 'excluded': excluded}


class SenlerService:
    def __init__(self, get_db, database_path, api_factory=BotAPI):
        self.get_db, self.database_path, self.api_factory = get_db, database_path, api_factory

    @contextmanager
    def db(self):
        conn = self.get_db()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def cipher(self):
        key = os.environ.get('SENLER_ENCRYPTION_KEY')
        if not key:
            path = Path(os.environ.get('SENLER_KEY_PATH') or (str(self.database_path) + '.senler-key'))
            if not path.exists():
                with self.db() as conn:
                    if conn.execute('SELECT 1 FROM senler_channels LIMIT 1').fetchone():
                        raise ValueError('Не найден ключ шифрования каналов. Восстановите файл ключа из резервной копии.')
                try:
                    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, 'wb') as handle:
                        handle.write(Fernet.generate_key())
                except FileExistsError:
                    pass
            key = path.read_bytes().strip()
        try:
            return Fernet(key)
        except (ValueError, TypeError):
            raise ValueError('Ключ шифрования каналов повреждён.')

    def api(self, channel):
        try:
            token = self.cipher().decrypt(channel['token'].encode()).decode()
        except InvalidToken:
            raise ValueError('Не удалось прочитать ключ канала. Проверьте ключ шифрования CRM.')
        return self.api_factory(channel, token)

    def audit(self, conn, action, detail, user_id=None):
        conn.execute('INSERT INTO senler_audit(action,detail,user_id,created_at) VALUES (?,?,?,?)', (action, detail, user_id, now()))

    def audience(self, conn, spec, owner_id):
        channels = spec.get('channels', [])
        groups = spec.get('groups', [])
        if not isinstance(channels, list) or not channels or len(channels) > 50:
            raise ValueError('Выберите хотя бы один канал.')
        channels = list(dict.fromkeys(integer(c, 'Канал') for c in channels))
        groups = list(dict.fromkeys(integer(g, 'Группа') for g in groups))
        if len(groups) > 100:
            raise ValueError('Выбрано слишком много групп.')
        placeholders = ','.join('?' for _ in channels)
        found = conn.execute('SELECT id FROM senler_channels WHERE owner_id=? AND id IN (' + placeholders + ')', [owner_id] + channels).fetchall()
        if len(found) != len(channels):
            raise ValueError('Один из каналов не найден.')
        if groups:
            found_groups = conn.execute('SELECT id FROM senler_groups WHERE owner_id=? AND id IN (' + ','.join('?' for _ in groups) + ')', [owner_id] + groups).fetchall()
            if len(found_groups) != len(groups):
                raise ValueError('Одна из групп не найдена.')
        params = list(channels)
        query = "SELECT s.* FROM senler_subscribers s WHERE s.status='active' AND s.channel_id IN (" + placeholders + ')'
        if groups:
            query += ' AND EXISTS(SELECT 1 FROM senler_group_members m WHERE m.subscriber_id=s.id AND m.group_id IN (' + ','.join('?' for _ in groups) + '))'
            params.extend(groups)
        return conn.execute(query + ' ORDER BY s.id', params).fetchall()

    def queue(self, conn, sub, body, key, kind='bot', campaign_id=None, run_id=None, node_id=None, due_at=None):
        first_name = (sub['name'].split() or ['друг'])[0][:40]
        text = body['text'].replace('{имя}', first_name).replace('{name}', first_name)
        rendered = dict(body, text=text)
        created = now()
        conn.execute('''INSERT OR IGNORE INTO senler_outbox
            (channel_id,subscriber_id,kind,campaign_id,run_id,node_id,body_json,dedupe_key,due_at,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)''', (sub['channel_id'], sub['id'], kind, campaign_id, run_id, node_id, dumps(rendered), key, due_at or created, created))
        return conn.execute('SELECT id FROM senler_outbox WHERE dedupe_key=?', (key,)).fetchone()['id']

    def stop_subscriber(self, conn, sub_id, status='unsubscribed'):
        conn.execute('UPDATE senler_subscribers SET status=?,bot_paused=0 WHERE id=?', (status, sub_id))
        conn.execute("UPDATE senler_outbox SET status='cancelled',error='Подписка отменена' WHERE subscriber_id=? AND status='pending'", (sub_id,))
        conn.execute("UPDATE senler_runs SET status='cancelled' WHERE subscriber_id=? AND status IN ('running','waiting_send','waiting_reply','paused')", (sub_id,))
        self.audit(conn, 'unsubscribe', 'Подписчик №{}'.format(sub_id))

    def _start_bot(self, conn, sub, trigger, text=''):
        if sub['bot_paused'] or sub['status'] != 'active':
            return
        bots = conn.execute("SELECT * FROM senler_bots WHERE channel_id=? AND status='active' ORDER BY priority,id", (sub['channel_id'],)).fetchall()
        matching = None
        for bot in bots:
            kind = bot['published_trigger']
            keywords = [x.strip().casefold() for x in bot['published_keywords'].split(',') if x.strip()]
            if (trigger == 'subscribe' and kind == 'subscribe') or (trigger == 'message' and (kind == 'any' or (kind == 'keyword' and text.casefold().strip() in keywords))):
                matching = bot
                break
        if not matching:
            return
        conn.execute("UPDATE senler_outbox SET status='cancelled' WHERE subscriber_id=? AND run_id IS NOT NULL AND status='pending'", (sub['id'],))
        conn.execute("UPDATE senler_runs SET status='cancelled' WHERE subscriber_id=? AND status IN ('running','waiting_send','waiting_reply')", (sub['id'],))
        definition = json.loads(matching['published_json'])
        conn.execute('''INSERT INTO senler_runs(bot_id,subscriber_id,definition_json,node_id,nonce,due_at,created_at)
                        VALUES (?,?,?,?,?,?,?)''', (matching['id'], sub['id'], matching['published_json'], definition['entry'], secrets.token_hex(4), now(), now()))

    def _handle_event(self, conn, event_row):
        event = json.loads(event_row['body_json'])
        user_id, channel_id = event['user_id'], event_row['channel_id']
        conn.execute('''INSERT OR IGNORE INTO senler_subscribers(channel_id,external_user_id,name,username,source,created_at)
                        VALUES (?,?,?,?,?,?)''', (channel_id, user_id, event['name'][:160], event['username'][:100], 'bot', now()))
        sub = conn.execute('SELECT * FROM senler_subscribers WHERE channel_id=? AND external_user_id=?', (channel_id, user_id)).fetchone()
        conn.execute('''UPDATE senler_subscribers SET last_seen=?,name=CASE WHEN ?!='' THEN ? ELSE name END,
                        username=CASE WHEN ?!='' THEN ? ELSE username END WHERE id=?''',
                     (now(), event['name'][:160], event['name'][:160], event['username'][:100], event['username'][:100], sub['id']))
        text = str(event.get('text') or '').strip()[:10000]
        command = text.casefold().split(' ', 1)[0] if text else ''
        callback = str(event.get('callback') or '')
        if text:
            conn.execute("INSERT INTO senler_messages(subscriber_id,direction,text,created_at) VALUES (?,'in',?,?)", (sub['id'], text, now()))
            conn.execute('UPDATE senler_subscribers SET unread=unread+1 WHERE id=?', (sub['id'],))
        if event['type'] == 'blocked' or command in STOP_WORDS or callback == 'unsubscribe':
            self.stop_subscriber(conn, sub['id'], 'blocked' if event['type'] == 'blocked' else 'unsubscribed')
            return
        if callback == 'subscribe' or command in {'подписаться', 'subscribe'}:
            changed = sub['status'] != 'active'
            conn.execute("UPDATE senler_subscribers SET status='active',consent_at=?,source='button' WHERE id=?", (now(), sub['id']))
            self.audit(conn, 'subscribe', 'Подписчик №{} подтвердил подписку'.format(sub['id']))
            sub = conn.execute('SELECT * FROM senler_subscribers WHERE id=?', (sub['id'],)).fetchone()
            if changed:
                self._start_bot(conn, sub, 'subscribe')
            self.queue(conn, sub, {'text': 'Вы подписались. Чтобы отменить рассылку, нажмите «Отписаться» или отправьте /stop.',
                       'buttons': [{'label': 'Отписаться', 'action': 'callback', 'value': 'unsubscribe'}]}, 'consent:' + str(event_row['id']), kind='notice')
            return
        if sub['status'] != 'active':
            if event['type'] != 'allow' and (text or event['type'] == 'start'):
                self.queue(conn, sub, {'text': 'Здесь можно получать наши новости и предложения. Подписаться на рассылку? Отменить подписку можно в любой момент командой /stop.',
                           'buttons': [{'label': 'Подписаться', 'action': 'callback', 'value': 'subscribe'}]},
                           'optin:' + str(event_row['id']), kind='consent')
            return
        if callback.startswith('sb:'):
            parts = callback.split(':')
            if len(parts) != 4 or not parts[1].isdigit():
                return
            run = conn.execute("SELECT * FROM senler_runs WHERE id=? AND subscriber_id=? AND status='waiting_reply'", (parts[1], sub['id'])).fetchone()
            if not run or not secrets.compare_digest(parts[3], run['nonce']) or sub['bot_paused']:
                return
            node = next(n for n in json.loads(run['definition_json'])['nodes'] if n['id'] == run['node_id'])
            if parts[2] not in [b['value'] for b in node.get('buttons', []) if b['action'] == 'goto']:
                return
            # New nonce invalidates every button from the previous visit, including loops.
            conn.execute("UPDATE senler_runs SET node_id=?,status='running',due_at=?,nonce=? WHERE id=?", (parts[2], now(), secrets.token_hex(4), run['id']))
            label = next(b['label'] for b in node['buttons'] if b['value'] == parts[2])
            text = 'Кнопка: ' + label
            conn.execute("INSERT INTO senler_messages(subscriber_id,direction,text,created_at) VALUES (?,'in',?,?)", (sub['id'], text, now()))
            conn.execute('UPDATE senler_subscribers SET unread=unread+1 WHERE id=?', (sub['id'],))
        if text:
            if not callback:
                self._start_bot(conn, sub, 'subscribe' if command in START_WORDS else 'message', text)
        elif event['type'] == 'start':
            self._start_bot(conn, sub, 'subscribe')

    def _advance_run(self, conn, run, next_node, due_at=None):
        conn.execute('UPDATE senler_runs SET node_id=?,status=?,due_at=? WHERE id=?',
                     (next_node or '', 'running' if next_node else 'completed', due_at or now(), run['id']))

    def _step(self, conn, run):
        sub = conn.execute('SELECT * FROM senler_subscribers WHERE id=?', (run['subscriber_id'],)).fetchone()
        if sub['status'] != 'active':
            conn.execute("UPDATE senler_runs SET status='cancelled' WHERE id=?", (run['id'],))
            return
        if sub['bot_paused']:
            return
        node = next((n for n in json.loads(run['definition_json'])['nodes'] if n['id'] == run['node_id']), None)
        if not node:
            self._advance_run(conn, run, '')
            return
        kind = node['type']
        if kind == 'message':
            buttons = [dict(b, action='callback', value='sb:{}:{}:{}'.format(run['id'], b['value'], run['nonce'])) if b['action'] == 'goto' else b for b in node['buttons']]
            buttons.append({'label': 'Отписаться', 'action': 'callback', 'value': 'unsubscribe'})
            self.queue(conn, sub, dict(node, buttons=buttons), 'run:{}:{}:{}'.format(run['id'], node['id'], run['nonce']), run_id=run['id'], node_id=node['id'])
            conn.execute("UPDATE senler_runs SET status='waiting_send' WHERE id=?", (run['id'],))
        elif kind == 'delay':
            self._advance_run(conn, run, node['next'], now() + node['minutes'] * 60)
        elif kind == 'condition':
            member = conn.execute('SELECT 1 FROM senler_group_members WHERE group_id=? AND subscriber_id=?', (node['group_id'], sub['id'])).fetchone()
            self._advance_run(conn, run, node['yes'] if member else node['no'])
        elif kind == 'group':
            if node['mode'] == 'add':
                conn.execute('INSERT OR IGNORE INTO senler_group_members VALUES (?,?)', (node['group_id'], sub['id']))
            else:
                conn.execute('DELETE FROM senler_group_members WHERE group_id=? AND subscriber_id=?', (node['group_id'], sub['id']))
            self._advance_run(conn, run, node['next'])
        elif kind == 'handoff':
            conn.execute("UPDATE senler_runs SET status='paused',node_id=? WHERE id=?", (node['next'], run['id']))
            conn.execute('UPDATE senler_subscribers SET bot_paused=1,unread=MAX(unread,1) WHERE id=?', (sub['id'],))

    def _delivery(self, job):
        with self.db() as conn:
            channel = dict(conn.execute('SELECT * FROM senler_channels WHERE id=?', (job['channel_id'],)).fetchone())
            sub = conn.execute('SELECT * FROM senler_subscribers WHERE id=?', (job['subscriber_id'],)).fetchone()
            if channel['status'] != 'connected':
                conn.execute("UPDATE senler_outbox SET status='pending',due_at=? WHERE id=?", (now() + 30, job['id']))
                return
            allowed = sub['status'] == 'active' or job['kind'] in ('consent', 'notice')
            if not allowed or (sub['bot_paused'] and job['kind'] == 'bot'):
                conn.execute("UPDATE senler_outbox SET status=? WHERE id=?", ('pending' if sub['bot_paused'] and sub['status'] == 'active' else 'cancelled', job['id']))
                if sub['bot_paused']:
                    conn.execute('UPDATE senler_outbox SET due_at=? WHERE id=?', (now() + 30, job['id']))
                return
            body = json.loads(job['body_json'])
            asset = conn.execute('SELECT * FROM senler_assets WHERE id=?', (body.get('asset_id'),)).fetchone()
            asset = dict(asset) if asset else None
            if asset:
                cached = conn.execute('SELECT payload_json FROM senler_asset_channels WHERE asset_id=? AND channel_id=?', (asset['id'],channel['id'])).fetchone()
                if cached:
                    asset['remote_payload'] = json.loads(cached['payload_json'])
        state, external_id, error, delay, blocked = 'sent', '', '', 0, False
        try:
            api = self.api(channel)
            if asset and channel['kind'] in ('vk', 'max') and 'remote_payload' not in asset:
                asset['remote_payload'] = api.upload_asset(asset, sub['external_user_id'])
                with self.db() as conn:
                    conn.execute('INSERT OR REPLACE INTO senler_asset_channels VALUES (?,?,?)', (asset['id'],channel['id'],dumps(asset['remote_payload'])))
            external_id = api.send(sub['external_user_id'], body, job['id'], asset=asset)
        except DeliveryError as exc:
            error, blocked = str(exc), exc.blocked
            if exc.uncertain:
                # VK random_id is durable and idempotent. Telegram and MAX offer no
                # equivalent: do not blindly repeat an ambiguously successful send.
                state, delay = ('pending', 30) if channel['kind'] == 'vk' and job['attempts'] < 5 else ('unknown', 0)
            elif exc.retry_after and job['attempts'] < 10:
                state, delay = 'pending', exc.retry_after
            else:
                state = 'error'
        except (ValueError, KeyError, TypeError):
            state, error = 'unknown', 'Не удалось подтвердить результат. Проверьте канал и диалог перед повторной отправкой.'
        with self.db() as conn:
            conn.execute('UPDATE senler_outbox SET status=?,external_id=?,error=?,due_at=?,sent_at=? WHERE id=?',
                         (state, external_id, error, now() + delay, now() if state == 'sent' else None, job['id']))
            if delay:
                conn.execute("UPDATE senler_outbox SET due_at=MAX(due_at,?) WHERE channel_id=? AND status='pending'", (now() + delay, channel['id']))
            if blocked:
                self.stop_subscriber(conn, sub['id'], 'blocked')
            if state == 'sent':
                conn.execute("INSERT OR IGNORE INTO senler_messages(subscriber_id,direction,text,asset_id,outbox_id,created_at) VALUES (?,'out',?,?,?,?)",
                             (sub['id'], body['text'], body.get('asset_id'), job['id'], now()))
            if job['run_id'] and state in ('sent', 'error', 'unknown'):
                run = conn.execute("SELECT * FROM senler_runs WHERE id=? AND status='waiting_send'", (job['run_id'],)).fetchone()
                if run:
                    if state != 'sent':
                        conn.execute("UPDATE senler_runs SET status='error',error=? WHERE id=?", (error, run['id']))
                    else:
                        node = next(n for n in json.loads(run['definition_json'])['nodes'] if n['id'] == run['node_id'])
                        if any(b['action'] == 'goto' for b in node['buttons']):
                            conn.execute("UPDATE senler_runs SET status='waiting_reply' WHERE id=?", (run['id'],))
                        else:
                            self._advance_run(conn, run, node['next'])

    def tick(self, seconds=18):
        deadline = time.monotonic() + seconds
        # The caller holds a process-shared flock. A claim left by a dead process
        # becomes unknown, never an automatic duplicate on non-idempotent APIs.
        with self.db() as conn:
            conn.execute("UPDATE senler_outbox SET status='unknown',error='Отправка прервалась при перезапуске. Проверьте диалог.' WHERE status='sending' AND claimed_at<?", (now() - 180,))
            conn.execute("""UPDATE senler_runs SET status='error',error='Отправка прервалась. Проверьте диалог.'
                WHERE status='waiting_send' AND EXISTS(SELECT 1 FROM senler_outbox o WHERE o.run_id=senler_runs.id AND o.status='unknown')""")
            conn.execute("UPDATE senler_campaigns SET status='running',started_at=? WHERE status='scheduled' AND scheduled_at<=?", (now(), now()))
        for _ in range(100):
            if time.monotonic() >= deadline:
                break
            with self.db() as conn:
                event = conn.execute('SELECT * FROM senler_events WHERE processed_at IS NULL ORDER BY id LIMIT 1').fetchone()
                if not event:
                    break
                self._handle_event(conn, event)
                conn.execute('UPDATE senler_events SET processed_at=? WHERE id=?', (now(), event['id']))
                channel = conn.execute('SELECT * FROM senler_channels WHERE id=?', (event['channel_id'],)).fetchone()
            if json.loads(event['body_json']).get('callback_id'):
                try:
                    self.api(channel).acknowledge(json.loads(event['body_json']))
                except (DeliveryError, ValueError):
                    pass
        while time.monotonic() < deadline:
            with self.db() as conn:
                # Drain incoming events, including opt-outs, before resuming sends.
                if conn.execute('SELECT 1 FROM senler_events WHERE processed_at IS NULL LIMIT 1').fetchone():
                    break
                runs = conn.execute('''SELECT r.* FROM senler_runs r JOIN senler_bots b ON b.id=r.bot_id
                    JOIN senler_subscribers s ON s.id=r.subscriber_id JOIN senler_channels c ON c.id=s.channel_id
                    WHERE r.status='running' AND r.due_at<=? AND b.status='active' AND s.bot_paused=0 AND c.status='connected'
                    ORDER BY r.id LIMIT 30''', (now(),)).fetchall()
                for run in runs:
                    self._step(conn, run)
                job = conn.execute('''SELECT o.* FROM senler_outbox o JOIN senler_channels c ON c.id=o.channel_id
                    LEFT JOIN senler_campaigns p ON p.id=o.campaign_id LEFT JOIN senler_runs r ON r.id=o.run_id
                    LEFT JOIN senler_bots b ON b.id=r.bot_id
                    WHERE o.status='pending' AND o.due_at<=? AND c.status='connected'
                    AND (p.id IS NULL OR p.status='running') AND (r.id IS NULL OR (b.status='active' AND r.status='waiting_send'))
                    ORDER BY CASE WHEN o.kind='campaign' THEN 1 ELSE 0 END,o.id LIMIT 1''', (now(),)).fetchone()
                if job:
                    conn.execute("UPDATE senler_outbox SET status='sending',claimed_at=?,attempts=attempts+1 WHERE id=? AND status='pending'", (now(), job['id']))
                    job = dict(job)
                    job['attempts'] += 1
            if job:
                self._delivery(job)
                # A conservative shared pace stays below API quotas and per-dialog limits.
                time.sleep(0.6)
            elif not runs:
                break
        with self.db() as conn:
            conn.execute("""UPDATE senler_campaigns SET status='completed',finished_at=? WHERE status='running'
                AND NOT EXISTS(SELECT 1 FROM senler_outbox o WHERE o.campaign_id=senler_campaigns.id AND o.status IN ('pending','sending'))""", (now(),))
            conn.execute('DELETE FROM senler_imports WHERE created_at<?', (now() - 86400,))
            conn.execute('DELETE FROM senler_events WHERE processed_at IS NOT NULL AND created_at<?', (now() - 30 * 86400,))
