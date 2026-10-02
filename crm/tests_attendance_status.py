"""
Corrected attendance status logic + retired standalone "Add New Lead".

Display statuses are ONLY PRESENT / HALF DAY / ABSENT / SUNDAY / HOLIDAY (plus NOT STARTED for an unfinished
"today"). Dates used: Mon 28/09/2026 ... Sun 27/09/2026 (a Sunday); Fri 25/12/2026 is a configured holiday.
"""
import csv
import datetime
import io

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import attendance, attendance_admin as aa
from .models import Attendance, Contact, Holiday, StaffProfile
from .tests_attendance import Clock, at

User = get_user_model()
D = datetime.date
H = datetime.timedelta


def mkrec(user, day, start_h, start_m, minutes):
    """A finished record whose status is derived the same way the real End Day does it."""
    start = at(start_h, start_m, day=day.day, month=day.month, year=day.year)
    dur = H(minutes=minutes)
    return Attendance.objects.create(
        user=user, work_date=day, start_time=start, end_time=start + dur, worked_duration=dur,
        attendance_status=attendance.compute_status(dur))


class StatusRuleTests(TestCase):
    def test_duration_thresholds(self):
        cs = attendance.compute_status
        self.assertEqual(cs(H(hours=8, minutes=30)), Attendance.STATUS_FULL_DAY)
        self.assertEqual(cs(H(hours=7)), Attendance.STATUS_FULL_DAY)          # THE bug: 7h used to be Half Day
        self.assertEqual(cs(H(hours=6, minutes=30)), Attendance.STATUS_FULL_DAY)
        self.assertEqual(cs(H(hours=6, minutes=29)), Attendance.STATUS_HALF_DAY)
        self.assertEqual(cs(H(hours=5, minutes=30)), Attendance.STATUS_HALF_DAY)
        self.assertEqual(cs(H(hours=5)), Attendance.STATUS_HALF_DAY)
        self.assertEqual(cs(H(hours=3, minutes=59)), Attendance.STATUS_SHORT_DAY)

    def test_thresholds_come_from_settings(self):
        self.assertEqual(attendance.settings.ATTENDANCE_FULL_DAY_MIN_MINUTES,
                         attendance.settings.ATTENDANCE_EXPECTED_FULL_DAY_MINUTES
                         - attendance.settings.ATTENDANCE_FULL_DAY_TOLERANCE_MINUTES)
        with override_settings(ATTENDANCE_FULL_DAY_MIN_MINUTES=480):
            self.assertEqual(attendance.compute_status(H(hours=7)), Attendance.STATUS_HALF_DAY)

    def test_display_mapping_and_priority(self):
        u = User.objects.create_user("s1", password="pw")
        mon, sun = D(2026, 9, 28), D(2026, 9, 27)
        full, half, short = mkrec(u, mon, 10, 0, 420), mkrec(u, D(2026, 9, 29), 10, 0, 330), mkrec(u, D(2026, 9, 30), 10, 0, 120)
        now = at(12, day=30, month=10)            # Fri-ish later date: every day above is in the past
        ds = lambda d, r=None, h=None: attendance.display_status(d, r, h, now=now)
        self.assertEqual(ds(mon, full), attendance.PRESENT)
        self.assertEqual(ds(D(2026, 9, 29), half), attendance.HALF_DAY)
        self.assertEqual(ds(D(2026, 9, 30), short), attendance.ABSENT)
        self.assertEqual(ds(D(2026, 9, 24)), attendance.ABSENT)                    # no Start Day, Thursday
        self.assertEqual(ds(sun), attendance.SUNDAY)                               # Sunday never absent
        self.assertEqual(ds(sun, full), attendance.SUNDAY)
        hol = Holiday.objects.create(name="Christmas", start_date=D(2026, 12, 25), end_date=D(2026, 12, 25), created_by=u)
        self.assertEqual(attendance.display_status(D(2026, 12, 25), None, hol, now=at(12, day=30, month=12)), attendance.HOLIDAY)
        # work done before a holiday was declared keeps its worked status (the note says so); only "no Start Day" becomes HOLIDAY
        self.assertEqual(attendance.display_status(D(2026, 12, 25), full, hol, now=at(12, day=30, month=12)), attendance.PRESENT)
        sunday_holiday = Holiday(name="x", start_date=sun, end_date=sun)
        self.assertEqual(attendance.display_status(sun, None, sunday_holiday, now=now), attendance.SUNDAY)  # Sunday first

    def test_today_is_not_absent_until_working_hours_end(self):
        today = D(2026, 9, 28)
        self.assertEqual(attendance.display_status(today, None, None, now=at(11, day=28)), attendance.NOT_STARTED_YET)
        self.assertEqual(attendance.display_status(today, None, None, now=at(19, 1, day=28)), attendance.ABSENT)
        self.assertEqual(attendance.display_status(D(2026, 9, 29), None, None, now=at(11, day=28)), attendance.NOT_STARTED_YET)


class EndToEndTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.u = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        StaffProfile.objects.filter(user=self.u).update(status="active")
        User.objects.filter(pk=self.u.pk).update(date_joined=at(9, day=1))
        self.u = User.objects.get(pk=self.u.pk)

    def day(self, start, end, day=28):
        with Clock(at(*start, day=day)):
            attendance.start_day(self.u)
        with Clock(at(*end, day=day)):
            return attendance.end_day(self.u)

    def test_start_end_seven_hours_is_present(self):
        rec = self.day((11, 0), (18, 0))                       # 7h, started an hour late
        self.assertEqual(rec.attendance_status, Attendance.STATUS_FULL_DAY)
        self.assertEqual(attendance.display_status(rec.work_date, rec, None, now=at(20, day=28)), attendance.PRESENT)

    def test_start_end_five_and_a_half_hours_is_half_day(self):
        rec = self.day((10, 0), (15, 30))
        self.assertEqual(attendance.display_status(rec.work_date, rec, None, now=at(20, day=28)), attendance.HALF_DAY)

    def test_normal_full_day_and_slightly_early_finish(self):
        rec = self.day((10, 0), (18, 30))
        self.assertEqual(attendance.display_status(rec.work_date, rec, None, now=at(20, day=28)), attendance.PRESENT)

    def test_staff_history_shows_every_day(self):
        Holiday.objects.create(name="Gandhi Jayanti", start_date=D(2026, 9, 25), end_date=D(2026, 9, 25), created_by=self.admin)
        mkrec(self.u, D(2026, 9, 28), 10, 0, 450)                                     # Mon present
        mkrec(self.u, D(2026, 9, 29), 10, 0, 330)                                     # Tue half day
        c = Client(); c.force_login(self.u)
        with Clock(at(20, day=30)):                                                   # Wed evening, Wed never started
            r = c.get(reverse("attendance"))
        self.assertEqual(r.status_code, 200)
        rows = {row["date"]: row["status"] for row in r.context["history"]}
        self.assertEqual(rows[D(2026, 9, 30)], "ABSENT")      # Wed: no Start Day
        self.assertEqual(rows[D(2026, 9, 29)], "HALF DAY")
        self.assertEqual(rows[D(2026, 9, 28)], "PRESENT")
        self.assertEqual(rows[D(2026, 9, 27)], "SUNDAY")
        self.assertEqual(rows[D(2026, 9, 26)], "ABSENT")      # Saturday, working day
        self.assertEqual(rows[D(2026, 9, 25)], "HOLIDAY")
        html = r.content.decode()
        for cls in ("att-status-present", "att-status-half", "att-status-absent", "att-status-sunday", "att-status-holiday"):
            self.assertIn(cls, html)
        self.assertEqual(Attendance.objects.filter(user=self.u).count(), 2)           # nothing was stored for empty days

    def test_admin_single_day_sunday_and_holiday_never_absent(self):
        c = Client(); c.force_login(self.admin)
        with Clock(at(20, day=28)):
            sun = c.get(reverse("staff_attendance"), {"date": "27/09/2026"})
            mon = c.get(reverse("staff_attendance"), {"date": "28/09/2026"})
        self.assertEqual([r.status_label for r in sun.context["rows"]], ["SUNDAY"])
        self.assertEqual([r.status_label for r in mon.context["rows"]], ["ABSENT"])
        self.assertIn("att-row-sunday", sun.content.decode())
        Holiday.objects.create(name="Christmas", start_date=D(2026, 12, 25), end_date=D(2026, 12, 25), created_by=self.admin)
        with Clock(at(20, day=28, month=12)):
            hol = c.get(reverse("staff_attendance"), {"date": "25/12/2026"})
        self.assertEqual([r.status_label for r in hol.context["rows"]], ["HOLIDAY"])

    def test_admin_range_includes_days_without_records_and_exports_them(self):
        mkrec(self.u, D(2026, 9, 28), 10, 0, 480)
        c = Client(); c.force_login(self.admin)
        qs = {"date": "26/09/2026", "date_to": "29/09/2026"}
        with Clock(at(20, day=30)):
            r = c.get(reverse("staff_attendance"), qs)
            ex = c.get(reverse("staff_attendance_export"), qs)
        got = {row.work_date: row.status_label for row in r.context["rows"]}
        self.assertEqual(got, {D(2026, 9, 26): "ABSENT", D(2026, 9, 27): "SUNDAY", D(2026, 9, 28): "PRESENT", D(2026, 9, 29): "ABSENT"})
        text = b"".join(ex.streaming_content).decode() if ex.streaming else ex.content.decode()
        self.assertIn("SUNDAY", text)
        self.assertIn("ABSENT", text)
        self.assertIn("PRESENT", text)

    def test_range_status_filter_and_today_cards(self):
        c = Client(); c.force_login(self.admin)
        with Clock(at(20, day=30)):
            r = c.get(reverse("staff_attendance"), {"date": "26/09/2026", "date_to": "29/09/2026", "status": "sunday"})
        self.assertEqual([x.work_date for x in r.context["rows"]], [D(2026, 9, 27)])
        with Clock(at(20, day=27)):                           # Sunday cards
            s = aa.today_summary()
        self.assertEqual((s["absent"], s["sunday"], s["not_started"]), (0, 1, 0))
        with Clock(at(20, day=29)):
            s = aa.today_summary()
        self.assertEqual((s["absent"], s["sunday"]), (1, 0))


@override_settings(ATTENDANCE_ENFORCED=False)
class AddNewLeadSidebarOnlyTests(TestCase):
    """"Add New Lead" is hidden ONLY in the admin left sidebar; everywhere else and the route are unchanged."""

    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.c = Client(); self.c.force_login(self.admin)

    def test_hidden_in_admin_sidebar(self):
        for name in ("dashboard", "my_leads", "all_leads", "contacts"):
            self.assertNotIn("nav-add-lead", self.c.get(reverse(name)).content.decode(), name)

    def test_kept_in_page_headers(self):
        for name in ("dashboard", "my_leads", "all_leads"):
            html = self.c.get(reverse(name)).content.decode()
            self.assertIn("+ Add New Lead", html, name)
            self.assertIn(reverse("lead_create"), html, name)

    def test_standalone_route_still_works(self):
        r = self.c.get(reverse("lead_create"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Add New Lead", r.content.decode())

    def test_convert_contact_page_still_opens(self):
        ct = Contact.objects.create(name="Asha", phone="9876543210")
        r = self.c.get(reverse("lead_create") + f"?from_contact={ct.pk}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Convert Contact to Lead", r.content.decode())
