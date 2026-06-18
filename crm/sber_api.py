import base64
import time
import uuid
import logging
from pathlib import Path

log = logging.getLogger('sber_api')

BASE_DIR  = Path(__file__).parent
CERT_FILE = BASE_DIR / 'certs' / 'sber_cert.pem'
KEY_FILE  = BASE_DIR / 'certs' / 'sber_key.pem'

# Авторизация браузером — sbi.sberbank.ru (публичный, без mTLS для браузера)
AUTH_URL  = 'https://sbi.sberbank.ru:9443/ic/sso/api/v2/oauth/authorize'
# Токены и API — fintech.sberbank.ru (mTLS с нашим сертификатом)
TOKEN_URL = 'https://fintech.sberbank.ru:9443/ic/sso/api/v2/oauth/token'
NPA_URL   = 'https://fintech.sberbank.ru:9443/ic/sso/api/v2/npa/token'
STMT_URL  = 'https://fintech.sberbank.ru:9443/fintech/api/v2/statement/transactions'

# Scope v1 — универсальный доступ ко всем операциям (включает BANK_CONTROL_STATEMENT и др.)
SCOPES = 'openid di-17ae8543-3452-4b7e-8ae4-93ae0045dcf1'


def _mtls():
    return (str(CERT_FILE), str(KEY_FILE))


def _make_jwt(client_id):
    """JWT самоподпись для NPA аутентификации (RS256 с нашим приватным ключом)."""
    try:
        import jwt as pyjwt
        private_key = KEY_FILE.read_text()
        now = int(time.time())
        payload = {
            'iss': client_id,
            'sub': client_id,
            'aud': NPA_URL,
            'iat': now,
            'exp': now + 300,
            'jti': str(uuid.uuid4()),
        }
        return pyjwt.encode(payload, private_key, algorithm='RS256')
    except ImportError:
        raise RuntimeError('Не установлен pyjwt: pip3 install pyjwt cryptography')


def build_auth_url(client_id, redirect_uri, state, nonce):
    """URL для редиректа пользователя на страницу авторизации СберБизнес."""
    from urllib.parse import quote
    params = (
        f'?response_type=code'
        f'&client_id={client_id}'
        f'&redirect_uri={quote(redirect_uri, safe="")}'
        f'&scope={quote(SCOPES, safe="")}'
        f'&state={state}'
        f'&nonce={nonce}'
    )
    return AUTH_URL + params


def exchange_code(client_id, client_secret, code, redirect_uri):
    """Обменять authorization code на access_token + refresh_token."""
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import base64
    auth = 'Basic ' + base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        cert=_mtls(),
        verify=False,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': auth,
        },
        data={
            'grant_type':   'authorization_code',
            'code':         code,
            'redirect_uri': redirect_uri,
            'client_id':    client_id,
            'client_secret': client_secret,
        },
        timeout=30,
    )
    log.info('exchange_code status=%s body=%s', resp.status_code, resp.text[:400])
    resp.raise_for_status()
    return resp.json()


def refresh_access_token(client_id, client_secret, refresh_token):
    """Обновить access_token через refresh_token."""
    import requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    import base64
    auth = 'Basic ' + base64.b64encode(f'{client_id}:{client_secret}'.encode()).decode()
    resp = requests.post(
        TOKEN_URL,
        cert=_mtls(),
        verify=False,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': auth,
        },
        data={
            'grant_type':    'refresh_token',
            'refresh_token': refresh_token,
            'client_id':     client_id,
            'client_secret': client_secret,
        },
        timeout=30,
    )
    log.info('refresh_token status=%s body=%s', resp.status_code, resp.text[:400])
    resp.raise_for_status()
    return resp.json()


def get_npa_token(client_id, scope=None):
    """Получить NPA access_token через JWT-аутентификацию (без участия пользователя)."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    jwt_token = _make_jwt(client_id)
    scope = scope or SCOPES

    resp = requests.post(
        NPA_URL,
        cert=_mtls(),
        verify=False,
        headers={
            'Content-Type': 'application/x-www-form-urlencoded',
            'Authorization': f'Bearer {jwt_token}',
            'rqUID': str(uuid.uuid4()),
        },
        data={'scope': scope},
        timeout=30,
    )
    log.info('npa_token status=%s body=%s', resp.status_code, resp.text[:400])

    if resp.status_code == 500:
        err = resp.json().get('message', resp.text)
        raise RuntimeError(f'Сбербанк: NPA-доступ не активирован. Обратитесь в поддержку СберAPI. ({err[:200]})')

    resp.raise_for_status()
    data = resp.json()
    return data.get('access_token') or data.get('token', '')


def refresh_access_token(client_id, client_secret=None, refresh_token=None):
    """Обновить или получить новый NPA access_token."""
    # NPA не использует refresh_token — получаем новый через JWT каждый раз
    return {'access_token': get_npa_token(client_id), 'expires_in': 300}


def get_statement(access_token, client_id, account_number, date_from, date_to):
    """Запрос выписки по счёту."""
    import requests
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    resp = requests.get(
        STMT_URL,
        cert=_mtls(),
        verify=False,
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
