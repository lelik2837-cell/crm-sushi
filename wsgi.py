import sys, os, logging

logging.basicConfig(level=logging.INFO)

_crm = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'crm')
sys.path.insert(0, _crm)

logging.info("wsgi: crm path = %s", _crm)
logging.info("wsgi: DATABASE_PATH env = %s", os.environ.get('DATABASE_PATH', 'NOT SET'))

try:
    from app import app
    logging.info("wsgi: app imported OK")
except Exception as e:
    logging.exception("wsgi: IMPORT FAILED: %s", e)
    raise
