"""
Django settings for the Kota Money CRM project.
"""

import os
import sys
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
    # Must stay AFTER Authentication + Messages: locks non-admin staff out of
    # every CRM page unless they have an ACTIVE attendance record today.
    "crm.middleware.AttendanceRequiredMiddleware",
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
                # Drives the sidebar Start Day / Working / Day Ended control.
                "crm.context_processors.attendance_widget",
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

# Lead -> n8n -> Google Sheets sync queue (crm/lead_sync.py).
# Automatic background delivery after a Lead is saved. Switched OFF while running the test
# suite so no test can ever POST to the real n8n webhook.
CRM_LEAD_SYNC_AUTODISPATCH = "test" not in sys.argv
# Minimum seconds between two sends inside one drain (Google Sheets write quota is ~60/min).
CRM_LEAD_SYNC_MIN_INTERVAL = float(os.environ.get("CRM_LEAD_SYNC_MIN_INTERVAL", "1.0"))
# OPTIONAL second n8n webhook used ONLY for edits/updates (not for a brand-new lead). Point it at
# a small workflow that just does the Google Sheets "Append or Update Row" so edits never re-trigger
# the new-lead Gmail / WhatsApp nodes. Empty = edits use N8N_LEAD_WEBHOOK_URL as well.
N8N_LEAD_UPDATE_WEBHOOK_URL = os.environ.get("N8N_LEAD_UPDATE_WEBHOOK_URL", "")

# Uploaded files above this size are streamed to a temp file by Django.
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024

# -----------------------------------------------------------------
# STAFF ATTENDANCE (Start Day / End Day)
# -----------------------------------------------------------------
# Business timezone for work_date and the 10:00-19:00 window. Explicit (not
# just TIME_ZONE) so attendance can never silently shift if TIME_ZONE changes.
ATTENDANCE_TIMEZONE = os.environ.get("ATTENDANCE_TIMEZONE", "Asia/Kolkata")
ATTENDANCE_WORK_START = os.environ.get("ATTENDANCE_WORK_START", "10:00")  # HH:MM, business tz
ATTENDANCE_WORK_END = os.environ.get("ATTENDANCE_WORK_END", "19:00")      # auto-end moment
# Worked duration (actual, start -> end) decides the status of a day that WAS started:
#   >= full-day minimum            -> PRESENT
#   >= half-day minimum            -> HALF DAY
#   below the half-day minimum     -> ABSENT (too little work to count even as a half day)
# Full-day minimum = expected full day - tolerance. Expected 8h (480) with a 90 min tolerance gives
# 6h30: a normal ~7h day (late start / early finish) is PRESENT, while 5-6h is HALF DAY.
# (The old rule was a flat 480 minutes, so ~7h wrongly came out as Half Day.)
# ATTENDANCE_FULL_DAY_MIN_MINUTES can still be set directly to override the computed value.
ATTENDANCE_EXPECTED_FULL_DAY_MINUTES = int(os.environ.get("ATTENDANCE_EXPECTED_FULL_DAY_MINUTES", "480"))
ATTENDANCE_FULL_DAY_TOLERANCE_MINUTES = int(os.environ.get("ATTENDANCE_FULL_DAY_TOLERANCE_MINUTES", "90"))
ATTENDANCE_FULL_DAY_MIN_MINUTES = int(os.environ.get(
    "ATTENDANCE_FULL_DAY_MIN_MINUTES",
    ATTENDANCE_EXPECTED_FULL_DAY_MINUTES - ATTENDANCE_FULL_DAY_TOLERANCE_MINUTES))
ATTENDANCE_HALF_DAY_MIN_MINUTES = int(os.environ.get("ATTENDANCE_HALF_DAY_MIN_MINUTES", "240"))
# Weekly off day (Python weekday(): Monday=0 ... Sunday=6).
ATTENDANCE_WEEKLY_OFF_WEEKDAY = 6
# Master switch. Leave True in production; set ATTENDANCE_ENFORCED=0 only as an
# emergency off-switch (staff are then never locked out; nothing else changes).
ATTENDANCE_ENFORCED = os.environ.get("ATTENDANCE_ENFORCED", "1").lower() in ("1", "true", "yes", "on")

# How many reverse proxies sit in front of Django and append to X-Forwarded-For
# (Render's load balancer = 1; add 1 if you also put Cloudflare in front).
# 0 = ignore X-Forwarded-For entirely and use the socket address. The client IP
# is taken from that many hops from the RIGHT, so a staff member can't spoof it
# by sending their own X-Forwarded-For header. Verify on the Attendance
# Verification settings page: it shows the IP the server currently sees for you.
ATTENDANCE_TRUSTED_PROXY_COUNT = int(os.environ.get("ATTENDANCE_TRUSTED_PROXY_COUNT", "1"))

# -----------------------------------------------------------------
# STAFF CRM ACTIVITY MONITORING (mobile heartbeat + 15-minute inactivity rule)
# -----------------------------------------------------------------
# Everything is measured from what the CRM web page itself reports; the server clock is the only clock.
# The CRM cannot (and does not claim to) know which OTHER phone app a staff member is using.
ACTIVITY_MONITORING_ENABLED = os.environ.get("ACTIVITY_MONITORING_ENABLED", "1").lower() in ("1", "true", "yes", "on")
ACTIVITY_HEARTBEAT_SECONDS = int(os.environ.get("ACTIVITY_HEARTBEAT_SECONDS", "60"))      # page -> server ping
ACTIVITY_REPORT_MIN_SECONDS = int(os.environ.get("ACTIVITY_REPORT_MIN_SECONDS", "30"))    # max 1 activity ping / 30 s
ACTIVITY_SERVER_MIN_SECONDS = int(os.environ.get("ACTIVITY_SERVER_MIN_SECONDS", "5"))     # server ignores faster repeats
ACTIVITY_INACTIVITY_MINUTES = int(os.environ.get("ACTIVITY_INACTIVITY_MINUTES", "15"))    # business rule
ACTIVITY_WARNING_MINUTES = int(os.environ.get("ACTIVITY_WARNING_MINUTES", "12"))          # warning shown from here
ACTIVITY_ACTIVE_WINDOW_SECONDS = int(os.environ.get("ACTIVITY_ACTIVE_WINDOW_SECONDS", "120"))  # "CRM Active" if this recent
ACTIVITY_CREDIT_SECONDS = int(os.environ.get("ACTIVITY_CREDIT_SECONDS", "60"))            # active time credited per interaction
ACTIVITY_NETWORK_GRACE_SECONDS = int(os.environ.get("ACTIVITY_NETWORK_GRACE_SECONDS", "150"))   # missed heartbeats tolerated
ACTIVITY_DISCONNECT_SECONDS = int(os.environ.get("ACTIVITY_DISCONNECT_SECONDS", "300"))   # silent this long -> Session Disconnected
ACTIVITY_LUNCH_MAX_MINUTES = int(os.environ.get("ACTIVITY_LUNCH_MAX_MINUTES", "50"))      # a lunch counts as valid this long (business rule: 50 min)
# Live assigned-customer-call channel (Android -> /api/staff-activity/live-call/). Separate from calling sync.
ACTIVITY_LIVE_CALL_TTL_SECONDS = int(os.environ.get("ACTIVITY_LIVE_CALL_TTL_SECONDS", "180"))          # no update for this long -> call is stale, stops counting
ACTIVITY_LIVE_CALL_HEARTBEAT_SECONDS = int(os.environ.get("ACTIVITY_LIVE_CALL_HEARTBEAT_SECONDS", "60"))  # Android refresh interval while on a call (keep well below TTL)
ACTIVITY_DEVICE_POLL_SECONDS = int(os.environ.get("ACTIVITY_DEVICE_POLL_SECONDS", "60"))              # Android warning-status poll interval
