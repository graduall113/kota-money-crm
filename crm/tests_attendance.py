"""Staff attendance: state machine, server-only timestamps, lockout, auto-end, concurrency."""
import datetime
import threading
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from . import attendance
from .models import Attendance

User = get_user_model()
IST = ZoneInfo("Asia/Kolkata")


def at(h, m=0, day=27, month=9, year=2026):
    return datetime.datetime(year, month, day, h, m, tzinfo=IST)


class Clock:
    """Patches attendance._now so tests control the server clock."""

    def __init__(self, when):
        self.when = when
        self._p = mock.patch.object(attendance, "_now", lambda: self.when)

    def __enter__(self):
        self._p.start()
        return self

    def __exit__(self, *a):
        self._p.stop()


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.other = User.objects.create_user("amit", password="pw")
        self.c = Client()
        self.c.force_login(self.staff)

    def start(self, when, user=None):
        with Clock(when):
            return attendance.start_day(user or self.staff)

    def end(self, when, user=None):
        with Clock(when):
            return attendance.end_day(user or self.staff)


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class StateMachineTests(Base):
    def test_start_records_server_time_and_work_date(self):
        rec = self.start(at(10, 5))
        self.assertEqual(rec.start_time, at(10, 5))
        self.assertEqual(rec.work_date, datetime.date(2026, 9, 27))
        self.assertIsNone(rec.end_time)
        self.assertEqual(rec.attendance_status, "in_progress")

    def test_work_date_uses_kolkata_not_utc(self):
        # 00:30 IST on the 28th is still the 27th in UTC — work_date must be the 28th.
        rec = self.start(at(0, 30, day=28))
        self.assertEqual(rec.work_date, datetime.date(2026, 9, 28))

    def test_duplicate_start_rejected(self):
        self.start(at(10, 0))
        with self.assertRaises(attendance.AlreadyStarted):
            self.start(at(10, 1))
        self.assertEqual(Attendance.objects.count(), 1)

    def test_end_day_and_duplicate_end(self):
        self.start(at(10, 5))
        rec = self.end(at(15, 30))
        self.assertEqual(rec.worked_duration, datetime.timedelta(hours=5, minutes=25))
        self.assertFalse(rec.auto_ended)
        with self.assertRaises(attendance.AlreadyEnded):
            self.end(at(15, 31))
        rec.refresh_from_db()
        self.assertEqual(rec.end_time, at(15, 30))  # untouched by the duplicate

    def test_no_restart_after_end(self):
        self.start(at(10, 0))
        self.end(at(12, 0))
        with self.assertRaises(attendance.AlreadyEnded):
            self.start(at(12, 5))

    def test_end_without_start(self):
        with self.assertRaises(attendance.NotStarted):
            self.end(at(12, 0))

    def test_cannot_start_after_work_hours(self):
        with self.assertRaises(attendance.OutsideWorkingHours):
            self.start(at(19, 0))
        self.assertEqual(Attendance.objects.count(), 0)

    def test_state_progression(self):
        with Clock(at(9, 0)):
            self.assertEqual(attendance.get_state(self.staff)[0], attendance.STATE_NOT_STARTED)
        self.start(at(10, 0))
        with Clock(at(11, 0)):
            self.assertEqual(attendance.get_state(self.staff)[0], attendance.STATE_ACTIVE)
        self.end(at(12, 0))
        with Clock(at(12, 1)):
            self.assertEqual(attendance.get_state(self.staff)[0], attendance.STATE_ENDED)

    def test_next_day_is_a_fresh_day(self):
        self.start(at(10, 0)); self.end(at(18, 0))
        with Clock(at(9, 0, day=28)):
            self.assertEqual(attendance.get_state(self.staff)[0], attendance.STATE_NOT_STARTED)
        self.start(at(10, 0, day=28))
        self.assertEqual(Attendance.objects.filter(user=self.staff).count(), 2)


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class CalculationTests(Base):
    def _run(self, s, e, day=27):
        self.start(s)
        return self.end(e)

    def test_full_day(self):
        self.assertEqual(self._run(at(10), at(19) - datetime.timedelta(seconds=1)).attendance_status, "full_day")

    def test_late_start_eight_hours_is_still_full_day(self):
        with Clock(at(19, 0)):  # 11:00 -> 19:00 via auto-end
            pass
        self.start(at(11, 0))
        self.assertEqual(attendance.auto_end_overdue(at(19, 1)), 1)
        rec = Attendance.objects.get()
        self.assertEqual(rec.worked_duration, datetime.timedelta(hours=8))
        self.assertEqual(rec.attendance_status, "full_day")

    def test_six_hours_is_half_day(self):
        self.assertEqual(self._run(at(10), at(16)).attendance_status, "half_day")

    def test_under_half_day_threshold_is_short_day(self):
        self.assertEqual(self._run(at(10), at(13, 59)).attendance_status, "short_day")

    @override_settings(ATTENDANCE_FULL_DAY_MIN_MINUTES=420, ATTENDANCE_HALF_DAY_MIN_MINUTES=120)
    def test_thresholds_are_configurable(self):
        self.assertEqual(self._run(at(10), at(17)).attendance_status, "full_day")

    def test_example_from_spec_ten_oh_five_to_seven(self):
        self.start(at(10, 5))
        attendance.auto_end_overdue(at(19, 0))
        rec = Attendance.objects.get()
        self.assertEqual(attendance.fmt_duration(rec.worked_duration), "08h 55m")
        self.assertEqual(attendance.fmt_clock(rec.end_time), "07:00 PM")


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class AutoEndTests(Base):
    def test_auto_end_all_active_at_seven(self):
        self.start(at(10, 5)); self.start(at(10, 30), self.other)
        with Clock(at(12)):
            self.assertEqual(attendance.auto_end_overdue(), 0)  # too early: nothing touched
        self.assertEqual(attendance.auto_end_overdue(at(19, 0)), 2)
        for r in Attendance.objects.all():
            self.assertTrue(r.auto_ended)
            self.assertEqual(r.end_time, at(19, 0))
            self.assertNotEqual(r.attendance_status, "in_progress")

    def test_auto_end_stamps_official_end_even_if_job_runs_late_and_is_idempotent(self):
        self.start(at(10, 0))
        self.assertEqual(attendance.auto_end_overdue(at(21, 40)), 1)
        self.assertEqual(Attendance.objects.get().end_time, at(19, 0))
        self.assertEqual(attendance.auto_end_overdue(at(21, 45)), 0)

    def test_auto_end_does_not_touch_manually_ended(self):
        self.start(at(10)); rec = self.end(at(15))
        attendance.auto_end_overdue(at(19, 5))
        rec.refresh_from_db()
        self.assertFalse(rec.auto_ended)
        self.assertEqual(rec.end_time, at(15))

    def test_missed_cron_is_healed_lazily_on_next_access(self):
        self.start(at(10))
        with Clock(at(19, 3)):  # cron never ran; staff opens the CRM at 19:03
            self.assertEqual(attendance.get_state(self.staff)[0], attendance.STATE_ENDED)
        rec = Attendance.objects.get()
        self.assertTrue(rec.auto_ended)
        self.assertEqual(rec.end_time, at(19, 0))

    def test_stale_record_from_previous_day_is_closed_at_that_days_seven(self):
        self.start(at(10, 0, day=26))
        with Clock(at(9, 0)):
            self.assertEqual(attendance.get_state(self.staff)[0], attendance.STATE_NOT_STARTED)
        self.assertEqual(Attendance.objects.get().end_time, at(19, 0, day=26))

    def test_management_command(self):
        self.start(at(10, 0, day=26))
        out = StringIO()
        call_command("auto_end_attendance", stdout=out)
        self.assertIn("Auto-ended 1", out.getvalue())
        self.assertTrue(Attendance.objects.get().auto_ended)


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class DatabaseConstraintTests(Base):
    def test_one_record_per_user_per_day_enforced_by_db(self):
        Attendance.objects.create(user=self.staff, work_date=datetime.date(2026, 9, 27), start_time=at(10))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Attendance.objects.create(user=self.staff, work_date=datetime.date(2026, 9, 27), start_time=at(11))

    def test_inconsistent_states_rejected_by_db(self):
        with self.assertRaises(IntegrityError), transaction.atomic():  # closed row without a final status
            Attendance.objects.create(user=self.staff, work_date=datetime.date(2026, 9, 1), start_time=at(10),
                                      end_time=at(12), worked_duration=datetime.timedelta(hours=2))
        with self.assertRaises(IntegrityError), transaction.atomic():  # ends before it starts
            Attendance.objects.create(user=self.staff, work_date=datetime.date(2026, 9, 2), start_time=at(12),
                                      end_time=at(10), worked_duration=datetime.timedelta(0), attendance_status="short_day")


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class HttpTests(Base):
    """Views, CSRF, permissions, lockout and direct-URL bypass."""

    LOCKED_URLS = ["dashboard", "my_leads", "contacts", "segment_list", "followups", "calling_dashboard", "lead_create"]

    def get(self, name, when, client=None, **kw):
        with Clock(when):
            return (client or self.c).get(reverse(name), **kw)

    def post(self, name, when, client=None, data=None):
        with Clock(when):
            return (client or self.c).post(reverse(name), data or {})

    def test_before_start_all_crm_pages_redirect_to_attendance(self):
        for name in self.LOCKED_URLS:
            r = self.get(name, at(9, 30))
            self.assertRedirects(r, reverse("attendance"), fetch_redirect_response=False, msg_prefix=name)

    def test_direct_url_bypass_blocked_for_detail_and_action_urls(self):
        for url in ["/leads/1/", "/leads/1/action/", "/contacts/1/", "/bulk/lead/", "/leads/export/", "/contacts/export/", "/jobs/1/", "/staff/", "/leads/add/"]:
            with Clock(at(11)):
                r = self.c.get(url)
            self.assertEqual(r.status_code, 302, url)
            self.assertEqual(r["Location"], reverse("attendance"), url)
        with Clock(at(11)):  # POST-based state-changing CRM URL also blocked
            r = self.c.post("/leads/1/action/", {"action": "x"})
        self.assertEqual(r["Location"], reverse("attendance"))

    def test_xhr_gets_403_json(self):
        with Clock(at(11)):
            r = self.c.get(reverse("my_leads"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["error"], "attendance_required")

    def test_allowed_before_start(self):
        for name in ["attendance", "profile", "settings_page"]:
            self.assertEqual(self.get(name, at(9, 30)).status_code, 200, name)
        self.assertEqual(self.get("logout", at(9, 30)).status_code, 302)
        self.assertEqual(Client().get(reverse("login")).status_code, 200)

    def test_start_then_active_access(self):
        r = self.post("attendance_start", at(10, 5))
        self.assertRedirects(r, reverse("dashboard"), fetch_redirect_response=False)
        for name in ["dashboard", "my_leads", "contacts", "lead_create"]:
            self.assertEqual(self.get(name, at(10, 6)).status_code, 200, name)

    def test_after_end_locked_again(self):
        self.post("attendance_start", at(10))
        self.post("attendance_end", at(15))
        for name in self.LOCKED_URLS:
            r = self.get(name, at(15, 1))
            self.assertRedirects(r, reverse("attendance"), fetch_redirect_response=False, msg_prefix=name)
        self.assertEqual(self.get("attendance", at(15, 1)).status_code, 200)

    def test_locked_after_auto_end_without_any_cron(self):
        self.post("attendance_start", at(10))
        self.assertEqual(self.get("my_leads", at(18, 59)).status_code, 200)
        self.assertEqual(self.get("my_leads", at(19, 0)).status_code, 302)
        self.assertTrue(Attendance.objects.get().auto_ended)

    def test_duplicate_http_start_and_end(self):
        self.post("attendance_start", at(10))
        r = self.post("attendance_start", at(10, 1))
        self.assertRedirects(r, reverse("attendance"), fetch_redirect_response=False)
        self.assertEqual(Attendance.objects.count(), 1)
        self.post("attendance_end", at(12)); self.post("attendance_end", at(12, 1))
        self.assertEqual(Attendance.objects.get().end_time, at(12))

    def test_get_not_allowed_on_state_changing_urls(self):
        for name in ["attendance_start", "attendance_end"]:
            self.assertEqual(self.get(name, at(10)).status_code, 405)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_csrf_enforced(self):
        strict = Client(enforce_csrf_checks=True)
        strict.force_login(self.staff)
        with Clock(at(10)):
            self.assertEqual(strict.post(reverse("attendance_start")).status_code, 403)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_login_required(self):
        with Clock(at(10)):
            r = Client().post(reverse("attendance_start"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r["Location"])
        self.assertEqual(Attendance.objects.count(), 0)

    def test_client_supplied_fields_are_ignored(self):
        evil = {"user": self.other.pk, "user_id": self.other.pk, "work_date": "2020-01-01", "start_time": "2020-01-01T03:00:00",
                "end_time": "2020-01-01T23:00:00", "worked_duration": "99:00:00", "attendance_status": "full_day"}
        self.post("attendance_start", at(10, 7), data=evil)
        rec = Attendance.objects.get()
        self.assertEqual((rec.user, rec.work_date, rec.start_time), (self.staff, datetime.date(2026, 9, 27), at(10, 7)))
        self.assertEqual(rec.attendance_status, "in_progress")
        self.post("attendance_end", at(11, 7), data=evil)
        rec.refresh_from_db()
        self.assertEqual((rec.end_time, rec.worked_duration), (at(11, 7), datetime.timedelta(hours=1)))
        self.assertFalse(Attendance.objects.filter(user=self.other).exists())

    def test_inactive_staff_account_cannot_start(self):
        self.staff.staff_profile.status = "inactive"; self.staff.staff_profile.save()
        self.post("attendance_start", at(10))
        self.assertEqual(Attendance.objects.count(), 0)

    # ---- admin ----------------------------------------------------------------
    def test_admin_never_locked(self):
        a = Client(); a.force_login(self.admin)
        for name in ["dashboard", "all_leads", "my_leads", "contacts", "staff_list", "settings_page", "lead_create"]:
            self.assertEqual(self.get(name, at(3), client=a).status_code, 200, name)
        with Clock(at(3)):
            self.assertEqual(a.get("/admin/").status_code, 200)

    def test_admin_role_via_profile_is_not_locked_and_cannot_clock_in(self):
        p = self.other.staff_profile; p.role = "admin"; p.save()
        a = Client(); a.force_login(self.other)
        self.assertEqual(self.get("my_leads", at(8), client=a).status_code, 200)
        self.post("attendance_start", at(10), client=a)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_api_endpoints_not_gated_by_attendance(self):
        with Clock(at(3)):  # token/key-authenticated APIs must keep working for n8n / the Android app
            r = Client().post("/api/leads/create/", {}, content_type="application/json")
        self.assertNotEqual(r.get("Location"), reverse("attendance"))

    # ---- UI -------------------------------------------------------------------
    def test_sidebar_states_and_add_lead_replaced_for_staff(self):
        html = self.get("attendance", at(9, 30)).content.decode()
        self.assertIn("Start Day", html)
        self.assertNotIn('class="nav-add-lead', html)
        self.post("attendance_start", at(10, 5))
        html = self.get("dashboard", at(12, 36)).content.decode()
        for s in ["Working", "Started: 10:05 AM", "02h 31m", "End Day"]:
            self.assertIn(s, html)
        self.assertIn("+ Add New Lead", html)  # functionality kept: reachable from the page header
        self.post("attendance_end", at(19, 0))
        html = self.get("attendance", at(19, 1)).content.decode()
        for s in ["Day Ended", "Started: 10:05 AM", "Ended: 07:00 PM", "Duration: 08h 55m", "Status: PRESENT"]:
            self.assertIn(s, html)

    def test_admin_sidebar_hides_add_lead_but_page_header_keeps_it(self):
        a = Client(); a.force_login(self.admin)
        html = self.get("dashboard", at(9), client=a).content.decode()
        self.assertNotIn("nav-add-lead", html)          # admin left-sidebar button hidden
        self.assertIn("+ Add New Lead", html)           # dashboard header button preserved
        self.assertNotIn("attWidget", html)
        self.assertNotIn("attWidget", html)

    def test_mobile_responsive_rules_present(self):
        from django.contrib.staticfiles import finders
        css = open(finders.find("css/style.css")).read()
        self.assertIn(".att-widget", css)
        self.assertRegex(css, r"@media \(max-width: 900px\) \{\s*\.att-widget")
        self.assertRegex(css, r"@media \(max-width: 560px\) \{\s*\.att-panel")
        html = self.get("attendance", at(9, 30)).content.decode()
        self.assertIn('name="viewport"', html)


class ConcurrencyTests(TransactionTestCase):
    """Real threads/connections. On PostgreSQL this exercises the row locks; on SQLite the DB serialises writers."""

    def setUp(self):
        self.staff = User.objects.create_user("rahul", password="pw")

    def _race(self, fn, n=8):
        results, barrier = [], threading.Barrier(n)

        def worker():
            try:
                barrier.wait()
                for _ in range(20):  # SQLite may briefly report "locked"; retry
                    try:
                        results.append(fn())
                        break
                    except attendance.AttendanceError as exc:
                        results.append(exc)
                        break
                    except Exception as exc:  # noqa: BLE001
                        if "locked" not in str(exc).lower():
                            results.append(exc)
                            break
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(n)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        return results

    def test_concurrent_start_creates_exactly_one_record(self):
        with Clock(at(10)):
            results = self._race(lambda: attendance.start_day(self.staff))
        self.assertEqual(Attendance.objects.filter(user=self.staff).count(), 1)
        self.assertEqual(sum(isinstance(r, Attendance) for r in results), 1)
        self.assertTrue(all(isinstance(r, (Attendance, attendance.AlreadyStarted)) for r in results), results)

    def test_concurrent_end_finalizes_exactly_once(self):
        with Clock(at(10)):
            attendance.start_day(self.staff)
        with Clock(at(15)):
            results = self._race(lambda: attendance.end_day(self.staff))
        self.assertEqual(sum(isinstance(r, Attendance) for r in results), 1)
        rec = Attendance.objects.get()
        self.assertEqual((rec.end_time, rec.worked_duration), (at(15), datetime.timedelta(hours=5)))


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class KillSwitchTests(Base):
    @override_settings(ATTENDANCE_ENFORCED=False)
    def test_switch_off_never_locks_staff(self):
        with Clock(at(3)):
            self.assertEqual(self.c.get(reverse("my_leads")).status_code, 200)
