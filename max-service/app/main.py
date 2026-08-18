import asyncio
import base64
import io
import json
import logging
import os
import random
import shutil
from typing import Optional

import httpx
import qrcode
from fastapi import FastAPI, Form, UploadFile, File as FastAPIFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pymax import ExtraConfig, Photo, QrAuthFlow, WebClient

PORT = int(os.environ.get('PORT', 3000))
AUTH_DIR = os.environ.get('AUTH_DIR', '/data/auth')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL', '')
WEBHOOK_TOKEN = os.environ.get('WEBHOOK_TOKEN', '')
RESUME_URL = os.environ.get('RESUME_URL', '')
PROXY_URL = os.environ.get('PROXY_URL') or None
# Для входящих сообщений (нужны "Оценке заказа" — принять ответ клиента с баллом) отдельная
# env-переменная не заводилась: путь выводится из WEBHOOK_URL, как и в whatsapp-service.
INBOUND_WEBHOOK_URL = WEBHOOK_URL.replace('/api/messenger-webhook', '/api/messenger-inbound')

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('max-service')

app = FastAPI()

# Мульти-сессионность — несколько MAX-аккаунтов в одном процессе, ключ — accountId,
# который генерирует Flask/SQLite при создании записи "номер-отправитель" (messenger_accounts).
# Тот же принцип, что в whatsapp-service (Map/dict accountId -> AccountState), но здесь
# на asyncio (PyMax — асинхронная библиотека) без моста между потоками: FastAPI-хендлеры и
# фоновая задача сессии/кампании живут на одном event loop.


class AccountState:
    def __init__(self, account_id: str):
        self.account_id = account_id
        self.client: Optional[WebClient] = None
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


def e164(phone: Optional[str]) -> str:
    """MAX (как и большинство примеров PyMax) ожидает телефон в формате +7999...,
    а в остальной системе (Flask/whatsapp-service) номера хранятся без "+"."""
    if not phone:
        return ''
    phone = phone.strip()
    return phone if phone.startswith('+') else f'+{phone}'


class QueueQrHandler:
    """QR-вход (WebClient) вместо телефон+SMS-код (Client): подтверждение делает уже
    залогиненный MAX на телефоне пользователя, отсканировав код — так же, как WhatsApp Web.
    Вход по SMS-коду у MAX на практике требует, чтобы на аккаунте заранее был установлен
    пароль (иначе сервер отказывает уже после кода) — с QR этого ограничения нет, поэтому
    выбран именно этот способ, а не Client из первой версии сервиса."""

    def __init__(self, state: AccountState):
        self.state = state

    async def show_qr(self, qr_url: str) -> None:
        self.state.qr_url = qr_url
        self.state.status = 'qr'


class QueuePasswordProvider:
    def __init__(self, state: AccountState):
        self.state = state

    async def get_password(self, hint: Optional[str] = None) -> str:
        self.state.status = 'awaiting_password'
        self.state.password_hint = hint
        password = await self.state.password_queue.get()
        self.state.status = 'connecting'
        return password


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


async def run_account(state: AccountState, phone: Optional[str]) -> None:
    state.status = 'connecting'
    state.error = None
    state.qr_url = None
    if phone and not state.phone:
        state.phone = phone  # временная метка для UI, перезапишется реальным номером после входа
    work_dir = auth_dir_for(state.account_id)

    # WebClient не принимает password_provider напрямую (в отличие от Client) — передаём
    # свой QrAuthFlow явно, чтобы завести туда и обработчик QR, и обработчик пароля 2FA.
    auth_flow = QrAuthFlow(
        qr_provider=QueueQrHandler(state),
        password_provider=QueuePasswordProvider(state),
    )
    client = WebClient(
        work_dir=work_dir,
        session_name='session.db',
        auth_flow=auth_flow,
        extra_config=ExtraConfig(proxy=PROXY_URL, telemetry=False, reconnect=True, log_level='WARNING'),
    )
    state.client = client

    @client.on_start()
    async def _on_start(c: WebClient) -> None:
        state.status = 'connected'
        state.error = None
        if c.me is not None and c.me.contact.phone:
            state.phone = str(c.me.contact.phone)
        log.info('account %s connected phone=%s', state.account_id, state.phone)
        if not state.resume_checked:
            state.resume_checked = True
            asyncio.create_task(check_resume(state))

    @client.on_disconnect()
    async def _on_disconnect(exc, reconnect, delay) -> None:
        log.warning('account %s disconnected reconnect=%s delay=%s exc=%s',
                     state.account_id, reconnect, delay, exc)
        if not reconnect:
            state.status = 'disconnected'

    # Нужно "Оценке заказа" — принять ответный текст клиента (баллом от 1 до 5) на запрос
    # об оценке заказа. Раньше сервис только отправлял, входящие никак не обрабатывались.
    # message.sender — числовой ID пользователя, не телефон (см. pymax/types/domain/message.py),
    # телефон приходится резолвить отдельным вызовом get_user().
    @client.on_message()
    async def _on_message(message, c: WebClient) -> None:
        if not message.text or message.sender is None:
            return
        if c.me is not None and message.sender == c.me.contact.id:
            return  # не реагируем на собственные исходящие
        try:
            user = await c.get_user(message.sender)
        except Exception:
            return
        if user is None or not user.phone:
            return
        asyncio.create_task(send_inbound_webhook({
            'channel': 'max', 'account_id': state.account_id,
            'phone': str(user.phone), 'text': message.text,
        }))

    try:
        await client.start()
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
            user = await state.client.search_by_phone(e164(phone))
        except Exception:
            user = None

        if user is None:
            result['status'] = 'invalid'
        else:
            try:
                chat_id = state.client.get_chat_id(
                    first_user_id=state.client.me.contact.id, second_user_id=user.id)
                attachments = [Photo(raw=image_bytes, name='image.jpg')] if image_bytes else None
                await state.client.send_message(chat_id=chat_id, text=text, attachments=attachments)
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
            # Идемпотентно — не поднимаем вторую сессию поверх той же auth-директории.
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
    try:
        if state.client:
            try:
                await state.client.logout()
            except Exception:
                log.exception('logout call failed account_id=%s', account_id)
        if state.session_task:
            state.session_task.cancel()
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
            await state.client.search_by_phone(e164(phone))
            results[phone] = True
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
        user = await state.client.search_by_phone(e164(body.phone))
        chat_id = state.client.get_chat_id(
            first_user_id=state.client.me.contact.id, second_user_id=user.id)
        await state.client.send_message(chat_id=chat_id, text=body.text)
        return {'ok': True}
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
    # Переживаем рестарт контейнера: поднимаем заново только аккаунты, у которых уже
    # есть сохранённая сессия (файл session.db) — на пустой auth-директории клиент
    # заново показал бы QR "в никуда"; такие незавершённые аккаунты просто остаются
    # disconnected, пользователь переподключит их через вкладку «Номера».
    os.makedirs(AUTH_DIR, exist_ok=True)
    for entry in os.scandir(AUTH_DIR):
        if entry.is_dir() and os.path.exists(os.path.join(entry.path, 'session.db')):
            state = get_account(entry.name)
            state.session_task = asyncio.create_task(run_account(state, phone=None))
