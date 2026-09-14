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
from senler_core import SenlerService, dumps, now, parse_import, validate_definition
from senler_api import BotAPI, DeliveryError, parse_event


class FakeAPI:
    sent = []
    failure = None

    def __init__(self, channel, token):
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
        FakeAPI.sent, FakeAPI.failure = [], None

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

    def sub(self, channel_id, external_id='123', status='active'):
        with self.service.db() as conn:
            return conn.execute('''INSERT INTO senler_subscribers(channel_id,external_user_id,status,name,created_at,consent_at)
                VALUES (?,?,?,?,?,?)''', (channel_id, external_id, status, 'Алексей Тест', now(), now())).lastrowid

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
        for path in ['campaigns/'+str(campaign1),'dialogs/'+str(sub1),'assets/'+str(image)]:
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
        with patch('senler_api.requests.request',return_value=response) as send:
            api=BotAPI(channel,'private-token')
            api.send('42',{'text':'Текст','buttons':[{'label':'Отписаться','action':'callback','value':'unsubscribe'}]},91)
            args=send.call_args.kwargs['json'];self.assertEqual(args['chat_id'],'42');self.assertEqual(args['reply_markup']['inline_keyboard'][0][0]['callback_data'],'unsubscribe')
        response.json.return_value={'message':{'body':{'mid':'abc'}}}
        with patch('senler_api.requests.request',return_value=response) as send:
            api=BotAPI(dict(channel,kind='max'),'private-token');api.send('42',{'text':'Текст','buttons':[]},91)
            self.assertEqual(send.call_args.args[1],'https://platform-api2.max.ru/messages')
            self.assertEqual(send.call_args.kwargs['headers'],{'Authorization':'private-token'})
        api=BotAPI(dict(channel,kind='vk'),'private-token')
        with patch.object(api,'vk',side_effect=[{'is_allowed':True},100]) as vk:
            api.send('42',{'text':'Текст','buttons':[]},91)
            self.assertEqual(vk.call_args.kwargs['random_id'],91)

    def test_api_error_never_exposes_telegram_token(self):
        import requests
        api=BotAPI({'kind':'telegram'},'very-private-token')
        with patch('senler_api.requests.request',side_effect=requests.ConnectionError('https://api.telegram.org/botvery-private-token/sendMessage')):
            with self.assertRaises(DeliveryError) as error:api.send('42',{'text':'x'},1)
            self.assertNotIn('very-private-token',str(error.exception));self.assertTrue(error.exception.uncertain)

    def test_event_parsing_private_only(self):
        event=self.message(123,'hi',1);event['message']['chat']['type']='group'
        self.assertIsNone(parse_event('telegram',event))
        self.assertIsNone(parse_event('vk',{'type':'message_new','object':{'message':{'from_id':1,'peer_id':200000001,'text':'group'}}}))
        max_event={'update_type':'message_callback','timestamp':100,'callback':{'callback_id':'abc','user':{'user_id':1,'first_name':'Алексей','username':None},'payload':'subscribe'},'message':{'recipient':{'chat_type':'dialog'},'body':{'mid':'x'}}}
        parsed=parse_event('max',max_event);self.assertEqual(parsed['callback'],'subscribe');self.assertEqual(parsed['username'],'')


if __name__ == '__main__':
    unittest.main()
