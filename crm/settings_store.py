"""
Admin-editable settings (stored in the DB so nothing needs a code change).
`get_setting()` always returns a value: stored value -> DEFAULTS -> given default.
"""
from urllib.parse import urlparse

from django.conf import settings as django_settings

from .models import Setting

DEFAULTS = {
    "n8n_webhook_url": "",  # empty -> falls back to settings.N8N_LEAD_WEBHOOK_URL
    "n8n_enabled": "1",
    "referrer_keeps_access": "1",     # referrer can still VIEW a lead after transferring it
    "staff_can_transfer": "1",        # staff may transfer leads they currently own
    "staff_can_export": "0",          # staff export of their own records (off by default)
    "allow_public_registration": "1",
    "import_chunk_size": "2000",
    "import_max_file_mb": "100",
    "import_default_duplicate_policy": "skip",
}

BOOL_KEYS = {"n8n_enabled", "referrer_keeps_access", "staff_can_transfer", "staff_can_export", "allow_public_registration"}


def get_setting(key, default=""):
    row = Setting.objects.filter(key=key).only("value").first()
    if row is not None:
        return row.value
    return DEFAULTS.get(key, default)


def get_bool(key):
    return str(get_setting(key)).strip().lower() in ("1", "true", "yes", "on")


def get_int(key, default):
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return default


def set_setting(key, value, user=None):
    if isinstance(value, bool):
        value = "1" if value else "0"
    Setting.objects.update_or_create(key=key, defaults={"value": str(value), "updated_by": user})


def get_n8n_webhook_url():
    """Single source of truth for the outbound webhook (DB value, else env/settings.py)."""
    url = get_setting("n8n_webhook_url").strip()
    return url or getattr(django_settings, "N8N_LEAD_WEBHOOK_URL", "")


def validate_webhook_url(url):
    """Returns an error string, or '' when the URL is acceptable."""
    url = (url or "").strip()
    if not url:
        return ""  # blank = fall back to the built-in default
    if len(url) > 500:
        return "URL is too long."
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "URL must start with http:// or https://"
    if not parsed.netloc or not parsed.hostname:
        return "URL must include a host name."
    if parsed.username or parsed.password:
        return "Do not embed credentials in the URL."
    return ""
