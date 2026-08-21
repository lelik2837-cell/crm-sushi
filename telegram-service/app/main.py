import asyncio
import base64
import io
import json
import logging
import os
import random
import shutil
from io import BytesIO
from typing import Optional
from urllib.parse import urlparse

import httpx
import qrcode
from fastapi import FastAPI, Form, UploadFile, File as FastAPIFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.contacts import ImportContactsRequest, DeleteContactsRequest
from telethon.tl.types import InputPhoneContact

PORT = int(os.environ.get('PORT', 3000))
AUTH_DIR = os.environ.get('AUTH_DIR', '/data/auth')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')
WEBHOOK_TOKEN = os.environ.get('WEBHOOK_TOKEN', '')
RESUME_URL = os.environ.get('RESUME_URL', '')
PROXY_URL = os.environ.get('PROXY_URL') or None
# api_id/api_hash — с my.telegram.org, привязаны к аккаунту, который регистрировал приложение,
# но применяются как "удостоверение клиента" для ЛЮБОГО номера, входящего через QR в этот
# сервис — это не логин/пароль конкретного аккаунта-отправителя, а как бы "версия приложения".
API_ID = int(os.environ.get('TELEGRAM_API_ID', '0'))
API_HASH = os.environ.get('TELEGRAM_API_HASH', '')
# Тот же приём, что в max-service: путь для входящих выводится из WEBHOOK_URL, без отдельной
# env-переменной, чтобы не требовать лишней правки docker-compose.yml/env-файлов на сервере.
INBOUND_WEBHOOK_URL = WEBHOOK_URL.replace('/api/messenger-webhook', '/api/messenger-inbound')

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('telegram-service')

app = FastAPI()

# Мульти-сессионность — несколько Telegram-аккаунтов в одном процессе, ключ — accountId
# (генерирует Flask/SQLite при создании записи "номер-отправитель" в messenger_accounts).
# Telethon, как и PyMax, асинхронный — тот же принцип, что в max-service: FastAPI-хендлеры и
# фоновая задача сессии/кампании живут на одном event loop, без моста между потоками.


class AccountState:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.client: Optional[TelegramClient] = None
        self.status = 'disconnected'  # disconnected|connecting|qr|awaiting_password|connected|error
        self.phone: Optional[str] = None
        self.error: Optional[str] = None
        self.qr_url: Optional[str] = None
        self.password_hint: Optional[str] = None
        self.password_queue: asyncio.Queue = asyncio.Queue()
        self.session_task: Optional[asyncio.Task] = None
        self.campaign: Optional[dict] = None
        self.lock = asyncio.Lock()
        self.resume_checked = False


accounts: dict[str, AccountState] = {}


def get_account(account_id: str) -> AccountState:
    if account_id not in accounts:
        accounts[account_id] = AccountState(account_id)
    return accounts[account_id]


def auth_dir_for(account_id: str) -> str:
    path = os.path.join(AUTH_DIR, str(account_id))
    os.makedirs(path, exist_ok=True)
    return path


def session_path_for(account_id: str) -> str:
    # Telethon сам добавит расширение .session к этому пути (SQLite-файл сессии).
    return os.path.join(auth_dir_for(account_id), 'session')


def e164(phone: Optional[str]) -> str:
    """Как и в max-service — телефон в остальной системе хранится без "+", а Telegram
    (ImportContactsRequest) ожидает международный формат с "+"."""
    if not phone:
        return ''
    phone = phone.strip()
    return phone if phone.startswith('+') else f'+{phone}'


def _parse_proxy(url: Optional[str]):
    """Разбирает PROXY_URL (socks5://host:port) в кортеж, который понимает Telethon
    (через PySocks). Пока не проверено, нужен ли для Telegram вообще прокси с этого
    сервера — в отличие от WhatsApp, Telegram в РФ формально не заблокирован, а MAX
    в итоге прокси тоже не понадобился (см. plan.md, п.254) — оставлено на случай,
    если реальная проверка на сервере покажет обратное."""
    if not url:
        return None
    import socks
    parsed = urlparse(url)
    scheme_map = {'socks5': socks.SOCKS5, 'socks4': socks.SOCKS4, 'http': socks.HTTP}
    proxy_type = scheme_map.get(parsed.scheme)
    if not proxy_type or not parsed.hostname or not parsed.port:
        return None
    return (proxy_type, parsed.hostname, parsed.port, True, parsed.username, parsed.password)


PROXY = _parse_proxy(PROXY_URL)


async def send_webhook(payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(f'{WEBHOOK_URL}/{WEBHOOK_TOKEN}', json=payload)
    except Exception:
        log.exception('webhook call failed')


async def send_inbound_webhook(payload: dict) -> None:
    if not WEBHOOK_URL:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            await http.post(f'{INBOUND_WEBHOOK_URL}/{WEBHOOK_TOKEN}', json=payload)
    except Exception:
        log.exception('inbound webhook call failed')


async def check_resume(state: AccountState) -> None:
    if not RESUME_URL or not WEBHOOK_TOKEN or state.campaign:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.get(RESUME_URL, params={'token': WEBHOOK_TOKEN, 'account_id': state.account_id})
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data or data.get('none') or not data.get('recipients'):
            return
        log.info('account %s resuming broadcast #%s, %d pending',
                  state.account_id, data.get('broadcast_id'), len(data['recipients']))
        cfg = {
            'broadcastId': data['broadcast_id'],
            'queue': data['recipients'],
            'intervalMin': data.get('interval_min_seconds', 5),
            'intervalMax': data.get('interval_max_seconds', 5),
            'batchSize': data.get('batch_size', 0),
            'batchPauseSeconds': data.get('batch_pause_seconds', 0),
            'image_bytes': base64.b64decode(data['image_base64']) if data.get('image_base64') else None,
            'imageMime': data.get('image_mime'),
        }
        asyncio.create_task(run_campaign(state, cfg))
    except Exception:
        log.exception('checkResume failed account_id=%s', state.account_id)


async def resolve_phone(client: TelegramClient, phone: str):
    """Проверка "зарегистрирован ли номер в Telegram" и подготовка к отправке — Telegram не
    даёт написать первым незнакомцу напрямую по номеру, официального аналога check_numbers
    нет, общепринятый приём (в т.ч. у Telethon в документации) — временно добавить номер
    контактом (ImportContactsRequest), после чего появляется User-сущность, которой можно
    написать; result.users пуст, если номер не найден или скрыл себя настройками приватности.
    Контакт сразу удаляется (DeleteContactsRequest), чтобы не засорять адресную книгу
    аккаунта-отправителя разовыми получателями рассылки."""
    contact = InputPhoneContact(client_id=random.randint(1, 2**31 - 1), phone=phone,
                                 first_name=phone, last_name='')
    result = await client(ImportContactsRequest([contact]))
    if not result.users:
        return None
    user = result.users[0]
    try:
        await client(DeleteContactsRequest(id=[user]))
    except Exception:
        log.exception('DeleteContactsRequest failed')
    return user


def register_handlers(state: AccountState, client: TelegramClient) -> None:
    # Нужно "Оценке заказа" — принять ответный текст клиента (баллом от 1 до 5). Раньше
    # сервис только отправлял, входящие никак не обрабатывались. incoming=True уже исключает
    # собственные исходящие сообщения — доп. проверки на self-id, как в max-service, не нужны.
    @client.on(events.NewMessage(incoming=True))
    async def _on_message(event) -> None:
        if not event.raw_text:
            return
        asyncio.create_task(send_inbound_webhook({
            'channel': 'telegram', 'account_id': state.account_id,
            'sender_id': str(event.sender_id), 'text': event.raw_text,
        }))


async def run_account(state: AccountState, phone: Optional[str]) -> None:
    state.status = 'connecting'
    state.error = None
    state.qr_url = None
    if phone and not state.phone:
        state.phone = phone  # временная метка для UI, перезапишется реальным номером после входа

    client = TelegramClient(session_path_for(state.account_id), API_ID, API_HASH, proxy=PROXY)
    state.client = client

    try:
        await client.connect()
        if not await client.is_user_authorized():
            qr_login = await client.qr_login()
            while True:
                state.qr_url = qr_login.url
                state.status = 'qr'
                try:
                    await qr_login.wait(timeout=60)
                    break
                except SessionPasswordNeededError:
                    # 2FA поверх QR — тот же принцип очереди, что в max-service.
                    state.status = 'awaiting_password'
                    password = await state.password_queue.get()
                    state.status = 'connecting'
                    await client.sign_in(password=password)
                    break
                except asyncio.TimeoutError:
                    await qr_login.recreate()  # QR истёк — новый код без пересоздания клиента

        me = await client.get_me()
        if me is not None and me.phone:
            state.phone = str(me.phone)
        state.status = 'connected'
        state.error = None
        state.qr_url = None
        log.info('account %s connected phone=%s', state.account_id, state.phone)
        register_handlers(state, client)
        if not state.resume_checked:
            state.resume_checked = True
            asyncio.create_task(check_resume(state))

        await client.run_until_disconnected()
        if state.status == 'connected':
            # Сюда попадаем и при штатном logout (тогда status уже переставлен на disconnected
            # раньше — см. session_logout), и при обрыве не по нашей инициативе.
            state.status = 'disconnected'
    except Exception as e:
        log.exception('account %s auth/start failed', state.account_id)
        state.status = 'error'
        state.error = str(e)


async def run_campaign(state: AccountState, cfg: dict) -> None:
    state.campaign = {'stopFlag': False, 'broadcastId': cfg['broadcastId']}
    sent_since_batch = 0
    image_bytes = cfg.get('image_bytes')

    for item in cfg['queue']:
        if state.campaign['stopFlag']:
            break
        phone = item['phone']
        text = item.get('message') or ''
        result = {'broadcast_id': cfg['broadcastId'], 'account_id': state.account_id,
                   'phone': phone, 'status': 'failed', 'error': None}
        try:
            user = await resolve_phone(state.client, e164(phone))
        except Exception:
            user = None

        if user is None:
            result['status'] = 'invalid'
        else:
            try:
                if image_bytes:
                    file = BytesIO(image_bytes)
                    file.name = 'image.jpg'
                    await state.client.send_file(user, file=file, caption=text)
                else:
                    await state.client.send_message(user, text)
                result['status'] = 'sent'
            except Exception as e:
                result['status'] = 'failed'
                result['error'] = str(e)

        await send_webhook(result)
        if result['status'] != 'invalid':
            sent_since_batch += 1

        if state.campaign['stopFlag']:
            break

        if cfg['batchSize'] > 0 and sent_since_batch >= cfg['batchSize']:
            sent_since_batch = 0
            await asyncio.sleep(cfg['batchPauseSeconds'])
        else:
            lo, hi = sorted((cfg['intervalMin'], cfg['intervalMax']))
            await asyncio.sleep(lo + random.random() * (hi - lo))

    state.campaign = None


class SessionStartBody(BaseModel):
    phone: Optional[str] = None


class PasswordBody(BaseModel):
    password: str


class CodeBody(BaseModel):
    code: str


class PhonesBody(BaseModel):
    phones: list[str]


class SendMessageBody(BaseModel):
    phone: str
    text: str


@app.get('/health')
async def health():
    return {'ok': True, 'accounts': len(accounts)}


@app.post('/accounts/{account_id}/session/start')
async def session_start(account_id: str, body: SessionStartBody = SessionStartBody()):
    state = get_account(account_id)
    async with state.lock:
        if state.status in ('connecting', 'qr', 'awaiting_password', 'connected'):
            return {'ok': True, 'status': state.status}
        state.session_task = asyncio.create_task(run_account(state, body.phone))
    return {'ok': True, 'status': state.status}


@app.get('/accounts/{account_id}/session/status')
async def session_status(account_id: str):
    state = get_account(account_id)
    return {'status': state.status, 'phone': state.phone, 'campaignRunning': bool(state.campaign),
            'error': state.error, 'passwordHint': state.password_hint}


@app.get('/accounts/{account_id}/session/qr')
async def session_qr(account_id: str):
    state = get_account(account_id)
    if not state.qr_url:
        return {'qr': None}
    img = qrcode.make(state.qr_url)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    data_url = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    return {'qr': data_url}


@app.post('/accounts/{account_id}/session/verify-code')
async def session_verify_code(account_id: str, body: CodeBody):
    # Вход только по QR — телефон+SMS-код у Telegram здесь не задействован, эндпоинт
    # существует только чтобы соответствовать общему контракту messenger_api.verify_code.
    return JSONResponse({'error': 'not_applicable_qr_only'}, status_code=409)


@app.post('/accounts/{account_id}/session/verify-password')
async def session_verify_password(account_id: str, body: PasswordBody):
    state = get_account(account_id)
    if state.status != 'awaiting_password':
        return JSONResponse({'error': 'not_awaiting_password'}, status_code=409)
    await state.password_queue.put(body.password)
    return {'ok': True}


@app.post('/accounts/{account_id}/session/logout')
async def session_logout(account_id: str):
    state = get_account(account_id)
    was_connected = state.status == 'connected'
    try:
        # Отменяем фоновую задачу сессии ДО log_out/disconnect — если она застряла внутри
        # client.connect() (сеть до Telegram не отвечает, см. plan.md п.274), отмена обрывает
        # retry-цикл сразу, а не ждёт, пока Telethon сам исчерпает все попытки. Без этого
        # запрос сюда мог зависать дольше, чем 10-секундный HTTP-таймаут на стороне Flask.
        if state.session_task:
            state.session_task.cancel()
        if state.client:
            try:
                # log_out() шлёт настоящий запрос auth.LogOutRequest — имеет смысл только если
                # аккаунт был реально авторизован; иначе (сессия зависла на этапе connect/QR)
                # достаточно просто разорвать ещё не до конца установленное соединение.
                if was_connected:
                    await asyncio.wait_for(state.client.log_out(), timeout=5)
                else:
                    await asyncio.wait_for(state.client.disconnect(), timeout=5)
            except Exception:
                log.exception('logout/disconnect call failed account_id=%s', account_id)
        shutil.rmtree(auth_dir_for(account_id), ignore_errors=True)
        os.makedirs(auth_dir_for(account_id), exist_ok=True)
        state.status = 'disconnected'
        state.phone = None
        state.client = None
        state.error = None
        state.resume_checked = False
        return {'ok': True}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@app.post('/accounts/{account_id}/numbers/check')
async def numbers_check(account_id: str, body: PhonesBody):
    state = get_account(account_id)
    if state.status != 'connected':
        return JSONResponse({'error': 'not_connected'}, status_code=409)
    if not body.phones:
        return JSONResponse({'error': 'empty'}, status_code=400)
    results = {}
    for phone in body.phones:
        try:
            user = await resolve_phone(state.client, e164(phone))
            results[phone] = user is not None
        except Exception:
            results[phone] = False
        await asyncio.sleep(0.3)
    return {'ok': True, 'results': results}


# Одно сообщение сразу, без очереди/пауз — в отличие от campaign/start (та под пачку
# с рандомными интервалами). Нужно для событийных одиночных отправок ("Оценка заказа").
@app.post('/accounts/{account_id}/message/send')
async def message_send(account_id: str, body: SendMessageBody):
    state = get_account(account_id)
    if state.status != 'connected':
        return JSONResponse({'error': 'not_connected'}, status_code=409)
    try:
        user = await resolve_phone(state.client, e164(body.phone))
        if user is None:
            return JSONResponse({'error': 'not_registered'}, status_code=404)
        await state.client.send_message(user, body.text)
        # recipient_ref — тот же ID, что придёт в event.sender_id у ответа этого пользователя
        # (см. register_handlers) — Flask сохраняет его, чтобы потом смочь сматчить входящий ответ.
        return {'ok': True, 'recipient_ref': str(user.id)}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@app.post('/accounts/{account_id}/campaign/start')
async def campaign_start(
    account_id: str,
    broadcastId: str = Form(...),
    recipients: str = Form(...),
    intervalMin: str = Form('5'),
    intervalMax: str = Form('5'),
    batchSize: str = Form('0'),
    batchPauseSeconds: str = Form('0'),
    image: Optional[UploadFile] = FastAPIFile(None),
):
    state = get_account(account_id)
    if state.status != 'connected':
        return JSONResponse({'error': 'not_connected'}, status_code=409)
    if state.campaign:
        return JSONResponse({'error': 'already_running'}, status_code=409)
    try:
        queue = json.loads(recipients)
    except Exception:
        return JSONResponse({'error': 'bad_recipients'}, status_code=400)
    if not queue or not queue[0].get('phone'):
        return JSONResponse({'error': 'empty_recipients'}, status_code=400)

    image_bytes = None
    if image is not None and image.filename:
        image_bytes = await image.read()

    cfg = {
        'broadcastId': broadcastId,
        'queue': queue,
        'intervalMin': float(intervalMin or 5),
        'intervalMax': float(intervalMax or 5),
        'batchSize': int(batchSize or 0),
        'batchPauseSeconds': float(batchPauseSeconds or 0),
        'image_bytes': image_bytes,
    }
    asyncio.create_task(run_campaign(state, cfg))
    return JSONResponse({'ok': True}, status_code=202)


@app.post('/accounts/{account_id}/campaign/stop')
async def campaign_stop(account_id: str):
    state = get_account(account_id)
    if state.campaign:
        state.campaign['stopFlag'] = True
    return {'ok': True}


@app.get('/accounts/{account_id}/campaign/status')
async def campaign_status(account_id: str):
    state = get_account(account_id)
    if not state.campaign:
        return {'running': False}
    return {'running': True, 'broadcastId': state.campaign['broadcastId']}


@app.on_event('startup')
async def restore_sessions() -> None:
    # Переживаем рестарт контейнера: поднимаем заново только аккаунты, у которых уже есть
    # сохранённая сессия (файл session.session) — на пустой auth-директории клиент заново
    # показал бы QR "в никуда"; такие незавершённые аккаунты остаются disconnected,
    # пользователь переподключит их через вкладку «Номера».
    os.makedirs(AUTH_DIR, exist_ok=True)
    for entry in os.scandir(AUTH_DIR):
        if entry.is_dir() and os.path.exists(os.path.join(entry.path, 'session.session')):
            state = get_account(entry.name)
            state.session_task = asyncio.create_task(run_account(state, phone=None))
