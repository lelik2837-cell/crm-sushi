import os
import json
import logging

import requests

log = logging.getLogger('whatsapp_api')

BASE_URL = os.environ.get('WHATSAPP_SERVICE_URL', 'http://whatsapp:3000')


def get_status(timeout=5):
    resp = requests.get(f'{BASE_URL}/session/status', timeout=timeout)
    log.info('get_status status=%s body=%s', resp.status_code, resp.text[:400])
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()


def get_qr(timeout=5):
    resp = requests.get(f'{BASE_URL}/session/qr', timeout=timeout)
    log.info('get_qr status=%s body=%s', resp.status_code, resp.text[:200])
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json().get('qr')


def logout(timeout=10):
    resp = requests.post(f'{BASE_URL}/session/logout', timeout=timeout)
    log.info('logout status=%s body=%s', resp.status_code, resp.text[:400])
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()


def start_campaign(broadcast_id, recipients, interval_min, interval_max,
                    batch_size, batch_pause_seconds, image_bytes=None, image_mime=None,
                    timeout=30):
    """recipients — список {'phone': ..., 'message': ...} (текст уже подобран по варианту получателя)."""
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
    resp = requests.post(f'{BASE_URL}/campaign/start', data=data, files=files, timeout=timeout)
    log.info('start_campaign broadcast_id=%s status=%s body=%s', broadcast_id, resp.status_code, resp.text[:400])
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()


def stop_campaign(broadcast_id, timeout=10):
    resp = requests.post(f'{BASE_URL}/campaign/stop', json={'broadcastId': str(broadcast_id)}, timeout=timeout)
    log.info('stop_campaign broadcast_id=%s status=%s body=%s', broadcast_id, resp.status_code, resp.text[:400])
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json()


def check_numbers(phones, timeout=60):
    """Проверка прямо на WhatsApp, зарегистрирован ли номер — до отправки, чтобы не
    тратить впустую отправки/паузы на заведомо мёртвые номера. Требует активной сессии
    (подключённого WhatsApp) — вызывающий код должен сам решить, что делать при ошибке
    (например, просто пропустить проверку и оставить номера как есть)."""
    resp = requests.post(f'{BASE_URL}/numbers/check', json={'phones': phones}, timeout=timeout)
    log.info('check_numbers count=%s status=%s', len(phones), resp.status_code)
    if not resp.ok:
        raise RuntimeError(f'{resp.status_code} {resp.text[:400]}')
    return resp.json().get('results', {})
