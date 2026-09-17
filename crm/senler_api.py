"""Official community/bot APIs. No personal-account sessions or Senler dependency."""
import json
import os
import errno
import re
import socket
import secrets
from functools import lru_cache
from pathlib import Path
import tempfile
from urllib.parse import urlparse

import requests


TRANSIENT_NETWORK_ERRORS = frozenset({
    'READ_TIMEOUT', 'CONNECT_TIMEOUT', 'TIMEOUT', 'CONNECTION_RESET',
    'REMOTE_CLOSED', 'CONNECTION', 'BROKEN_PIPE', 'HTTP_PROTOCOL',
})


@lru_cache(maxsize=None)
def _max_ca_bundle(configured_path=''):
    if configured_path:
        return configured_path
    standard_bundle = Path(requests.certs.where()).read_bytes().rstrip()
    ministry_root = (Path(__file__).parent / 'certs' / 'russian_trusted_root_ca.pem').read_bytes().strip()
    target = Path(tempfile.gettempdir()) / 'crm-senler-max-ca.pem'
    expected = standard_bundle + b'\n' + ministry_root + b'\n'
    try:
        if target.read_bytes() == expected:
            return str(target)
    except FileNotFoundError:
        pass
    with tempfile.NamedTemporaryFile(dir=str(target.parent), prefix=target.name + '.', delete=False) as output:
        output.write(expected)
        temporary = output.name
    os.replace(temporary, target)
    return str(target)


def max_ca_bundle():
    """Return standard web roots plus the official Ministry root required by MAX."""
    return _max_ca_bundle(os.environ.get('SENLER_MAX_CA_BUNDLE', ''))


class DeliveryError(Exception):
    def __init__(self, message, retry_after=0, uncertain=False, blocked=False, network_code='', api_code=0):
        super().__init__(message)
        self.retry_after = retry_after
        self.uncertain = uncertain
        self.blocked = blocked
        self.network_code = network_code
        self.api_code = api_code


def network_error_code(error):
    """Classify nested requests/urllib3/PySocks errors without logging their text."""
    pending, errors, visited = [error], [], set()
    while pending and len(errors) < 20:
        current = pending.pop(0)
        if not isinstance(current, BaseException) or id(current) in visited:
            continue
        visited.add(id(current))
        errors.append(current)
        pending.extend(getattr(current, name, None) for name in
                       ('__cause__', '__context__', 'reason', 'socket_err', 'original_error'))
        pending.extend(value for value in current.args if isinstance(value, BaseException))
    # Specific underlying socket failures take priority over a generic wrapper.
    rules = [
        (socket.gaierror, 'DNS'),
        (requests.exceptions.InvalidSchema, 'TRANSPORT_SETUP'),
        (requests.exceptions.InvalidURL, 'INVALID_URL'),
        (requests.exceptions.SSLError, 'TLS'),
        (requests.exceptions.ConnectTimeout, 'CONNECT_TIMEOUT'),
        (requests.exceptions.ReadTimeout, 'READ_TIMEOUT'),
        (ConnectionRefusedError, 'CONNECTION_REFUSED'),
        (ConnectionResetError, 'CONNECTION_RESET'),
        (BrokenPipeError, 'BROKEN_PIPE'),
        (TimeoutError, 'TIMEOUT'),
    ]
    for error_class, code in rules:
        if any(isinstance(item, error_class) for item in errors):
            return code
    if any(isinstance(item, OSError) and item.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH) for item in errors):
        return 'ROUTE_UNAVAILABLE'
    # Some urllib3/PySocks wrappers retain only a typed inner exception.
    for name, code in [('ReadTimeoutError', 'READ_TIMEOUT'), ('ConnectTimeoutError', 'CONNECT_TIMEOUT'),
                       ('RemoteDisconnected', 'REMOTE_CLOSED'), ('ProtocolError', 'HTTP_PROTOCOL'),
                       ('SOCKS5AuthError', 'PROXY_AUTH'), ('SOCKS5Error', 'SOCKS_REPLY')]:
        if any(type(item).__name__ == name for item in errors):
            return code
    if isinstance(error, requests.exceptions.ProxyError):
        return 'PROXY_CONNECTION'
    if isinstance(error, requests.exceptions.ConnectionError):
        return 'CONNECTION'
    return 'REQUEST'


def telegram_request_options():
    """Use the CRM's configured Telegram proxy, without changing other integrations."""
    proxy = os.environ.get('SENLER_TELEGRAM_PROXY_URL', '').strip()
    if not proxy:
        return {}
    try:
        parsed = urlparse(proxy)
        if (parsed.scheme not in ('http', 'https', 'socks5', 'socks5h') or not parsed.hostname
                or not parsed.port or parsed.path not in ('', '/') or parsed.query or parsed.fragment
                or any(char.isspace() for char in proxy)):
            raise ValueError
    except ValueError:
        raise DeliveryError('Некорректный адрес прокси Telegram в настройках сервера CRM.') from None
    # Resolve Telegram's hostname at the proxy too, not through a blocked local DNS.
    if parsed.scheme == 'socks5':
        proxy = parsed._replace(scheme='socks5h').geturl()
    return {'proxies': {'http': proxy, 'https': proxy}}


DEFAULT_TELEGRAM_BASE = 'https://api.telegram.org'


def telegram_api_base():
    """An optional relay (e.g. a Cloudflare Worker) placed in front of api.telegram.org,
    for networks where reaching Telegram directly is unreliable but reaching the relay is not.
    Unlike SENLER_TELEGRAM_PROXY_URL (a transport-level tunnel), this changes which host the
    request targets; the relay is expected to forward it to the real Telegram API unchanged."""
    base = os.environ.get('SENLER_TELEGRAM_API_BASE', '').strip().rstrip('/')
    if not base:
        return DEFAULT_TELEGRAM_BASE
    parsed = urlparse(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.query or parsed.fragment or any(char.isspace() for char in base):
        raise DeliveryError('Некорректный адрес узла Telegram в настройках сервера CRM.')
    return base


def telegram_uses_polling():
    mode = os.environ.get('SENLER_TELEGRAM_RECEIVE_MODE', 'polling').strip().lower()
    if mode not in ('polling', 'webhook'):
        raise DeliveryError('Режим приёма Telegram должен быть polling или webhook.')
    return mode == 'polling'


# The message editor lets the owner mark up text as **bold**, _italic_, ++underline++
# and [label](url) links. MAX uses this exact Markdown syntax natively (dev.max.ru's
# formatting table: **strong**/__strong__, *emphasized*/_emphasized_, ++underline++,
# [Inline URL](url)), Telegram needs it converted to HTML, and VK community messages
# don't support inline formatting or links at all, so both are flattened to plain text.
FORMAT_RE = re.compile(
    r'\[(?P<link_text>[^\[\]\n]+)\]\((?P<link_url>https?://[^\s)]+)\)'
    r'|\*\*(?P<bold>[^\n*]+?)\*\*'
    r'|\+\+(?P<underline>[^\n+]+?)\+\+'
    # (?<!\w)…(?!\w): a lone underscore inside a word (promo codes, snake_case) stays literal.
    r'|(?<!\w)_(?P<italic>[^\n_]+?)_(?!\w)',
    re.S)


def _walk_formatting(text, escape, bold, italic, underline, link):
    """Recursive so a browser-produced combo like bold-containing-italic (the rich text
    editor makes this easy: select a word, click Bold then Italic) nests correctly instead
    of only the outermost marker being recognised and the inner one left as literal text."""
    out, pos = [], 0
    for match in FORMAT_RE.finditer(text):
        out.append(escape(text[pos:match.start()]))
        if match.group('link_text') is not None:
            inner = _walk_formatting(match.group('link_text'), escape, bold, italic, underline, link)
            out.append(link(inner, match.group('link_url')))
        elif match.group('bold') is not None:
            out.append(bold(_walk_formatting(match.group('bold'), escape, bold, italic, underline, link)))
        elif match.group('underline') is not None:
            out.append(underline(_walk_formatting(match.group('underline'), escape, bold, italic, underline, link)))
        else:
            out.append(italic(_walk_formatting(match.group('italic'), escape, bold, italic, underline, link)))
        pos = match.end()
    out.append(escape(text[pos:]))
    return ''.join(out)


def strip_formatting(text):
    identity = lambda value: value
    return _walk_formatting(text, identity, identity, identity, identity,
                             lambda inner, url: '{} ({})'.format(inner, url))


def format_to_telegram_html(text):
    escape = lambda value: value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
    return _walk_formatting(
        text, escape,
        lambda inner: '<b>{}</b>'.format(inner),
        lambda inner: '<i>{}</i>'.format(inner),
        lambda inner: '<u>{}</u>'.format(inner),
        lambda inner, url: '<a href="{}">{}</a>'.format(escape(url), inner))


class BotAPI:
    VK_VERSION = '5.199'
    MAX_BASE = 'https://platform-api2.max.ru'

    def __init__(self, channel, token, requester=None):
        self.channel = dict(channel)
        self.kind = channel['kind']
        self.token = token
        self.requester = requester

    def _request(self, method, url, sending=False, timeout=(5, 15), **kwargs):
        # Never interpolate request exceptions: Telegram URLs contain the credential.
        try:
            response = (self.requester or requests.request)(method, url, timeout=timeout, **kwargs)
        except requests.exceptions.RequestException as exc:
            code = network_error_code(exc)
            if self.kind == 'telegram' and isinstance(exc, requests.exceptions.ConnectTimeout):
                # ConnectTimeout means the request has not reached Telegram.
                # Other connection errors/read timeouts can be ambiguous.
                raise DeliveryError('Не удалось установить соединение с Telegram. Запрос будет повторён.',
                                    retry_after=2, network_code=code) from None
            if isinstance(exc, requests.exceptions.SSLError):
                raise DeliveryError('Не удалось проверить сертификат сервера. Проверьте доверенные сертификаты на сервере CRM.',
                                    network_code=code) from None
            if isinstance(exc, requests.exceptions.ConnectTimeout):
                raise DeliveryError('Сервис не отвечает при подключении.', retry_after=30, network_code=code) from None
            message = ('Не удалось подключиться через прокси. Проверьте прокси на сервере CRM.'
                       if isinstance(exc, requests.exceptions.ProxyError) else
                       'Не удалось связаться с Telegram API.' if self.kind == 'telegram' else
                       'Не удалось связаться с сервисом.')
            raise DeliveryError('Соединение прервано. Результат отправки неизвестен.' if sending else message,
                                uncertain=sending, retry_after=30 if not sending else 0, network_code=code) from None
        try:
            data = response.json()
        except ValueError:
            raise DeliveryError('Сервис вернул неожиданный ответ.', uncertain=sending)
        if not isinstance(data, dict):
            raise DeliveryError('Сервис вернул неожиданный ответ.', uncertain=sending)
        error = data.get('error')
        vk_code = error.get('error_code', 0) if isinstance(error, dict) else 0
        code = vk_code or data.get('error_code', 0) or response.status_code
        if response.status_code == 429 or code in (6, 9, 29, 429):
            delay = data.get('parameters', {}).get('retry_after') or response.headers.get('Retry-After') or 60
            try:
                delay = max(1, min(3600, int(delay)))
            except (TypeError, ValueError):
                delay = 60
            raise DeliveryError('Лимит сервиса: отправка продолжится автоматически.', retry_after=delay)
        if response.status_code >= 500:
            raise DeliveryError('Временная ошибка сервиса.', uncertain=sending, retry_after=30 if not sending else 0)
        if self.kind == 'telegram' and code == 409:
            raise DeliveryError('Приём Telegram занят другим подключением. Повторяем подключение.',
                                retry_after=5, api_code=409)
        if data.get('code') == 'attachment.not.ready':
            raise DeliveryError('Картинка обрабатывается мессенджером.', retry_after=10)
        if not response.ok or error or data.get('ok') is False or data.get('success') is False or data.get('code'):
            blocked = code in (901, 902) or (self.kind == 'telegram' and code == 403)
            message = ('Пользователь запретил сообщения.' if blocked else
                       'Ключ недействителен или у него недостаточно прав.' if code in (5, 15, 27, 28, 401, 403) else
                       'Сервис отклонил запрос. Проверьте настройки канала и сообщение.')
            raise DeliveryError(message, blocked=blocked)
        return data

    def vk(self, method, **params):
        return self._request('POST', 'https://api.vk.com/method/' + method,
                             sending=method == 'messages.send',
                             data=dict(params, access_token=self.token, v=self.VK_VERSION)).get('response')

    def telegram(self, method, payload=None, files=None, sending=False, timeout=None, retry=True):
        kwargs = {'data': payload, 'files': files} if files else {'json': payload or {}}
        base = telegram_api_base()
        relayed = base != DEFAULT_TELEGRAM_BASE
        # A relay is reached directly, on the assumption it is chosen precisely because it is
        # reachable where api.telegram.org itself is not; the VPN tunnel is not layered under it.
        options = {} if relayed else telegram_request_options()
        if relayed:
            secret = os.environ.get('SENLER_TELEGRAM_RELAY_SECRET', '').strip()
            if secret:
                options['headers'] = {'X-Relay-Secret': secret}
        # Only read-only checks retry here. connect() reconciles an uncertain
        # webhook registration before deciding whether to repeat that mutation.
        read_only = method in ('getMe', 'getWebhookInfo') and not sending and retry
        timeout = timeout or ((5, 30) if method in ('getMe', 'getWebhookInfo', 'setWebhook', 'deleteWebhook') else (5, 15))
        try:
            for attempt in range(2 if read_only else 1):
                try:
                    return self._request('POST', base + '/bot' + self.token + '/' + method,
                                         sending=sending, timeout=timeout, **options, **kwargs).get('result')
                except DeliveryError as exc:
                    if not read_only or attempt or exc.network_code not in TRANSIENT_NETWORK_ERRORS:
                        raise
        except DeliveryError as exc:
            if not exc.network_code or sending:
                raise
            stage, operation = {
                'getMe': ('проверка токена', 'TOKEN'),
                'setWebhook': ('подключение приёма сообщений', 'WEBHOOK'),
                'getWebhookInfo': ('проверка приёма сообщений', 'WEBHOOK_INFO'),
                'deleteWebhook': ('переключение на получение сообщений', 'POLL_SETUP'),
                'getUpdates': ('получение сообщений', 'POLL'),
            }.get(method, ('запрос к Telegram', 'API'))
            route = 'RELAY' if relayed else 'PROXY' if options.get('proxies') else 'DIRECT'
            code = 'TG-{}-{}-{}'.format(operation, route, exc.network_code)
            raise DeliveryError('{} Этап: {}. Код диагностики: {}.'.format(exc, stage, code),
                                retry_after=exc.retry_after, uncertain=exc.uncertain,
                                blocked=exc.blocked, network_code=exc.network_code, api_code=exc.api_code) from None

    def max(self, method, path, params=None, payload=None, sending=False):
        return self._request(method, self.MAX_BASE + path, params=params,
                             json=payload, headers={'Authorization': self.token}, sending=sending,
                             verify=max_ca_bundle())

    def telegram_file(self, file_path):
        """Downloads a file from Telegram's file API and returns its raw bytes. Kept separate
        from telegram(): that method expects a JSON response, while a file download does not,
        and the URL below embeds the bot token so it must never reach the browser directly."""
        base = telegram_api_base()
        relayed = base != DEFAULT_TELEGRAM_BASE
        options = {} if relayed else telegram_request_options()
        if relayed:
            secret = os.environ.get('SENLER_TELEGRAM_RELAY_SECRET', '').strip()
            if secret:
                options['headers'] = {'X-Relay-Secret': secret}
        response = (self.requester or requests.request)('GET', base + '/file/bot' + self.token + '/' + file_path,
                                                          timeout=(5, 15), **options)
        response.raise_for_status()
        return response.content, response.headers.get('Content-Type') or 'image/jpeg'

    def identity(self):
        if self.kind == 'telegram':
            me = self.telegram('getMe')
            if not me.get('is_bot'):
                raise DeliveryError('Нужен ключ бота Telegram.')
            return {'external_id': str(me['id']), 'username': me.get('username', ''),
                    'title': me.get('first_name', ''), 'subscribe_url': 'https://t.me/' + me.get('username', '')}
        if self.kind == 'max':
            me = self.max('GET', '/me')
            if not me.get('is_bot'):
                raise DeliveryError('Нужен ключ официального бота MAX.')
            username = me.get('username') or ''
            return {'external_id': str(me['user_id']), 'username': username,
                    'title': me.get('first_name') or me.get('name') or '', 'subscribe_url': 'https://max.ru/' + username if username else ''}
        groups = self.vk('groups.getById', group_ids=self.channel['external_id'])
        groups = groups.get('groups', []) if isinstance(groups, dict) else groups
        reference = str(self.channel['external_id'])
        if not groups or reference.isdigit() and str(groups[0]['id']) != reference:
            raise DeliveryError('Ключ не соответствует выбранному сообществу.')
        me = groups[0]
        confirmation = self.vk('groups.getCallbackConfirmationCode', group_id=me['id'])
        return {'external_id': str(me['id']), 'username': me.get('screen_name', ''),
                'title': me['name'], 'subscribe_url': 'https://vk.me/' + (me.get('screen_name') or 'club' + str(me['id'])),
                'confirmation': confirmation['code']}

    def connect(self, url):
        if self.kind == 'telegram' and telegram_uses_polling():
            self.start_polling()
            return ''
        secret = self.channel['webhook_secret']
        if not url.startswith('https://'):
            raise DeliveryError('Для подключения нужен публичный HTTPS-адрес CRM.')
        if self.kind == 'telegram':
            # A fresh marker lets getWebhookInfo prove THIS registration was
            # applied, including its secret. A matching old URL alone cannot do
            # that because Telegram does not return the configured secret_token.
            parsed = urlparse(url)
            marker = 'setup=' + secrets.token_urlsafe(16)
            registration_url = parsed._replace(query=(parsed.query + '&' if parsed.query else '') + marker).geturl()
            payload = {'url': registration_url, 'secret_token': secret,
                       'allowed_updates': ['message', 'callback_query', 'my_chat_member'],
                       'drop_pending_updates': False, 'max_connections': 4}
            for attempt in range(2):
                try:
                    self.telegram('setWebhook', payload)
                    return ''
                except DeliveryError as exc:
                    if exc.network_code not in TRANSIENT_NETWORK_ERRORS:
                        raise
                    try:
                        info = self.telegram('getWebhookInfo', timeout=(3, 7), retry=False)
                    except DeliveryError:
                        info = None
                    if isinstance(info, dict) and info.get('url') == registration_url:
                        return ''
                    if attempt:
                        raise
                    # Exactly the same registration, no queue clearing. Unlike
                    # sendMessage, repeating setWebhook cannot duplicate messages.
        if self.kind == 'max':
            self.max('POST', '/subscriptions', payload={'url': url, 'secret': secret,
                     'update_types': ['message_created', 'message_callback', 'bot_started', 'bot_stopped']})
            return ''
        group_id = self.channel['external_id']
        servers = self.vk('groups.getCallbackServers', group_id=group_id).get('items', [])
        existing = next((s for s in servers if s.get('url') == url), None)
        params = dict(group_id=group_id, url=url, title='CRM Сенлер', secret_key=secret)
        if existing:
            server_id = existing['id']
            if existing.get('secret_key') != secret:
                self.vk('groups.editCallbackServer', server_id=server_id, **params)
        else:
            server_id = self.vk('groups.addCallbackServer', **params)['server_id']
        self.vk('groups.setCallbackSettings', group_id=group_id, server_id=server_id,
                api_version=self.VK_VERSION, message_new=1, message_allow=1, message_deny=1, message_event=1)
        confirmed = self.vk('groups.getCallbackServers', group_id=group_id, server_ids=server_id).get('items', [])
        if not confirmed or confirmed[0].get('status') != 'ok':
            raise DeliveryError('ВК ещё не подтвердил приём событий. Через несколько секунд нажмите «Подключить» ещё раз. Если ошибка повторится, проверьте сервер «CRM Сенлер» в настройках Callback API сообщества.')
        return str(server_id)

    def start_polling(self):
        # Idempotent on retries/restarts. Never discard Telegram's pending queue.
        result = self.telegram('deleteWebhook', {'drop_pending_updates': False})
        if result is not True:
            raise DeliveryError('Telegram не подтвердил переключение приёма сообщений.')

    def get_updates(self, offset):
        return self.telegram('getUpdates', {
            'offset': offset, 'limit': 100, 'timeout': 25,
            'allowed_updates': ['message', 'callback_query', 'my_chat_member'],
        }, timeout=(5, 35), retry=False)

    @staticmethod
    def _keyboard(buttons, kind):
        rows = []
        for button in buttons:
            if kind == 'telegram':
                item = {'text': button['label'], 'url' if button['action'] == 'url' else 'callback_data': button['value']}
            elif kind == 'max':
                item = {'type': 'link' if button['action'] == 'url' else 'callback', 'text': button['label'],
                        'url' if button['action'] == 'url' else 'payload': button['value']}
            else:
                action = {'type': 'open_link', 'label': button['label'], 'link': button['value']} if button['action'] == 'url' else {
                    'type': 'callback', 'label': button['label'], 'payload': json.dumps({'senler': button['value']})}
                item = {'action': action}
                if button['action'] != 'url':
                    item['color'] = button.get('color') or 'secondary'
            rows.append([item])
        return rows

    def send(self, external_user_id, body, random_id, asset=None):
        buttons = body.get('buttons', [])
        keyboard = self._keyboard(buttons, self.kind)
        if self.kind == 'telegram':
            payload = {'chat_id': external_user_id, 'parse_mode': 'HTML'}
            if buttons:
                payload['reply_markup'] = {'inline_keyboard': keyboard}
            text = format_to_telegram_html(body['text'])
            if asset:
                payload['caption'] = text
                if buttons:
                    payload['reply_markup'] = json.dumps(payload['reply_markup'])
                sent = self.telegram('sendPhoto', payload, files={'photo': ('image.' + asset['extension'], asset['data'], asset['mime'])}, sending=True)
            else:
                payload['text'] = text
                sent = self.telegram('sendMessage', payload, sending=True)
            return str(sent['message_id'])
        if self.kind == 'max':
            payload = {'text': body['text'], 'format': 'markdown', 'attachments': []}
            if asset:
                uploaded = asset.get('remote_payload') or self.upload_asset(asset, external_user_id)
                payload['attachments'].append({'type': 'image', 'payload': uploaded})
            if buttons:
                payload['attachments'].append({'type': 'inline_keyboard', 'payload': {'buttons': keyboard}})
            sent = self.max('POST', '/messages', params={'user_id': external_user_id}, payload=payload, sending=True)
            return str(sent['message']['body']['mid'])
        allowed = self.vk('messages.isMessagesFromGroupAllowed', group_id=self.channel['external_id'], user_id=external_user_id)
        if not allowed.get('is_allowed'):
            raise DeliveryError('Пользователь запретил сообщения сообщества.', blocked=True)
        payload = {'user_id': external_user_id, 'message': strip_formatting(body['text']), 'random_id': random_id}
        if buttons:
            payload['keyboard'] = json.dumps({'inline': True, 'buttons': keyboard}, ensure_ascii=False)
        if asset:
            payload['attachment'] = asset.get('remote_payload') or self.upload_asset(asset, external_user_id)
        return str(self.vk('messages.send', **payload))

    def upload_asset(self, asset, external_user_id):
        if self.kind == 'max':
            upload = self.max('POST', '/uploads', params={'type': 'image'})
            return self._upload(upload['url'], asset, {'max.ru', 'oneme.ru'})
        upload = self.vk('photos.getMessagesUploadServer', peer_id=external_user_id)
        uploaded = self._upload(upload['upload_url'], asset, {'vk.com', 'vk.ru', 'userapi.com'}, field='photo')
        saved = self.vk('photos.saveMessagesPhoto', **{key: uploaded[key] for key in ('server', 'photo', 'hash')})[0]
        return 'photo{}_{}'.format(saved['owner_id'], saved['id'])

    def _upload(self, url, asset, domains, field='data'):
        parsed = urlparse(url)
        if parsed.scheme != 'https' or not any(parsed.hostname == d or (parsed.hostname or '').endswith('.' + d) for d in domains):
            raise DeliveryError('Сервис вернул неизвестный адрес загрузки.')
        return self._request('POST', url, files={field: ('image.' + asset['extension'], asset['data'], asset['mime'])}, allow_redirects=False,
                             verify=max_ca_bundle() if self.kind == 'max' else True)

    def acknowledge(self, event):
        if not event.get('callback_id'):
            return
        if self.kind == 'telegram':
            self.telegram('answerCallbackQuery', {'callback_query_id': event['callback_id']},
                          timeout=(5, 3), retry=False)
        elif self.kind == 'max':
            self.max('POST', '/answers', params={'callback_id': event['callback_id']}, payload={'notification': 'Готово'})
        else:
            self.vk('messages.sendMessageEventAnswer', event_id=event['callback_id'], user_id=event['user_id'], peer_id=event['user_id'])


def parse_event(kind, data):
    """Normalize only private, inbound user events; group chats and bot echoes are ignored."""
    event = {'text': '', 'callback': '', 'callback_id': '', 'name': '', 'username': '', 'type': 'message'}
    if kind == 'telegram':
        member = data.get('my_chat_member')
        if member:
            if member.get('chat', {}).get('type') != 'private':
                return None
            event.update(user_id=str(member['chat']['id']), type='blocked' if member.get('new_chat_member', {}).get('status') in ('kicked', 'left') else 'allow')
        else:
            callback = data.get('callback_query', {})
            message = callback.get('message', {}) if callback else data.get('message', {})
            sender = callback.get('from', {}) if callback else message.get('from', {})
            if not sender or sender.get('is_bot') or message.get('chat', {}).get('type') != 'private':
                return None
            if str(message['chat']['id']) != str(sender['id']):
                return None
            event.update(user_id=str(sender['id']), name=' '.join(filter(None, [sender.get('first_name'), sender.get('last_name')])),
                         username=sender.get('username', ''), text=message.get('text', message.get('caption', '')) if not callback else '',
                         callback=callback.get('data', ''), callback_id=callback.get('id', ''))
        event['key'] = str(data['update_id'])
    elif kind == 'vk':
        action = data.get('type')
        obj = data.get('object') or {}
        if action in ('message_allow', 'message_deny'):
            event.update(user_id=str(obj['user_id']), type='allow' if action == 'message_allow' else 'blocked')
        elif action == 'message_new':
            msg = obj.get('message', obj)
            if msg.get('out') or not msg.get('from_id') or msg['from_id'] <= 0 or msg.get('peer_id') != msg['from_id']:
                return None
            event.update(user_id=str(msg['from_id']), text=msg.get('text', ''))
        elif action == 'message_event':
            if obj.get('peer_id') != obj.get('user_id') or int(obj.get('user_id', 0)) <= 0:
                return None
            payload = obj.get('payload') or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    payload = {}
            event.update(user_id=str(obj['user_id']), callback=payload.get('senler', ''), callback_id=obj.get('event_id', ''))
        else:
            return None
        event['key'] = str(data.get('event_id') or '')
    else:
        action = data.get('update_type')
        if action in ('bot_started', 'bot_stopped'):
            sender = data.get('user', {})
            event.update(type='start' if action == 'bot_started' else 'blocked')
        elif action in ('message_created', 'message_callback'):
            message = data.get('message') or {}
            if message.get('recipient', {}).get('chat_type') != 'dialog':
                return None
            callback = data.get('callback') or {}
            sender = callback.get('user', {}) if callback else message.get('sender', {})
            if sender.get('is_bot'):
                return None
            event.update(text=message.get('body', {}).get('text', '') if not callback else '',
                         callback=callback.get('payload', ''), callback_id=callback.get('callback_id', ''))
        else:
            return None
        if not sender.get('user_id'):
            return None
        event.update(user_id=str(sender['user_id']), name=sender.get('first_name') or sender.get('name') or '', username=sender.get('username') or '')
        event['key'] = str(data.get('event_id') or '')
    if not str(event.get('user_id', '')).isdigit() or int(event['user_id']) <= 0:
        return None
    return event
