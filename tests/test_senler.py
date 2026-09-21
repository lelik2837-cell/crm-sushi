"""Run: python3 -B -m unittest discover -s tests -v. No production DB or network."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'crm'))
from flask import Flask, session
from senler import register_senler
from senler_core import SenlerService, dumps, init_schema, now, parse_import, validate_definition
from senler_templates import LEGACY_ORDER_BUTTONS, LEGACY_ORDER_TEXT
from senler_api import BotAPI, DeliveryError, parse_event


class FakeAPI:
    sent = []
    failure = None
    conversations = {}
    polled = []
    read_check_failure = None
    vk_photos = {}
    vk_names = {}
    telegram_photos = {}

    def __init__(self, channel, token, requester=None):
        self.channel = dict(channel)

    def identity(self):
        return dict(external_id=self.channel['external_id'] or str(90000 + self.channel['id']), username='test_bot', title='Test', subscribe_url='https://t.me/test_bot')

    def connect(self, url):
        return ''

    def send(self, user_id, body, random_id, asset=None):
        if self.failure:
            raise self.failure
        self.sent.append((self.channel['kind'], user_id, body, random_id))
        return str(random_id)

    def acknowledge(self, event):
        pass

    def vk(self, method, **params):
        if method == 'messages.getConversationsById':
            if self.read_check_failure:
                raise self.read_check_failure
            items = []
            self.polled.append([p for p in params['peer_ids'].split(',') if p])
            for peer_id in [p for p in params['peer_ids'].split(',') if p]:
                if peer_id in self.conversations:
                    items.append({'peer': {'id': int(peer_id)}, 'out_read': self.conversations[peer_id]})
            return {'items': items}
        if method == 'users.get':
            return [dict({'id': int(uid), 'photo_100': self.vk_photos.get(uid, '')}, **self.vk_names.get(uid, {}))
                    for uid in params['user_ids'].split(',') if uid]
        return {}

    def telegram(self, method, payload=None, **kwargs):
        if method == 'getUserProfilePhotos':
            uid = str(payload['user_id'])
            if uid in self.telegram_photos:
                return {'total_count': 1, 'photos': [[{'file_id': 'fid-' + uid}]]}
            return {'total_count': 0, 'photos': []}
        if method == 'getFile':
            return {'file_id': payload['file_id'], 'file_path': 'photos/' + payload['file_id'] + '.jpg'}
        return {}

    def telegram_file(self, file_path):
        return b'FAKE-AVATAR-BYTES', 'image/jpeg'


class SenlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='senler-test-')
        self.path = str(Path(self.tmp.name) / 'crm.db')
        def get_db():
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            return conn
        self.app = Flask(__name__)
        self.app.secret_key = 'isolated-test-key'
        self.app.config['TESTING'] = True
        self.service = register_senler(self.app, get_db, self.path, lambda item: session.get('role') == 'owner')
        self.service.api_factory = FakeAPI
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=1, role='owner', senler_csrf='csrf-test')
        FakeAPI.sent, FakeAPI.failure, FakeAPI.conversations = [], None, {}
        FakeAPI.polled, FakeAPI.read_check_failure = [], None
        FakeAPI.vk_photos, FakeAPI.vk_names, FakeAPI.telegram_photos = {}, {}, {}

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, path, data=None, status=200):
        result = self.client.post('/reports/senler/api/' + path, json=data or {}, headers={'X-CSRF-Token':'csrf-test'})
        self.assertEqual(result.status_code, status, result.get_data(as_text=True))
        return result.get_json()

    def channel(self, kind='telegram'):
        data = self.post('channels', {'kind':kind, 'name':kind, 'token':'test-secret-'+kind, 'external_id':'123456'})
        self.post('channels/{}/connect'.format(data['id']))
        return data['id']

    def sub(self, channel_id, external_id='123', status='active', name='Алексей Тест'):
        with self.service.db() as conn:
            return conn.execute('''INSERT INTO senler_subscribers(channel_id,external_user_id,status,name,created_at,consent_at)
                VALUES (?,?,?,?,?,?)''', (channel_id, external_id, status, name, now(), now())).lastrowid

    def sent_message(self, channel_id, sub_id, external_id, age=0):
        stamp = now() - age
        with self.service.db() as conn:
            conn.execute('''INSERT INTO senler_outbox(channel_id,subscriber_id,kind,body_json,dedupe_key,status,due_at,created_at,sent_at,external_id)
                VALUES (?,?,'campaign','{}',?,'sent',?,?,?,?)''', (channel_id, sub_id, 'test:{}:{}'.format(sub_id, external_id), stamp, stamp, stamp, str(external_id)))

    def read_peers(self):
        with self.service.db() as conn:
            return {r['external_user_id'] for r in conn.execute(
                'SELECT s.external_user_id FROM senler_outbox o JOIN senler_subscribers s ON s.id=o.subscriber_id WHERE o.read_at IS NOT NULL')}

    def one(self, table, where='1=1', params=()):
        with self.service.db() as conn:
            result = conn.execute('SELECT * FROM '+table+' WHERE '+where, params).fetchone()
            return dict(result) if result else None

    def tick(self):
        with patch('senler_core.time.sleep'):
            self.service.tick(seconds=0.2)

    def webhook(self, channel_id, update, secret=True):
        channel = self.one('senler_channels', 'id=?', (channel_id,))
        headers = {'X-Telegram-Bot-Api-Secret-Token': channel['webhook_secret']} if secret else {}
        return self.client.post('/api/senler/webhook/telegram/'+str(channel_id), json=update, headers=headers)

    def message(self, user_id, text, update_id):
        return {'update_id':update_id, 'message':{'message_id':update_id, 'from':{'id':int(user_id),'first_name':'Алексей'}, 'chat':{'id':int(user_id),'type':'private'}, 'text':text}}

    def callback(self, user_id, value, update_id):
        return {'update_id':update_id, 'callback_query':{'id':str(update_id),'from':{'id':int(user_id),'first_name':'Алексей'},'message':{'chat':{'id':int(user_id),'type':'private'}},'data':value}}

    def bot(self, channel, nodes=None, trigger='subscribe'):
        nodes = nodes or [{'id':'start','type':'message','text':'Здравствуйте, {имя}!','buttons':[],'next':''}]
        data = dict(name='Бот',channel_id=channel,trigger_type=trigger,keywords='меню',priority=10,definition={'entry':nodes[0]['id'],'nodes':nodes})
        saved=self.post('bots',data)
        self.post('bots/{}/publish'.format(saved['id']))
        return saved['id'],data

    def campaign(self, channels, groups=None):
        return self.post('campaigns', {'name':'Выходные','body':{'text':'Привет, {имя}!','buttons':[]},'audience':{'channels':channels,'groups':groups or []}})['id']

    def test_delivery_menu_is_seeded_as_editable_draft_for_each_channel(self):
        for kind in ('telegram', 'vk', 'max'):
            channel = self.channel(kind)
            items = self.client.get('/reports/senler/api/bots?channel=' + str(channel)).get_json()['items']
            self.assertEqual(len(items), 1)
            bot = items[0]
            self.assertEqual((bot['name'], bot['template_key'], bot['status'], bot['version']),
                             ('Меню доставки', 'delivery_menu', 'draft', 0))
            nodes = {n['id']: n for n in bot['definition']['nodes']}
            self.assertEqual([b['value'] for b in nodes['menu']['buttons']],
                             ['order', 'promotions', 'bonuses', 'operator', 'contacts'])
            self.assertTrue(all(n['type'] == 'message' for n in nodes.values()))
            self.assertEqual([(b['label'], b['action'], b['value']) for b in nodes['order']['buttons']], [
                ('📱 App Store', 'url', 'https://apps.apple.com/ru/app/id1510725657'),
                ('📱 Google Play', 'url', 'https://play.google.com/store/apps/details?id=ru.dvfx.papasushi'),
                ('🌐 Сайт', 'url', 'https://papasushi.ru/novokuznetsk'),
                ('🏠 Главное меню', 'goto', 'menu'),
            ])
            self.assertIn('https://t.me/papa_sushi', nodes['operator']['text'])
            self.assertIn('https://papasushi.ru/novokuznetsk/bonus_card', nodes['bonuses']['text'])
            self.assertEqual([b['value'] for b in nodes['promotions']['buttons']], ['birthday', 'sale', 'menu'])
            self.assertNotIn('отзыв', dumps(bot['definition']).casefold())
            with self.service.db() as conn:
                validate_definition(bot['definition'], conn, 1)
        self.assertFalse(FakeAPI.sent)
        self.assertIsNone(self.one('senler_outbox'))

    def test_menu_migration_preserves_user_edits_and_published_versions(self):
        channel = self.channel()
        custom_id, _ = self.bot(channel)
        custom_before = self.one('senler_bots', 'id=?', (custom_id,))
        preset = self.one('senler_bots', 'channel_id=? AND template_key=?', (channel, 'delivery_menu'))
        # Simulate upgrading a channel that predates the starter menu.
        with self.service.db() as conn:
            conn.execute('DELETE FROM senler_bots WHERE id=?', (preset['id'],))
        with self.service.db() as conn:
            init_schema(conn)
        self.assertEqual(self.one('senler_bots', 'id=?', (custom_id,)), custom_before)
        bot = next(b for b in self.client.get('/reports/senler/api/bots').get_json()['items']
                   if b['template_key'] == 'delivery_menu')
        bot['name'] = 'Наше меню'
        bot['definition']['nodes'][0]['text'] = 'Наше приветствие'
        self.post('bots', bot)
        self.post('bots/{}/publish'.format(bot['id']))
        bot['definition']['nodes'][0]['text'] = 'Новая версия только в черновике'
        self.post('bots', bot)
        self.post('bots/{}/pause'.format(bot['id']))
        before = self.one('senler_bots', 'id=?', (bot['id'],))
        for _ in range(2):
            with self.service.db() as conn:
                init_schema(conn)
        self.assertEqual(self.one('senler_bots', 'id=?', (bot['id'],)), before)
        with self.service.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM senler_bots WHERE channel_id=?', (channel,)).fetchone()[0], 2)

    def test_existing_menu_gets_order_links_without_losing_custom_content(self):
        channels = [self.channel(kind) for kind in ('telegram', 'vk', 'max')]
        rows = [self.one('senler_bots', 'channel_id=? AND template_key=?', (channel, 'delivery_menu'))
                for channel in channels]
        for index, row in enumerate(rows):
            definition = json.loads(row['draft_json'])
            order = next(node for node in definition['nodes'] if node['id'] == 'order')
            order['buttons'] = [dict(button) for button in LEGACY_ORDER_BUTTONS]
            order['text'] = LEGACY_ORDER_TEXT if index == 0 else 'Мой текст заказа'
            definition['nodes'][0]['text'] = 'Моё главное меню'
            if index == 2:
                order['buttons'].insert(0, {'label': 'Своя ссылка', 'action': 'url', 'value': 'https://example.com'})
            with self.service.db() as conn:
                conn.execute('''UPDATE senler_bots SET draft_json=?,published_json=?,status='active',version=1
                    WHERE id=?''', (dumps(definition), dumps(definition), row['id']))
        before_custom = self.one('senler_bots', 'id=?', (rows[2]['id'],))

        for _ in range(2):
            with self.service.db() as conn:
                init_schema(conn)

        for index, row in enumerate(rows[:2]):
            updated = self.one('senler_bots', 'id=?', (row['id'],))
            self.assertEqual(updated['version'], 2)
            for column in ('draft_json', 'published_json'):
                definition = json.loads(updated[column])
                self.assertEqual(definition['nodes'][0]['text'], 'Моё главное меню')
                order = next(node for node in definition['nodes'] if node['id'] == 'order')
                self.assertEqual([b['label'] for b in order['buttons'][:3]],
                                 ['📱 App Store', '📱 Google Play', '🌐 Сайт'])
                self.assertEqual(order['text'], '🍣 Выберите, где удобнее сделать заказ:'
                                 if index == 0 else 'Мой текст заказа')
        self.assertEqual(self.one('senler_bots', 'id=?', (rows[2]['id'],)), before_custom)

    def test_menu_can_navigate_all_branches_and_return_without_handoff(self):
        channel = self.channel()
        sub_id = self.sub(channel)
        bot = self.one('senler_bots', 'channel_id=? AND template_key=?', (channel, 'delivery_menu'))
        self.post('bots/{}/publish'.format(bot['id']))
        self.webhook(channel, self.message(123, '/start', 1))
        self.tick()
        run = self.one('senler_runs', 'subscriber_id=?', (sub_id,))
        self.assertEqual((run['node_id'], run['status']), ('menu', 'waiting_reply'))
        update_id = 1
        for target in ('order', 'menu', 'bonuses', 'menu', 'operator', 'menu', 'contacts', 'menu',
                       'promotions', 'birthday', 'promotions', 'sale', 'order', 'menu'):
            current = FakeAPI.sent[-1][2]
            callback = next(b['value'] for b in current['buttons']
                            if b['value'].startswith('sb:') and b['value'].split(':')[3] == target)
            update_id += 1
            self.webhook(channel, self.callback(123, callback, update_id))
            self.tick()
            run = self.one('senler_runs', 'id=?', (run['id'],))
            self.assertEqual((run['node_id'], run['status']), (target, 'waiting_reply'))
            self.assertEqual(self.one('senler_subscribers', 'id=?', (sub_id,))['bot_paused'], 0)
            self.assertNotIn('[[', FakeAPI.sent[-1][2]['text'])
            if target == 'order':
                self.assertEqual([b['action'] for b in FakeAPI.sent[-1][2]['buttons'][:3]],
                                 ['url', 'url', 'url'])
        # The default remains scoped to explicit start/subscription, not every incoming message.
        sent = len(FakeAPI.sent)
        self.webhook(channel, self.message(123, 'Вопрос о доставке', update_id + 1))
        self.tick()
        self.assertEqual(len(FakeAPI.sent), sent)

    def test_access_and_csrf(self):
        self.assertEqual(self.client.post('/reports/senler/api/groups', json={'name':'x'}).status_code,403)
        with self.client.session_transaction() as s:
            s.clear()
        self.assertEqual(self.client.get('/reports/senler/api/bootstrap').status_code,401)
        with self.client.session_transaction() as s:
            s.update(user_id=2,role='admin')
        self.assertEqual(self.client.get('/reports/senler/api/bootstrap').status_code,403)

    def test_credentials_encrypted_and_hidden(self):
        channel=self.channel()
        stored=self.one('senler_channels','id=?',(channel,))
        self.assertNotIn('test-secret',stored['token'])
        self.assertEqual(self.service.cipher().decrypt(stored['token'].encode()),b'test-secret-telegram')
        payload=self.client.get('/reports/senler/api/bootstrap').get_data(as_text=True)
        self.assertNotIn(stored['token'],payload)
        self.assertNotIn(stored['webhook_secret'],payload)
        self.assertEqual(Path(self.path+'.senler-key').stat().st_mode & 0o777,0o600)

    def test_overview_subscriber_statistics_all_channels_and_filter(self):
        vk=self.channel('vk');telegram=self.channel('telegram')
        active_vk=self.sub(vk,'101','active')
        pending_vk=self.sub(vk,'102','pending')
        unsubscribed_vk=self.sub(vk,'103','unsubscribed')
        active_telegram=self.sub(telegram,'201','active')
        blocked_telegram=self.sub(telegram,'202','blocked')
        with self.service.db() as conn:
            conn.execute('UPDATE senler_subscribers SET consent_at=NULL WHERE id IN (?,?)',(pending_vk,blocked_telegram))
            conn.execute('UPDATE senler_subscribers SET consent_at=? WHERE id=?',(now()-10*86400,unsubscribed_vk))
            conn.execute('UPDATE senler_subscribers SET consent_at=? WHERE id=?',(now()-40*86400,active_telegram))

        overview=self.client.get('/reports/senler/api/bootstrap').get_json()
        self.assertEqual(overview['subscriber_stats'],{
            'active':2,'blocked':1,'new_30':2,'pending':1,'total':5,'unsubscribed':1})
        self.assertEqual(sum(day['n'] for day in overview['subscriber_activity']),2)
        by_channel={item['id']:item for item in overview['subscriber_channels']}
        self.assertEqual((by_channel[vk]['active'],by_channel[vk]['total'],by_channel[vk]['new_30']),(1,3,2))
        self.assertEqual((by_channel[telegram]['active'],by_channel[telegram]['total'],by_channel[telegram]['new_30']),(1,2,0))

        selected=self.client.get('/reports/senler/api/bootstrap?channel='+str(vk)).get_json()
        self.assertEqual(selected['subscriber_stats']['total'],3)
        self.assertEqual(selected['subscriber_stats']['active'],1)
        self.assertEqual(selected['subscriber_stats']['new_30'],2)
        self.assertEqual([item['id'] for item in selected['subscriber_channels']],[vk])
        self.assertEqual(sum(day['n'] for day in selected['subscriber_activity']),2)

        with self.client.session_transaction() as session:
            session['user_id']=2
        private=self.client.get('/reports/senler/api/bootstrap?channel='+str(vk)).get_json()
        self.assertEqual(private['subscriber_stats']['total'],0)
        self.assertEqual(private['subscriber_channels'],[])
        self.assertEqual(private['subscriber_activity'],[])

    def test_subscription_button_settings_validation_and_owner_scope(self):
        channel=self.channel('vk');before=self.one('senler_channels')
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','unsubscribe_label':'Не получать акции','unsubscribe_enabled':False})
        saved=self.one('senler_channels')
        self.assertEqual(saved['token'],before['token']);self.assertEqual(saved['status'],'connected')
        self.assertEqual(saved['unsubscribe_label'],'Не получать акции');self.assertEqual(saved['unsubscribe_enabled'],0)
        visible=self.client.get('/reports/senler/api/bootstrap').get_json()['channels'][0]
        self.assertEqual(visible['unsubscribe_label'],'Не получать акции');self.assertEqual(visible['unsubscribe_enabled'],0)
        for fields in [{'unsubscribe_label':''},{'unsubscribe_label':'x'*41},{'unsubscribe_enabled':'false'}]:
            self.post('channels/{}/edit'.format(channel),dict(name='ВК',**fields),400)
        self.post('channels/{}/edit'.format(channel),{'name':'Новое название'})
        self.assertEqual(self.one('senler_channels')['unsubscribe_enabled'],0)
        with self.client.session_transaction() as s:s['user_id']=2
        self.post('channels/{}/edit'.format(channel),{'name':'Чужой канал','unsubscribe_label':'Другой текст','unsubscribe_enabled':True},400)
        self.assertEqual(self.one('senler_channels')['unsubscribe_label'],'Не получать акции')

    def test_channel_message_settings_defaults_differ_by_kind_and_are_configurable(self):
        vk=self.channel('vk');tg=self.channel('telegram');mx=self.channel('max')
        self.assertEqual(self.one('senler_channels','id=?',(vk,))['greeting_trigger'],'off')
        self.assertEqual(self.one('senler_channels','id=?',(tg,))['greeting_trigger'],'on_message')
        self.assertEqual(self.one('senler_channels','id=?',(mx,))['greeting_trigger'],'on_message')
        self.assertIn('Здесь можно получать',self.one('senler_channels','id=?',(vk,))['greeting_text'])
        self.assertIn('Жаль, что вы отписались',self.one('senler_channels','id=?',(vk,))['stop_text'])
        for fields,status in [({'greeting_text':''},400),({'greeting_text':'x'*3501},400),
                              ({'greeting_trigger':'always'},400),({'stop_text':''},400),({'stop_text':'x'*3501},400)]:
            self.post('channels/{}/edit'.format(tg),dict(name='Telegram',**fields),status)
        self.post('channels/{}/edit'.format(tg),{'name':'Telegram','greeting_text':'Особые условия для новых гостей!','greeting_trigger':'off','stop_text':'Очень жаль, возвращайтесь.'})
        saved=self.one('senler_channels','id=?',(tg,))
        self.assertEqual(saved['greeting_text'],'Особые условия для новых гостей!')
        self.assertEqual(saved['greeting_trigger'],'off')
        self.assertEqual(saved['stop_text'],'Очень жаль, возвращайтесь.')
        self.post('channels/{}/edit'.format(tg),{'name':'Telegram'})
        self.assertEqual(self.one('senler_channels','id=?',(tg,))['greeting_text'],'Особые условия для новых гостей!')

    def test_existing_channels_get_message_defaults_by_kind_on_upgrade(self):
        vk=self.channel('vk');tg=self.channel('telegram')
        with self.service.db() as conn:
            for column in ('greeting_text','greeting_trigger','stop_text'):
                conn.execute('ALTER TABLE senler_channels DROP COLUMN '+column)
            init_schema(conn)
        self.assertEqual(self.one('senler_channels','id=?',(vk,))['greeting_trigger'],'off')
        self.assertEqual(self.one('senler_channels','id=?',(tg,))['greeting_trigger'],'on_message')

    def test_vk_silent_by_default_others_greet_and_greeting_text_is_configurable(self):
        vk=self.channel('vk');tg=self.channel('telegram')
        vk_secret=self.one('senler_channels','id=?',(vk,))['webhook_secret']
        self.client.post('/api/senler/webhook/vk/'+str(vk),json={'type':'message_new','event_id':'1','group_id':123456,'secret':vk_secret,
            'object':{'message':{'from_id':555,'peer_id':555,'text':'Сколько стоит доставка?'}}})
        self.tick()
        self.assertFalse(FakeAPI.sent)
        self.assertEqual(self.one('senler_subscribers',"channel_id=? AND external_user_id='555'",(vk,))['status'],'pending')
        self.webhook(tg,self.message(777,'Привет','1'));self.tick()
        greeting=next(sent for sent in FakeAPI.sent if sent[1]=='777')
        self.assertIn('Здесь можно получать',greeting[2]['text'])
        self.assertEqual([b['value'] for b in greeting[2]['buttons']],['subscribe'])
        self.post('channels/{}/edit'.format(tg),{'name':'Telegram','greeting_text':'Особое предложение! Подписаться?'})
        self.webhook(tg,self.message(888,'Привет','2'));self.tick()
        custom=next(sent for sent in FakeAPI.sent if sent[1]=='888')
        self.assertEqual(custom[2]['text'],'Особое предложение! Подписаться?')
        self.post('channels/{}/edit'.format(vk),{'name':'ВК','greeting_trigger':'on_message'})
        self.client.post('/api/senler/webhook/vk/'+str(vk),json={'type':'message_new','event_id':'2','group_id':123456,'secret':vk_secret,
            'object':{'message':{'from_id':666,'peer_id':666,'text':'Ещё вопрос'}}})
        self.tick()
        self.assertTrue(any(sent[1]=='666' for sent in FakeAPI.sent))

    def test_stop_sends_configurable_farewell_once_but_blocked_sends_nothing(self):
        channel=self.channel();self.sub(channel,'123','active')
        self.webhook(channel,self.message(123,'Стоп','1'));self.tick()
        self.assertEqual(self.one('senler_subscribers')['status'],'unsubscribed')
        farewell=[sent for sent in FakeAPI.sent if sent[1]=='123']
        self.assertEqual(len(farewell),1)
        self.assertIn('Жаль, что вы отписались',farewell[0][2]['text'])
        self.assertEqual([b['value'] for b in farewell[0][2]['buttons']],['subscribe'])
        self.webhook(channel,self.message(123,'Стоп','2'));self.tick()
        self.assertEqual(len([sent for sent in FakeAPI.sent if sent[1]=='123']),1)
        self.post('channels/{}/edit'.format(channel),{'name':'Бот','stop_text':'Очень жаль! Возвращайтесь.'})
        self.sub(channel,'321','active')
        self.webhook(channel,self.message(321,'Стоп','3'));self.tick()
        custom=next(sent for sent in FakeAPI.sent if sent[1]=='321')
        self.assertEqual(custom[2]['text'],'Очень жаль! Возвращайтесь.')
        vk=self.channel('vk');self.sub(vk,'999','active')
        vk_secret=self.one('senler_channels','id=?',(vk,))['webhook_secret']
        self.client.post('/api/senler/webhook/vk/'+str(vk),json={'type':'message_deny','event_id':'1','group_id':123456,'secret':vk_secret,'object':{'user_id':999}})
        self.tick()
        self.assertEqual(self.one('senler_subscribers','channel_id=? AND external_user_id=?',(vk,'999'))['status'],'blocked')
        self.assertFalse(any(sent[1]=='999' for sent in FakeAPI.sent))

    def test_campaign_buttons_follow_each_channel_and_keep_launch_snapshot(self):
        vk=self.channel('vk');tg=self.channel();self.sub(vk);self.sub(tg)
        self.post('channels/{}/edit'.format(vk),{'name':'ВК','unsubscribe_label':'Отказаться от акций','unsubscribe_enabled':True,
                                                  'default_button_enabled':True,'default_button_label':'Меню бота','default_button_url':'https://example.com/menu-vk'})
        self.post('channels/{}/edit'.format(tg),{'name':'Telegram','unsubscribe_label':'Без акций','unsubscribe_enabled':False})
        campaign=self.post('campaigns',{'name':'Акция','body':{'text':'Сегодня акция','buttons':[{'label':'Меню','action':'url','value':'https://example.com/menu'}]},'audience':{'channels':[vk,tg]}})['id']
        self.post('campaigns/{}/launch'.format(campaign));self.tick()
        self.post('channels/{}/edit'.format(vk),{'name':'ВК','unsubscribe_label':'Новый текст','unsubscribe_enabled':False,
                                                  'default_button_enabled':False,'default_button_label':'','default_button_url':''})
        detail=self.client.get('/reports/senler/api/campaigns/'+str(campaign)).get_json()
        self.assertEqual(detail['subscription_buttons'][str(vk)],{'unsubscribe_enabled':1,'unsubscribe_label':'Отказаться от акций',
                                                                    'default_button_enabled':1,'default_button_label':'Меню бота'})
        self.assertEqual(detail['subscription_buttons'][str(tg)]['unsubscribe_enabled'],0)
        self.assertEqual(detail['subscription_buttons'][str(tg)]['default_button_enabled'],0)
        # the queued message really carries the owner's own button, then the resolved default one, then unsubscribe
        vk_sent=next(sent for sent in FakeAPI.sent if sent[0]=='vk')
        self.assertEqual([b['value'] for b in vk_sent[2]['buttons']],
                          ['https://example.com/menu','https://example.com/menu-vk','unsubscribe'])
        tg_sent=next(sent for sent in FakeAPI.sent if sent[0]=='telegram')
        self.assertEqual([b['value'] for b in tg_sent[2]['buttons']],['https://example.com/menu'])
        self.assertEqual([b['label'] for b in vk_sent[2]['buttons']],['Меню','Меню бота','Отказаться от акций'])

    def test_campaign_list_carries_image_and_the_buttons_as_sent_for_the_full_view(self):
        vk=self.channel('vk');self.sub(vk)
        self.post('channels/{}/edit'.format(vk),{'name':'ВК','unsubscribe_label':'Отказаться от акций','unsubscribe_enabled':True,
                                                  'default_button_enabled':True,'default_button_label':'Меню бота','default_button_url':'https://example.com/menu-vk'})
        image=self.client.post('/reports/senler/api/assets', data={'file':(io.BytesIO(b'\x89PNG\r\n\x1a\nphoto'),'x.png')},headers={'X-CSRF-Token':'csrf-test'}).get_json()['id']
        sent=self.post('campaigns',{'name':'С фото','body':{'text':'Сегодня акция','asset_id':image,'buttons':[{'label':'Заказать','action':'url','value':'https://example.com/order'}]},'audience':{'channels':[vk]}})['id']
        draft=self.post('campaigns',{'name':'Черновик','body':{'text':'Пока не отправлена','buttons':[]},'audience':{'channels':[vk]}})['id']
        self.post('campaigns/{}/launch'.format(sent))  # queuing already freezes each message's buttons; no delivery needed
        # The channel is edited after launch: the list must keep showing what was really sent.
        self.post('channels/{}/edit'.format(vk),{'name':'ВК','unsubscribe_label':'Новый текст','unsubscribe_enabled':False,
                                                  'default_button_enabled':False,'default_button_label':'','default_button_url':''})
        items={i['id']:i for i in self.client.get('/reports/senler/api/campaigns').get_json()['items']}
        self.assertEqual(items[sent]['body']['asset_id'],image)
        self.assertEqual(items[sent]['body']['buttons'][0]['label'],'Заказать')
        self.assertEqual(items[sent]['subscription_buttons'],{str(vk):{'unsubscribe_enabled':1,'unsubscribe_label':'Отказаться от акций',
                                                                         'default_button_enabled':1,'default_button_label':'Меню бота'}})
        # Same answer as the campaign's own details page, which is what the preview there is drawn from.
        detail=self.client.get('/reports/senler/api/campaigns/'+str(sent)).get_json()
        self.assertEqual(items[sent]['subscription_buttons'],detail['subscription_buttons'])
        # Nothing has been sent for a draft yet, so there is no frozen copy: the page falls back to the channel's current settings.
        self.assertEqual((items[draft]['subscription_buttons'],items[draft]['body']['asset_id']),({},None))

    def test_campaign_list_breaks_results_down_per_channel_only_for_multichannel_campaigns(self):
        vk=self.channel('vk');tg=self.channel('telegram')
        self.sub(vk,'1');self.sub(vk,'2');self.sub(tg,'3')
        both=self.campaign([vk,tg]);only_vk=self.campaign([vk])
        for campaign in (both,only_vk):
            self.post('campaigns/{}/launch'.format(campaign))
        self.tick();self.tick()
        # One of the VK messages of the two-channel mailing fails after all.
        with self.service.db() as conn:
            conn.execute("UPDATE senler_outbox SET status='error' WHERE id=(SELECT MIN(o.id) FROM senler_outbox o WHERE o.campaign_id=? AND o.channel_id=?)",(both,vk))
        items={i['id']:i for i in self.client.get('/reports/senler/api/campaigns').get_json()['items']}
        self.assertEqual(items[both]['channel_counts'],{str(vk):{'sent':1,'error':1},str(tg):{'sent':1}})
        self.assertEqual(items[both]['counts'],{'sent':2,'error':1})
        # With a single channel the per-channel figures would just repeat `counts`, so they are not computed.
        self.assertNotIn('channel_counts',items[only_vk])

    def test_group_delete_removes_members_but_is_blocked_while_a_bot_uses_it(self):
        channel=self.channel();s1=self.sub(channel,'1');s2=self.sub(channel,'2')
        group=self.post('groups',{'name':'Розыгрыш'})['id']
        self.post('subscribers/bulk',{'ids':[s1,s2],'action':'add_group','group_id':group})
        self.assertEqual(len(self.client.get('/reports/senler/api/subscribers?group={}'.format(group)).get_json()['items']),2)
        nodes=[{'id':'add','type':'group','group_id':group,'mode':'add','next':'msg'},
               {'id':'msg','type':'message','text':'Вы участвуете!','buttons':[]}]
        bot_id,_=self.bot(channel,nodes,trigger='keyword')
        self.post('groups/{}/delete'.format(group),{},400)
        self.assertIsNotNone(self.one('senler_groups','id=?',(group,)))
        plain=[{'id':'msg','type':'message','text':'Привет','buttons':[]}]
        self.post('bots',{'id':bot_id,'name':'Бот','channel_id':channel,'trigger_type':'keyword','keywords':'слово','priority':10,'definition':{'entry':'msg','nodes':plain}})
        self.post('bots/{}/publish'.format(bot_id))
        group2=self.post('groups',{'name':'Розыгрыш 2'})['id']
        with self.client.session_transaction() as s:s['user_id']=2
        self.post('groups/{}/delete'.format(group2),{},400)
        with self.client.session_transaction() as s:s['user_id']=1
        self.assertIsNotNone(self.one('senler_groups','id=?',(group2,)))
        self.post('groups/{}/delete'.format(group),{})
        self.assertIsNone(self.one('senler_groups','id=?',(group,)))
        self.assertIsNone(self.one('senler_group_members','group_id=?',(group,)))

    def test_hidden_button_keeps_bot_navigation_and_stop_command(self):
        channel=self.channel()
        self.post('channels/{}/edit'.format(channel),{'name':'Бот','unsubscribe_enabled':False})
        nodes=[{'id':'menu','type':'message','text':'Выберите','buttons':[{'label':'Далее','action':'goto','value':'finish'}]},
               {'id':'finish','type':'message','text':'Готово','buttons':[]}]
        self.bot(channel,nodes)
        self.webhook(channel,self.message(123,'Подписаться',1));self.tick()
        self.assertTrue(any(b['value'].startswith('sb:') for sent in FakeAPI.sent for b in sent[2]['buttons']))
        self.assertFalse(any(b['value']=='unsubscribe' for sent in FakeAPI.sent for b in sent[2]['buttons']))
        campaign=self.campaign([channel]);self.post('campaigns/{}/launch'.format(campaign))
        self.webhook(channel,self.message(123,'Стоп',2));self.tick()
        self.assertEqual(self.one('senler_subscribers')['status'],'unsubscribed')
        self.assertEqual(self.one('senler_outbox','campaign_id=?',(campaign,))['status'],'cancelled')

    def test_existing_channels_get_safe_button_defaults_on_upgrade(self):
        channel=self.channel('vk');before=self.one('senler_channels')
        with self.service.db() as conn:
            conn.execute('ALTER TABLE senler_channels DROP COLUMN unsubscribe_label')
            conn.execute('ALTER TABLE senler_channels DROP COLUMN unsubscribe_enabled')
            init_schema(conn)
        saved=self.one('senler_channels')
        self.assertEqual(saved['unsubscribe_label'],'Отписаться');self.assertEqual(saved['unsubscribe_enabled'],1)
        self.assertEqual(saved['token'],before['token']);self.assertEqual(saved['owner_id'],before['owner_id'])
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','unsubscribe_label':'Не получать','unsubscribe_enabled':False})
        with self.service.db() as conn:init_schema(conn)
        self.assertEqual(self.one('senler_channels')['unsubscribe_label'],'Не получать')
        self.assertEqual(self.one('senler_channels')['unsubscribe_enabled'],0)

    def test_existing_channels_get_default_button_disabled_on_upgrade(self):
        channel=self.channel('vk')
        with self.service.db() as conn:
            conn.execute('ALTER TABLE senler_channels DROP COLUMN default_button_enabled')
            conn.execute('ALTER TABLE senler_channels DROP COLUMN default_button_label')
            conn.execute('ALTER TABLE senler_channels DROP COLUMN default_button_url')
            conn.execute('ALTER TABLE senler_channels DROP COLUMN default_button_action')
            init_schema(conn)
        saved=self.one('senler_channels')
        self.assertEqual((saved['default_button_enabled'],saved['default_button_label'],saved['default_button_url']),(0,'',''))
        # Buttons that exist before the "show the menu" option was added stay links.
        self.assertEqual(saved['default_button_action'],'url')
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_enabled':True,'default_button_label':'Меню','default_button_url':'https://example.com/menu'})
        self.assertEqual(self.one('senler_channels')['default_button_label'],'Меню')

    def test_default_button_requires_label_and_full_url_only_when_enabled(self):
        channel=self.channel('vk')
        # disabled: an incomplete draft (no url yet) is fine to save
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_enabled':False,'default_button_label':'Меню','default_button_url':''})
        # enabling it without a valid https link is rejected
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_enabled':True,'default_button_label':'Меню','default_button_url':'not-a-url'},400)
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_enabled':True,'default_button_label':'','default_button_url':'https://example.com'},400)
        self.assertEqual(self.one('senler_channels')['default_button_enabled'],0)

    def test_default_button_menu_mode_needs_no_link_and_the_action_is_validated(self):
        channel=self.channel('vk')
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_enabled':True,'default_button_label':'Меню','default_button_action':'menu','default_button_url':''})
        saved=self.one('senler_channels')
        self.assertEqual((saved['default_button_enabled'],saved['default_button_action'],saved['default_button_url']),(1,'menu',''))
        # Link mode still needs a link, and only the two known actions are accepted.
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_action':'url'},400)
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_action':'call-me'},400)
        self.assertEqual(self.one('senler_channels')['default_button_action'],'menu')
        # The link typed while menu mode was on is kept, so switching back does not lose it.
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_action':'menu','default_button_url':'https://example.com/menu'})
        self.post('channels/{}/edit'.format(channel),{'name':'ВК','default_button_action':'url'})
        saved=self.one('senler_channels')
        self.assertEqual((saved['default_button_action'],saved['default_button_url']),('url','https://example.com/menu'))

    def test_default_button_can_show_the_bot_menu_in_the_chat_instead_of_opening_a_link(self):
        channel=self.channel();sub_id=self.sub(channel)
        bot=self.one('senler_bots','channel_id=? AND template_key=?',(channel,'delivery_menu'))
        self.post('bots/{}/publish'.format(bot['id']))
        self.post('channels/{}/edit'.format(channel),{'name':'TG','default_button_enabled':True,'default_button_label':'Меню','default_button_action':'menu','default_button_url':''})
        campaign=self.campaign([channel]);self.post('campaigns/{}/launch'.format(campaign));self.tick()
        # In the mailing the button is an in-chat action (no link), between the owner's buttons and Unsubscribe.
        mailing=FakeAPI.sent[-1][2]
        self.assertEqual([(b['label'],b['action'],b['value']) for b in mailing['buttons']],[('Меню','callback','menu'),('Отписаться','callback','unsubscribe')])
        detail=self.client.get('/reports/senler/api/campaigns/'+str(campaign)).get_json()
        self.assertEqual((detail['subscription_buttons'][str(channel)]['default_button_enabled'],detail['subscription_buttons'][str(channel)]['default_button_label']),(1,'Меню'))
        # Pressing it brings the delivery menu up in the same chat; pressing again restarts it.
        sent=len(FakeAPI.sent)
        self.webhook(channel,self.callback(123,'menu',801));self.tick()
        self.assertEqual(len(FakeAPI.sent),sent+1)
        self.assertIn('Это Папа Суши',FakeAPI.sent[-1][2]['text'])
        self.assertEqual([b['label'] for b in FakeAPI.sent[-1][2]['buttons']][:2],['🍣 Сделать заказ','🔥 Актуальные акции'])
        run=self.one('senler_runs','subscriber_id=?',(sub_id,))
        self.assertEqual((run['node_id'],run['status']),('menu','waiting_reply'))
        self.webhook(channel,self.callback(123,'menu',802));self.tick()
        self.assertEqual(len(FakeAPI.sent),sent+2)
        # The press shows up in the thread for context, but does not count as an unread message.
        self.assertEqual(self.one('senler_messages',"subscriber_id=? AND direction='in'",(sub_id,))['text'],'Кнопка: Меню')
        self.assertEqual(self.one('senler_subscribers','id=?',(sub_id,))['unread'],0)

    def test_menu_button_stays_silent_without_a_running_menu_bot_or_during_operator_handoff(self):
        channel=self.channel();sub_id=self.sub(channel)
        bot=self.one('senler_bots','channel_id=? AND template_key=?',(channel,'delivery_menu'))
        # The delivery menu is only a draft until published: nothing to show yet, and no error either.
        self.webhook(channel,self.callback(123,'menu',901));self.tick()
        self.assertEqual(FakeAPI.sent,[])
        self.post('bots/{}/publish'.format(bot['id']))
        with self.service.db() as conn:
            conn.execute('UPDATE senler_subscribers SET bot_paused=1 WHERE id=?',(sub_id,))
        self.webhook(channel,self.callback(123,'menu',902));self.tick()
        self.assertEqual(FakeAPI.sent,[])  # a human has the conversation: the bot keeps quiet
        with self.service.db() as conn:
            conn.execute('UPDATE senler_subscribers SET bot_paused=0 WHERE id=?',(sub_id,))
        self.webhook(channel,self.callback(123,'menu',903));self.tick()
        self.assertEqual(len(FakeAPI.sent),1)

    def test_import_2500_preview_preserves_optouts_and_duplicates(self):
        channel=self.channel('vk');self.sub(channel,'1','unsubscribed')
        raw=('user_id;name;status\n'+'\n'.join('{};Гость {};active'.format(i,i) for i in range(1,2501))+'\n2;Дубль;active\nbad;Ошибка;active').encode()
        result=self.client.post('/reports/senler/api/import/preview',data={'channel_id':str(channel),'file':(io.BytesIO(raw),'senler.csv')},headers={'X-CSRF-Token':'csrf-test'})
        self.assertEqual(result.status_code,200)
        preview=result.get_json();self.assertEqual(preview['summary']['new'],2499);self.assertEqual(preview['summary']['duplicates'],1);self.assertEqual(preview['summary']['invalid'],1)
        self.post('import/confirm',{'id':preview['id'],'consent':False},400)
        self.post('import/confirm',{'id':preview['id'],'consent':True})
        self.post('import/confirm',{'id':preview['id'],'consent':True})
        with self.service.db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM senler_subscribers').fetchone()[0],2500)
        self.assertEqual(self.one('senler_subscribers','external_user_id=?',('1',))['status'],'unsubscribed')

    def test_webhook_auth_dedup_and_explicit_subscription(self):
        channel=self.channel();self.bot(channel)
        event=self.message(123,'/start',1)
        self.assertEqual(self.webhook(channel,event,secret=False).status_code,403)
        self.assertEqual(self.webhook(channel,event).status_code,200)
        self.webhook(channel,event);self.tick()
        self.assertEqual(self.one('senler_subscribers')['status'],'pending')
        self.assertIsNone(self.one('senler_runs'))
        self.assertEqual(len(FakeAPI.sent),1)
        self.webhook(channel,self.callback(123,'subscribe',2));self.tick()
        self.assertEqual(self.one('senler_subscribers')['status'],'active')
        self.assertEqual(self.one('senler_runs')['status'],'completed')
        self.assertTrue(any('Здравствуйте, Алексей!'==call[2]['text'] for call in FakeAPI.sent))

    def test_unsubscribe_cancels_scheduled_messages_even_on_paused_channel(self):
        channel=self.channel();subscriber=self.sub(channel)
        campaign=self.campaign([channel]);self.post('campaigns/{}/launch'.format(campaign))
        self.post('channels/{}/pause'.format(channel))
        self.webhook(channel,self.message(123,'/stop',1));self.tick()
        self.assertEqual(self.one('senler_subscribers','id=?',(subscriber,))['status'],'unsubscribed')
        self.assertEqual(self.one('senler_outbox')['status'],'cancelled')
        self.assertFalse(FakeAPI.sent)

    def test_multichannel_and_group_dedup_delivery(self):
        channels=[self.channel(k) for k in ['vk','telegram','max']]
        groups=[self.post('groups',{'name':name})['id'] for name in ['Сеты','Акции']]
        for channel in channels:
            sub=self.sub(channel)
            for group in groups:self.post('subscribers/bulk',{'ids':[sub],'action':'add_group','group_id':group})
        campaign=self.campaign(channels,groups)
        self.post('campaigns/{}/launch'.format(campaign))
        self.post('campaigns/{}/launch'.format(campaign),status=400)
        self.tick();self.tick()
        self.assertEqual(len(FakeAPI.sent),3)
        self.assertEqual({call[0] for call in FakeAPI.sent},{'vk','telegram','max'})
        self.assertEqual(self.one('senler_campaigns')['status'],'completed')
        self.assertTrue(all(call[2]['buttons'][-1]['value']=='unsubscribe' for call in FakeAPI.sent))

    def test_uncertain_telegram_is_not_retried(self):
        channel=self.channel();self.sub(channel);campaign=self.campaign([channel]);self.post('campaigns/{}/launch'.format(campaign))
        FakeAPI.failure=DeliveryError('Connection lost',uncertain=True)
        self.tick();FakeAPI.failure=None;self.tick()
        self.assertEqual(self.one('senler_outbox')['status'],'unknown');self.assertFalse(FakeAPI.sent)

    def test_rate_limit_keeps_durable_queue(self):
        channel=self.channel();self.sub(channel);campaign=self.campaign([channel]);self.post('campaigns/{}/launch'.format(campaign))
        FakeAPI.failure=DeliveryError('Rate limit',retry_after=90);self.tick()
        job=self.one('senler_outbox');self.assertEqual(job['status'],'pending');self.assertGreater(job['due_at'],now()+80)
        self.assertEqual(self.one('senler_campaigns')['status'],'running')
        FakeAPI.failure=None
        with self.service.db() as conn:conn.execute('UPDATE senler_outbox SET due_at=?',(now()-1,))
        self.tick();self.assertEqual(len(FakeAPI.sent),1)

    def test_campaign_pause_and_schedule(self):
        channel=self.channel();self.sub(channel);campaign=self.campaign([channel])
        self.post('campaigns/{}/launch'.format(campaign),{'scheduled_at':'2099-01-01T12:00'})
        self.tick();self.assertFalse(FakeAPI.sent)
        self.post('campaigns/{}/pause'.format(campaign));self.post('campaigns/{}/resume'.format(campaign))
        self.assertEqual(self.one('senler_campaigns')['status'],'scheduled')
        self.post('campaigns/{}/cancel'.format(campaign));self.assertEqual(self.one('senler_outbox')['status'],'cancelled')

    def test_scheduled_draft_preserves_time_without_starting_queue(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        channel=self.channel('vk');self.sub(channel)
        data={'name':'На завтра','body':{'text':'Предложение'},'audience':{'channels':[channel]},'scheduled_local':'2099-02-03T12:30'}
        data['id']=self.post('campaigns',data)['id']
        saved=self.client.get('/reports/senler/api/campaigns/'+str(data['id'])).get_json()
        expected=int(datetime(2099,2,3,12,30,tzinfo=ZoneInfo('Asia/Novosibirsk')).timestamp())
        self.assertEqual(saved['scheduled_at'],expected);self.assertEqual(saved['status'],'draft')
        self.tick();self.assertFalse(FakeAPI.sent);self.assertIsNone(self.one('senler_outbox'))
        data['scheduled_local']='';self.post('campaigns',data)
        self.assertIsNone(self.one('senler_campaigns')['scheduled_at'])

    def test_vk_read_receipts_polled_and_shown_in_campaign_stats(self):
        channel=self.channel('vk');self.sub(channel,'777');self.sub(channel,'888')
        campaign=self.campaign([channel]);self.post('campaigns/{}/launch'.format(campaign))
        self.tick();self.tick()
        with self.service.db() as conn:
            rows={r['external_user_id']:dict(r) for r in conn.execute(
                'SELECT o.*,s.external_user_id FROM senler_outbox o JOIN senler_subscribers s ON s.id=o.subscriber_id')}
        self.assertEqual({r['status'] for r in rows.values()},{'sent'})
        self.assertTrue(all(r['read_at'] is None for r in rows.values()))
        detail=self.client.get('/reports/senler/api/campaigns/'+str(campaign)).get_json()
        self.assertEqual(detail['vk_sent'],2);self.assertEqual(detail['vk_read'],0)
        # 777's out_read matches its message id (read); 888's is one behind (still unread).
        FakeAPI.conversations={'777':int(rows['777']['external_id']),'888':int(rows['888']['external_id'])-1}
        self.service._poll_reads()
        with self.service.db() as conn:
            rows={r['external_user_id']:dict(r) for r in conn.execute(
                'SELECT o.*,s.external_user_id FROM senler_outbox o JOIN senler_subscribers s ON s.id=o.subscriber_id')}
        self.assertIsNotNone(rows['777']['read_at']);self.assertIsNone(rows['888']['read_at'])
        detail=self.client.get('/reports/senler/api/campaigns/'+str(campaign)).get_json()
        self.assertEqual(detail['vk_read'],1)
        # A second poll is a no-op for the already-read message and does not error on the unread one.
        self.service._poll_reads()
        self.assertEqual(self.client.get('/reports/senler/api/campaigns/'+str(campaign)).get_json()['vk_read'],1)

    def test_vk_read_polling_covers_every_unread_recipient_not_only_the_oldest_batch(self):
        channel=self.channel('vk')
        peers=[str(1000+i) for i in range(350)]
        for i,peer in enumerate(peers):
            self.sent_message(channel,self.sub(channel,peer),5000+i)
        # Only the first and the last recipients opened the mailing; the ~330 in between never did.
        # They stay unread for the whole 7-day window and must not crowd the two groups out of the poll.
        readers=list(range(10))+list(range(340,350))
        FakeAPI.conversations={peers[i]:5000+i for i in readers}
        with patch.object(SenlerService,'READ_POLL_PEERS',100,create=True):
            for _ in range(4):
                self.service._poll_reads()
        self.assertEqual(self.read_peers(),{peers[i] for i in readers})
        self.assertTrue(all(len(batch)<=100 for batch in FakeAPI.polled))

    def test_vk_read_polling_moves_on_after_a_failed_check_instead_of_retrying_the_same_batch(self):
        channel=self.channel('vk')
        peers=[str(1000+i) for i in range(150)]
        for i,peer in enumerate(peers):
            self.sent_message(channel,self.sub(channel,peer),5000+i)
        FakeAPI.conversations={peers[140]:5140}
        with patch.object(SenlerService,'READ_POLL_PEERS',100,create=True):
            FakeAPI.read_check_failure=DeliveryError('VK временно недоступен')
            self.service._poll_reads()  # VK is down: nothing is learned, and nothing must blow up
            self.assertEqual(self.read_peers(),set())
            FakeAPI.read_check_failure=None
            self.service._poll_reads()  # the peers not yet tried come first, not the batch that just failed
        self.assertEqual(self.read_peers(),{peers[140]})

    def test_subscriber_avatars_fetched_cached_and_served_with_owner_scope(self):
        vk_channel=self.channel('vk');vk_sub=self.sub(vk_channel,'555')
        tg_channel=self.channel('telegram');tg_sub=self.sub(tg_channel,'777')
        max_channel=self.channel('max');max_sub=self.sub(max_channel,'999')
        FakeAPI.vk_photos={'555':'https://vk.example/photo555.jpg'}
        FakeAPI.telegram_photos={'777':True}
        self.service._poll_avatars()
        items={i['id']:i for i in self.client.get('/reports/senler/api/subscribers').get_json()['items']}
        self.assertEqual(items[vk_sub]['avatar_url'],'https://vk.example/photo555.jpg');self.assertEqual(items[vk_sub]['avatar_file'],0)
        self.assertEqual(items[tg_sub]['avatar_url'],'');self.assertEqual(items[tg_sub]['avatar_file'],1)
        # MAX has no documented endpoint to fetch a photo outside of chat membership: never queried, no row at all.
        self.assertEqual((items[max_sub]['avatar_url'],items[max_sub]['avatar_file']),(None,0))
        tg_avatar=self.client.get('/reports/senler/api/subscribers/{}/avatar'.format(tg_sub))
        self.assertEqual((tg_avatar.status_code,tg_avatar.content_type,tg_avatar.data),(200,'image/jpeg',b'FAKE-AVATAR-BYTES'))
        # VK is hotlinked straight to its own CDN, never cached locally, so the proxy route has nothing to serve.
        self.assertEqual(self.client.get('/reports/senler/api/subscribers/{}/avatar'.format(vk_sub)).status_code,404)
        # A second poll is a no-op: this subscriber already has a name and a freshly checked photo,
        # so it is skipped entirely until the 30-day refresh window passes.
        FakeAPI.vk_photos={'555':'https://vk.example/changed.jpg'}
        self.service._poll_avatars()
        self.assertEqual(self.one('senler_avatars','subscriber_id=?',(vk_sub,))['url'],'https://vk.example/photo555.jpg')
        with self.client.session_transaction() as session:
            session['user_id']=2
        self.assertEqual(self.client.get('/reports/senler/api/subscribers/{}/avatar'.format(tg_sub)).status_code,400)

    def test_vk_name_and_username_backfilled_from_the_same_photo_call_and_retried_while_blank(self):
        # Real VK message events never carry the sender's name (unlike Telegram/MAX), so a VK
        # subscriber genuinely starts out with a blank name until users.get fills it in.
        channel=self.channel('vk');sub=self.sub(channel,'555',name='')
        FakeAPI.vk_photos={'555':'https://vk.example/photo555.jpg'}
        FakeAPI.vk_names={'555':{'screen_name':'ivan_petrov'}}  # deactivated/hidden profiles can omit first/last name
        self.service._poll_avatars()
        stored=self.one('senler_subscribers','id=?',(sub,))
        self.assertEqual((stored['name'],stored['username']),('','ivan_petrov'))
        # The name is still blank, so the next poll retries this subscriber even though its photo
        # was just checked — and the same call happens to refresh the photo too, at no extra cost.
        FakeAPI.vk_photos={'555':'https://vk.example/photo555-v2.jpg'}
        FakeAPI.vk_names={'555':{'first_name':'Иван','last_name':'Петров','screen_name':'ivan_petrov'}}
        self.service._poll_avatars()
        stored=self.one('senler_subscribers','id=?',(sub,))
        self.assertEqual((stored['name'],stored['username']),('Иван Петров','ivan_petrov'))
        self.assertEqual(self.one('senler_avatars','subscriber_id=?',(sub,))['url'],'https://vk.example/photo555-v2.jpg')
        # Now that the name is filled in, a further poll leaves it alone until the photo itself is due.
        FakeAPI.vk_names={'555':{'first_name':'Другое','last_name':'Имя','screen_name':'ivan_petrov'}}
        self.service._poll_avatars()
        self.assertEqual(self.one('senler_subscribers','id=?',(sub,))['name'],'Иван Петров')

    def test_bot_buttons_condition_delay_and_operator_handoff(self):
        channel=self.channel();sub=self.sub(channel)
        group=self.post('groups',{'name':'Промокод'})['id']
        nodes=[{'id':'menu','type':'message','text':'Выберите действие','buttons':[{'label':'Получить','action':'goto','value':'group'}]},
               {'id':'group','type':'group','group_id':group,'mode':'add','next':'condition'},
               {'id':'condition','type':'condition','group_id':group,'yes':'delay','no':'operator'},
               {'id':'delay','type':'delay','minutes':1,'next':'operator'},
               {'id':'operator','type':'handoff','next':'finish'},
               {'id':'finish','type':'message','text':'Готово','buttons':[]}]
        self.bot(channel,nodes)
        self.webhook(channel,self.message(123,'/start',1));self.tick()
        self.assertEqual(self.one('senler_runs')['status'],'waiting_reply')
        payload=FakeAPI.sent[-1][2]['buttons'][0]['value']
        self.webhook(channel,self.callback(999,payload,2));self.tick()
        self.assertEqual(self.one('senler_runs')['status'],'waiting_reply')
        self.webhook(channel,self.callback(123,payload,3));self.tick()
        run=self.one('senler_runs');self.assertEqual(run['node_id'],'operator');self.assertGreater(run['due_at'],now())
        with self.service.db() as conn:conn.execute('UPDATE senler_runs SET due_at=?',(now()-1,))
        self.tick();self.assertEqual(self.one('senler_runs')['status'],'paused')
        self.assertEqual(self.one('senler_subscribers','id=?',(sub,))['bot_paused'],1)
        self.post('dialogs/{}/resume'.format(sub));self.tick()
        self.assertEqual(self.one('senler_runs')['status'],'completed')

    def test_bot_buttons_from_older_messages_stay_clickable_but_forged_or_stale_targets_are_rejected(self):
        channel=self.channel();self.sub(channel)
        nodes=[{'id':'menu','type':'message','text':'Меню','buttons':[
                    {'label':'A','action':'goto','value':'a'},
                    {'label':'B','action':'goto','value':'b'}]},
               {'id':'a','type':'message','text':'Ветка A','buttons':[{'label':'Назад','action':'goto','value':'menu'}]},
               {'id':'b','type':'message','text':'Ветка B','buttons':[{'label':'Назад','action':'goto','value':'menu'}]}]
        self.bot(channel,nodes)
        self.webhook(channel,self.message(123,'/start',1));self.tick()
        menu_message=FakeAPI.sent[-1][2]
        button_a=next(b['value'] for b in menu_message['buttons'] if b['value'].startswith('sb:') and b['value'].split(':')[3]=='a')
        button_b=next(b['value'] for b in menu_message['buttons'] if b['value'].startswith('sb:') and b['value'].split(':')[3]=='b')
        run_id=self.one('senler_runs')['id']
        # a forged callback claiming an option the menu never actually offered is ignored
        forged='sb:{}:menu:not-a-real-target'.format(run_id)
        sent_before=len(FakeAPI.sent)
        self.webhook(channel,self.callback(123,forged,2));self.tick()
        self.assertEqual(len(FakeAPI.sent),sent_before)
        self.assertEqual(self.one('senler_runs')['node_id'],'menu')
        # click A first — moves the run forward, so "menu" is no longer the freshest message
        self.webhook(channel,self.callback(123,button_a,3));self.tick()
        self.assertEqual((self.one('senler_runs')['node_id'],FakeAPI.sent[-1][2]['text']),('a','Ветка A'))
        # scrolling back up and tapping B from the *older* menu message still works
        self.webhook(channel,self.callback(123,button_b,4));self.tick()
        self.assertEqual((self.one('senler_runs')['node_id'],FakeAPI.sent[-1][2]['text']),('b','Ветка B'))
        # once the subscriber leaves the bot entirely, even a well-formed old callback does nothing
        self.webhook(channel,self.message(123,'стоп',5));self.tick()
        self.assertEqual(self.one('senler_runs')['status'],'cancelled')
        sent_before=len(FakeAPI.sent)
        self.webhook(channel,self.callback(123,button_a,6));self.tick()
        self.assertEqual(len(FakeAPI.sent),sent_before)

    def test_bot_errors_endpoint_lists_failed_runs_with_reason_and_step(self):
        channel=self.channel();sub=self.sub(channel)
        nodes=[{'id':'greet','type':'message','title':'Приветствие','text':'Привет!','buttons':[],'next':'menu'},
               {'id':'menu','type':'message','text':'Меню тут','buttons':[]}]
        bot_id,_=self.bot(channel,nodes)
        FakeAPI.failure=DeliveryError('Ключ недействителен.')
        self.webhook(channel,self.message(123,'/start',1));self.tick()
        FakeAPI.failure=None
        run=self.one('senler_runs')
        self.assertEqual((run['status'],run['node_id']),('error','greet'))
        items=self.client.get('/reports/senler/api/bots/{}/errors'.format(bot_id),
                               headers={'X-CSRF-Token':'csrf-test'}).get_json()['items']
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['error'],'Ключ недействителен.')
        self.assertEqual((items[0]['subscriber_id'],items[0]['subscriber_name']),(sub,'Алексей'))
        self.assertEqual((items[0]['step_title'],items[0]['step_type']),('Приветствие','message'))
        # Once the underlying run is no longer in 'error' (e.g. retried successfully), it drops off the list.
        with self.service.db() as conn:conn.execute("UPDATE senler_runs SET status='running',due_at=?",(now()-1,))
        self.tick()
        self.assertEqual(self.client.get('/reports/senler/api/bots/{}/errors'.format(bot_id),
                          headers={'X-CSRF-Token':'csrf-test'}).get_json()['items'],[])

    def test_draft_does_not_replace_published_definition(self):
        channel=self.channel();sub=self.sub(channel);bot_id,data=self.bot(channel)
        data['id']=bot_id;data['definition']['nodes'][0]['text']='Новый текст'
        self.post('bots',data)
        self.webhook(channel,self.message(123,'/start',1));self.tick()
        self.assertEqual(FakeAPI.sent[-1][2]['text'],'Здравствуйте, Алексей!')

    def test_invalid_flow_cycles_and_targets_rejected(self):
        channel=self.channel()
        for nodes in [[{'id':'a','type':'message','text':'x','next':'missing'}],
                      [{'id':'a','type':'message','text':'x','next':'b'},{'id':'b','type':'delay','minutes':1,'next':'a'}]]:
            self.post('bots',dict(name='Invalid',channel_id=channel,trigger_type='subscribe',definition={'entry':'a','nodes':nodes}),400)

    def test_button_color_validated_and_saved_for_goto_buttons_only(self):
        channel=self.channel('vk')
        nodes=[{'id':'menu','type':'message','text':'Меню','buttons':[
                    {'label':'Акция','action':'goto','value':'promo','color':'positive'}],'next':''},
               {'id':'promo','type':'message','text':'Акция','buttons':[],'next':''}]
        bot_id,_=self.bot(channel,nodes)
        saved=self.one('senler_bots','id=?',(bot_id,))
        self.assertEqual(json.loads(saved['draft_json'])['nodes'][0]['buttons'][0]['color'],'positive')
        bad_nodes=[{'id':'menu','type':'message','text':'Меню','buttons':[
                        {'label':'Акция','action':'goto','value':'promo','color':'rainbow'}],'next':''},
                   {'id':'promo','type':'message','text':'Акция','buttons':[],'next':''}]
        self.post('bots',dict(name='Bad',channel_id=channel,trigger_type='subscribe',definition={'entry':'menu','nodes':bad_nodes}),400)

    def test_reply_idempotency_and_takeover(self):
        channel=self.channel();sub=self.sub(channel)
        for _ in range(2):self.post('dialogs/{}/reply'.format(sub),{'text':'Здравствуйте','request_key':'same-logical-send'})
        self.tick();self.assertEqual(len(FakeAPI.sent),1)
        self.assertEqual(self.one('senler_subscribers')['bot_paused'],1)

    def test_webhook_vk_confirmation_and_wrong_group(self):
        channel=self.channel('vk');stored=self.one('senler_channels','id=?',(channel,))
        with self.service.db() as conn:conn.execute("UPDATE senler_channels SET confirmation='confirmation-code' WHERE id=?",(channel,))
        endpoint='/api/senler/webhook/vk/'+str(channel)
        data={'type':'confirmation','group_id':123456,'secret':stored['webhook_secret']}
        self.assertEqual(self.client.post(endpoint,json=data).get_data(as_text=True),'confirmation-code')
        data['group_id']=99;self.assertEqual(self.client.post(endpoint,json=data).status_code,403)

    def test_vk_link_resolves_before_callback_registration_and_requires_confirmation(self):
        self.service.api_factory=BotAPI
        channel=self.post('channels',{'kind':'vk','name':'Папа Суши','external_id':'https://vk.ru/papa_sushi','token':'fixture-key'})['id']
        registered=[]
        def vk(method,**params):
            if method=='groups.getById':
                return {'groups':[{'id':654321,'name':'Папа Суши','screen_name':'papa_sushi'}]}
            self.assertEqual(str(params['group_id']),'654321')
            if method=='groups.getCallbackConfirmationCode':return {'code':'confirmed-code'}
            if method=='groups.getCallbackServers':
                return {'items':registered}
            if method=='groups.addCallbackServer':
                stored=self.one('senler_channels','id=?',(channel,))
                self.assertEqual(stored['external_id'],'654321')
                self.assertEqual(stored['confirmation'],'confirmed-code')
                registered.append({'id':77,'url':params['url'],'secret_key':params['secret_key'],'status':'wait'})
                return {'server_id':77}
            if method=='groups.setCallbackSettings':return 1
            self.fail('Unexpected VK method '+method)
        with patch.dict(os.environ,{'SENLER_PUBLIC_URL':'https://crm.example'}), patch.object(BotAPI,'vk',side_effect=vk):
            self.post('channels/{}/connect'.format(channel),status=502)
            self.assertEqual(self.one('senler_channels')['status'],'configured')
            registered[0]['status']='ok'
            self.post('channels/{}/connect'.format(channel))
        self.assertEqual(len(registered),1)
        self.assertEqual(self.one('senler_channels')['status'],'connected')
        for value in ['https://evil.test/papa_sushi','https://vk.ru/one/two','0']:
            self.post('channels',{'kind':'vk','name':'Invalid','external_id':value,'token':'fixture-key'},400)

    def test_vk_subscription_bot_buttons_and_unsubscribe_end_to_end(self):
        channel=self.channel('vk')
        nodes=[{'id':'menu','type':'message','text':'Наше меню','buttons':[{'label':'Предложение','action':'goto','value':'offer'}]},
               {'id':'offer','type':'message','text':'Специальное предложение','buttons':[]}]
        self.bot(channel,nodes)
        secret=self.one('senler_channels')['webhook_secret']
        def incoming(sequence,kind,obj):
            payload={'type':kind,'event_id':str(sequence),'group_id':123456,'secret':secret,'object':obj}
            self.assertEqual(self.client.post('/api/senler/webhook/vk/'+str(channel),json=payload).status_code,200)
            self.tick()
        incoming(1,'message_new',{'message':{'from_id':123,'peer_id':123,'text':'Начать'}})
        self.assertEqual(self.one('senler_subscribers')['status'],'pending')
        incoming(2,'message_event',{'event_id':'button-2','user_id':123,'peer_id':123,'payload':{'senler':'subscribe'}})
        self.assertEqual(self.one('senler_subscribers')['status'],'active')
        menu=next(sent for sent in FakeAPI.sent if sent[2]['text']=='Наше меню')
        incoming(3,'message_event',{'event_id':'button-3','user_id':123,'peer_id':123,'payload':{'senler':menu[2]['buttons'][0]['value']}})
        self.assertTrue(any(sent[2]['text']=='Специальное предложение' for sent in FakeAPI.sent))
        campaign=self.campaign([channel]);self.post('campaigns/{}/launch'.format(campaign))
        incoming(4,'message_deny',{'user_id':123})
        self.assertEqual(self.one('senler_subscribers')['status'],'blocked')
        self.assertEqual(self.one('senler_outbox','campaign_id=?',(campaign,))['status'],'cancelled')

    def test_vk_photo_and_queue_survive_retry_and_worker_restart(self):
        channel=self.channel('vk');self.sub(channel);self.sub(channel,'456')
        with self.service.db() as conn:
            asset=conn.execute("INSERT INTO senler_assets(owner_id,data,mime,extension,created_at) VALUES (1,?,'image/png','png',?)",(b'fixture-png',now())).lastrowid
        campaign=self.post('campaigns',{'name':'Фото','body':{'text':'Фото предложения','asset_id':asset},'audience':{'channels':[channel]}})['id']
        self.post('campaigns/{}/launch'.format(campaign))
        with patch.object(FakeAPI,'upload_asset',return_value='photo-123_456',create=True) as upload:
            FakeAPI.failure=DeliveryError('Connection lost',uncertain=True);self.tick()
            self.assertEqual(self.one('senler_outbox')['status'],'pending')
            self.service=SenlerService(self.service.get_db,self.path,FakeAPI)
            FakeAPI.failure=None
            with self.service.db() as conn:conn.execute('UPDATE senler_outbox SET due_at=?',(now()-1,))
            self.tick();self.tick()
            self.assertEqual(upload.call_count,1)
        self.assertEqual(len(FakeAPI.sent),2)
        self.assertEqual(len({sent[3] for sent in FakeAPI.sent}),2)

    def test_replacing_token_cannot_move_subscriptions_to_another_bot(self):
        channel=self.channel();subscriber=self.sub(channel)
        with patch.object(FakeAPI,'identity',return_value={'external_id':'999999','title':'Different bot','username':'other','subscribe_url':'https://t.me/other'}):
            self.post('channels/{}/check'.format(channel),status=400)
        self.assertNotEqual(self.one('senler_channels')['external_id'],'999999')
        self.assertEqual(self.one('senler_subscribers','id=?',(subscriber,))['channel_id'],channel)

    def test_export_neutralizes_spreadsheet_formula(self):
        channel=self.channel();sub=self.sub(channel)
        with self.service.db() as conn:conn.execute("UPDATE senler_subscribers SET name='=1+1' WHERE id=?",(sub,))
        result=self.client.get('/reports/senler/api/subscribers/export')
        self.assertIn("'=1+1",result.get_data(as_text=True))

    def test_two_owners_cannot_read_or_modify_each_others_data(self):
        channel1=self.channel();sub1=self.sub(channel1)
        group1=self.post('groups',{'name':'Акции'})['id']
        campaign1=self.campaign([channel1]);bot1,_=self.bot(channel1)
        image=self.client.post('/reports/senler/api/assets', data={'file':(io.BytesIO(b'\x89PNG\r\n\x1a\npreview'),'x.png')},headers={'X-CSRF-Token':'csrf-test'}).get_json()['id']
        with self.client.session_transaction() as session:
            session['user_id']=2
        bootstrap=self.client.get('/reports/senler/api/bootstrap').get_json()
        self.assertEqual(bootstrap['channels'],[]);self.assertEqual(bootstrap['groups'],[]);self.assertEqual(bootstrap['recent'],[])
        self.assertEqual(self.client.get('/reports/senler/api/subscribers').get_json()['total'],0)
        self.assertEqual(self.client.get('/reports/senler/api/bots').get_json()['items'],[])
        for path in ['campaigns/'+str(campaign1),'dialogs/'+str(sub1),'assets/'+str(image),'bots/'+str(bot1)+'/errors']:
            self.assertEqual(self.client.get('/reports/senler/api/'+path).status_code,400,path)
        for path in ['channels/{}/pause'.format(channel1),'campaigns/{}/launch'.format(campaign1),'bots/{}/publish'.format(bot1),'dialogs/{}/takeover'.format(sub1)]:
            self.post(path,status=400)
        self.post('subscribers/bulk',{'ids':[sub1],'action':'unsubscribe'},400)
        self.post('audience',{'channels':[channel1]},400)
        channel2=self.channel();self.sub(channel2,'999')
        self.post('groups',{'name':'Акции'})  # same label is allowed in a different workspace
        self.post('audience',{'channels':[channel2],'groups':[group1]},400)
        self.post('campaigns',{'name':'Steal asset','body':{'text':'x','asset_id':image},'audience':{'channels':[channel2]}},400)
        own=self.client.get('/reports/senler/api/bootstrap').get_json()
        self.assertEqual([c['id'] for c in own['channels']],[channel2])
        self.assertEqual(own['stats']['active'],1)
        self.assertEqual(self.one('senler_subscribers','id=?',(sub1,))['status'],'active')

    def test_api_payloads_for_all_three_channels(self):
        channel={'kind':'telegram','external_id':'7','webhook_secret':'secret'}
        response=Mock(ok=True,status_code=200,headers={})
        response.json.return_value={'ok':True,'result':{'message_id':12}}
        marked_up='Текст **жирным** _курсивом_ ++подчёркнутым++ [ссылкой](https://example.com) промокод USE_CODE_2026 & <b>'
        with patch('senler_api.requests.request',return_value=response) as send:
            api=BotAPI(channel,'private-token')
            api.send('42',{'text':marked_up,'buttons':[{'label':'Отписаться','action':'callback','value':'unsubscribe'}]},91)
            args=send.call_args.kwargs['json'];self.assertEqual(args['chat_id'],'42');self.assertEqual(args['reply_markup']['inline_keyboard'][0][0]['callback_data'],'unsubscribe')
            self.assertEqual(args['parse_mode'],'HTML')
            self.assertEqual(args['text'],'Текст <b>жирным</b> <i>курсивом</i> <u>подчёркнутым</u> <a href="https://example.com">ссылкой</a> промокод USE_CODE_2026 &amp; &lt;b&gt;')
        response.json.return_value={'message':{'body':{'mid':'abc'}}}
        with patch('senler_api.requests.request',return_value=response) as send:
            api=BotAPI(dict(channel,kind='max'),'private-token');api.send('42',{'text':marked_up,'buttons':[]},91)
            self.assertEqual(send.call_args.args[1],'https://platform-api2.max.ru/messages')
            self.assertEqual(send.call_args.kwargs['headers'],{'Authorization':'private-token'})
            self.assertEqual(send.call_args.kwargs['json']['format'],'markdown')
            self.assertEqual(send.call_args.kwargs['json']['text'],marked_up)
            bundle=Path(send.call_args.kwargs['verify'])
            self.assertTrue(bundle.is_file())
            ministry_root=Path(__file__).resolve().parents[1]/'crm'/'certs'/'russian_trusted_root_ca.pem'
            self.assertIn(ministry_root.read_bytes().strip(),bundle.read_bytes())
        api=BotAPI(dict(channel,kind='vk'),'private-token')
        with patch.object(api,'vk',side_effect=[{'is_allowed':True},100]) as vk:
            api.send('42',{'text':marked_up,'buttons':[]},91)
            self.assertEqual(vk.call_args.kwargs['random_id'],91)
            self.assertEqual(vk.call_args.kwargs['message'],'Текст жирным курсивом подчёркнутым ссылкой (https://example.com) промокод USE_CODE_2026 & <b>')
        # The rich-text editor makes combined formatting easy (select a word, click Bold then
        # Italic), so nested markup must resolve correctly rather than leaving inner markers literal.
        nested='**жирное и _курсив внутри_ тоже**'
        response.json.return_value={'ok':True,'result':{'message_id':13}}
        with patch('senler_api.requests.request',return_value=response) as send:
            BotAPI(channel,'private-token').send('42',{'text':nested,'buttons':[]},92)
            self.assertEqual(send.call_args.kwargs['json']['text'],'<b>жирное и <i>курсив внутри</i> тоже</b>')
        api_vk=BotAPI(dict(channel,kind='vk'),'private-token')
        with patch.object(api_vk,'vk',side_effect=[{'is_allowed':True},100]) as vk:
            api_vk.send('42',{'text':nested,'buttons':[]},92)
            self.assertEqual(vk.call_args.kwargs['message'],'жирное и курсив внутри тоже')

    def test_vk_button_color_is_forwarded_other_channels_ignore_it(self):
        channel={'kind':'vk','external_id':'7','webhook_secret':'secret'}
        buttons=[{'label':'Акция','action':'callback','value':'promo','color':'positive'},
                 {'label':'Сайт','action':'url','value':'https://example.com'}]
        api=BotAPI(dict(channel,kind='vk'),'private-token')
        with patch.object(api,'vk',side_effect=[{'is_allowed':True},100]) as vk:
            api.send('42',{'text':'Текст','buttons':buttons},91)
            keyboard=json.loads(vk.call_args.kwargs['keyboard'])
            self.assertEqual([row[0].get('color') for row in keyboard['buttons']],['positive',None])
        response=Mock(ok=True,status_code=200,headers={})
        response.json.return_value={'ok':True,'result':{'message_id':1}}
        with patch('senler_api.requests.request',return_value=response) as send:
            BotAPI(dict(channel,kind='telegram'),'private-token').send('42',{'text':'Текст','buttons':buttons},91)
            self.assertNotIn('color',send.call_args.kwargs['json']['reply_markup']['inline_keyboard'][0][0])

    def test_api_error_never_exposes_telegram_token(self):
        import requests
        api=BotAPI({'kind':'telegram'},'very-private-token')
        with patch('senler_api.requests.request',side_effect=requests.ConnectionError('https://api.telegram.org/botvery-private-token/sendMessage')):
            with self.assertRaises(DeliveryError) as error:api.send('42',{'text':'x'},1)
            self.assertNotIn('very-private-token',str(error.exception));self.assertTrue(error.exception.uncertain)

    def test_failed_connect_records_the_failing_telegram_stage(self):
        import requests
        self.service.api_factory = BotAPI
        channel = self.post('channels', {'kind': 'telegram', 'name': 'Telegram', 'token': 'private-token'})['id']
        identity = Mock(ok=True, status_code=200, headers={})
        identity.json.return_value = {'ok': True, 'result': {'id': 7, 'is_bot': True, 'username': 'test_bot'}}
        with patch.dict(os.environ, {'SENLER_TELEGRAM_PROXY_URL': 'socks5h://proxy:1080', 'SENLER_PUBLIC_URL': 'https://crm.example', 'SENLER_TELEGRAM_RECEIVE_MODE': 'webhook'}):
            with patch('senler_api.requests.request', side_effect=requests.ConnectionError('private-token')) as request:
                result = self.post('channels/{}/connect'.format(channel), status=502)
                self.assertIn('TG-TOKEN-PROXY-CONNECTION', result['error'])
                self.assertEqual(request.call_count, 2)
            with patch('senler_api.requests.request', side_effect=[identity] + [requests.ReadTimeout('private-token')] * 4) as request:
                result = self.post('channels/{}/connect'.format(channel), status=502)
                self.assertIn('TG-WEBHOOK-PROXY-READ_TIMEOUT', result['error'])
                self.assertEqual(request.call_count, 5)
        stored = self.one('senler_channels', 'id=?', (channel,))
        self.assertEqual(stored['status'], 'configured')
        self.assertEqual(stored['last_error'], result['error'])
        self.assertNotIn('private-token', stored['last_error'])
        self.assertEqual(stored['external_id'], '7')

    def test_connect_reconciles_timeout_and_accepts_authenticated_marked_webhook(self):
        import requests
        self.service.api_factory = BotAPI
        channel = self.post('channels', {'kind': 'telegram', 'name': 'Telegram', 'token': 'private-token'})['id']
        registration = {}
        def request(method, url, **kwargs):
            response = Mock(ok=True, status_code=200, headers={})
            if url.endswith('/getMe'):
                result = {'id': 7, 'is_bot': True, 'username': 'test_bot'}
            elif url.endswith('/setWebhook'):
                registration.update(kwargs['json'])
                raise requests.ReadTimeout('private-token')
            else:
                self.assertTrue(url.endswith('/getWebhookInfo'))
                result = {'url': registration['url']}
            response.json.return_value = {'ok': True, 'result': result}
            return response
        with patch.dict(os.environ, {'SENLER_PUBLIC_URL': 'https://crm.example', 'SENLER_TELEGRAM_RECEIVE_MODE': 'webhook'}), \
                patch('senler_api.requests.request', side_effect=request):
            self.post('channels/{}/connect'.format(channel))
        stored = self.one('senler_channels', 'id=?', (channel,))
        self.assertEqual(stored['status'], 'connected')
        self.assertEqual(stored['last_error'], '')
        callback = registration['url'].removeprefix('https://crm.example')
        self.assertEqual(self.client.post(callback, json=self.message(123, '/start', 1)).status_code, 403)
        response = self.client.post(callback, json=self.message(123, '/start', 1),
                                    headers={'X-Telegram-Bot-Api-Secret-Token': stored['webhook_secret']})
        self.assertEqual(response.status_code, 200)

    def test_event_parsing_private_only(self):
        event=self.message(123,'hi',1);event['message']['chat']['type']='group'
        self.assertIsNone(parse_event('telegram',event))
        self.assertIsNone(parse_event('vk',{'type':'message_new','object':{'message':{'from_id':1,'peer_id':200000001,'text':'group'}}}))
        max_event={'update_type':'message_callback','timestamp':100,'callback':{'callback_id':'abc','user':{'user_id':1,'first_name':'Алексей','username':None},'payload':'subscribe'},'message':{'recipient':{'chat_type':'dialog'},'body':{'mid':'x'}}}
        parsed=parse_event('max',max_event);self.assertEqual(parsed['callback'],'subscribe');self.assertEqual(parsed['username'],'')


if __name__ == '__main__':
    unittest.main()
