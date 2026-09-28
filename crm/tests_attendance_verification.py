"""
Feature 5: attendance anti-fraud / office verification.

Covers every layer (geofence, GPS accuracy, office IP, trusted device), the
neutral anomaly trail, admin override, replay/duplicate/concurrency, and that
nothing identity-, date- or time-related can be steered from the browser.
"""
import datetime
import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.db import connection
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from . import attendance, attendance_verify as av
from .models import Attendance, AttendanceEvent, AuditLog, StaffProfile, TrustedDevice
from .settings_store import set_setting
from .tests_attendance import Clock, at

User = get_user_model()

# The office used by these tests. Real coordinates live in Settings, never in code.
OFFICE = (25.135753, 75.823947)
NEAR = (25.135800, 75.824000)     # ~7 m away
FAR = (25.185000, 75.870000)      # several km away (e.g. home)
OFFICE_IP = "203.0.113.10"
OTHER_IP = "198.51.100.77"


def configure(**kw):
    """Turns on office verification the way an admin would (through the settings table)."""
    values = {
        "att_office_lat": OFFICE[0], "att_office_lng": OFFICE[1], "att_geofence_radius_m": 200,
        "att_max_accuracy_m": 100, "att_repeat_threshold": 5,
    }
    values.update(kw)
    for k, v in values.items():
        set_setting(k, v)


def loc(point, acc=15):
    return {"latitude": str(point[0]), "longitude": str(point[1]), "accuracy": str(acc)}


class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.other = User.objects.create_user("amit", password="pw")
        self.c = Client(REMOTE_ADDR="127.0.0.1")
        self.c.force_login(self.staff)

    def start(self, data=None, when=None, client=None, **extra):
        with Clock(when or at(10, 5)):
            return (client or self.c).post(reverse("attendance_start"), data or {}, **extra)

    def end(self, data=None, when=None, client=None, **extra):
        with Clock(when or at(17, 0)):
            return (client or self.c).post(reverse("attendance_end"), data or {}, **extra)

    def events(self, **kw):
        return AttendanceEvent.objects.filter(**kw)

    def last_message(self, response):
        return " ".join(str(m) for m in get_messages(response.wsgi_request))

    def enroll(self, user, client, label="Laptop"):
        """Admin issues a code, staff redeems it on `client`. Returns the TrustedDevice."""
        with Clock(at(9, 0)):
            device, code = av.create_enrollment(self.admin, user, label)
            r = client.post(reverse("attendance_device_enroll"), {"code": code})
        self.assertEqual(r.status_code, 302)
        return TrustedDevice.objects.get(pk=device.pk)


# ============================================================ 1. office geofence
class GeofenceTests(Base):
    def setUp(self):
        super().setUp()
        configure(att_require_geofence=1)

    def test_correct_office_location_starts_the_day_and_stores_evidence(self):
        r = self.start(loc(NEAR))
        self.assertRedirects(r, reverse("dashboard"), fetch_redirect_response=False)
        rec = Attendance.objects.get(user=self.staff)
        self.assertEqual(rec.start_verification, "verified")
        self.assertAlmostEqual(rec.start_latitude, NEAR[0], places=5)
        self.assertEqual(rec.start_accuracy, 15)
        self.assertLess(rec.start_distance_from_office, 50)
        self.assertEqual(rec.start_ip, "127.0.0.1")

    def test_outside_office_is_rejected_with_a_professional_message_and_event(self):
        r = self.start(loc(FAR))
        self.assertRedirects(r, reverse("attendance"), fetch_redirect_response=False)
        self.assertFalse(Attendance.objects.exists())
        self.assertIn("only available at the office", self.last_message(r))
        ev = self.events(event_type="location_outside").get()
        self.assertEqual((ev.user, ev.action, ev.outcome, ev.review_status), (self.staff, "start", "rejected", "open"))
        self.assertGreater(ev.distance_from_office, 1000)

    def test_distance_is_calculated_by_the_server_not_trusted_from_the_client(self):
        data = dict(loc(FAR), is_inside_office="true", distance_from_office="3", inside_office="1", distance="0")
        r = self.start(data)
        self.assertFalse(Attendance.objects.exists())          # still rejected
        self.assertTrue(self.events(event_type="unexpected_input").exists())
        self.assertGreater(self.events(event_type="location_outside").get().distance_from_office, 1000)

    def test_radius_is_configurable(self):
        configure(att_require_geofence=1, att_geofence_radius_m=5000)
        self.start(loc(FAR))                       # ~7 km away: still outside a 5 km radius
        self.assertFalse(Attendance.objects.exists())
        self.start(loc((25.150, 75.830)))          # ~1.7 km away: inside a 5 km radius
        self.assertEqual(Attendance.objects.filter(user=self.staff).count(), 1)

    def test_missing_location_is_a_retryable_error_not_a_crash_or_permanent_block(self):
        r = self.start({"geo_error": "denied"})
        self.assertFalse(Attendance.objects.exists())
        self.assertIn("Location permission is blocked", self.last_message(r))
        self.assertEqual(self.events(event_type="location_unavailable").count(), 1)
        # ...and the very next try, with location, works.
        self.start(loc(NEAR))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_gps_unavailable_message(self):
        r = self.start({"geo_error": "unavailable"})
        self.assertIn("couldn't get your location", self.last_message(r))

    def test_malformed_and_impossible_coordinates_rejected(self):
        for bad in (
            {"latitude": "abc", "longitude": "75.8", "accuracy": "10"},
            {"latitude": "95", "longitude": "75.8", "accuracy": "10"},
            {"latitude": "nan", "longitude": "75.8", "accuracy": "10"},
            {"latitude": "25.1", "longitude": "inf", "accuracy": "10"},
            dict(loc(NEAR), accuracy="0"),
            dict(loc(NEAR), accuracy="-5"),
        ):
            self.start(bad)
        self.assertFalse(Attendance.objects.exists())
        self.assertEqual(self.events(event_type="verification_failed").count(), 6)

    def test_not_enforced_when_geofence_switched_off_but_location_still_recorded(self):
        configure(att_require_geofence=0)
        self.start(loc(FAR))
        rec = Attendance.objects.get(user=self.staff)
        self.assertEqual(rec.start_verification, "not_checked")
        self.assertGreater(rec.start_distance_from_office, 1000)
        self.assertFalse(AttendanceEvent.objects.exists())

    def test_geofence_required_but_office_not_configured_never_locks_everyone_out(self):
        set_setting("att_office_lat", "")
        set_setting("att_office_lng", "")
        self.start({})
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())


# ============================================================ 2. GPS accuracy
class AccuracyTests(Base):
    def setUp(self):
        super().setUp()
        configure(att_require_geofence=1)

    def test_poor_accuracy_asks_for_retry_with_exact_message(self):
        r = self.start(loc(NEAR, acc=450))
        self.assertFalse(Attendance.objects.exists())
        self.assertIn("Your location accuracy is too low. Please enable precise location and try again.", self.last_message(r))
        ev = self.events(event_type="poor_accuracy").get()
        self.assertEqual(ev.accuracy, 450)

    def test_missing_accuracy_is_treated_as_poor(self):
        self.start({"latitude": str(NEAR[0]), "longitude": str(NEAR[1])})
        self.assertFalse(Attendance.objects.exists())
        self.assertEqual(self.events(event_type="poor_accuracy").count(), 1)

    def test_one_bad_reading_never_blocks_permanently(self):
        self.start(loc(NEAR, acc=900))
        self.start(loc(NEAR, acc=12))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_threshold_is_configurable(self):
        configure(att_require_geofence=1, att_max_accuracy_m=500)
        self.start(loc(NEAR, acc=450))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_accuracy_exactly_at_threshold_passes(self):
        self.start(loc(NEAR, acc=100))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())


# ============================================================ 3. office public IP
class OfficeIpTests(Base):
    def setUp(self):
        super().setUp()
        configure(att_require_office_ip=1, att_office_ips=f"{OFFICE_IP}\n192.0.2.0/24")

    def c_from(self, ip):
        c = Client(REMOTE_ADDR=ip)
        c.force_login(self.staff)
        return c

    def test_valid_office_ip_allowed_and_logged(self):
        self.start(client=self.c_from(OFFICE_IP))
        rec = Attendance.objects.get(user=self.staff)
        self.assertEqual(rec.start_ip, OFFICE_IP)

    def test_cidr_supported(self):
        self.start(client=self.c_from("192.0.2.200"))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_invalid_ip_rejected_with_event(self):
        r = self.start(client=self.c_from(OTHER_IP))
        self.assertFalse(Attendance.objects.exists())
        self.assertIn("office network", self.last_message(r))
        self.assertEqual(self.events(event_type="ip_not_allowed").get().ip, OTHER_IP)

    def test_office_ip_change_is_recoverable_by_admin_setting(self):
        self.start(client=self.c_from(OTHER_IP))
        self.assertFalse(Attendance.objects.exists())
        configure(att_require_office_ip=1, att_office_ips=f"{OTHER_IP}")          # admin updates the allow-list
        self.start(client=self.c_from(OTHER_IP), when=at(10, 30))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_ip_check_disabled_lets_any_ip_through(self):
        configure(att_require_office_ip=0, att_office_ips=OFFICE_IP)
        self.start(client=self.c_from(OTHER_IP))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_geofence_can_work_without_ip_check(self):
        configure(att_require_office_ip=0, att_office_ips="", att_require_geofence=1)
        self.start(loc(NEAR), client=self.c_from(OTHER_IP))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_require_ip_with_empty_allowlist_is_not_enforced(self):
        configure(att_require_office_ip=1, att_office_ips="")
        self.start(client=self.c_from(OTHER_IP))
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    @override_settings(ATTENDANCE_TRUSTED_PROXY_COUNT=1)
    def test_x_forwarded_for_spoofing_does_not_help(self):
        # The attacker prepends a fake office IP; the proxy appends the real one on the right.
        self.start(client=self.c_from("10.0.0.1"), HTTP_X_FORWARDED_FOR=f"{OFFICE_IP}, {OTHER_IP}")
        self.assertFalse(Attendance.objects.exists())
        self.assertEqual(self.events(event_type="ip_not_allowed").get().ip, OTHER_IP)

    @override_settings(ATTENDANCE_TRUSTED_PROXY_COUNT=1)
    def test_real_office_ip_through_the_proxy(self):
        self.start(client=self.c_from("10.0.0.1"), HTTP_X_FORWARDED_FOR=f"{OFFICE_IP}")
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    @override_settings(ATTENDANCE_TRUSTED_PROXY_COUNT=0)
    def test_forwarded_header_ignored_when_no_proxy_is_trusted(self):
        self.start(client=self.c_from(OTHER_IP), HTTP_X_FORWARDED_FOR=OFFICE_IP)
        self.assertFalse(Attendance.objects.exists())


# ============================================================ 4. trusted device
class TrustedDeviceTests(Base):
    def setUp(self):
        super().setUp()
        configure(att_require_trusted_device=1)

    def test_unregistered_device_rejected(self):
        r = self.start()
        self.assertFalse(Attendance.objects.exists())
        self.assertIn("isn't registered for attendance", self.last_message(r))
        self.assertEqual(self.events(event_type="device_mismatch").count(), 1)

    def test_enrolled_device_can_start_and_is_recorded(self):
        device = self.enroll(self.staff, self.c)
        self.assertTrue(device.is_enrolled)
        self.start()
        rec = Attendance.objects.get(user=self.staff)
        self.assertEqual(rec.start_device, device)
        device.refresh_from_db()
        self.assertIsNotNone(device.last_seen_at)

    def test_only_a_hash_is_stored_and_cookie_is_httponly(self):
        with Clock(at(9)):
            device, code = av.create_enrollment(self.admin, self.staff, "x")
            r = self.c.post(reverse("attendance_device_enroll"), {"code": code})
        cookie = r.cookies[av.DEVICE_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        device.refresh_from_db()
        self.assertNotEqual(device.token_hash, cookie.value)
        self.assertEqual(len(device.token_hash), 64)
        self.assertEqual(device.enrollment_code_hash, "")     # the code is burned

    def test_user_agent_alone_is_never_an_identity(self):
        self.enroll(self.staff, self.c)
        fresh = Client(HTTP_USER_AGENT=self.c.defaults.get("HTTP_USER_AGENT", "Mozilla/5.0"))
        fresh.force_login(self.staff)                           # same UA, no cookie
        self.start(client=fresh)
        self.assertFalse(Attendance.objects.exists())

    def test_device_mismatch_another_staffs_device_is_rejected_and_flagged(self):
        other_client = Client()
        other_client.force_login(self.other)
        self.enroll(self.other, other_client)
        borrowed = Client()
        borrowed.force_login(self.staff)
        borrowed.cookies[av.DEVICE_COOKIE] = other_client.cookies[av.DEVICE_COOKIE].value
        self.start(client=borrowed)
        self.assertFalse(Attendance.objects.filter(user=self.staff).exists())
        self.assertEqual(self.events(event_type="device_mismatch").get().details["problem"], "other_user")

    def test_forged_cookie_rejected(self):
        c = Client()
        c.force_login(self.staff)
        c.cookies[av.DEVICE_COOKIE] = "a" * 64
        self.start(client=c)
        self.assertFalse(Attendance.objects.exists())

    def test_revoked_device_stops_working_immediately(self):
        device = self.enroll(self.staff, self.c)
        admin_c = Client()
        admin_c.force_login(self.admin)
        r = admin_c.post(reverse("attendance_device_revoke", args=[device.pk]))
        self.assertEqual(r.status_code, 302)
        device.refresh_from_db()
        self.assertFalse(device.is_active)
        self.start()
        self.assertFalse(Attendance.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="attendance_device_revoked").exists())

    def test_after_revoke_staff_can_be_re_enrolled_and_recover(self):
        device = self.enroll(self.staff, self.c)
        av.revoke_device(device, self.admin)
        self.enroll(self.staff, self.c, label="New laptop")
        self.start()
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_enrolment_code_is_single_use_expires_and_is_bound_to_its_user(self):
        with Clock(at(9)):
            d1, code = av.create_enrollment(self.admin, self.staff, "a")
            wrong = Client()
            wrong.force_login(self.other)
            wrong.post(reverse("attendance_device_enroll"), {"code": code})            # someone else's code
            self.assertNotIn(av.DEVICE_COOKIE, wrong.cookies)
            self.c.post(reverse("attendance_device_enroll"), {"code": code})           # correct user
            again = Client()
            again.force_login(self.staff)
            again.post(reverse("attendance_device_enroll"), {"code": code})            # replay
            self.assertNotIn(av.DEVICE_COOKIE, again.cookies)
        with Clock(at(9)):
            d2, code2 = av.create_enrollment(self.admin, self.staff, "b")
        with Clock(at(9) + datetime.timedelta(minutes=31)):
            late = Client()
            late.force_login(self.staff)
            late.post(reverse("attendance_device_enroll"), {"code": code2})            # expired
            self.assertNotIn(av.DEVICE_COOKIE, late.cookies)

    def test_enrolment_brute_force_is_throttled(self):
        with Clock(at(9)):
            for _ in range(av.THROTTLE_MAX_ENROLL_ATTEMPTS):
                self.c.post(reverse("attendance_device_enroll"), {"code": "AAAAAAAA"})
            device, code = av.create_enrollment(self.admin, self.staff, "x")
            r = self.c.post(reverse("attendance_device_enroll"), {"code": code}, follow=True)
        self.assertNotIn(av.DEVICE_COOKIE, r.client.cookies)
        self.assertIn("Too many attempts", " ".join(str(m) for m in r.context["messages"]))

    def test_calling_devices_are_untouched(self):
        from .models import CallDevice
        cd = CallDevice.objects.create(staff=self.staff, label="Phone")
        cd.start_pairing()
        self.enroll(self.staff, self.c)
        cd.refresh_from_db()
        self.assertTrue(cd.pairing_code)
        self.assertEqual(TrustedDevice.objects.count(), 1)

    def test_non_admin_cannot_issue_or_revoke_devices(self):
        r = self.c.post(reverse("attendance_device_new"), {"staff": self.staff.pk})
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(TrustedDevice.objects.exists())

    def test_enrolment_page_reachable_before_start_day(self):
        with Clock(at(9)):
            r = self.c.get(reverse("attendance"))
        self.assertEqual(r.status_code, 200)


# ============================================================ 5. server time
class ServerTimeTests(Base):
    def test_fake_browser_timestamp_and_dates_are_ignored(self):
        data = {
            "timestamp": "2020-01-01T03:00:00Z", "start_time": "2020-01-01 03:00", "client_time": "1577847600000",
            "work_date": "2020-01-01", "date": "2020-01-01", "duration": "99:00:00", "status": "full_day",
            "attendance_status": "full_day", "worked_duration": "99:00:00",
        }
        self.start(data, when=at(11, 20))
        rec = Attendance.objects.get(user=self.staff)
        self.assertEqual(rec.start_time, at(11, 20))
        self.assertEqual(rec.work_date, datetime.date(2026, 9, 27))
        self.assertEqual(rec.attendance_status, "in_progress")
        self.assertIsNone(rec.worked_duration)
        fields = self.events(event_type="unexpected_input").get().details["fields"]
        self.assertIn("timestamp", fields)
        self.assertIn("work_date", fields)

    def test_end_day_ignores_client_time_and_duration(self):
        self.start(when=at(10, 0))
        self.end({"end_time": "2026-09-27 23:00", "duration": "23:00:00", "status": "full_day"}, when=at(13, 0))
        rec = Attendance.objects.get()
        self.assertEqual(rec.end_time, at(13, 0))
        self.assertEqual(rec.worked_duration, datetime.timedelta(hours=3))
        self.assertEqual(rec.attendance_status, "short_day")

    def test_work_date_is_asia_kolkata(self):
        self.start(when=at(0, 30, day=28))
        self.assertEqual(Attendance.objects.get().work_date, datetime.date(2026, 9, 28))


# ============================================================ 6/7. anti-replay + staff identity
class ReplayAndIdentityTests(Base):
    def test_duplicate_start_is_rejected_and_recorded(self):
        self.start(when=at(10, 0))
        r = self.start(when=at(10, 1))
        self.assertIn("already started", self.last_message(r))
        self.assertEqual(Attendance.objects.count(), 1)
        self.assertEqual(self.events(event_type="duplicate_start").count(), 1)
        self.assertEqual(Attendance.objects.get().start_time, at(10, 0))

    def test_duplicate_end_is_rejected_and_recorded(self):
        self.start(when=at(10, 0))
        self.end(when=at(15, 0))
        r = self.end(when=at(16, 0))
        self.assertIn("already ended", self.last_message(r))
        self.assertEqual(self.events(event_type="duplicate_end").count(), 1)
        self.assertEqual(Attendance.objects.get().end_time, at(15, 0))

    def test_start_after_end_same_day_rejected(self):
        self.start(when=at(10, 0))
        self.end(when=at(12, 0))
        self.start(when=at(13, 0))
        self.assertEqual(Attendance.objects.count(), 1)
        self.assertEqual(self.events(event_type="duplicate_start").count(), 1)

    def test_end_without_start_is_an_unexpected_transition(self):
        r = self.end(when=at(12, 0))
        self.assertIn("haven't started", self.last_message(r))
        self.assertEqual(self.events(event_type="suspicious_transition").count(), 1)
        self.assertFalse(Attendance.objects.exists())

    def test_fake_staff_id_cannot_start_someone_elses_day(self):
        self.start({"staff_id": str(self.other.pk), "user": str(self.other.pk), "user_id": str(self.other.pk)})
        self.assertFalse(Attendance.objects.filter(user=self.other).exists())
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())
        self.assertIn("staff_id", self.events(event_type="unexpected_input").get().details["fields"])

    def test_fake_staff_id_cannot_end_someone_elses_day(self):
        oc = Client()
        oc.force_login(self.other)
        self.start(when=at(10, 0), client=oc)
        self.end({"staff_id": str(self.other.pk)}, when=at(11, 0))            # rahul never started
        self.assertIsNone(Attendance.objects.get(user=self.other).end_time)

    def test_direct_api_get_and_unauthenticated_calls(self):
        with Clock(at(10)):
            self.assertEqual(self.c.get(reverse("attendance_start")).status_code, 405)
            self.assertEqual(self.c.get(reverse("attendance_end")).status_code, 405)
            self.assertEqual(self.c.get(reverse("attendance_device_enroll")).status_code, 405)
            anon = Client()
            for name in ("attendance_start", "attendance_end", "attendance_device_enroll"):
                r = anon.post(reverse(name), {})
                self.assertEqual(r.status_code, 302)
                self.assertIn("login", r["Location"])
        self.assertFalse(Attendance.objects.exists())

    def test_csrf_is_enforced_on_start_end_and_enrol(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.staff)
        for name in ("attendance_start", "attendance_end", "attendance_device_enroll"):
            self.assertEqual(strict.post(reverse(name), {}).status_code, 403, name)
        self.assertFalse(Attendance.objects.exists())

    def test_json_body_manipulation_has_no_effect(self):
        with Clock(at(10)):
            r = self.c.post(reverse("attendance_start"), '{"staff_id": %d, "work_date": "2020-01-01"}' % self.other.pk,
                            content_type="application/json")
        self.assertEqual(r.status_code, 302)
        rec = Attendance.objects.get()
        self.assertEqual((rec.user, rec.work_date), (self.staff, datetime.date(2026, 9, 27)))

    def test_database_still_enforces_one_record_per_day(self):
        self.start(when=at(10, 0))
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Attendance.objects.create(user=self.staff, work_date=datetime.date(2026, 9, 27), start_time=at(10, 5))

    def test_admin_cannot_use_staff_endpoints_to_clock_in(self):
        ac = Client()
        ac.force_login(self.admin)
        with Clock(at(10)):
            ac.post(reverse("attendance_start"), {"staff_id": self.staff.pk})
        self.assertFalse(Attendance.objects.exists())


# ============================================================ 8. anomaly trail + repeated attempts
class AnomalyTests(Base):
    def setUp(self):
        super().setUp()
        configure(att_require_geofence=1, att_repeat_threshold=3)

    def test_events_use_neutral_labels(self):
        labels = {label for _, label in AttendanceEvent.TYPE_CHOICES}
        for word in ("Fraud", "Cheat", "Fake", "Liar", "Suspect"):
            self.assertFalse(any(word.lower() in label.lower() for label in labels), word)
        self.assertTrue({"Verification Failed", "Location Outside Office", "IP Not Allowed", "Device Mismatch",
                         "Repeated Attempt"} <= labels)

    def test_repeated_failed_attempts_are_flagged_not_blocked(self):
        for minute in range(3):
            self.start(loc(FAR), when=at(10, minute))
        self.assertEqual(self.events(event_type="repeated_attempt").count(), 1)
        self.assertEqual(self.events(event_type="repeated_attempt").get().outcome, "flagged")
        self.start(loc(NEAR), when=at(10, 5))                                   # no punishment: works when at the office
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_burst_of_attempts_gets_a_temporary_cooldown_that_expires(self):
        configure(att_require_geofence=1, att_repeat_threshold=50)
        for i in range(av.THROTTLE_MAX_START_ATTEMPTS):
            self.start(loc(FAR), when=at(10, 0, ) + datetime.timedelta(seconds=i))
        r = self.start(loc(NEAR), when=at(10, 1))
        self.assertIn("Too many attempts", self.last_message(r))
        self.assertFalse(Attendance.objects.exists())
        self.start(loc(NEAR), when=at(10, 20))                                  # window has passed
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_admin_can_review_events(self):
        self.start(loc(FAR))
        ev = self.events(event_type="location_outside").get()
        ac = Client()
        ac.force_login(self.admin)
        r = ac.post(reverse("attendance_event_review", args=[ev.pk]), {"status": "reviewed", "note": "was at client site"})
        self.assertEqual(r.status_code, 302)
        ev.refresh_from_db()
        self.assertEqual((ev.review_status, ev.reviewed_by, ev.review_note), ("reviewed", self.admin, "was at client site"))
        self.assertEqual(ac.get(reverse("attendance_events")).status_code, 200)
        r = ac.post(reverse("attendance_event_review", args=[ev.pk]), {"status": "convicted"})
        ev.refresh_from_db()
        self.assertEqual(ev.review_status, "reviewed")

    def test_staff_cannot_see_or_review_events(self):
        self.start(loc(NEAR), when=at(10, 0))                    # active day, so the attendance gate lets them through...
        self.start(loc(NEAR), when=at(10, 1))                    # ...and a duplicate start leaves an event behind
        ev = self.events().first()
        with Clock(at(10, 5)):
            self.assertEqual(self.c.get(reverse("attendance_events")).status_code, 403)   # ...but the admin check refuses
            self.assertEqual(self.c.post(reverse("attendance_event_review", args=[ev.pk]), {"status": "dismissed"}).status_code, 403)
        ev.refresh_from_db()
        self.assertEqual(ev.review_status, "open")


# ============================================================ 9. end day
class EndDayTests(Base):
    def test_end_day_records_location_ip_device_and_never_blocks(self):
        configure(att_require_geofence=1, att_require_office_ip=1, att_office_ips=OFFICE_IP)
        oc = Client(REMOTE_ADDR=OFFICE_IP)
        oc.force_login(self.staff)
        self.start(loc(NEAR), client=oc, when=at(10, 0))
        # Ends from home, on the wrong network, with no GPS at all: still ends, but is flagged.
        hc = Client(REMOTE_ADDR=OTHER_IP)
        hc.force_login(self.staff)
        self.end({"geo_error": "denied"}, client=hc, when=at(18, 0))
        rec = Attendance.objects.get()
        self.assertEqual(rec.end_time, at(18, 0))
        self.assertEqual(rec.end_ip, OTHER_IP)
        self.assertIsNone(rec.end_latitude)
        flagged = set(self.events(action="end", outcome="flagged").values_list("event_type", flat=True))
        self.assertEqual(flagged, {"location_unavailable", "ip_not_allowed"})

    def test_end_day_stores_location_when_given(self):
        configure(att_require_geofence=1)
        self.start(loc(NEAR), when=at(10, 0))
        self.end(loc(FAR), when=at(18, 0))
        rec = Attendance.objects.get()
        self.assertGreater(rec.end_distance_from_office, 1000)
        self.assertEqual(self.events(event_type="location_outside", action="end", outcome="flagged").count(), 1)

    def test_auto_end_keeps_working_and_has_no_location(self):
        self.start(when=at(10, 0))
        with Clock(at(19, 30)):
            attendance.auto_end_overdue()
        rec = Attendance.objects.get()
        self.assertTrue(rec.auto_ended)
        self.assertIsNone(rec.end_latitude)


# ============================================================ 10. admin configuration
class SettingsTests(Base):
    def setUp(self):
        super().setUp()
        self.ac = Client()
        self.ac.force_login(self.admin)

    def save(self, **over):
        data = {
            "section": "attendance", "att_office_lat": "25.135753", "att_office_lng": "75.823947",
            "att_geofence_radius_m": "200", "att_max_accuracy_m": "100", "att_office_ips": "203.0.113.10",
            "att_repeat_threshold": "5", "att_require_geofence": "on",
        }
        data.update(over)
        data = {k: v for k, v in data.items() if v is not None}
        return self.ac.post(reverse("settings_page"), data)

    def test_admin_saves_config_and_it_drives_verification(self):
        self.save()
        cfg = av.load_config()
        self.assertTrue(cfg.geofence_active)
        self.assertAlmostEqual(cfg.lat, 25.135753)
        self.assertEqual(cfg.radius, 200)
        self.assertTrue(AuditLog.objects.filter(action="settings_changed").exists())
        self.start(loc(FAR))
        self.assertFalse(Attendance.objects.exists())

    def test_staff_cannot_change_settings(self):
        self.c.post(reverse("settings_page"), {"section": "attendance", "att_require_geofence": "on", "att_office_lat": "1", "att_office_lng": "1"})
        self.assertFalse(av.load_config().require_geofence)

    def test_invalid_input_saves_nothing(self):
        for bad in (
            {"att_office_lat": "999"}, {"att_office_lng": ""}, {"att_geofence_radius_m": "5"},
            {"att_geofence_radius_m": "abc"}, {"att_max_accuracy_m": "1"}, {"att_office_ips": "not-an-ip"},
            {"att_office_ips": "0.0.0.0/0"}, {"att_require_office_ip": "on", "att_office_ips": ""},
            {"att_office_lat": "", "att_office_lng": ""},           # geofence required with no coordinates
        ):
            self.save(**bad)
        self.assertFalse(av.load_config().office_configured)

    def test_geofence_needs_coordinates_before_it_can_be_required(self):
        self.save(att_office_lat="", att_office_lng="")
        self.assertFalse(av.load_config().require_geofence)

    def test_settings_page_renders_and_shows_detected_ip(self):
        r = self.ac.get(reverse("settings_page") + "?section=attendance")
        self.assertContains(r, "Attendance Verification")
        self.assertContains(r, "127.0.0.1")

    def test_defaults_are_off_so_deploying_locks_nobody_out(self):
        cfg = av.load_config()
        self.assertFalse(cfg.any_active)
        self.start()
        self.assertTrue(Attendance.objects.filter(user=self.staff).exists())

    def test_office_coordinates_are_not_hardcoded_in_source(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent
        for path in list(root.glob("*.py")) + list((root.parent / "templates").rglob("*.html")):
            if path.name.startswith("tests_") or path.name.startswith("set_office"):
                continue
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("25.135753", text, path.name)
            self.assertNotIn("75.823947", text, path.name)


# ============================================================ admin override
class AdminOverrideTests(Base):
    def setUp(self):
        super().setUp()
        configure(att_require_geofence=1)
        self.ac = Client()
        self.ac.force_login(self.admin)

    def override(self, **over):
        data = {"staff": self.staff.pk, "reason": "Phone battery dead, verified in person"}
        data.update(over)
        with Clock(at(10, 40)):
            return self.ac.post(reverse("attendance_override"), data)

    def test_override_starts_the_day_with_server_time_and_full_audit(self):
        self.override()
        rec = Attendance.objects.get(user=self.staff)
        self.assertEqual(rec.start_time, at(10, 40))
        self.assertEqual((rec.start_verification, rec.override_by), ("admin_override", self.admin))
        self.assertIn("battery", rec.override_reason)
        ev = self.events(event_type="admin_override").get()
        self.assertEqual((ev.user, ev.outcome, ev.attendance), (self.staff, "override", rec))
        self.assertTrue(AuditLog.objects.filter(action="attendance_override", actor=self.admin).exists())
        # and the staff member now has normal access
        with Clock(at(10, 45)):
            self.assertEqual(self.c.get(reverse("my_leads")).status_code, 200)

    def test_override_requires_a_reason(self):
        self.override(reason="  ")
        self.assertFalse(Attendance.objects.exists())

    def test_override_cannot_backdate_or_double_start(self):
        self.override(work_date="2020-01-01", start_time="2020-01-01 09:00")
        self.assertEqual(Attendance.objects.get().work_date, datetime.date(2026, 9, 27))
        self.override()
        self.assertEqual(Attendance.objects.count(), 1)

    def test_override_after_working_hours_is_refused(self):
        with Clock(at(19, 30)):
            self.ac.post(reverse("attendance_override"), {"staff": self.staff.pk, "reason": "late arrival, approved"})
        self.assertFalse(Attendance.objects.exists())

    def test_staff_cannot_override_and_cannot_target_admins(self):
        with Clock(at(10)):
            self.c.post(reverse("attendance_override"), {"staff": self.staff.pk, "reason": "let me in please"})
        self.assertFalse(Attendance.objects.exists())
        self.override(staff=self.admin.pk)
        self.assertFalse(Attendance.objects.filter(user=self.admin).exists())


# ============================================================ evidence never leaves for n8n
class PrivacyTests(Base):
    def test_attendance_data_is_not_part_of_the_n8n_payload_code(self):
        import pathlib
        root = pathlib.Path(__file__).resolve().parent
        for name in ("n8n_integration.py", "api.py"):
            text = (root / name).read_text(encoding="utf-8").lower()
            for word in ("attendance", "start_latitude", "start_ip", "trusteddevice"):
                self.assertNotIn(word, text, f"{name} mentions {word}")


# ============================================================ concurrency (real threads)
class VerifiedConcurrencyTests(TransactionTestCase):
    def setUp(self):
        self.staff = User.objects.create_user("rahul", password="pw")

    def _race(self, fn, n=8):
        results, barrier = [], threading.Barrier(n)

        def worker():
            try:
                barrier.wait()
                for _ in range(20):
                    try:
                        results.append(fn())
                        break
                    except attendance.AttendanceError as exc:
                        results.append(exc)
                        break
                    except Exception as exc:  # noqa: BLE001 - SQLite may say "locked"; retry
                        if "locked" not in str(exc).lower():
                            results.append(exc)
                            break
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results

    def _evidence(self, action):
        return av.Evidence(action=action, ip="203.0.113.10", latitude=25.1358, longitude=75.824,
                           accuracy=12.0, distance=7.0, any_check_active=True)

    def test_concurrent_verified_starts_create_exactly_one_record(self):
        with Clock(at(10)):
            results = self._race(lambda: attendance.start_day(self.staff, evidence=self._evidence("start")))
        self.assertEqual(Attendance.objects.filter(user=self.staff).count(), 1)
        self.assertEqual(sum(isinstance(r, Attendance) for r in results), 1)
        self.assertTrue(all(isinstance(r, (Attendance, attendance.AlreadyStarted)) for r in results), results)
        self.assertEqual(Attendance.objects.get().start_verification, "verified")

    def test_concurrent_verified_ends_finalize_once_and_keep_first_evidence(self):
        with Clock(at(10)):
            attendance.start_day(self.staff)
        with Clock(at(15)):
            results = self._race(lambda: attendance.end_day(self.staff, evidence=self._evidence("end")))
        self.assertEqual(sum(isinstance(r, Attendance) for r in results), 1)
        rec = Attendance.objects.get()
        self.assertEqual((rec.end_time, rec.end_ip, rec.end_distance_from_office), (at(15), "203.0.113.10", 7.0))


# ============================================================ pure logic (no DB)
class PureLogicTests(TestCase):
    def test_haversine_known_distances(self):
        self.assertAlmostEqual(av.haversine_m(*OFFICE, *OFFICE), 0, places=3)
        # 0.001 deg latitude ~ 111.2 m
        self.assertAlmostEqual(av.haversine_m(25.0, 75.0, 25.001, 75.0), 111.2, delta=0.5)
        # symmetric
        self.assertAlmostEqual(av.haversine_m(*OFFICE, *FAR), av.haversine_m(*FAR, *OFFICE), places=6)

    def test_parse_allowlist(self):
        nets, bad = av.parse_allowlist("203.0.113.10, 192.0.2.0/24\n2001:db8::/48 junk 0.0.0.0/0 ::/0")
        self.assertEqual(len(nets), 3)
        self.assertEqual(set(bad), {"junk", "0.0.0.0/0", "::/0"})
        self.assertTrue(av.ip_allowed("192.0.2.9", nets))
        self.assertTrue(av.ip_allowed("203.0.113.10", nets))
        self.assertFalse(av.ip_allowed("203.0.113.11", nets))
        self.assertFalse(av.ip_allowed(None, nets))

    def test_parse_location(self):
        self.assertEqual(av.parse_location({})[3], "missing")
        self.assertEqual(av.parse_location({"latitude": "1", "longitude": "x"})[3], "invalid")
        self.assertEqual(av.parse_location({"latitude": "25.1", "longitude": "75.8", "accuracy": "20"}), (25.1, 75.8, 20.0, "ok"))
