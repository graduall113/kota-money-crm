"""
Django settings for the Kota Money CRM project.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# -----------------------------------------------------------------
# SECURITY
# -----------------------------------------------------------------
# Replace this with a fresh secret before deploying:
#   python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
SECRET_KEY = "django-insecure-change-me-before-deploying"

DEBUG = True

ALLOWED_HOSTS = ["*"]  # tighten this before deploying to production

# -----------------------------------------------------------------
# APPS
# -----------------------------------------------------------------
INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "crm",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    'whitenoise.middleware.WhiteNoiseMiddleware',
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "kota_money.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Makes N8N_FORM_URL (and other settings below) available
                # inside every template without passing it manually.
                "kota_money.context_processors.site_settings",
            ],
        },
    },
]

WSGI_APPLICATION = "kota_money.wsgi.application"

# -----------------------------------------------------------------
# DATABASE
# -----------------------------------------------------------------
# Uses DATABASE_URL when set (Render/Postgres in production); falls back to
# local SQLite for local development. Works the same way on both backends —
# no SQLite-only queries are used anywhere in the app.
import dj_database_url  # noqa: E402

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# -----------------------------------------------------------------
# PASSWORD VALIDATION
# -----------------------------------------------------------------
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# -----------------------------------------------------------------
# INTERNATIONALIZATION
# -----------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# -----------------------------------------------------------------
# STATIC FILES
# -----------------------------------------------------------------
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -----------------------------------------------------------------
# LOGGING
# -----------------------------------------------------------------
# Render captures stdout as the app's log stream, so a plain StreamHandler
# on the console is all that's needed to see these in the Render dashboard.
# Previously there was NO LOGGING config at all: any logger.info(...) call
# anywhere in the app (e.g. crm/calling_api.py's [CALLING API] diagnostic
# logs) would silently go nowhere, since Python's logging module only
# auto-configures a WARNING-level "handler of last resort" with no
# handlers attached to a custom logger. Root-caused this while adding the
# calling-sync diagnostic logging — the logs would have been invisible in
# production even after being added.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "loggers": {
        "crm": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

# -----------------------------------------------------------------
# AUTH
# -----------------------------------------------------------------
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

# Password-reset emails print to the console in development.
# Swap this for a real SMTP backend before deploying.
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = "Kota Money CRM <no-reply@kotamoney.local>"

# -----------------------------------------------------------------
# KOTA MONEY CRM SETTINGS
# -----------------------------------------------------------------
# Reference only — this is the ORIGINAL n8n Lead Form the native Django
# LeadForm (crm/forms.py) is built to mirror field-for-field (order, types,
# required rules, labels). The CRM does NOT redirect users here and does
# NOT submit to this URL; it exists purely so anyone auditing the form can
# cross-check it against the source. Updated to the current form referenced
# in the latest integration request (previously pointed at an older form id).
N8N_FORM_URL = "https://soyacil.app.n8n.cloud/form/664c5f8c-5fda-4f3d-9a1f-d5116a7dd74d"

# Shared secret n8n's HTTP Request node sends back as the X-N8N-API-KEY
# header when it posts a submitted lead to /api/leads/create/. Set this
# via an environment variable — never hardcode the real value here.
#   export N8N_API_KEY="some-long-random-secret"
# With no env var set (e.g. fresh local checkout), the API key check
# always fails closed — the endpoint simply rejects every request
# rather than silently accepting unauthenticated ones.
N8N_API_KEY = os.environ.get("N8N_API_KEY", "")

# Outbound: Django CRM -> n8n Production webhook. The native "Add New
# Lead" Django form (crm/views.py:lead_create) POSTs every newly created
# lead here so the existing n8n Gmail / Google Sheets / WhatsApp
# automation keeps running. Never hardcode this elsewhere in the code —
# always read it from this setting.
#   export N8N_LEAD_WEBHOOK_URL="https://soyacil.app.n8n.cloud/webhook/kota-money-lead"
# Falls back to the known production URL if the env var isn't set, so a
# fresh checkout still works out of the box — override it per-environment
# via the env var instead of editing this file.
N8N_LEAD_WEBHOOK_URL = os.environ.get(
    "N8N_LEAD_WEBHOOK_URL",
    "https://soyacil.app.n8n.cloud/webhook/kota-money-lead",
)

# -----------------------------------------------------------------
# IMPORTS / BACKGROUND WORK
# -----------------------------------------------------------------
# Where uploaded CSV/Excel import files are kept while being processed. This is
# NOT publicly served. (These are admin data-import sheets — the CRM never stores
# customer document files.)
IMPORT_UPLOAD_DIR = Path(os.environ.get("IMPORT_UPLOAD_DIR", BASE_DIR / "import_uploads"))

# Run bulk jobs / imports inline instead of in a worker thread (used by tests).
CRM_JOBS_INLINE = False

# Uploaded files above this size are streamed to a temp file by Django.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
