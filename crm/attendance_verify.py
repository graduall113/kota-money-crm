"""
Attendance verification (Feature 5): defence in depth, backend authoritative.

Independent layers, each configurable from Settings -> Attendance Verification:

  1. Office geofence      distance is CALCULATED here from the admin-configured
                          office coordinates; the browser only supplies raw
                          latitude / longitude / accuracy, all of which are validated.
  2. GPS accuracy         a poor reading is a "try again", never a permanent block.
  3. Office public IP     optional allow-list of IPs / CIDRs.
  4. Trusted device       admin-issued enrolment code -> random secret in an
                          HttpOnly cookie (only its hash is stored). User-Agent
                          is never used as identity.

None of this can make browser-based attendance impossible to cheat (GPS can be
spoofed on a rooted phone, a VPN can fake an IP, a cookie can be copied). The
point is to stop ordinary fraud - home starts, edited requests, borrowed
accounts, replays - and to leave a neutral, reviewable trail for the rest.

Rules that hold everywhere in this module:
  * Nothing about who / when / how long is read from the request. Staff always
    comes from request.user, times from the server clock.
  * Failures are recorded as neutral AttendanceEvent rows (what the server
    observed), never as accusations.
  * End Day is never blocked by verification - a person must always be able to
    close their day. Problems are flagged for review instead.
"""
import datetime
import ipaddress
import math
import re
import secrets
import uuid
from dataclasses import dataclass, field
from typing import List, Optional

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import attendance
from .models import AttendanceEvent, Setting, TrustedDevice, hash_token
from .settings_store import DEFAULTS

ACTION_START = AttendanceEvent.ACTION_START
ACTION_END = AttendanceEvent.ACTION_END
ACTION_ENROLL = AttendanceEvent.ACTION_ENROLL

DEVICE_COOKIE = "km_att_device"
DEVICE_COOKIE_MAX_AGE = 365 * 24 * 3600

# Rate limiting (temporary cool-down only - nobody is ever locked out for good).
THROTTLE_WINDOW = datetime.timedelta(minutes=10)
THROTTLE_MAX_START_ATTEMPTS = 10
THROTTLE_MAX_ENROLL_ATTEMPTS = 5

# A normal attendance form only ever posts these. Anything below appearing in a
# POST is somebody editing the request; it is ignored AND recorded.
ALLOWED_GEO_ERRORS = {"denied", "unavailable", "timeout", "unsupported"}
FORBIDDEN_FIELDS = {
    "staff_id", "staff", "user", "user_id", "username", "work_date", "date", "attendance_date",
    "start_time", "end_time", "time", "timestamp", "client_time", "duration", "worked_duration",
    "status", "attendance_status", "is_inside_office", "inside_office", "distance",
    "distance_from_office", "ip", "device", "device_id", "verified", "auto_ended",
}

KEYS = (
    "att_office_lat", "att_office_lng", "att_geofence_radius_m", "att_max_accuracy_m", "att_office_ips",
    "att_require_geofence", "att_require_office_ip", "att_require_trusted_device", "att_repeat_threshold",
)

# ---------------------------------------------------------------- user-facing messages
MSG_ACCURACY = "Your location accuracy is too low. Please enable precise location and try again."
MSG_UNAVAILABLE = (
    "We couldn't get your location. Allow location access for this site in your browser, "
    "make sure device location is turned on, and try again."
)
MSG_DENIED = (
    "Location permission is blocked for this site. Allow location access in your browser settings, "
    "then try again."
)
MSG_BAD_LOCATION = "We couldn't read your location. Please try again."
MSG_IP = "Start Day is only available from the office network. Connect to the office Wi-Fi and try again."
MSG_DEVICE = (
    "This device isn't registered for attendance. Ask an admin for an enrolment code, "
    "then enter it on the Attendance page."
)
MSG_THROTTLED = "Too many attempts in a short time. Please wait a few minutes and try again."


def _outside_message(distance, radius):
    return (
        f"Start Day is only available at the office. You appear to be about {int(round(distance))} m away "
        f"(allowed: {int(round(radius))} m). If you are at the office, step near a window or open area and try again."
    )


class VerificationFailed(attendance.AttendanceError):
    """A blocking check failed. `.evidence` carries everything the server measured."""

    message = "Attendance verification failed."

    def __init__(self, evidence):
        self.evidence = evidence
        msgs = []
        for f in evidence.blocking:
            if f.message not in msgs:
                msgs.append(f.message)
        super().__init__(" ".join(msgs) or self.message)


class AttemptsThrottled(attendance.AttendanceError):
    message = MSG_THROTTLED


class EnrollmentError(attendance.AttendanceError):
    message = "That code is invalid or has expired. Ask an admin for a new one."


# ---------------------------------------------------------------- configuration
def _to_float(value, lo, hi):
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or not (lo <= f <= hi):
        return None
    return f


def _to_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    lat: Optional[float]
    lng: Optional[float]
    radius: float
    max_accuracy: float
    networks: list
    bad_ips: list
    require_geofence: bool
    require_ip: bool
    require_device: bool
    repeat_threshold: int

    @property
    def office_configured(self):
        return self.lat is not None and self.lng is not None

    @property
    def geofence_active(self):
        # Required but coordinates missing/invalid = misconfiguration. We do NOT
        # silently lock every employee out over it; the settings page warns and
        # refuses to save that combination.
        return self.require_geofence and self.office_configured

    @property
    def ip_active(self):
        return self.require_ip and bool(self.networks)

    @property
    def device_active(self):
        return self.require_device

    @property
    def any_active(self):
        return self.geofence_active or self.ip_active or self.device_active

    def problems(self):
        out = []
        if self.require_geofence and not self.office_configured:
            out.append("Geofence is switched on but the office latitude/longitude are missing or invalid, so it is not being enforced.")
        if self.require_ip and not self.networks:
            out.append("Office IP check is switched on but the allow-list is empty, so it is not being enforced.")
        if self.bad_ips:
            out.append("Ignored invalid IP allow-list entries: " + ", ".join(self.bad_ips[:5]))
        return out


def load_config():
    stored = dict(Setting.objects.filter(key__in=KEYS).values_list("key", "value"))
    get = lambda k: stored.get(k, DEFAULTS.get(k, ""))  # noqa: E731
    nets, bad = parse_allowlist(get("att_office_ips"))
    radius = _to_float(get("att_geofence_radius_m"), 10, 5000) or 200.0
    accuracy = _to_float(get("att_max_accuracy_m"), 5, 2000) or 100.0
    try:
        repeat = max(2, int(get("att_repeat_threshold")))
    except (TypeError, ValueError):
        repeat = 5
    return Config(
        lat=_to_float(get("att_office_lat"), -90, 90),
        lng=_to_float(get("att_office_lng"), -180, 180),
        radius=radius, max_accuracy=accuracy, networks=nets, bad_ips=bad,
        require_geofence=_to_bool(get("att_require_geofence")),
        require_ip=_to_bool(get("att_require_office_ip")),
        require_device=_to_bool(get("att_require_trusted_device")),
        repeat_threshold=repeat,
    )


# ---------------------------------------------------------------- geometry
def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def parse_location(post):
    """
    Returns (lat, lng, accuracy, status). status is "ok", "missing" (nothing
    sent) or "invalid" (sent but malformed / impossible). Values are only ever
    used as raw measurements; nothing derived from them is trusted from the client.
    """
    raw_lat, raw_lng = (post.get("latitude") or "").strip(), (post.get("longitude") or "").strip()
    raw_acc = (post.get("accuracy") or "").strip()
    if not raw_lat and not raw_lng:
        return None, None, None, "missing"
    lat, lng = _to_float(raw_lat, -90, 90), _to_float(raw_lng, -180, 180)
    if lat is None or lng is None:
        return None, None, None, "invalid"
    acc = None
    if raw_acc:
        acc = _to_float(raw_acc, 0, 1_000_000)
        if acc is None or acc <= 0:  # real GPS never reports exactly 0 / negative / NaN
            return None, None, None, "invalid"
    return round(lat, 6), round(lng, 6), (round(acc, 1) if acc is not None else None), "ok"


# ---------------------------------------------------------------- client IP
def _normalize_ip(value):
    try:
        addr = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    mapped = getattr(addr, "ipv4_mapped", None)
    return str(mapped or addr)


def client_ip(request):
    """
    The client's public IP as seen through exactly N trusted proxies
    (settings.ATTENDANCE_TRUSTED_PROXY_COUNT). The N-th entry from the right of
    X-Forwarded-For is used, so extra entries a user prepends cannot spoof it.
    """
    remote = request.META.get("REMOTE_ADDR", "")
    hops = max(int(getattr(settings, "ATTENDANCE_TRUSTED_PROXY_COUNT", 0) or 0), 0)
    candidate = remote
    if hops:
        parts = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
        if len(parts) >= hops:
            candidate = parts[-hops]
    return _normalize_ip(candidate)


def parse_allowlist(text):
    """-> (networks, invalid_tokens). Accepts single IPs and CIDRs; v4 or v6."""
    nets, bad = [], []
    for token in re.split(r"[\s,;]+", text or ""):
        if not token:
            continue
        try:
            net = ipaddress.ip_network(token, strict=False)
        except ValueError:
            bad.append(token)
            continue
        too_broad = net.prefixlen < (8 if net.version == 4 else 32)
        if too_broad:
            bad.append(token)  # e.g. 0.0.0.0/0 would silently allow the whole internet
        else:
            nets.append(net)
    return nets, bad


def ip_allowed(ip, networks):
    if not ip:
        return False
    addr = ipaddress.ip_address(ip)
    return any(addr.version == n.version and addr in n for n in networks)


# ---------------------------------------------------------------- trusted device
def resolve_device(request, user):
    """
    -> (device, problem). `device` is set only for an enrolled, active device
    that belongs to THIS user. problem is one of:
      None            a valid trusted device
      "none"          no device cookie at all
      "unknown"       cookie present but matches nothing
      "revoked"       the device was revoked by an admin
      "other_user"    the cookie belongs to a different staff member's device
    """
    raw = request.COOKIES.get(DEVICE_COOKIE, "")
    if not raw:
        return None, "none"
    dev = TrustedDevice.objects.filter(token_hash=hash_token(raw)).first()
    if dev is None:
        return None, "unknown"
    if dev.user_id != user.pk:
        return None, "other_user"
    if not dev.is_active:
        return None, "revoked"
    return dev, None


def _touch_device(device, ip, request):
    TrustedDevice.objects.filter(pk=device.pk).update(
        last_seen_at=attendance._now(), last_ip=ip, user_agent=(request.META.get("HTTP_USER_AGENT", "") or "")[:255],
    )


# ---------------------------------------------------------------- evidence
@dataclass
class Failure:
    event_type: str
    message: str
    blocking: bool
    detail: dict = field(default_factory=dict)


@dataclass
class Evidence:
    action: str
    ip: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    distance: Optional[float] = None
    device: Optional[TrustedDevice] = None
    any_check_active: bool = False
    failures: List[Failure] = field(default_factory=list)

    @property
    def blocking(self):
        return [f for f in self.failures if f.blocking]

    @property
    def flagged(self):
        return [f for f in self.failures if not f.blocking]

    def _fields(self, prefix):
        return {
            f"{prefix}_latitude": self.latitude,
            f"{prefix}_longitude": self.longitude,
            f"{prefix}_accuracy": self.accuracy,
            f"{prefix}_distance_from_office": self.distance,
            f"{prefix}_ip": self.ip,
            f"{prefix}_device": self.device,
        }

    def start_fields(self):
        out = self._fields("start")
        out["start_verification"] = "verified" if self.any_check_active else "not_checked"
        return out

    def end_fields(self):
        return self._fields("end")


def unexpected_fields(request):
    """Field names in the POST that a genuine attendance form never sends."""
    return {k: (request.POST.get(k) or "")[:40] for k in request.POST if k.lower() in FORBIDDEN_FIELDS}


def verify(request, user, action):
    """
    Measures and judges one Start/End request. Pure with respect to attendance
    state; never raises for a failed check (the caller decides). For End Day
    nothing is blocking - problems are only flagged.
    """
    cfg = load_config()
    enforce = action == ACTION_START
    ev = Evidence(action=action, ip=client_ip(request), any_check_active=cfg.any_active)

    # --- location
    lat, lng, acc, status = parse_location(request.POST)
    geo_error = (request.POST.get("geo_error") or "").strip().lower()
    geo_error = geo_error if geo_error in ALLOWED_GEO_ERRORS else ""
    if status == "ok":
        ev.latitude, ev.longitude, ev.accuracy = lat, lng, acc
        if cfg.office_configured:
            ev.distance = round(haversine_m(lat, lng, cfg.lat, cfg.lng), 1)  # calculated HERE, never received
    if cfg.geofence_active:
        if status == "missing":
            msg = MSG_DENIED if geo_error == "denied" else MSG_UNAVAILABLE
            ev.failures.append(Failure(AttendanceEvent.TYPE_LOCATION_UNAVAILABLE, msg, enforce, {"geo_error": geo_error}))
        elif status == "invalid":
            ev.failures.append(Failure(AttendanceEvent.TYPE_VERIFICATION_FAILED, MSG_BAD_LOCATION, enforce, {"reason": "malformed location"}))
        elif acc is None or acc > cfg.max_accuracy:
            ev.failures.append(Failure(
                AttendanceEvent.TYPE_POOR_ACCURACY, MSG_ACCURACY, enforce,
                {"accuracy": acc, "max_accuracy": cfg.max_accuracy},
            ))
        elif ev.distance > cfg.radius:
            ev.failures.append(Failure(
                AttendanceEvent.TYPE_LOCATION_OUTSIDE, _outside_message(ev.distance, cfg.radius), enforce,
                {"distance_m": ev.distance, "radius_m": cfg.radius},
            ))

    # --- office IP
    if cfg.ip_active and not ip_allowed(ev.ip, cfg.networks):
        ev.failures.append(Failure(AttendanceEvent.TYPE_IP_NOT_ALLOWED, MSG_IP, enforce, {"ip": ev.ip}))

    # --- trusted device
    device, problem = resolve_device(request, user)
    ev.device = device
    if device is None:
        if cfg.device_active:
            ev.failures.append(Failure(AttendanceEvent.TYPE_DEVICE_MISMATCH, MSG_DEVICE, enforce, {"problem": problem}))
        elif problem in ("other_user", "revoked"):
            # Not required, but a cookie that belongs to someone else (or to a
            # revoked device) is worth a neutral note for review.
            ev.failures.append(Failure(AttendanceEvent.TYPE_DEVICE_MISMATCH, MSG_DEVICE, False, {"problem": problem}))
    return ev


# ---------------------------------------------------------------- recording
def _day_start(now):
    d = attendance.business_date(now)
    return datetime.datetime.combine(d, datetime.time.min, tzinfo=attendance.business_tz())


def _make_event(user, action, event_type, outcome, message="", evidence=None, attendance_rec=None,
                attempt_key="", details=None, now=None):
    return AttendanceEvent.objects.create(
        user=user if getattr(user, "pk", None) else None,
        user_name=(user.get_full_name() or user.username) if getattr(user, "pk", None) else "",
        attendance=attendance_rec, action=action, event_type=event_type, outcome=outcome,
        attempt_key=attempt_key, message=message[:300],
        latitude=getattr(evidence, "latitude", None), longitude=getattr(evidence, "longitude", None),
        accuracy=getattr(evidence, "accuracy", None), distance_from_office=getattr(evidence, "distance", None),
        ip=getattr(evidence, "ip", None), device=getattr(evidence, "device", None),
        details=details or {}, created_at=now or attendance._now(),
    )


def record_failures(user, evidence, failures, outcome, attendance_rec=None):
    """One event row per failed check, all sharing an attempt_key. Returns the key."""
    key = uuid.uuid4().hex
    for f in failures:
        _make_event(user, evidence.action, f.event_type, outcome, f.message, evidence, attendance_rec, key, f.detail)
    return key


def record_event(user, action, event_type, outcome, message="", evidence=None, attendance_rec=None, details=None):
    return _make_event(user, action, event_type, outcome, message, evidence, attendance_rec, uuid.uuid4().hex, details)


def check_throttle(user, action=ACTION_START, now=None):
    """Temporary cool-down after a burst of rejected attempts (never permanent)."""
    now = now or attendance._now()
    limit = THROTTLE_MAX_START_ATTEMPTS if action == ACTION_START else THROTTLE_MAX_ENROLL_ATTEMPTS
    recent = (
        AttendanceEvent.objects.filter(
            user=user, action=action, outcome=AttendanceEvent.OUTCOME_REJECTED, created_at__gte=now - THROTTLE_WINDOW,
        ).values("attempt_key").distinct().count()
    )
    if recent >= limit:
        raise AttemptsThrottled()


def note_repeated_attempts(user, evidence, threshold):
    """After a rejected Start, flag (once per `threshold` attempts) that this person keeps failing today."""
    now = attendance._now()
    attempts = (
        AttendanceEvent.objects.filter(
            user=user, action=ACTION_START, outcome=AttendanceEvent.OUTCOME_REJECTED, created_at__gte=_day_start(now),
        ).values("attempt_key").distinct().count()
    )
    if attempts >= threshold and attempts % threshold == 0:
        record_event(
            user, ACTION_START, AttendanceEvent.TYPE_REPEATED_ATTEMPT, AttendanceEvent.OUTCOME_FLAGGED,
            f"{attempts} rejected Start Day attempts today.", evidence, details={"attempts_today": attempts},
        )


def record_unexpected_input(user, action, request, evidence=None):
    extra = unexpected_fields(request)
    if extra:
        record_event(
            user, action, AttendanceEvent.TYPE_UNEXPECTED_INPUT, AttendanceEvent.OUTCOME_FLAGGED,
            "The request contained fields the attendance form never sends; they were ignored.",
            evidence, details={"fields": extra},
        )
    return extra


# ---------------------------------------------------------------- trusted-device lifecycle
def create_enrollment(admin, staff, label=""):
    """Admin issues a single-use, 30-minute code bound to ONE staff member. -> (device, plaintext_code)."""
    device = TrustedDevice.objects.create(user=staff, label=(label or "")[:100], created_by=admin)
    return device, device.start_enrollment(attendance._now())


def _norm_code(raw):
    return re.sub(r"[\s\-]", "", raw or "").upper()


def redeem_enrollment(request, user, raw_code):
    """
    Staff enter the code on the device to trust. The code must have been issued
    for THIS logged-in user, is single-use and expires. -> (device, raw_token);
    the caller sets the cookie. Failures are recorded and rate-limited.
    """
    check_throttle(user, ACTION_ENROLL)
    code = _norm_code(raw_code)
    ip = client_ip(request)
    now = attendance._now()
    device = raw_token = None
    if len(code) == 8:
        with transaction.atomic():
            device = (
                TrustedDevice.objects.select_for_update()
                .filter(
                    user=user, enrollment_code_hash=hash_token(code), is_active=True,
                    token_hash__isnull=True, enrollment_expires__gt=now,
                )
                .first()
            )
            if device is not None:
                raw_token = secrets.token_hex(32)  # 256 bits from the OS CSPRNG
                device.token_hash = hash_token(raw_token)
                device.enrolled_at = now
                device.enrollment_code_hash = ""
                device.enrollment_expires = None
                device.last_seen_at, device.last_ip = now, ip
                device.user_agent = (request.META.get("HTTP_USER_AGENT", "") or "")[:255]
                device.save()
    if device is None:
        _make_event(
            user, ACTION_ENROLL, AttendanceEvent.TYPE_VERIFICATION_FAILED, AttendanceEvent.OUTCOME_REJECTED,
            "Invalid or expired device enrolment code.", Evidence(action=ACTION_ENROLL, ip=ip),
            attempt_key=uuid.uuid4().hex,
        )
        raise EnrollmentError()
    return device, raw_token


def revoke_device(device, admin):
    if not device.is_active:
        return False
    device.is_active = False
    device.revoked_at = attendance._now()
    device.revoked_by = admin
    device.enrollment_code_hash = ""
    device.enrollment_expires = None
    device.save(update_fields=["is_active", "revoked_at", "revoked_by", "enrollment_code_hash", "enrollment_expires"])
    return True


def device_cookie_kwargs(request):
    secure = request.is_secure() or request.META.get("HTTP_X_FORWARDED_PROTO", "").lower() == "https"
    return {"max_age": DEVICE_COOKIE_MAX_AGE, "httponly": True, "secure": secure, "samesite": "Lax"}


def touch_device(device, evidence_ip, request):
    if device is not None:
        _touch_device(device, evidence_ip, request)
