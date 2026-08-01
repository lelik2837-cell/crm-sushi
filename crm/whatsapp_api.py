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


def start_campaign(broadcast_id, message, recipients, interval_min, interval_max,
                    batch_size, batch_pause_seconds, image_bytes=None, image_mime=None,
                    timeout=30):
    data = {
        'broadcastId': str(broadcast_id),
        'message': message or '',
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
