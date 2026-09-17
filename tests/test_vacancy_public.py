"""Public hiring form and card uploads. Uses an isolated temporary database."""
import importlib.util
import io
import os
import sys
from pathlib import Path
import tempfile
import unittest

from PIL import Image


class VacancyPublicTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='vacancy-public-test-')
        cls.saved_env = {key: os.environ.get(key) for key in ('DATABASE_PATH', 'CRM_DISABLE_SCHEDULER')}
        os.environ['DATABASE_PATH'] = str(Path(cls.tmp.name) / 'test.db')
        os.environ['CRM_DISABLE_SCHEDULER'] = '1'
        spec = importlib.util.spec_from_file_location('vacancy_test_app', Path(__file__).resolve().parents[1] / 'crm/app.py')
        cls.module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.module
        spec.loader.exec_module(cls.module)
        cls.app = cls.module.app
        cls.app.config['TESTING'] = True
        with cls.module.get_db() as conn:
            cls.owner = conn.execute("SELECT id FROM users WHERE role='owner' LIMIT 1").fetchone()[0]
            cls.other_owner = conn.execute("INSERT INTO users(username,password_hash,role,full_name) VALUES('card-test-owner','unused','owner','Test')").lastrowid
            conn.commit()

    @classmethod
    def tearDownClass(cls):
        for key, value in cls.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.modules.pop('vacancy_test_app', None)
        cls.tmp.cleanup()

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session.update(user_id=self.owner, role='owner')
        with self.module.get_db() as conn:
            for table in ('job_application_values', 'job_applications', 'job_position_cards', 'job_application_fields', 'job_positions', 'job_vacancy_tokens'):
                conn.execute('DELETE FROM ' + table)
            conn.commit()
            self.module._ensure_job_defaults(conn, self.owner)
            self.positions = conn.execute('SELECT * FROM job_positions WHERE owner_id=? ORDER BY sort_order', (self.owner,)).fetchall()
            self.pos = self.positions[0]
            self.token = self.module._job_vacancy_token(conn, self.owner)
            self.other_token = self.module._job_vacancy_token(conn, self.other_owner)
        self.public = '/vacancy/apply/' + self.token
        self.card = f'/vacancies/settings/position/{self.pos["id"]}/card'

    def image(self, color=(255, 0, 0, 128)):
        out = io.BytesIO()
        Image.new('RGBA', (32, 48), color).save(out, format='PNG')
        out.seek(0)
        return out

    def upload(self, **extra):
        return self.client.post(self.card, data={'description': 'Короткое описание', 'image': (self.image(), 'courier.png'), **extra})

    def test_choice_then_only_selected_questions(self):
        home = self.client.get(self.public).get_data(as_text=True)
        self.assertIn('Хорошая работа.', home)
        self.assertNotIn('id="application-form"', home)
        for pos in self.positions:
            self.assertIn(f'position={pos["id"]}', home)
        html = self.client.get(self.public + f'?position={self.pos["id"]}').get_data(as_text=True)
        self.assertIn('id="application-form"', html)
        with self.module.get_db() as conn:
            for field in conn.execute('SELECT id,position_id FROM job_application_fields'):
                self.assertEqual(f'name="field_{field["id"]}"' in html,
                                 field['position_id'] in (None, self.pos['id']))
        self.assertNotIn('id="application-form"', self.client.get(self.public+'?position=999999').get_data(as_text=True))

    def test_validation_keeps_selected_job_and_entered_values(self):
        data = {'position_id': self.pos['id'], 'phone': '+79990000000', 'full_name': 'Имя <script>'}
        html = self.client.post(self.public, data=data).get_data(as_text=True)
        self.assertIn('Проверьте', html)
        self.assertIn('Имя &lt;script&gt;', html)
        self.assertIn(f'name="position_id" value="{self.pos["id"]}"', html)
        with self.module.get_db() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM job_applications').fetchone()[0], 0)

    def test_submission_and_honeypot(self):
        data = {'position_id': self.pos['id'], 'phone': '+79990000000', 'full_name': 'Тест'}
        with self.module.get_db() as conn:
            fields = conn.execute('SELECT * FROM job_application_fields WHERE position_id=? OR position_id IS NULL', (self.pos['id'],)).fetchall()
        for field in fields:
            if field['is_required']:
                data[f'field_{field["id"]}'] = self.module.fromjson(field['options'])[0] if field['field_type'] == 'select' else 'Опыт есть'
        self.assertIn('Спасибо за отклик!', self.client.post(self.public, data=data).get_data(as_text=True))
        self.client.post(self.public, data={**data, 'website': 'bot.example'})
        with self.module.get_db() as conn:
            row = conn.execute('SELECT * FROM job_applications').fetchall()
            self.assertEqual(len(row), 1)
            self.assertEqual(row[0]['position_id'], self.pos['id'])

    def test_image_persists_alpha_description_and_etag(self):
        result = self.upload()
        self.assertEqual(result.status_code, 302)
        self.assertIn(f'section=position-{self.pos["id"]}', result.location)
        self.assertIn('Короткое описание', self.client.get(self.public).get_data(as_text=True))
        image_url = self.public + f'/image/{self.pos["id"]}'
        image = self.client.get(image_url)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.mimetype, 'image/png')
        self.assertEqual(Image.open(io.BytesIO(image.data)).getpixel((0, 0))[3], 128)
        self.assertEqual(self.client.get(image_url, headers={'If-None-Match': image.headers['ETag']}).status_code, 304)
        self.assertEqual(self.client.get('/vacancy/apply/'+self.other_token+f'/image/{self.pos["id"]}').status_code, 404)
        self.assertEqual(self.client.get('/vacancy/apply/invalid/image/'+str(self.pos['id'])).status_code, 404)
        self.module.init_db()  # Same schema initialization as a deploy/restart.
        self.assertEqual(self.client.get(image_url).status_code, 200)
        self.client.post(self.card, data={'description': 'Без картинки', 'remove_image': '1'})
        self.assertEqual(self.client.get(image_url).status_code, 404)
        self.assertIn('Без картинки', self.client.get(self.public).get_data(as_text=True))

    def test_bad_upload_does_not_replace_existing_card(self):
        self.upload()
        for payload in (b'<svg onload="alert(1)"/>', b'\x89PNG\r\n\x1a\ntruncated', b'x' * (5*1024*1024+1)):
            result = self.client.post(self.card, data={'description': 'Не сохранять', 'image': (io.BytesIO(payload), 'fake.png')})
            self.assertEqual(result.status_code, 302)
            with self.module.get_db() as conn:
                card = conn.execute('SELECT * FROM job_position_cards WHERE position_id=?', (self.pos['id'],)).fetchone()
                self.assertEqual(card['description'], 'Короткое описание')
                self.assertTrue(card['image_data'])

    def test_card_owner_and_role_isolation(self):
        self.upload()
        with self.client.session_transaction() as session:
            session.update(user_id=self.other_owner, role='owner')
        self.assertEqual(self.upload().status_code, 404)
        self.assertEqual(self.client.get(f'/vacancies/settings/position/{self.pos["id"]}/image').status_code, 404)
        with self.client.session_transaction() as session:
            session.update(user_id=self.owner, role='courier')
        self.assertEqual(self.upload().status_code, 302)
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.upload().status_code, 302)

    def test_hidden_and_empty_positions(self):
        self.upload()
        with self.module.get_db() as conn:
            conn.execute('UPDATE job_positions SET is_active=0 WHERE owner_id=?', (self.owner,))
            conn.commit()
        self.assertIn('Сейчас нет открытых вакансий', self.client.get(self.public).get_data(as_text=True))
        self.assertNotIn('id="application-form"', self.client.get(self.public+f'?position={self.pos["id"]}').get_data(as_text=True))
        self.assertEqual(self.client.get(self.public+f'/image/{self.pos["id"]}').status_code, 404)
        self.assertEqual(self.client.get(f'/vacancies/settings/position/{self.pos["id"]}/image').status_code, 200)
        self.assertEqual(self.client.get('/vacancy/apply/invalid').status_code, 404)

    def test_card_settings_and_position_deletion(self):
        self.upload()
        html = self.client.get('/vacancies?tab=settings&section=position-'+str(self.pos['id'])).get_data(as_text=True)
        self.assertIn('multipart/form-data', html)
        self.assertIn('Короткое описание', html)
        self.assertIn('name="remove_image"', html)
        self.client.post(f'/vacancies/settings/position/{self.pos["id"]}/delete')
        with self.module.get_db() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM job_position_cards WHERE position_id=?', (self.pos['id'],)).fetchone())


if __name__ == '__main__':
    unittest.main()
