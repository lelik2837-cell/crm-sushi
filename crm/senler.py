"""Flask boundary for the independent Senler module."""
import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session, url_for

from senler_api import DeliveryError, parse_event
from senler_core import KINDS, SenlerService, dumps, init_schema, integer, now, parse_import, validate_body, validate_definition


def register_senler(app, get_db, database_path, item_visible):
    service = SenlerService(get_db, database_path)
    with service.db() as conn:
        init_schema(conn)
    bp = Blueprint('senler', __name__)

    @bp.before_request
    def authorize():
        if request.endpoint == 'senler.webhook':
            return None
        if 'user_id' not in session:
            if '/api/' in request.path:
                return jsonify(error='Сессия завершилась. Войдите в CRM заново.'), 401
            return redirect(url_for('login'))
        if session.get('role') != 'owner' or not item_visible('senler'):
            return jsonify(error='Нет доступа к разделу «Сенлер».'), 403
        if request.method != 'GET':
            expected = session.get('senler_csrf', '')
            supplied = request.headers.get('X-CSRF-Token', '')
            if not expected or not secrets.compare_digest(expected, supplied):
                return jsonify(error='Обновите страницу и повторите действие.'), 403
        if request.content_length and request.content_length > 9 * 1024 * 1024:
            return jsonify(error='Слишком большой файл. Максимум 8 МБ.'), 413

    @bp.errorhandler(ValueError)
    def invalid(exc):
        return jsonify(error=str(exc)), 400

    @bp.errorhandler(DeliveryError)
    def provider_error(exc):
        return jsonify(error=str(exc)), 502

    @bp.errorhandler(sqlite3.IntegrityError)
    def conflict(exc):
        return jsonify(error='Такая запись уже существует или связана с другими данными.'), 409

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('Не удалось прочитать форму. Обновите страницу.')
        return value

    def row(conn, table, item_id):
        # Table names are constants supplied by route code, never user input.
        if table in ('senler_channels', 'senler_groups', 'senler_assets', 'senler_campaigns', 'senler_bots'):
            result = conn.execute('SELECT * FROM ' + table + ' WHERE id=? AND owner_id=?', (item_id, session['user_id'])).fetchone()
        elif table == 'senler_subscribers':
            result = conn.execute('''SELECT s.* FROM senler_subscribers s JOIN senler_channels c ON c.id=s.channel_id
                WHERE s.id=? AND c.owner_id=?''', (item_id,session['user_id'])).fetchone()
        elif table == 'senler_imports':
            result = conn.execute('SELECT * FROM senler_imports WHERE id=? AND user_id=?', (item_id,session['user_id'])).fetchone()
        else:
            raise ValueError('Неизвестный тип записи.')
        if not result:
            raise ValueError('Запись не найдена. Возможно, она уже удалена.')
        return result

    def public_channel(record):
        channel = dict(record)
        channel.pop('token', None)
        channel.pop('webhook_secret', None)
        channel.pop('confirmation', None)
        channel.pop('callback_server_id', None)
        channel['kind_label'] = KINDS[channel['kind']]
        return channel

    def campaign_data(conn, record, details=False):
        item = dict(record)
        item['body'] = json.loads(item.pop('body_json'))
        item['audience'] = json.loads(item.pop('audience_json'))
        counts = {r['status']: r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM senler_outbox WHERE campaign_id=? GROUP BY status', (item['id'],))}
        item['counts'] = counts
        item['total'] = sum(counts.values())
        if details:
            item['subscription_buttons'] = {}
            for snapshot in conn.execute('''SELECT channel_id,body_json FROM senler_outbox WHERE id IN
                    (SELECT MIN(id) FROM senler_outbox WHERE campaign_id=? GROUP BY channel_id)''', (item['id'],)):
                button = next((b for b in json.loads(snapshot['body_json']).get('buttons', [])
                               if b.get('action') == 'callback' and b.get('value') == 'unsubscribe'), None)
                item['subscription_buttons'][str(snapshot['channel_id'])] = {
                    'unsubscribe_enabled': int(button is not None), 'unsubscribe_label': button['label'] if button else 'Отписаться'}
            item['deliveries'] = [dict(r) for r in conn.execute('''SELECT o.id,o.status,o.error,o.sent_at,s.name,s.external_user_id,c.name channel_name
                    FROM senler_outbox o JOIN senler_subscribers s ON s.id=o.subscriber_id JOIN senler_channels c ON c.id=o.channel_id
                    WHERE o.campaign_id=? ORDER BY CASE WHEN o.status IN ('error','unknown') THEN 0 ELSE 1 END,o.id DESC LIMIT 200''', (item['id'],))]
        return item

    @bp.get('/reports/senler')
    def page():
        session.setdefault('senler_csrf', secrets.token_urlsafe(32))
        return render_template('senler.html', senler_csrf=session['senler_csrf'])

    @bp.get('/reports/senler/api/bootstrap')
    def bootstrap():
        channel_id = request.args.get('channel', type=int)
        with service.db() as conn:
            channels = [public_channel(r) for r in conn.execute('''SELECT c.*,
                (SELECT COUNT(*) FROM senler_subscribers s WHERE s.channel_id=c.id AND s.status='active') subscribers
                FROM senler_channels c WHERE c.owner_id=? ORDER BY c.id''', (session['user_id'],))]
            groups = [dict(r) for r in conn.execute('''SELECT g.*,COUNT(m.subscriber_id) members FROM senler_groups g
                LEFT JOIN senler_group_members m ON m.group_id=g.id WHERE g.owner_id=? GROUP BY g.id ORDER BY g.name''', (session['user_id'],))]
            scope = ' WHERE channel_id IN (SELECT id FROM senler_channels WHERE owner_id=?)' + (' AND channel_id=?' if channel_id else '')
            params = [session['user_id']] + ([channel_id] if channel_id else [])
            status = {r['status']: r['n'] for r in conn.execute('SELECT status,COUNT(*) n FROM senler_subscribers' + scope + ' GROUP BY status', params)}
            delivery_scope = ' AND channel_id IN (SELECT id FROM senler_channels WHERE owner_id=?)' + (' AND channel_id=?' if channel_id else '')
            delivery_params = [now() - 30 * 86400] + params
            sent = conn.execute("SELECT COUNT(*) n FROM senler_outbox WHERE status='sent' AND sent_at>=?" + delivery_scope, delivery_params).fetchone()['n']
            errors = conn.execute("SELECT COUNT(*) n FROM senler_outbox WHERE status IN ('error','unknown') AND created_at>=?" + delivery_scope, delivery_params).fetchone()['n']
            active_bots = conn.execute("SELECT COUNT(*) n FROM senler_bots WHERE status='active'" + delivery_scope, params).fetchone()['n']
            running = conn.execute("SELECT COUNT(*) n FROM senler_campaigns WHERE owner_id=? AND status IN ('running','scheduled','paused')", (session['user_id'],)).fetchone()['n']
            recent = [campaign_data(conn, r) for r in conn.execute('SELECT * FROM senler_campaigns WHERE owner_id=? ORDER BY id DESC LIMIT 5', (session['user_id'],))]
            activity = [dict(r) for r in conn.execute('''SELECT date(sent_at,'unixepoch','+7 hours') day,COUNT(*) n FROM senler_outbox
                WHERE status='sent' AND sent_at>=?''' + delivery_scope + ' GROUP BY day ORDER BY day', [now() - 7 * 86400] + params)]
            unread = conn.execute('SELECT COALESCE(SUM(unread),0) n FROM senler_subscribers' + scope, params).fetchone()['n']
        return jsonify(channels=channels, groups=groups, stats=dict(status, sent=sent, errors=errors, active_bots=active_bots, running=running, unread=unread),
                       recent=recent, activity=activity, timezone='Asia/Novosibirsk', server_time=now())

    @bp.post('/reports/senler/api/channels')
    def save_channel():
        data = body()
        unsubscribe_label, unsubscribe_enabled = subscription_button_settings(data)
        kind, name, token = data.get('kind'), str(data.get('name', '')).strip(), str(data.get('token', '')).strip()
        if kind not in KINDS or not name or len(name) > 100:
            raise ValueError('Выберите мессенджер и введите название до 100 символов.')
        if not token or len(token) > 2048 or any(c.isspace() for c in token):
            raise ValueError('Введите ключ сообщества или бота без пробелов.')
        external_id = ''
        if kind == 'vk':
            external_id = str(data.get('external_id', '')).strip()
            if '://' in external_id:
                reference = urlparse(external_id)
                if reference.scheme not in ('http', 'https') or reference.hostname not in ('vk.com', 'vk.ru', 'www.vk.com', 'www.vk.ru', 'm.vk.com', 'm.vk.ru'):
                    raise ValueError('Вставьте ссылку на сообщество vk.com или vk.ru.')
                external_id = reference.path.strip('/')
            if re.fullmatch(r'(club|public)\d+', external_id):
                external_id = re.sub(r'^(club|public)', '', external_id)
            if not re.fullmatch(r'[A-Za-z0-9_.]{1,80}', external_id) or external_id.isdigit() and int(external_id) == 0:
                raise ValueError('Введите ссылку, короткое имя или числовой ID сообщества ВК.')
        encrypted = service.cipher().encrypt(token.encode()).decode()
        with service.db() as conn:
            item_id = conn.execute('''INSERT INTO senler_channels(owner_id,kind,name,external_id,token,webhook_secret,created_at)
                         VALUES (?,?,?,?,?,?,?)''', (session['user_id'], kind, name, external_id, encrypted, secrets.token_urlsafe(32), now())).lastrowid
            conn.execute('UPDATE senler_channels SET unsubscribe_label=?,unsubscribe_enabled=? WHERE id=?',
                         (unsubscribe_label, unsubscribe_enabled, item_id))
            service.audit(conn, 'channel_created', 'Канал №{}'.format(item_id), session['user_id'])
        return jsonify(id=item_id)

    def subscription_button_settings(data, channel=None):
        defaults = channel or {'unsubscribe_label': 'Отписаться', 'unsubscribe_enabled': True}
        label = data.get('unsubscribe_label', defaults['unsubscribe_label'])
        enabled = data.get('unsubscribe_enabled', bool(defaults['unsubscribe_enabled']))
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 40:
            raise ValueError('Текст кнопки отписки должен содержать от 1 до 40 символов.')
        if not isinstance(enabled, bool):
            raise ValueError('Укажите, показывать ли кнопку отписки.')
        return label.strip(), enabled

    @bp.post('/reports/senler/api/channels/<int:item_id>/<action>')
    def channel_action(item_id, action):
        with service.db() as conn:
            channel = dict(row(conn, 'senler_channels', item_id))
        if action == 'edit':
            data = body()
            unsubscribe_label, unsubscribe_enabled = subscription_button_settings(data, channel)
            name = str(data.get('name', '')).strip()
            if not name or len(name) > 100:
                raise ValueError('Введите название до 100 символов.')
            token = str(data.get('token', '')).strip()
            if len(token) > 2048 or any(c.isspace() for c in token):
                raise ValueError('Введите ключ без пробелов, до 2048 символов.')
            encrypted = service.cipher().encrypt(token.encode()).decode() if token else channel['token']
            with service.db() as conn:
                conn.execute('UPDATE senler_channels SET name=?,token=?,status=?,checked_at=?,unsubscribe_label=?,unsubscribe_enabled=? WHERE id=?',
                    (name, encrypted, 'configured' if token else channel['status'], None if token else channel['checked_at'], unsubscribe_label, unsubscribe_enabled, item_id))
            return jsonify(ok=True)
        if action == 'pause':
            with service.db() as conn:
                conn.execute("UPDATE senler_channels SET status='paused' WHERE id=?", (item_id,))
            return jsonify(ok=True)
        if action not in ('check', 'connect'):
            raise ValueError('Неизвестное действие.')
        api = service.api(channel)
        try:
            identity = api.identity()
            if channel['kind'] != 'vk' and channel['external_id'] and channel['external_id'] != identity['external_id']:
                raise ValueError('Этот ключ относится к другому боту. Создайте отдельный канал, чтобы сохранить его собственную базу подписок.')
            # Persist VK confirmation before addCallbackServer calls our endpoint.
            with service.db() as conn:
                conn.execute('''UPDATE senler_channels SET external_id=?,username=?,subscribe_url=?,confirmation=?,checked_at=?,last_error='' WHERE id=?''',
                             (identity['external_id'], identity['username'], identity['subscribe_url'], identity.get('confirmation', ''), now(), item_id))
            if action == 'connect':
                channel.update(identity)
                api = service.api(channel)
                public_base = (os.environ.get('SENLER_PUBLIC_URL') or request.url_root).rstrip('/')
                server_id = api.connect(public_base + url_for('senler.webhook', kind=channel['kind'], channel_id=item_id))
                with service.db() as conn:
                    conn.execute("UPDATE senler_channels SET status='connected',callback_server_id=?,last_error='' WHERE id=?", (server_id, item_id))
                    service.audit(conn, 'channel_connected', 'Канал №{}'.format(item_id), session['user_id'])
        except DeliveryError as exc:
            with service.db() as conn:
                conn.execute('UPDATE senler_channels SET last_error=? WHERE id=?', (str(exc), item_id))
            raise
        return jsonify(ok=True, title=identity['title'])

    @bp.post('/api/senler/webhook/<kind>/<int:channel_id>')
    def webhook(kind, channel_id):
        if request.content_length and request.content_length > 1024 * 1024:
            return 'too large', 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return 'invalid', 400
        with service.db() as conn:
            channel = conn.execute('SELECT * FROM senler_channels WHERE id=? AND kind=?', (channel_id, kind)).fetchone()
            if not channel:
                return 'not found', 404
            supplied = data.get('secret', '') if kind == 'vk' else request.headers.get('X-Telegram-Bot-Api-Secret-Token' if kind == 'telegram' else 'X-Max-Bot-Api-Secret', '')
            if not isinstance(supplied, str) or not secrets.compare_digest(channel['webhook_secret'], supplied):
                return 'forbidden', 403
            if kind == 'vk' and str(data.get('group_id')) != channel['external_id']:
                return 'forbidden', 403
            if kind == 'vk' and data.get('type') == 'confirmation':
                conn.execute('UPDATE senler_channels SET webhook_at=? WHERE id=?', (now(), channel_id))
                return channel['confirmation'] or ('not configured', 409)
            try:
                event = parse_event(kind, data)
            except (KeyError, TypeError, ValueError, AttributeError):
                return 'invalid', 400
            if event:
                event_key = event['key'] or hashlib.sha256(dumps(data).encode()).hexdigest()
                conn.execute('INSERT OR IGNORE INTO senler_events(channel_id,event_key,body_json,created_at) VALUES (?,?,?,?)',
                             (channel_id, event_key, dumps(event), now()))
            conn.execute('UPDATE senler_channels SET webhook_at=? WHERE id=?', (now(), channel_id))
        return 'ok'

    def subscriber_query():
        clauses, params = ['s.channel_id IN (SELECT id FROM senler_channels WHERE owner_id=?)'], [session['user_id']]
        for arg, column in [('channel', 's.channel_id'), ('group', 'm.group_id')]:
            value = request.args.get(arg, type=int)
            if value:
                if arg == 'group':
                    clauses.append('EXISTS(SELECT 1 FROM senler_group_members m WHERE m.subscriber_id=s.id AND m.group_id=?)')
                else:
                    clauses.append(column + '=?')
                params.append(value)
        status = request.args.get('status', '')
        if status in ('active', 'pending', 'unsubscribed', 'blocked'):
            clauses.append('s.status=?')
            params.append(status)
        query = request.args.get('q', '').strip()[:160]
        if query:
            clauses.append('(s.name LIKE ? OR s.username LIKE ? OR s.external_user_id LIKE ?)')
            params.extend(['%' + query + '%'] * 3)
        return ' AND '.join(clauses), params

    @bp.get('/reports/senler/api/subscribers')
    def subscribers():
        where, params = subscriber_query()
        page_number = max(1, request.args.get('page', 1, type=int) or 1)
        with service.db() as conn:
            total = conn.execute('SELECT COUNT(*) n FROM senler_subscribers s WHERE ' + where, params).fetchone()['n']
            items = [dict(r) for r in conn.execute('''SELECT s.*,c.name channel_name,c.kind,
                (SELECT GROUP_CONCAT(g.name, ', ') FROM senler_group_members m JOIN senler_groups g ON g.id=m.group_id WHERE m.subscriber_id=s.id) groups
                FROM senler_subscribers s JOIN senler_channels c ON c.id=s.channel_id WHERE ''' + where + ' ORDER BY s.id DESC LIMIT 50 OFFSET ?', params + [(page_number - 1) * 50])]
        return jsonify(items=items, total=total, page=page_number, pages=max(1, (total + 49) // 50))

    @bp.get('/reports/senler/api/subscribers/export')
    def export_subscribers():
        where, params = subscriber_query()
        with service.db() as conn:
            items = conn.execute('SELECT s.*,c.kind,c.name channel_name FROM senler_subscribers s JOIN senler_channels c ON c.id=s.channel_id WHERE ' + where, params).fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=';')
        writer.writerow(['user_id', 'name', 'channel', 'channel_name', 'status', 'consent_at', 'source'])
        def cell(value):
            value = str(value or '')
            return "'" + value if value.startswith(('=', '+', '-', '@', '\t', '\r')) else value
        for item in items:
            writer.writerow([cell(item[k]) for k in ('external_user_id', 'name', 'kind', 'channel_name', 'status', 'consent_at', 'source')])
        return Response('\ufeff' + buf.getvalue(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=senler-subscribers.csv'})

    @bp.post('/reports/senler/api/groups')
    def create_group():
        name = str(body().get('name', '')).strip()
        if not name or len(name) > 80:
            raise ValueError('Название группы: от 1 до 80 символов.')
        with service.db() as conn:
            item_id = conn.execute('INSERT INTO senler_groups(owner_id,name,created_at) VALUES (?,?,?)', (session['user_id'], name, now())).lastrowid
        return jsonify(id=item_id)

    @bp.post('/reports/senler/api/subscribers/bulk')
    def subscribers_bulk():
        data = body()
        ids = data.get('ids')
        if not isinstance(ids, list) or not 1 <= len(ids) <= 500:
            raise ValueError('Выберите подписчиков в таблице.')
        ids = list(dict.fromkeys(integer(i) for i in ids))
        action = data.get('action')
        with service.db() as conn:
            if action in ('add_group', 'remove_group'):
                group_id = integer(data.get('group_id'), 'Группа')
                row(conn, 'senler_groups', group_id)
                for item_id in ids:
                    row(conn, 'senler_subscribers', item_id)
                    if action == 'add_group':
                        conn.execute('INSERT OR IGNORE INTO senler_group_members VALUES (?,?)', (group_id, item_id))
                    else:
                        conn.execute('DELETE FROM senler_group_members WHERE group_id=? AND subscriber_id=?', (group_id, item_id))
            elif action == 'unsubscribe':
                for item_id in ids:
                    row(conn, 'senler_subscribers', item_id)
                    service.stop_subscriber(conn, item_id)
            else:
                raise ValueError('Выберите действие.')
            service.audit(conn, 'subscribers_' + action, '{} подписчиков'.format(len(ids)), session['user_id'])
        return jsonify(ok=True)

    @bp.post('/reports/senler/api/import/preview')
    def import_preview():
        channel_id = integer(request.form.get('channel_id'), 'Канал')
        file = request.files.get('file')
        if not file:
            raise ValueError('Выберите TXT или CSV-файл.')
        rows, summary = parse_import(file.read(4 * 1024 * 1024 + 1))
        token = secrets.token_urlsafe(24)
        with service.db() as conn:
            channel = row(conn, 'senler_channels', channel_id)
            existing = {r['external_user_id']: r['status'] for r in conn.execute('SELECT external_user_id,status FROM senler_subscribers WHERE channel_id=?', (channel_id,))}
            summary['existing'] = sum(r['external_user_id'] in existing for r in rows)
            summary['protected'] = sum(existing.get(r['external_user_id']) in ('unsubscribed', 'blocked') for r in rows)
            summary['new'] = sum(r['external_user_id'] not in existing and not r['excluded'] for r in rows)
            conn.execute('INSERT INTO senler_imports(id,channel_id,user_id,rows_json,summary_json,created_at) VALUES (?,?,?,?,?,?)',
                         (token, channel_id, session['user_id'], dumps(rows), dumps(summary), now()))
        return jsonify(id=token, summary=summary, sample=rows[:8], channel_name=channel['name'], kind=channel['kind'])

    @bp.post('/reports/senler/api/import/confirm')
    def import_confirm():
        data = body()
        if data.get('consent') is not True:
            raise ValueError('Подтвердите, что импортируете действующие подписки именно этого сообщества или бота.')
        with service.db() as conn:
            job = row(conn, 'senler_imports', str(data.get('id', '')))
            if job['user_id'] != session['user_id'] or job['created_at'] < now() - 3600:
                raise ValueError('Предпросмотр устарел. Загрузите файл заново.')
            if job['applied_at']:
                return jsonify(ok=True, already_applied=True)
            group_id = integer(data['group_id']) if data.get('group_id') else None
            if group_id:
                row(conn, 'senler_groups', group_id)
            for imported in json.loads(job['rows_json']):
                conn.execute('''INSERT OR IGNORE INTO senler_subscribers
                    (channel_id,external_user_id,name,status,source,consent_at,created_at) VALUES (?,?,?,?,?,?,?)''',
                    (job['channel_id'], imported['external_user_id'], imported['name'], 'unsubscribed' if imported['excluded'] else 'active', 'senler_import', now(), now()))
                sub = conn.execute('SELECT * FROM senler_subscribers WHERE channel_id=? AND external_user_id=?', (job['channel_id'], imported['external_user_id'])).fetchone()
                if imported['excluded']:
                    service.stop_subscriber(conn, sub['id'])
                elif sub['status'] == 'pending':
                    conn.execute("UPDATE senler_subscribers SET status='active',source='senler_import',consent_at=? WHERE id=?", (now(), sub['id']))
                if group_id and not imported['excluded'] and sub['status'] not in ('blocked', 'unsubscribed'):
                    conn.execute('INSERT OR IGNORE INTO senler_group_members VALUES (?,?)', (group_id, sub['id']))
            conn.execute('UPDATE senler_imports SET applied_at=? WHERE id=?', (now(), job['id']))
            service.audit(conn, 'import', 'Канал №{}, {}'.format(job['channel_id'], job['summary_json']), session['user_id'])
        return jsonify(ok=True)

    @bp.post('/reports/senler/api/assets')
    def upload_asset():
        file = request.files.get('file')
        if not file:
            raise ValueError('Выберите картинку.')
        data = file.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise ValueError('Картинка должна быть не больше 8 МБ.')
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            mime, extension = 'image/png', 'png'
        elif data.startswith(b'\xff\xd8\xff'):
            mime, extension = 'image/jpeg', 'jpg'
        else:
            raise ValueError('Поддерживаются JPG и PNG.')
        with service.db() as conn:
            item_id = conn.execute('INSERT INTO senler_assets(owner_id,data,mime,extension,created_at) VALUES (?,?,?,?,?)', (session['user_id'], data, mime, extension, now())).lastrowid
        return jsonify(id=item_id)

    @bp.get('/reports/senler/api/assets/<int:item_id>')
    def asset(item_id):
        with service.db() as conn:
            image = row(conn, 'senler_assets', item_id)
        return Response(image['data'], mimetype=image['mime'], headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'private, max-age=3600'})

    @bp.post('/reports/senler/api/audience')
    def preview_audience():
        with service.db() as conn:
            items = service.audience(conn, body(), session['user_id'])
            counts = {}
            for item in items:
                counts[str(item['channel_id'])] = counts.get(str(item['channel_id']), 0) + 1
        return jsonify(total=len(items), channels=counts)

    @bp.route('/reports/senler/api/campaigns', methods=['GET', 'POST'])
    def campaigns():
        if request.method == 'GET':
            channel = request.args.get('channel', type=int)
            with service.db() as conn:
                items = [campaign_data(conn, r) for r in conn.execute('SELECT * FROM senler_campaigns WHERE owner_id=? ORDER BY id DESC LIMIT 200', (session['user_id'],))]
            if channel:
                items = [item for item in items if channel in item['audience']['channels']]
            return jsonify(items=items)
        data = body()
        name = str(data.get('name', '')).strip()
        if not name or len(name) > 120:
            raise ValueError('Введите название рассылки до 120 символов.')
        message = validate_body(data.get('body'))
        audience = data.get('audience')
        if not isinstance(audience, dict):
            raise ValueError('Выберите аудиторию.')
        audience = {'channels': list(dict.fromkeys(integer(c) for c in audience.get('channels', []))),
                    'groups': list(dict.fromkeys(integer(g) for g in audience.get('groups', [])))}
        scheduled_at = None
        if data.get('scheduled_local'):
            try:
                scheduled_at = int(datetime.strptime(data['scheduled_local'], '%Y-%m-%dT%H:%M').replace(tzinfo=ZoneInfo('Asia/Novosibirsk')).timestamp())
            except (TypeError, ValueError):
                raise ValueError('Укажите корректные дату и время отправки.')
        with service.db() as conn:
            service.audience(conn, audience, session['user_id'])
            if message['asset_id']:
                row(conn, 'senler_assets', message['asset_id'])
            if data.get('id'):
                item_id = integer(data['id'])
                existing = row(conn, 'senler_campaigns', item_id)
                if existing['status'] != 'draft':
                    raise ValueError('После запуска сообщение фиксируется. Создайте копию рассылки для изменений.')
                conn.execute('UPDATE senler_campaigns SET name=?,body_json=?,audience_json=?,scheduled_at=? WHERE id=?', (name, dumps(message), dumps(audience), scheduled_at, item_id))
            else:
                item_id = conn.execute('INSERT INTO senler_campaigns(owner_id,name,body_json,audience_json,scheduled_at,created_at,created_by) VALUES (?,?,?,?,?,?,?)',
                                      (session['user_id'], name, dumps(message), dumps(audience), scheduled_at, now(), session['user_id'])).lastrowid
        return jsonify(id=item_id)

    @bp.get('/reports/senler/api/campaigns/<int:item_id>')
    def campaign(item_id):
        with service.db() as conn:
            data = campaign_data(conn, row(conn, 'senler_campaigns', item_id), details=True)
        return jsonify(data)

    @bp.post('/reports/senler/api/campaigns/<int:item_id>/<action>')
    def campaign_action(item_id, action):
        data = body()
        with service.db() as conn:
            # Serialize double clicks and concurrent operators before taking the audience snapshot.
            conn.execute('BEGIN IMMEDIATE')
            campaign = row(conn, 'senler_campaigns', item_id)
            if action == 'copy':
                copied = conn.execute('INSERT INTO senler_campaigns(owner_id,name,body_json,audience_json,created_at,created_by) VALUES (?,?,?,?,?,?)',
                                      (session['user_id'], 'Копия: ' + campaign['name'], campaign['body_json'], campaign['audience_json'], now(), session['user_id'])).lastrowid
                return jsonify(id=copied)
            if action == 'pause' and campaign['status'] in ('running', 'scheduled'):
                conn.execute("UPDATE senler_campaigns SET status='paused' WHERE id=?", (item_id,))
            elif action == 'resume' and campaign['status'] == 'paused':
                status = 'scheduled' if campaign['scheduled_at'] and campaign['scheduled_at'] > now() else 'running'
                conn.execute('UPDATE senler_campaigns SET status=? WHERE id=?', (status, item_id))
            elif action == 'cancel' and campaign['status'] in ('draft', 'paused', 'scheduled', 'running'):
                conn.execute("UPDATE senler_campaigns SET status='cancelled',finished_at=? WHERE id=?", (now(), item_id))
                conn.execute("UPDATE senler_outbox SET status='cancelled' WHERE campaign_id=? AND status='pending'", (item_id,))
            elif action == 'launch' and campaign['status'] == 'draft':
                audience = json.loads(campaign['audience_json'])
                subscribers = service.audience(conn, audience, session['user_id'])
                if not subscribers:
                    raise ValueError('В выбранной аудитории пока нет активных подписчиков.')
                for channel_id in audience['channels']:
                    if row(conn, 'senler_channels', channel_id)['status'] != 'connected':
                        raise ValueError('Сначала подключите все выбранные каналы.')
                scheduled_at = None
                if data.get('scheduled_at'):
                    try:
                        local = datetime.strptime(data['scheduled_at'], '%Y-%m-%dT%H:%M').replace(tzinfo=ZoneInfo('Asia/Novosibirsk'))
                        scheduled_at = int(local.timestamp())
                    except (TypeError, ValueError):
                        raise ValueError('Укажите дату и время отправки.')
                    if scheduled_at <= now():
                        raise ValueError('Время отправки уже прошло. Выберите будущее время.')
                message = json.loads(campaign['body_json'])
                message['buttons'].append({'label': 'Отписаться', 'action': 'callback', 'value': 'unsubscribe'})
                for sub in subscribers:
                    service.queue(conn, sub, message, 'campaign:{}:{}'.format(item_id, sub['id']), kind='campaign', campaign_id=item_id, due_at=scheduled_at)
                conn.execute('UPDATE senler_campaigns SET status=?,scheduled_at=?,started_at=? WHERE id=?',
                             ('scheduled' if scheduled_at else 'running', scheduled_at, None if scheduled_at else now(), item_id))
            else:
                raise ValueError('Состояние рассылки уже изменилось. Обновите страницу.')
            service.audit(conn, 'campaign_' + action, 'Рассылка №{}'.format(item_id), session['user_id'])
        return jsonify(ok=True)

    @bp.route('/reports/senler/api/bots', methods=['GET', 'POST'])
    def bots():
        with service.db() as conn:
            if request.method == 'GET':
                channel_id = request.args.get('channel', type=int)
                items = [dict(r) for r in conn.execute('''SELECT b.*,c.kind,c.name channel_name,
                    (SELECT COUNT(*) FROM senler_runs r WHERE r.bot_id=b.id AND r.status='completed') completed,
                    (SELECT COUNT(*) FROM senler_runs r WHERE r.bot_id=b.id AND r.status IN ('running','waiting_reply','waiting_send','paused')) running,
                    (SELECT COUNT(*) FROM senler_runs r WHERE r.bot_id=b.id AND r.status='error') errors
                    FROM senler_bots b JOIN senler_channels c ON c.id=b.channel_id WHERE b.owner_id=?''' + (' AND b.channel_id=?' if channel_id else '') + ' ORDER BY b.id DESC', [session['user_id']] + ([channel_id] if channel_id else []))]
                for item in items:
                    item['definition'] = json.loads(item.pop('draft_json'))
                    item['has_changes'] = item.get('published_json') != dumps(item['definition']) or item['trigger_type'] != item['published_trigger'] or item['keywords'] != item['published_keywords']
                    item.pop('published_json', None)
                return jsonify(items=items)
            data = body()
            name = str(data.get('name', '')).strip()
            if not name or len(name) > 120:
                raise ValueError('Введите название бота до 120 символов.')
            channel_id = integer(data.get('channel_id'), 'Канал')
            row(conn, 'senler_channels', channel_id)
            trigger = data.get('trigger_type')
            keywords = str(data.get('keywords', '')).strip()[:1000]
            if trigger not in ('subscribe', 'keyword', 'any') or (trigger == 'keyword' and not keywords):
                raise ValueError('Выберите условие запуска и заполните ключевые слова.')
            priority = integer(data.get('priority', 10), 'Приоритет', maximum=100)
            definition = validate_definition(data.get('definition'), conn, session['user_id'])
            if data.get('id'):
                item_id = integer(data['id'])
                existing = row(conn, 'senler_bots', item_id)
                if existing['channel_id'] != channel_id:
                    raise ValueError('У существующего бота нельзя менять канал. Создайте нового бота.')
                conn.execute('UPDATE senler_bots SET name=?,trigger_type=?,keywords=?,priority=?,draft_json=?,updated_at=? WHERE id=?',
                             (name, trigger, keywords, priority, dumps(definition), now(), item_id))
            else:
                item_id = conn.execute('''INSERT INTO senler_bots(owner_id,name,channel_id,trigger_type,keywords,priority,draft_json,updated_at)
                            VALUES (?,?,?,?,?,?,?,?)''', (session['user_id'], name, channel_id, trigger, keywords, priority, dumps(definition), now())).lastrowid
        return jsonify(id=item_id)

    @bp.post('/reports/senler/api/bots/<int:item_id>/<action>')
    def bot_action(item_id, action):
        with service.db() as conn:
            bot = row(conn, 'senler_bots', item_id)
            if action == 'publish':
                validate_definition(json.loads(bot['draft_json']), conn, session['user_id'])
                if row(conn, 'senler_channels', bot['channel_id'])['status'] != 'connected':
                    raise ValueError('Сначала подключите канал бота. До этого можно сохранить и проверить сценарий в симуляторе.')
                conn.execute("""UPDATE senler_bots SET published_json=draft_json,published_trigger=trigger_type,
                    published_keywords=keywords,version=version+1,status='active',updated_at=? WHERE id=?""", (now(), item_id))
            elif action == 'pause':
                conn.execute("UPDATE senler_bots SET status='paused' WHERE id=?", (item_id,))
            elif action == 'resume' and bot['published_json']:
                conn.execute("UPDATE senler_bots SET status='active' WHERE id=?", (item_id,))
            else:
                raise ValueError('Неизвестное действие.')
            service.audit(conn, 'bot_' + action, 'Бот №{}'.format(item_id), session['user_id'])
        return jsonify(ok=True)

    @bp.get('/reports/senler/api/dialogs')
    def dialogs():
        channel = request.args.get('channel', type=int)
        q = '%' + request.args.get('q', '').strip()[:160] + '%'
        with service.db() as conn:
            items = [dict(r) for r in conn.execute('''SELECT s.*,c.name channel_name,c.kind,
                    (SELECT text FROM senler_messages m WHERE m.subscriber_id=s.id ORDER BY m.id DESC LIMIT 1) last_message,
                    (SELECT MAX(created_at) FROM senler_messages m WHERE m.subscriber_id=s.id) message_at
                    FROM senler_subscribers s JOIN senler_channels c ON c.id=s.channel_id
                    WHERE c.owner_id=? AND s.last_seen IS NOT NULL AND (s.name LIKE ? OR s.external_user_id LIKE ?)''' +
                    (' AND s.channel_id=?' if channel else '') + ' ORDER BY s.bot_paused DESC,s.unread DESC,s.last_seen DESC LIMIT 100', [session['user_id'], q, q] + ([channel] if channel else []))]
        return jsonify(items=items)

    @bp.get('/reports/senler/api/dialogs/<int:item_id>')
    def dialog(item_id):
        with service.db() as conn:
            sub = dict(row(conn, 'senler_subscribers', item_id))
            conn.execute('UPDATE senler_subscribers SET unread=0 WHERE id=?', (item_id,))
            messages = [dict(r) for r in conn.execute('SELECT * FROM senler_messages WHERE subscriber_id=? ORDER BY id DESC LIMIT 200', (item_id,))][::-1]
            pending = [dict(r) for r in conn.execute("SELECT id,body_json,status,error,created_at FROM senler_outbox WHERE subscriber_id=? AND status IN ('pending','sending','error','unknown') ORDER BY id DESC LIMIT 30", (item_id,))]
            for item in pending:
                item['text'] = json.loads(item.pop('body_json'))['text']
        return jsonify(subscriber=sub, messages=messages, pending=pending)

    @bp.post('/reports/senler/api/dialogs/<int:item_id>/<action>')
    def dialog_action(item_id, action):
        data = body()
        with service.db() as conn:
            sub = row(conn, 'senler_subscribers', item_id)
            if action == 'reply':
                if sub['status'] != 'active':
                    raise ValueError('Подписчик пока не разрешил рассылку или отписался. Предложение подписаться отправляется при его обращении к боту.')
                if row(conn, 'senler_channels', sub['channel_id'])['status'] != 'connected':
                    raise ValueError('Канал не подключён.')
                message = validate_body({'text': data.get('text', '')})
                request_key = str(data.get('request_key', ''))
                if not re_safe_key(request_key):
                    raise ValueError('Обновите страницу и повторите отправку.')
                service.queue(conn, sub, message, 'manual:{}:{}'.format(item_id, request_key), kind='manual')
                conn.execute('UPDATE senler_subscribers SET bot_paused=1 WHERE id=?', (item_id,))
            elif action in ('takeover', 'resume'):
                conn.execute('UPDATE senler_subscribers SET bot_paused=? WHERE id=?', (action == 'takeover', item_id))
                if action == 'resume':
                    conn.execute("UPDATE senler_runs SET status=CASE WHEN node_id='' THEN 'completed' ELSE 'running' END,due_at=? WHERE subscriber_id=? AND status='paused'", (now(), item_id))
            else:
                raise ValueError('Неизвестное действие.')
        return jsonify(ok=True)

    app.register_blueprint(bp)
    return service


def re_safe_key(value):
    return 8 <= len(value) <= 80 and all(c.isalnum() or c in '-_' for c in value)
