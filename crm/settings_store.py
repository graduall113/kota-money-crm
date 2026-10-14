"""
Admin-editable settings (stored in the DB so nothing needs a code change).
`get_setting()` always returns a value: stored value -> DEFAULTS -> given default.
"""
import threading
from urllib.parse import urlparse

from django.conf import settings as django_settings
from django.core.signals import request_finished, request_started
from django.db.models.signals import post_delete, post_save

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
    # --- Attendance verification (Feature 5). Everything is OFF until an admin
    # configures it, so deploying this can never lock anybody out by surprise.
    "att_office_lat": "",             # decimal degrees; set by an admin, never in code
    "att_office_lng": "",
    "att_geofence_radius_m": "200",
    "att_max_accuracy_m": "100",      # worse (larger) GPS accuracy than this -> "try again"
    "att_office_ips": "",             # IPs / CIDRs, one per line or comma-separated
    "att_require_geofence": "0",
    "att_require_office_ip": "0",
    "att_require_trusted_device": "0",
    "att_repeat_threshold": "5",      # failed Start attempts in a day before a Repeated Attempt flag
}

BOOL_KEYS = {
    "n8n_enabled", "referrer_keeps_access", "staff_can_transfer", "staff_can_export", "allow_public_registration",
    "att_require_geofence", "att_require_office_ip", "att_require_trusted_device",
}


# ---------------------------------------------------------------------------
# Request-scoped snapshot of the Setting table.
#
# One page view reads many settings (access.visible_leads(), the n8n context processor,
# can_export(), ...): that used to be one SELECT per read. While an HTTP request is being handled
# the whole (tiny) table is now loaded ONCE and reused for the rest of that request, then thrown
# away when the request finishes. Nothing survives into the next request, so a setting changed by
# an admin (in this or any other worker process, or in the Django admin) is seen by the very next
# request - there is no stale-permission window. Outside a request (management commands, worker
# threads, tests that call helpers directly) nothing is cached and every read hits the DB as before.
# Any write through the ORM (set_setting, admin, Setting.save/delete) drops the snapshot at once.
# ---------------------------------------------------------------------------
_local = threading.local()


def _begin_request(**_kwargs):
    _local.snapshot = {}


def _end_request(**_kwargs):
    _local.snapshot = None


def _drop_snapshot(**_kwargs):
    if getattr(_local, "snapshot", None) is not None:
        _local.snapshot = {}


request_started.connect(_begin_request, dispatch_uid="crm_settings_snapshot_begin")
request_finished.connect(_end_request, dispatch_uid="crm_settings_snapshot_end")
post_save.connect(_drop_snapshot, sender=Setting, dispatch_uid="crm_settings_snapshot_save")
post_delete.connect(_drop_snapshot, sender=Setting, dispatch_uid="crm_settings_snapshot_delete")


def get_setting(key, default=""):
    snapshot = getattr(_local, "snapshot", None)
    if snapshot is not None:  # inside an HTTP request
        if "__all__" not in snapshot:
            snapshot["__all__"] = dict(Setting.objects.values_list("key", "value"))
        if key in snapshot["__all__"]:
            return snapshot["__all__"][key]
        return DEFAULTS.get(key, default)
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
