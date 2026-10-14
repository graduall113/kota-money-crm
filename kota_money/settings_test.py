"""
Settings used ONLY for running the automated tests:

    python manage.py test --settings=kota_money.settings_test

* Production settings (kota_money/settings.py) are untouched. They force HTTPS (SECURE_SSL_REDIRECT) and secure
  cookies, which make Django's plain-HTTP test client receive 301 redirects instead of pages.
* The database is ALWAYS a throw-away SQLite file in the system temp directory. DATABASE_URL is deliberately ignored,
  so a test run can never connect to (or create/alter a `test_*` database on) a production PostgreSQL server even if
  the shell still has the production DATABASE_URL exported.
* No real secrets: SECRET_KEY is a fixed test value; outbound integrations are not configured.
* The n8n webhooks point at a reserved ``.invalid`` host (RFC 2606: it can never resolve). Without this the tests
  inherit the PRODUCTION webhook URL from settings.py, and every test that saves a lead through the Add/Edit Lead
  form would try to POST a fake lead to the real n8n workflow (Google Sheet row, Gmail, WhatsApp) when the test
  machine is online. Tests that need to observe a send mock ``requests.post`` and still work (the URL is non-empty).
"""
import tempfile
from pathlib import Path

from .settings import *  # noqa: F401,F403

_TMP = Path(tempfile.gettempdir())

SECRET_KEY = "test-only-secret-key-not-for-production"
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

N8N_LEAD_WEBHOOK_URL = "https://n8n.invalid/webhook/kota-money-lead-test"
N8N_LEAD_UPDATE_WEBHOOK_URL = ""
N8N_API_KEY = ""

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(_TMP / "kota_money_test_main.sqlite3"),
        # A FILE (not :memory:) so the multi-threaded Lead-ID test can share it; generous busy timeout for lock waits.
        "TEST": {"NAME": str(_TMP / "kota_money_test_run.sqlite3")},
        "OPTIONS": {"timeout": 30},
    }
}
