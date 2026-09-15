"""Read-only network check: no real bot credentials, database or messages involved."""
import json
import sys

import requests

from senler_api import DeliveryError, telegram_request_options


def probe(label, options, trust_env=True):
    try:
        with requests.Session() as session:
            session.trust_env = trust_env
            response = session.post('https://api.telegram.org/bot0:senler-network-check/getMe',
                                    json={}, timeout=(5, 15), allow_redirects=False, **options)
            data = response.json()
            reachable = (response.status_code in (401, 404) and isinstance(data, dict)
                         and data.get('ok') is False and data.get('error_code') == response.status_code)
            result = {'route': label, 'reachable': reachable, 'http_status': response.status_code}
    except (requests.RequestException, ValueError) as exc:
        reachable = False
        # No URLs, exception strings, request headers or environment values in logs.
        result = {'route': label, 'reachable': False, 'error_type': type(exc).__name__}
    print(json.dumps(result), flush=True)
    return reachable


def main():
    probe('direct', {}, trust_env=False)
    try:
        options = telegram_request_options()
    except DeliveryError:
        print(json.dumps({'route': 'configured', 'reachable': False, 'error_type': 'InvalidProxyConfig'}))
        return 1
    return 0 if probe('configured_proxy' if options else 'configured_direct', options) else 1


if __name__ == '__main__':
    sys.exit(main())
