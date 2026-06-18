import base64
import requests
import uuid
import logging
from pathlib import Path

log = logging.getLogger('sber_api')

BASE_DIR  = Path(__file__).parent
CERT_FILE = BASE_DIR / 'certs' / 'sber_cert.pem'
KEY_FILE  = BASE_DIR / 'certs' / 'sber_key.pem'

AUTH_URL  = 'https://api.sberbank.ru/ru/prod/authoriz.do'
TOKEN_URL = 'https://api.sberbank.ru/ru/prod/tokens/v3/oauth'
STMT_URL  = 'https://api.sberbank.ru/fintech/api/v1/statement'

SCOPES = 'openid GET_STATEMENT_TRANSACTION GET_STATEMENT_ACCOUNT BANK_CONTROL_STATEMENT'


def _mtls():
    return (str(CERT_FILE), str(KEY_FILE))


def _basic_auth(client_id, client_secret):
    token = base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    return f'Basic {token}'


def build_auth_url(client_id, redirect_uri, state, nonce):
    """URL для редиректа пользователя на страницу авторизации СберБизнес."""
    params = (
        f'?response_type=code'
        f'&client_id={client_id}'
        f'&redirect_uri={redirect_uri}'
        f'&scope={SCOPES.replace(" ", "%20")}'
        f'&state={state}'
        f'&nonce={nonce}'
    )
    return AUTH_URL + params


def exchange_code(client_id, client_secret, code, redirect_uri):
    """Обменять authorization code на access_token + refresh_token."""
    resp = requests.post(
        TOKEN_URL,
        cert=_mtls(),
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': _basic_auth(client_id, client_secret),
        },
        data={
            'grant_type':   'authorization_code',
            'code':         code,
            'redirect_uri': redirect_uri,
        },
        timeout=30,
        verify=True,
    )
    log.info('exchange_code status=%s body=%s', resp.status_code, resp.text[:400])
    resp.raise_for_status()
    return resp.json()  # {access_token, refresh_token, expires_in, ...}


def refresh_access_token(client_id, client_secret, refresh_token):
    """Обновить access_token через refresh_token (без участия пользователя)."""
    resp = requests.post(
        TOKEN_URL,
        cert=_mtls(),
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': _basic_auth(client_id, client_secret),
        },
        data={
            'grant_type':    'refresh_token',
            'refresh_token': refresh_token,
        },
        timeout=30,
        verify=True,
    )
    log.info('refresh_token status=%s body=%s', resp.status_code, resp.text[:400])
    resp.raise_for_status()
    return resp.json()


def get_statement(access_token, client_id, account_number, date_from, date_to):
    """Запрос выписки по счёту."""
    resp = requests.get(
        STMT_URL,
        cert=_mtls(),
        headers={
            'Authorization':   f'Bearer {access_token}',
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
    log.info('get_statement status=%s body=%s', resp.status_code, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


def parse_transactions(data):
    """Разбирает ответ Sber API в список {'date','amount','description','counterparty'}."""
    ops = (
        data.get('Statement', {}).get('Transactions') or
        data.get('transactions') or
        data.get('operations') or
        data.get('operationList') or
        []
    )
    result = []
    for op in ops:
        date_raw = (
            op.get('OperationDate') or op.get('operationDate') or
            op.get('date') or op.get('valueDate') or ''
        )
        date_str = str(date_raw)[:10]
        if not date_str or date_str == 'None':
            continue

        amount = float(op.get('Amount', op.get('amount', op.get('sum', 0))) or 0)

        direction = str(
            op.get('Direction', op.get('direction', op.get('operationType',
            op.get('OperType', op.get('indicator', '')))))
        ).upper()
        if any(x in direction for x in ('OUT', 'DEBIT', 'РАСХОД', 'СПИСАН', '2', 'D')):
            amount = -abs(amount)
        elif any(x in direction for x in ('IN', 'CREDIT', 'ПРИХОД', 'ЗАЧИСЛ', '1', 'C')):
            amount = abs(amount)

        desc = str(
            op.get('Purpose', op.get('purpose', op.get('paymentPurpose',
            op.get('operationName', op.get('description', ''))))) or ''
        ).strip()

        if amount >= 0:
            counterparty = str(
                op.get('PayerName', op.get('payerName', op.get('debtorName', ''))) or ''
            ).strip()
        else:
            counterparty = str(
                op.get('RecipientName', op.get('recipientName', op.get('creditorName', ''))) or ''
            ).strip()
        if not counterparty:
            counterparty = str(
                op.get('counterPartyName', op.get('contragentName', '')) or ''
            ).strip()

        result.append({
            'date':         date_str,
            'amount':       amount,
            'description':  desc,
            'counterparty': counterparty,
        })
    return result
