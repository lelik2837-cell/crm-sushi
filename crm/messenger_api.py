import os
import json
import logging

import requests

log = logging.getLogger('messenger_api')

# Три канала — три отдельных микросервиса (whatsapp-service/, telegram-service/, max-service/),
# у всех один и тот же HTTP-контракт (/accounts/:accountId/...), только разная реализация внутри
# (Baileys/Telethon/PyMax). Раньше был отдельный crm/whatsapp_api.py на один жёстко зашитый канал —
# заменён этим параметризованным клиентом при добавлении Max и Telegram.
CHANNEL_URLS = {
    'whatsapp': os.environ.get('WHATSAPP_SERVICE_URL', 'http://whatsapp:3000'),
    'telegram': os.environ.get('TELEGRAM_SERVICE_URL', 'http://telegram:3000'),
    'max': os.environ.get('MAX_SERVICE_URL', 'http://max:3000'),
}


def _base_url(channel):
    try:
        return CHANNEL_URLS[channel]
    except KeyError:
        raise ValueError(f'неизвестный канал рассылки: {channel}')


def start_session(channel, account_id, phone=None, timeout=10):
    """Инициировать подключение нового аккаунта (запускает QR-цикл на стороне сервиса).
    Идемпотентно — повторный вызов для уже подключающегося/подключённого аккаунта просто
    возвращает текущий статус, не плодит вторую сессию поверх той же auth-директории."""
    resp = requests.post(f'{_base_url(channel)}/accounts/{account_id}/session/start',
                          json={'phone': phone} if phone else {}, timeout=timeout)
    log.info('start_session channel=%s account_id=%s status=%s', channel, account_id, resp.status_code)
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()


def get_status(channel, account_id, timeout=5):
    resp = requests.get(f'{_base_url(channel)}/accounts/{account_id}/session/status', timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()


def get_qr(channel, account_id, timeout=5):
    resp = requests.get(f'{_base_url(channel)}/accounts/{account_id}/session/qr', timeout=timeout)
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json().get('qr')


def logout(channel, account_id, timeout=10):
    resp = requests.post(f'{_base_url(channel)}/accounts/{account_id}/session/logout', timeout=timeout)
    log.info('logout channel=%s account_id=%s status=%s', channel, account_id, resp.status_code)
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()


def check_numbers(channel, account_id, phones, timeout=60):
    """Проверка прямо на стороне канала, зарегистрирован ли номер — до отправки/до назначения
    получателя на этот аккаунт в каскаде. Требует активной сессии этого аккаунта — вызывающий
    код сам решает, что делать при ошибке (обычно — просто пропустить проверку)."""
    resp = requests.post(f'{_base_url(channel)}/accounts/{account_id}/numbers/check',
                          json={'phones': phones}, timeout=timeout)
    log.info('check_numbers channel=%s account_id=%s count=%s status=%s',
              channel, account_id, len(phones), resp.status_code)
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json().get('results', {})


def start_campaign(channel, account_id, broadcast_id, recipients, interval_min, interval_max,
                    batch_size, batch_pause_seconds, image_bytes=None, image_mime=None,
                    timeout=30):
    """recipients — список {'phone': ..., 'message': ...} для получателей, назначенных именно
    на этот аккаунт (см. _assign_broadcast_recipients в app.py)."""
    data = {
        'broadcastId': str(broadcast_id),
        'recipients': json.dumps(recipients),
        'intervalMin': str(interval_min),
        'intervalMax': str(interval_max),
        'batchSize': str(batch_size),
        'batchPauseSeconds': str(batch_pause_seconds),
    }
    files = None
    if image_bytes:
        files = {'image': ('image', image_bytes, image_mime or 'application/octet-stream')}
    resp = requests.post(f'{_base_url(channel)}/accounts/{account_id}/campaign/start',
                          data=data, files=files, timeout=timeout)
    log.info('start_campaign channel=%s account_id=%s broadcast_id=%s status=%s',
              channel, account_id, broadcast_id, resp.status_code)
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()


def stop_campaign(channel, account_id, broadcast_id, timeout=10):
    resp = requests.post(f'{_base_url(channel)}/accounts/{account_id}/campaign/stop',
                          json={'broadcastId': str(broadcast_id)}, timeout=timeout)
    log.info('stop_campaign channel=%s account_id=%s broadcast_id=%s status=%s',
              channel, account_id, broadcast_id, resp.status_code)
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()
