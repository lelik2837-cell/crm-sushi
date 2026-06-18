import requests
import uuid
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger('sber_api')

BASE_DIR  = Path(__file__).parent
CERT_FILE = BASE_DIR / 'certs' / 'sber_cert.pem'
KEY_FILE  = BASE_DIR / 'certs' / 'sber_key.pem'

TOKEN_URL = 'https://api.sberbank.ru/ru/prod/tokens/v3/oauth'
STMT_URL  = 'https://api.sberbank.ru/fintech/api/v1/statement'


def _mtls():
    return (str(CERT_FILE), str(KEY_FILE))


def get_token(client_id, client_secret):
    """OAuth2 client_credentials с mTLS. Возвращает (access_token, expires_in)."""
    resp = requests.post(
        TOKEN_URL,
        cert=_mtls(),
        data={
            'grant_type':    'client_credentials',
            'scope':         'BANK_CONTROL_STATEMENT',
            'client_id':     client_id,
            'client_secret': client_secret,
        },
        headers={'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=30,
        verify=True,
    )
    log.debug('token status=%s body=%s', resp.status_code, resp.text[:300])
    resp.raise_for_status()
    data = resp.json()
    return data['access_token'], int(data.get('expires_in', 3600))


def get_statement(access_token, client_id, account_number, date_from, date_to):
    """Запрос выписки. date_from/date_to — строки YYYY-MM-DD."""
    resp = requests.get(
        STMT_URL,
        cert=_mtls(),
        headers={
            'Authorization':  f'Bearer {access_token}',
            'x-ibm-client-id': str(client_id),
            'rqUID':           str(uuid.uuid4()),
            'Accept':          'application/json',
        },
        params={
            'accountNumber': account_number,
            'startDate':     date_from,
            'endDate':       date_to,
        },
        timeout=60,
        verify=True,
    )
    log.debug('stmt status=%s body=%s', resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def parse_transactions(data):
    """Разбирает ответ Sber API в список {'date','amount','description','counterparty'}."""
    # Sber API может вернуть разные форматы — пробуем все варианты
    ops = (
        data.get('Statement', {}).get('Transactions') or
        data.get('transactions') or
        data.get('operations') or
        data.get('operationList') or
        []
    )
    result = []
    for op in ops:
        # Дата
        date_raw = (
            op.get('OperationDate') or op.get('operationDate') or
            op.get('date') or op.get('valueDate') or ''
        )
        date_str = str(date_raw)[:10]
        if not date_str or date_str == 'None':
            continue

        # Сумма
        amount = float(op.get('Amount', op.get('amount', op.get('sum', 0))) or 0)

        # Направление: IN/OUT, DEBIT/CREDIT, C/D, 1/2
        direction = str(
            op.get('Direction', op.get('direction', op.get('operationType',
            op.get('OperType', op.get('indicator', '')))))
        ).upper()
        if any(x in direction for x in ('OUT', 'DEBIT', 'РАСХОД', 'СПИСАН', '2', 'D')):
            amount = -abs(amount)
        elif any(x in direction for x in ('IN', 'CREDIT', 'ПРИХОД', 'ЗАЧИСЛ', '1', 'C')):
            amount = abs(amount)

        # Назначение
        desc = str(
            op.get('Purpose', op.get('purpose', op.get('paymentPurpose',
            op.get('operationName', op.get('description', ''))))) or ''
        ).strip()

        # Контрагент: для прихода — плательщик, для расхода — получатель
        if amount >= 0:
            counterparty = str(
                op.get('PayerName', op.get('payerName', op.get('debtorName', ''))) or ''
            ).strip()
        else:
            counterparty = str(
                op.get('RecipientName', op.get('recipientName', op.get('creditorName', ''))) or ''
            ).strip()

        if not counterparty:
            counterparty = str(op.get('counterPartyName', op.get('contragentName', '')) or '').strip()

        result.append({
            'date':         date_str,
            'amount':       amount,
            'description':  desc,
            'counterparty': counterparty,
        })
    return result
