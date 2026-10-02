"""
Holiday integration with the EXISTING attendance system.

  1. single-day holiday                 5. HOLIDAY shown instead of Absent
  2. date-range holiday                 6. holiday range displayed correctly
  3. Start Day blocked                  7. holiday removed -> normal rules return
  4. direct POST blocked                8. holiday declared after attendance started

The clock is frozen with tests_attendance.Clock; "today" is 27/09/2026 unless stated.
"""
import csv
import datetime
import io

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import override_settings, Client, TestCase
from django.urls import reverse

from . import attendance
from .models import Attendance, Holiday, StaffProfile
from .tests_attendance import Clock, at

User = get_user_model()
D = datetime.date


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = self.mk("rahul", "Rahul")
        self.sonia = self.mk("sonia", "Sonia")
        self.admin_c = Client()
        self.admin_c.force_login(self.admin)
        self.staff_c = Client()
        self.staff_c.force_login(self.sonia)

    def mk(self, username, first):
        u = User.objects.create_user(username, password="pw", first_name=first)
        StaffProfile.objects.filter(user=u).update(status="active")
        User.objects.filter(pk=u.pk).update(date_joined=at(9, day=1))
        return User.objects.get(pk=u.pk)

    def hol(self, name="Diwali Holiday", start=D(2026, 10, 20), end=D(2026, 10, 27), active=True):
        return Holiday.objects.create(name=name, start_date=start, end_date=end, is_active=active, created_by=self.admin)

    def start_service(self, when, user=None):
        with Clock(when):
            return attendance.start_day(user or self.sonia)

    def post_start(self, when, client=None, **data):
        with Clock(when):
            return (client or self.staff_c).post(reverse("attendance_start"), data)

    def get(self, name, when, client=None, **params):
        with Clock(when):
            return (client or self.staff_c).get(reverse(name), params)

    def dash(self, when, **params):
        with Clock(when):
            return self.admin_c.get(reverse("staff_attendance"), params)

    @staticmethod
    def msgs(resp):
        return " | ".join(str(m) for m in get_messages(resp.wsgi_request))

    @staticmethod
    def rows(resp):
        return {r.user.username: r for r in resp.context["rows"]}


# ==================================================================== 1. single-day holiday
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class SingleDayHolidayTests(Base):
    def test_start_is_refused_on_the_holiday_only(self):
        self.hol("Founders Day", D(2026, 9, 27), D(2026, 9, 27))
        with self.assertRaises(attendance.HolidayBlocked) as ctx:
            self.start_service(at(10))
        self.assertIn("Today is a holiday", ctx.exception.message)
        self.assertIn("Founders Day", ctx.exception.message)
        self.assertEqual(Attendance.objects.count(), 0)
        # the next day is a normal working day
        rec = self.start_service(at(10, day=28))
        self.assertEqual(rec.work_date, D(2026, 9, 28))

    def test_single_day_is_shown_as_one_date_not_a_range(self):
        self.hol("Founders Day", D(2026, 9, 27), D(2026, 9, 27))
        resp = self.get("attendance", at(10))
        self.assertContains(resp, "Today is a holiday")
        self.assertContains(resp, "Founders Day")
        self.assertContains(resp, "27/09/2026")
        self.assertNotContains(resp, "27/09/2026 – 27/09/2026")

    def test_inactive_holiday_does_not_block(self):
        self.hol("Founders Day", D(2026, 9, 27), D(2026, 9, 27), active=False)
        self.assertEqual(self.start_service(at(10)).work_date, D(2026, 9, 27))


# ==================================================================== 2. date-range holiday
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class RangeHolidayTests(Base):
    def test_every_day_of_the_range_is_blocked_both_ends_included(self):
        self.hol()  # 20/10/2026 - 27/10/2026
        for day in (20, 21, 24, 26, 27):
            with self.assertRaises(attendance.HolidayBlocked, msg=f"{day}/10"):
                self.start_service(at(10, day=day, month=10))
        self.assertEqual(Attendance.objects.count(), 0)

    def test_day_before_and_day_after_are_working_days(self):
        self.hol()
        self.assertEqual(self.start_service(at(10, day=19, month=10)).work_date, D(2026, 10, 19))
        self.assertEqual(self.start_service(at(10, day=28, month=10), user=self.rahul).work_date, D(2026, 10, 28))

    def test_overlapping_holidays_still_block(self):
        self.hol("Diwali Holiday")
        self.hol("Bhai Dooj", D(2026, 10, 26), D(2026, 10, 29))
        with self.assertRaises(attendance.HolidayBlocked):
            self.start_service(at(10, day=28, month=10))


# ==================================================================== 3. Start Day blocked (UI + view)
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class StartDayBlockedTests(Base):
    def setUp(self):
        super().setUp()
        self.hol()
        self.when = at(10, day=22, month=10)

    def test_attendance_page_shows_holiday_card_and_no_start_form(self):
        resp = self.get("attendance", self.when)
        self.assertContains(resp, "Today is a holiday")
        self.assertContains(resp, "Diwali Holiday")
        self.assertNotContains(resp, f'action="{reverse("attendance_start")}"')  # page body AND sidebar widget
        self.assertNotContains(resp, "Ready to begin?")

    def test_start_day_post_is_rejected_with_a_message_and_creates_nothing(self):
        resp = self.post_start(self.when)
        self.assertRedirects(resp, reverse("attendance"), fetch_redirect_response=False)
        self.assertIn("Today is a holiday", self.msgs(resp))
        self.assertEqual(Attendance.objects.count(), 0)

    def test_staff_stay_locked_out_of_the_crm_with_a_holiday_message(self):
        with Clock(self.when):
            resp = self.staff_c.get(reverse("my_leads"), follow=True)
        self.assertRedirects(resp, reverse("attendance"))
        self.assertContains(resp, "Today is a holiday")


# ==================================================================== 4. direct POST blocked
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class DirectPostBlockedTests(Base):
    def setUp(self):
        super().setUp()
        self.hol()
        self.when = at(10, day=22, month=10)

    def test_forged_fields_cannot_bypass_the_holiday(self):
        resp = self.post_start(self.when, work_date="2026-10-19", user=self.sonia.pk, holiday="0",
                               start_time="2026-10-19T10:00", override="1", latitude="26.9", longitude="75.8")
        self.assertRedirects(resp, reverse("attendance"), fetch_redirect_response=False)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_ajax_and_json_style_posts_are_blocked_too(self):
        with Clock(self.when):
            resp = self.staff_c.post(reverse("attendance_start"), HTTP_X_REQUESTED_WITH="XMLHttpRequest",
                                     HTTP_ACCEPT="application/json")
        self.assertEqual(Attendance.objects.count(), 0)
        self.assertNotEqual(resp.status_code, 201)

    def test_repeated_posts_never_create_a_record(self):
        for _ in range(8):
            self.post_start(self.when)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_service_layer_is_the_enforcement_point_even_if_the_view_check_is_skipped(self):
        # start_day() is what every path calls (view, admin override): it must refuse by itself.
        with self.assertRaises(attendance.HolidayBlocked):
            self.start_service(self.when)

    def test_admin_override_cannot_start_a_day_on_a_holiday_either(self):
        with Clock(self.when):
            resp = self.admin_c.post(reverse("attendance_override"), {"staff": self.sonia.pk, "reason": "phone broken"})
        self.assertRedirects(resp, reverse("attendance_events"), fetch_redirect_response=False)
        self.assertIn("Today is a holiday", self.msgs(resp))
        self.assertEqual(Attendance.objects.count(), 0)

    def test_get_is_not_a_way_in(self):
        with Clock(self.when):
            resp = self.staff_c.get(reverse("attendance_start"))
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(Attendance.objects.count(), 0)


# ==================================================================== 5. HOLIDAY instead of Absent
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class HolidayInsteadOfAbsentTests(Base):
    def test_past_holiday_is_holiday_not_absent(self):
        self.hol("Mid-term break", D(2026, 9, 25), D(2026, 9, 26))
        resp = self.dash(at(12), date="25/09/2026")
        row = self.rows(resp)["sonia"]
        self.assertEqual(row.status, "holiday")
        self.assertEqual(row.status_label, "HOLIDAY")
        self.assertContains(resp, "Mid-term break")
        self.assertNotContains(resp, 'att-status-badge absent')

    def test_same_past_day_without_a_holiday_is_still_absent(self):
        resp = self.dash(at(12), date="25/09/2026")
        self.assertEqual(self.rows(resp)["sonia"].status, "absent")

    def test_today_holiday_is_holiday_not_not_started(self):
        self.hol("Founders Day", D(2026, 9, 27), D(2026, 9, 27))
        resp = self.dash(at(12))
        self.assertEqual(self.rows(resp)["sonia"].status, "holiday")
        self.assertEqual(resp.context["summary"]["not_started"], 0)
        self.assertEqual(resp.context["summary"]["on_holiday"], 2)
        self.assertContains(resp, "On Holiday")

    def test_status_filters(self):
        self.hol("Mid-term break", D(2026, 9, 25), D(2026, 9, 26))
        by_status = lambda s: sorted(self.rows(self.dash(at(12), date="25/09/2026", status=s)))  # noqa: E731
        self.assertEqual(by_status("holiday"), ["rahul", "sonia"])
        self.assertEqual(by_status("absent"), [])
        self.assertEqual(by_status("not_started"), [])

    def test_export_says_holiday(self):
        self.hol("Mid-term break", D(2026, 9, 25), D(2026, 9, 26))
        with Clock(at(12)):
            resp = self.admin_c.get(reverse("staff_attendance_export"), {"date": "25/09/2026"})
        rows = list(csv.reader(io.StringIO(b"".join(resp.streaming_content).decode("utf-8-sig"))))
        self.assertEqual({r[5] for r in rows[1:]}, {"HOLIDAY"})

    def test_staff_history_lists_the_holiday_not_an_absence(self):
        self.hol("Mid-term break", D(2026, 9, 25), D(2026, 9, 26))
        resp = self.get("attendance", at(12))
        statuses = {r["date"]: r["status"] for r in resp.context["history"]}
        self.assertEqual(statuses[D(2026, 9, 25)], "HOLIDAY")
        self.assertEqual(statuses[D(2026, 9, 26)], "HOLIDAY")


# ==================================================================== 6. range displayed correctly
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class RangeDisplayTests(Base):
    RANGE = "20/10/2026 – 27/10/2026"

    def test_staff_card_shows_name_and_full_range(self):
        self.hol()
        resp = self.get("attendance", at(10, day=23, month=10))
        self.assertContains(resp, "Diwali Holiday")
        self.assertContains(resp, self.RANGE)
        self.assertContains(resp, "Holiday")

    def test_admin_row_shows_holiday_and_range(self):
        self.hol()
        resp = self.dash(at(12, day=23, month=10), date="23/10/2026")
        row = self.rows(resp)["sonia"]
        self.assertEqual(row.status, "holiday")
        self.assertEqual(row.holiday_name, "Diwali Holiday")
        self.assertEqual(row.holiday_period, self.RANGE)
        self.assertContains(resp, self.RANGE)

    def test_range_helper_formats(self):
        self.assertEqual(attendance.holiday_label(self.hol()), self.RANGE)
        single = Holiday(name="x", start_date=D(2026, 9, 27), end_date=D(2026, 9, 27))
        self.assertEqual(attendance.holiday_label(single), "27/09/2026")


# ==================================================================== 7. holiday removed
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class HolidayRemovedTests(Base):
    def setUp(self):
        super().setUp()
        self.h = self.hol("Founders Day", D(2026, 9, 27), D(2026, 9, 27))

    def test_deactivating_restores_start_and_absent_rules(self):
        with self.assertRaises(attendance.HolidayBlocked):
            self.start_service(at(10))
        with Clock(at(12)):
            self.admin_c.post(reverse("holiday_toggle", args=[self.h.pk]))
        self.h.refresh_from_db()
        self.assertFalse(self.h.is_active)
        self.assertEqual(self.start_service(at(10)).work_date, D(2026, 9, 27))
        self.assertEqual(self.rows(self.dash(at(12), date="26/09/2026"))["sonia"].status, "absent")

    def test_deleting_restores_normal_status_and_the_start_button(self):
        with Clock(at(12)):
            self.admin_c.post(reverse("holiday_delete", args=[self.h.pk]))
        self.assertFalse(Holiday.objects.exists())
        self.assertEqual(self.rows(self.dash(at(12)))["sonia"].status, "not_started")
        resp = self.get("attendance", at(12))
        self.assertContains(resp, "Ready to begin?")
        self.assertNotContains(resp, "Today is a holiday")
        self.assertRedirects(self.post_start(at(12)), reverse("dashboard"), fetch_redirect_response=False)
        self.assertEqual(Attendance.objects.filter(user=self.sonia).count(), 1)

    def test_reactivating_blocks_again(self):
        with Clock(at(12)):
            self.admin_c.post(reverse("holiday_toggle", args=[self.h.pk]))
            self.admin_c.post(reverse("holiday_toggle", args=[self.h.pk]))
        with self.assertRaises(attendance.HolidayBlocked):
            self.start_service(at(10))


# ==================================================================== 8. holiday declared after attendance started
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class DeclaredAfterStartTests(Base):
    def setUp(self):
        super().setUp()
        self.rec = self.start_service(at(10), user=self.rahul)     # rahul is already working
        self.rahul_c = Client()
        self.rahul_c.force_login(self.rahul)
        self.h = self.hol("Surprise closure", D(2026, 9, 27), D(2026, 9, 27))   # declared later, same date

    def test_existing_record_is_preserved_untouched(self):
        self.assertEqual(Attendance.objects.count(), 1)
        after = Attendance.objects.get(pk=self.rec.pk)
        self.assertEqual(after.start_time, at(10))
        self.assertIsNone(after.end_time)

    def test_staff_keep_working_and_can_end_the_day(self):
        with Clock(at(12)):
            self.assertEqual(self.rahul_c.get(reverse("my_leads")).status_code, 200)   # not locked out
        with Clock(at(15)):
            self.rahul_c.post(reverse("attendance_end"))
        done = Attendance.objects.get(pk=self.rec.pk)
        self.assertIsNotNone(done.end_time)
        self.assertEqual(done.worked_duration, datetime.timedelta(hours=5))

    def test_staff_page_says_worked_holiday_declared_later(self):
        resp = self.get("attendance", at(12), client=self.rahul_c)
        self.assertContains(resp, "Worked — Holiday Declared Later")
        self.assertContains(resp, "Working")            # still shows the live working state
        self.assertContains(resp, "End Day")

    def test_admin_sees_worked_status_plus_note_not_holiday_or_absent(self):
        resp = self.dash(at(12))
        rahul, sonia = self.rows(resp)["rahul"], self.rows(resp)["sonia"]
        self.assertEqual(rahul.status, "present")   # worked before the holiday was declared
        self.assertEqual(rahul.holiday_note, "Worked — Holiday Declared Later")
        self.assertContains(resp, "Worked — Holiday Declared Later")
        self.assertEqual(sonia.status, "holiday")
        self.assertEqual(resp.context["summary"]["on_holiday"], 1)   # only sonia: rahul has a record

    def test_admin_detail_and_export_carry_the_note(self):
        with Clock(at(12)):
            detail = self.admin_c.get(reverse("staff_attendance_detail", args=[self.rec.pk]))
            exp = self.admin_c.get(reverse("staff_attendance_export"))
        self.assertContains(detail, "Worked — Holiday Declared Later")
        rows = list(csv.reader(io.StringIO(b"".join(exp.streaming_content).decode("utf-8-sig"))))
        self.assertIn("PRESENT (Worked — Holiday Declared Later)", {r[5] for r in rows[1:]})

    def test_removing_the_holiday_returns_to_normal_rules(self):
        self.h.delete()
        self.assertEqual(Attendance.objects.get(pk=self.rec.pk).start_time, at(10))
        resp = self.dash(at(12))
        self.assertEqual(self.rows(resp)["rahul"].holiday_note, "")
        self.assertNotContains(resp, "Worked — Holiday Declared Later")
        self.assertEqual(self.rows(resp)["sonia"].status, "not_started")

    def test_a_second_start_is_still_a_duplicate_not_a_holiday_error(self):
        resp = self.post_start(at(12), client=self.rahul_c)
        self.assertIn("already started", self.msgs(resp))
        self.assertEqual(Attendance.objects.count(), 1)
