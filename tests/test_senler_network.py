"""Telegram proxy regression checks. All external requests are mocked."""
import io
import json
import os
import socket
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'crm'))
from senler_api import BotAPI, DeliveryError, telegram_request_options, network_error_code
import check_telegram_network


class TelegramNetworkTests(unittest.TestCase):
    def test_network_codes_classify_nested_errors_without_exception_text(self):
        from urllib3.exceptions import MaxRetryError, ProtocolError
        dns = requests.ConnectionError(MaxRetryError(None, 'private-token', socket.gaierror(-2, 'private-password')))
        reset = requests.ConnectionError(ProtocolError('private-token', ConnectionResetError(104, 'private-password')))
        socks = OSError('private-token')
        socks.socket_err = ConnectionRefusedError(111, 'private-password')
        cases = [(dns, 'DNS'), (reset, 'CONNECTION_RESET'),
                 (requests.ConnectionError(socks), 'CONNECTION_REFUSED'),
                 (requests.ReadTimeout('private-token'), 'READ_TIMEOUT'),
                 (requests.exceptions.InvalidSchema('private-password'), 'TRANSPORT_SETUP'),
                 (requests.ConnectionError('private-token'), 'CONNECTION')]
        for error, code in cases:
            with self.subTest(code=code):
                self.assertEqual(network_error_code(error), code)
        # Exception graphs may contain cycles; diagnostics must remain bounded.
        socks.__cause__ = socks
        self.assertEqual(network_error_code(socks), 'CONNECTION_REFUSED')

    def test_connection_error_identifies_operation_and_actual_route(self):
        api = BotAPI({'kind': 'telegram'}, 'private-token')
        for proxy, route in [('', 'DIRECT'), ('socks5h://user:private-password@proxy:1080', 'PROXY')]:
            for method, operation in [('getMe', 'TOKEN'), ('setWebhook', 'WEBHOOK')]:
                with self.subTest(route=route, operation=operation), \
                        patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': proxy}), \
                        patch('senler_api.requests.request', side_effect=requests.ReadTimeout('private-token private-password')) as request:
                    with self.assertRaises(DeliveryError) as error:
                        api.telegram(method)
                    self.assertIn('TG-{}-{}-READ_TIMEOUT'.format(operation, route), str(error.exception))
                    self.assertNotIn('private-token', str(error.exception))
                    self.assertNotIn('private-password', str(error.exception))
                    self.assertEqual(error.exception.retry_after, 30)
                    request.assert_called_once()

    def test_supported_proxy_routes_and_explicit_direct_mode(self):
        for value, expected in [
            ('socks5://xray:1080', 'socks5h://xray:1080'),
            ('socks5h://user:private-password@proxy.example:1080',
             'socks5h://user:private-password@proxy.example:1080'),
            ('http://proxy.example:8080', 'http://proxy.example:8080'),
            ('https://proxy.example:8443', 'https://proxy.example:8443'),
        ]:
            with self.subTest(scheme=value.split(':')[0]), patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': value}):
                self.assertEqual(telegram_request_options(), {'proxies': {'http': expected, 'https': expected}})
        # An unrelated integration's proxy must not silently enable this one.
        with patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': '', 'PROXY_URL': 'socks5://other:1080'}):
            response = Mock(ok=True, status_code=200, headers={})
            response.json.return_value = {'ok': True, 'result': True}
            with patch('senler_api.requests.request', return_value=response) as request:
                BotAPI({'kind': 'telegram'}, 'test-token').telegram('getMe')
                self.assertNotIn('proxies', request.call_args.kwargs)

    def test_invalid_configuration_fails_before_network_and_hides_credentials(self):
        values = ['xray:1080', 'ftp://user:private-password@proxy:21',
                  'socks5h://proxy', 'socks5h://proxy:0', 'socks5h://proxy:65536',
                  'socks5h://proxy:bad', 'socks5h://[invalid:1080',
                  'http://proxy:8080/path', 'http://proxy:8080?password=private-password',
                  'http://proxy:8080#fragment', 'socks5h://bad host:1080']
        with patch('senler_api.requests.request') as request:
            for value in values:
                with self.subTest(value=value), patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': value}):
                    with self.assertRaises(DeliveryError) as error:
                        BotAPI({'kind': 'telegram'}, 'private-token').identity()
                    self.assertNotIn('private-password', str(error.exception))
                    self.assertNotIn('private-token', str(error.exception))
            request.assert_not_called()

    def test_proxy_covers_connect_messages_photos_and_callback_answers(self):
        api = BotAPI({'kind': 'telegram', 'webhook_secret': 'test-secret'}, 'test-token')
        results = [dict(id=7, is_bot=True, username='test_bot', first_name='Test'),
                   True, {'message_id': 12}, {'message_id': 13}, True]
        responses = []
        for result in results:
            response = Mock(ok=True, status_code=200, headers={})
            response.json.return_value = {'ok': True, 'result': result}
            responses.append(response)
        with patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': 'socks5://xray:1080'}), \
                patch('senler_api.requests.request', side_effect=responses) as request:
            self.assertEqual(api.identity()['external_id'], '7')
            api.connect('https://crm.example/webhook')
            self.assertEqual(api.send('42', {'text': 'Test'}, 1), '12')
            self.assertEqual(api.send('42', {'text': 'Photo'}, 2,
                                     asset={'extension': 'png', 'data': b'image', 'mime': 'image/png'}), '13')
            api.acknowledge({'callback_id': 'test-callback'})
        calls = request.call_args_list
        self.assertEqual([call.args[1].rsplit('/', 1)[-1] for call in calls],
                         ['getMe', 'setWebhook', 'sendMessage', 'sendPhoto', 'answerCallbackQuery'])
        for call in calls:
            self.assertEqual(call.kwargs['proxies']['https'], 'socks5h://xray:1080')
            self.assertEqual(call.kwargs['timeout'], (5, 15))
            self.assertIsNot(call.kwargs.get('verify'), False)
        self.assertFalse(calls[1].kwargs['json']['drop_pending_updates'])
        self.assertIn('photo', calls[3].kwargs['files'])

    def test_telegram_proxy_does_not_change_vk_or_max(self):
        response = Mock(ok=True, status_code=200, headers={})
        response.json.return_value = {'response': {}, 'is_bot': True}
        with patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': 'socks5h://xray:1080'}), \
                patch('senler_api.requests.request', return_value=response) as request:
            BotAPI({'kind': 'vk'}, 'test-token').vk('groups.getById')
            BotAPI({'kind': 'max'}, 'test-token').max('GET', '/me')
        for call in request.call_args_list:
            self.assertNotIn('proxies', call.kwargs)
        self.assertTrue(Path(request.call_args.kwargs['verify']).is_file())

    def test_network_errors_hide_secrets_without_retrying_uncertain_sends(self):
        api = BotAPI({'kind': 'telegram'}, 'private-token')
        for exception in (requests.exceptions.ProxyError, requests.ConnectionError, requests.ReadTimeout):
            for sending in (True, False):
                with self.subTest(error=exception.__name__, sending=sending), \
                        patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': 'socks5h://user:private-password@proxy:1080'}), \
                        patch('senler_api.requests.request', side_effect=exception('private-token private-password')) as request:
                    with self.assertRaises(DeliveryError) as error:
                        api.telegram('sendMessage' if sending else 'getMe', sending=sending)
                    self.assertNotIn('private-token', str(error.exception))
                    self.assertNotIn('private-password', str(error.exception))
                    self.assertEqual(error.exception.uncertain, sending)
                    self.assertEqual(error.exception.retry_after, 0 if sending else 30)
                    request.assert_called_once()

    def test_server_diagnostic_uses_fake_token_and_sanitized_output(self):
        response = Mock(status_code=404)
        response.json.return_value = {'ok': False, 'error_code': 404}
        session = Mock()
        session.post.side_effect = [requests.ConnectTimeout('private-token private-password'), response]
        session_context = Mock()
        session_context.__enter__ = Mock(return_value=session)
        session_context.__exit__ = Mock(return_value=False)
        with patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': 'socks5h://user:private-password@proxy:1080'}), \
                patch('check_telegram_network.requests.Session', return_value=session_context), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(check_telegram_network.main(), 0)
        lines = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([line['reachable'] for line in lines], [False, True])
        self.assertEqual(lines[1]['route'], 'configured_proxy')
        self.assertNotIn('private-password', output.getvalue())
        self.assertNotIn('private-token', output.getvalue())
        for call in session.post.call_args_list:
            self.assertEqual(call.args[0], 'https://api.telegram.org/bot0:senler-network-check/getMe')
        self.assertNotIn('proxies', session.post.call_args_list[0].kwargs)
        self.assertIn('proxies', session.post.call_args_list[1].kwargs)


if __name__ == '__main__':
    unittest.main()
