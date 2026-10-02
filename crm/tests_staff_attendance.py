"""
Feature 6: Staff Attendance admin dashboard.

Permissions, today cards, table statuses, every filter, search, pagination
(incl. "queries don't grow with the data"), detail page, corrections + audit
trail, export, indexes/migration, and static responsiveness checks.
"""
import csv
import datetime
import io
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.management import call_command
from django.db import connection
from django.test import override_settings, Client, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from . import attendance, attendance_admin as aa
from .models import Attendance, AttendanceCorrection, AttendanceEvent, AuditLog, StaffProfile, TrustedDevice
from .tests_attendance import Clock, at

User = get_user_model()
TODAY = datetime.date(2026, 9, 27)
TODAY_STR = "27/09/2026"


@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class Base(TestCase):
    """
    Fixture for "today" (27 Sep 2026, clock frozen at 18:45 IST - before the 19:00 auto-end):
      rahul   working (started 10:05)
      amit    full day 10:00-18:30 (8.5h), start verification = verified
      neha    half day 10:00-15:00
      vikram  full day, auto ended at 19:00
      kiran   short day 10:00-12:00  -> "Ended"
      sonia   no record (Not Started)
    plus an inactive staff member and an admin, who must never be counted.
    """

    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)  # signal -> admin profile
        self.rahul = self.mk("rahul", "Rahul")
        self.amit = self.mk("amit", "Amit", code="KM-104")
        self.neha = self.mk("neha", "Neha", last="Sharma")
        self.vikram = self.mk("vikram", "Vikram")
        self.kiran = self.mk("kiran", "Kiran")
        self.sonia = self.mk("sonia", "Sonia")
        self.inactive = self.mk("olga", "Olga", active=False)

        self.r_rahul = self.rec(self.rahul, at(10, 5))
        self.r_amit = self.rec(self.amit, at(10, 0), at(18, 30))
        self.r_neha = self.rec(self.neha, at(10, 0), at(15, 0))
        self.r_vikram = self.rec(self.vikram, at(10, 0), at(19, 0), auto=True)
        self.r_kiran = self.rec(self.kiran, at(10, 0), at(18, 45))

        self.c = Client()
        self.c.force_login(self.admin)

    # ---- builders
    def mk(self, username, first="", last="", code="", active=True):
        u = User.objects.create_user(username, password="pw", first_name=first, last_name=last)
        StaffProfile.objects.filter(user=u).update(reference_code=code, status="active" if active else "inactive")
        User.objects.filter(pk=u.pk).update(date_joined=at(9, day=1))  # joined well before "today"
        return User.objects.get(pk=u.pk)

    def rec(self, user, start, end=None, day=27, auto=False, verification=Attendance.VERIFY_VERIFIED, **extra):
        kw = dict(user=user, work_date=datetime.date(2026, 9, day), start_time=start, auto_ended=auto,
                  start_verification=verification, **extra)
        if end is not None:
            dur = end - start
            kw.update(end_time=end, worked_duration=dur, attendance_status=attendance.compute_status(dur))
        return Attendance.objects.create(**kw)

    def event(self, user, when=None, etype="location_outside", review="open", action="start"):
        return AttendanceEvent.objects.create(
            user=user, user_name=user.username, action=action, event_type=etype,
            outcome="flagged", created_at=when or at(10, 4), review_status=review,
        )

    def get(self, name="staff_attendance", client=None, when=None, **params):
        with Clock(when or at(18, 45)):
            return (client or self.c).get(reverse(name), params)

    def rows(self, resp):
        return {r.user.username: r for r in resp.context["rows"]}

    def names(self, resp):
        return sorted(r.user.username for r in resp.context["rows"])

    def last_message(self, response):
        return " ".join(str(m) for m in get_messages(response.wsgi_request))

    def correct(self, rec, when=None, **data):
        with Clock(when or at(18, 45)):
            return self.c.post(reverse("staff_attendance_correct", args=[rec.pk]), data)


ALL_STAFF = ["amit", "kiran", "neha", "rahul", "sonia", "vikram"]


# ==================================================================== permissions
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class PermissionTests(Base):
    def test_admin_can_open_dashboard_detail_and_export(self):
        self.assertEqual(self.get().status_code, 200)
        self.assertEqual(self.get("staff_attendance_export").status_code, 200)
        with Clock(at(18, 45)):
            self.assertEqual(self.c.get(reverse("staff_attendance_detail", args=[self.r_amit.pk])).status_code, 200)

    def test_anonymous_is_sent_to_login(self):
        resp = Client().get(reverse("staff_attendance"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])

    def test_staff_with_a_started_day_gets_403_on_every_endpoint(self):
        staff = Client()
        staff.force_login(self.rahul)  # rahul is mid-shift, so the attendance gate lets him through -> the admin check must stop him
        before = Attendance.objects.get(pk=self.r_amit.pk)
        for name, args in (("staff_attendance", []), ("staff_attendance_export", []),
                           ("staff_attendance_detail", [self.r_amit.pk])):
            with Clock(at(18, 45)):
                self.assertEqual(staff.get(reverse(name, args=args)).status_code, 403, name)
        with Clock(at(18, 45)):
            resp = staff.post(reverse("staff_attendance_correct", args=[self.r_amit.pk]),
                              {"end_time": "12:00", "reason": "I would like to leave early"})
        self.assertEqual(resp.status_code, 403)
        after = Attendance.objects.get(pk=self.r_amit.pk)
        self.assertEqual((before.end_time, before.attendance_status), (after.end_time, after.attendance_status))
        self.assertFalse(AttendanceCorrection.objects.exists())

    def test_staff_who_has_not_started_is_also_denied_and_sees_nothing(self):
        staff = Client()
        staff.force_login(self.sonia)
        with Clock(at(18, 45)):
            resp = staff.get(reverse("staff_attendance"))
        self.assertNotEqual(resp.status_code, 200)
        self.assertNotIn(b"Rahul", resp.content)

    def test_staff_cannot_use_export_to_get_other_staff_data(self):
        staff = Client()
        staff.force_login(self.rahul)
        with Clock(at(18, 45)):
            resp = staff.get(reverse("staff_attendance_export"), {"format": "xlsx"})
        self.assertEqual(resp.status_code, 403)

    def test_sidebar_link_only_for_admin_and_own_page_shows_only_own_data(self):
        self.assertContains(self.get(), reverse("staff_attendance"))
        staff = Client()
        staff.force_login(self.rahul)
        with Clock(at(18, 45)):
            resp = staff.get(reverse("attendance"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, reverse("staff_attendance"))
        self.assertNotContains(resp, "Amit")

    def test_write_endpoints_reject_get_and_dashboard_rejects_post(self):
        with Clock(at(18, 45)):
            self.assertEqual(self.c.get(reverse("staff_attendance_correct", args=[self.r_amit.pk])).status_code, 405)
            self.assertEqual(self.c.post(reverse("staff_attendance")).status_code, 405)


# ==================================================================== dashboard cards
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class DashboardCountTests(Base):
    def test_today_counts(self):
        self.event(self.rahul)                                   # flagged, has a record
        self.event(self.sonia, etype="location_outside")         # rejected attempt, still no record
        self.event(self.amit, review="dismissed")                # dismissed -> not an anomaly
        self.event(self.neha, when=at(10, 0, day=26))            # yesterday -> not today's anomaly
        s = self.get().context["summary"]
        self.assertEqual(
            {k: s[k] for k in ("total", "started", "not_started", "working", "ended", "present", "half_day", "absent", "auto_ended", "anomalies")},
            # present = 3 full days + 1 still working
            {"total": 6, "started": 5, "not_started": 1, "working": 1, "ended": 4,
             "present": 4, "half_day": 1, "absent": 0, "auto_ended": 1, "anomalies": 2},
        )

    def test_started_plus_not_started_equals_total(self):
        with Clock(at(18, 45)):
            s = aa.today_summary(TODAY)
        self.assertEqual(s["started"] + s["not_started"], s["total"])

    def test_admins_and_inactive_staff_are_never_counted(self):
        self.rec(self.inactive, at(10, 0), at(19, 0), day=27)  # a deactivated user's record must not inflate the cards
        with Clock(at(18, 45)):
            s = aa.today_summary()
        self.assertEqual(s["total"], 6)
        self.assertEqual(s["started"], 5)

    def test_cards_render_and_link_to_filtered_table(self):
        resp = self.get()
        for label in ("Total Staff", "Started", "Not Started", "Currently Working", "Ended", "Present", "Half Day", "Absent",
                      "Auto Ended", "Verification Anomalies"):
            self.assertContains(resp, label)
        self.assertContains(resp, "status=working")
        self.assertContains(resp, "anomaly=yes")

    def test_cards_ignore_table_filters(self):
        s = self.get(status="half_day", q="neha").context["summary"]
        self.assertEqual(s["total"], 6)


# ==================================================================== table + statuses
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class TableTests(Base):
    def test_every_staff_member_is_listed_with_the_right_status(self):
        rows = self.rows(self.get())
        self.assertEqual(sorted(rows), ALL_STAFF)  # not the admin, not the inactive one
        with Clock(at(18, 45)):   # Row.status is derived lazily from "now"
            got = {u: r.status for u, r in rows.items()}
        self.assertEqual(got, {
            "rahul": "present", "amit": "present", "neha": "half_day", "vikram": "present",
            "kiran": "present", "sonia": "not_started",
        })
        self.assertTrue(rows["vikram"].auto_ended)
        self.assertFalse(rows["amit"].auto_ended)

    def test_table_has_all_requested_columns(self):
        resp = self.get()
        for col in ("Staff", "Date", "Start Time", "End Time", "Worked Hours", "Status", "Verification",
                    "Device", "IP", "Auto Ended", "Anomaly", "Actions"):
            self.assertContains(resp, f"<th>{col}</th>", html=False)

    def test_past_day_without_record_is_absent_and_future_day_is_empty(self):
        resp = self.get(date="26/09/2026")
        self.assertEqual({r.status for r in resp.context["rows"]}, {"absent"})
        self.assertEqual(self.names(resp), ALL_STAFF)
        self.assertEqual(self.names(self.get(date="28/09/2026")), [])

    def test_staff_who_joined_later_are_not_absent_before_they_joined(self):
        late = self.mk("latecomer", "Late")
        User.objects.filter(pk=late.pk).update(date_joined=at(9, day=27))
        self.assertNotIn("latecomer", self.names(self.get(date="26/09/2026")))
        self.assertIn("latecomer", self.names(self.get(date="27/09/2026")))

    def test_deactivated_staff_with_a_record_still_appear_on_that_day(self):
        self.rec(self.inactive, at(10, 0), at(19, 0), day=26)
        self.assertIn("olga", self.names(self.get(date="26/09/2026")))
        self.assertNotIn("olga", self.names(self.get(date="27/09/2026")))

    def test_worked_hours_running_and_final(self):
        rows = self.rows(self.get())
        self.assertEqual(rows["amit"].worked_label, "08h 30m")
        with Clock(at(18, 45)):  # the running total is computed lazily against "now"
            self.assertEqual(rows["rahul"].worked_label, "08h 40m")  # 10:05 -> 18:45
        self.assertEqual(rows["sonia"].worked_label, "")

    def test_table_shows_ip_device_and_verification(self):
        dev = TrustedDevice.objects.create(user=self.amit, label="Front-desk PC")
        Attendance.objects.filter(pk=self.r_amit.pk).update(start_ip="203.0.113.10", start_device=dev)
        resp = self.get()
        self.assertContains(resp, "203.0.113.10")
        self.assertContains(resp, "Front-desk PC")


# ==================================================================== filters
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class FilterTests(Base):
    def test_status_filters(self):
        expected = {
            "not_started": ["sonia"], "working": ["rahul"], "present": ["amit", "kiran", "rahul", "vikram"], "half_day": ["neha"],
            "ended": ["amit", "kiran", "neha", "vikram"], "auto_ended": ["vikram"], "absent": [],
            "full_day": ["amit", "kiran", "rahul", "vikram"],   # legacy bookmarked value maps to present
        }
        for status, names in expected.items():
            self.assertEqual(self.names(self.get(status=status)), names, status)

    def test_absent_filter_on_a_past_day(self):
        self.assertEqual(self.names(self.get(status="absent", date="26/09/2026")), ALL_STAFF)
        self.assertEqual(self.names(self.get(status="not_started", date="26/09/2026")), [])

    def test_verification_filter(self):
        Attendance.objects.filter(pk=self.r_amit.pk).update(start_verification="admin_override")
        Attendance.objects.filter(pk=self.r_neha.pk).update(start_verification="not_checked")
        self.assertEqual(self.names(self.get(verification="admin_override")), ["amit"])
        self.assertEqual(self.names(self.get(verification="not_checked")), ["neha"])
        self.assertEqual(self.names(self.get(verification="verified")), ["kiran", "rahul", "vikram"])

    def test_auto_ended_filter(self):
        self.assertEqual(self.names(self.get(auto_ended="yes")), ["vikram"])
        self.assertEqual(self.names(self.get(auto_ended="no")), ["amit", "kiran", "neha", "rahul"])  # sonia has no record

    def test_anomaly_filter(self):
        self.event(self.rahul)
        self.event(self.sonia)
        self.assertEqual(self.names(self.get(anomaly="yes")), ["rahul", "sonia"])
        self.assertEqual(self.names(self.get(anomaly="no")), ["amit", "kiran", "neha", "vikram"])

    def test_anomaly_ignores_dismissed_events_enrolment_and_other_days(self):
        self.event(self.amit, review="dismissed")
        self.event(self.neha, action="enroll")
        self.event(self.kiran, when=at(10, 0, day=26))
        self.assertEqual(self.names(self.get(anomaly="yes")), [])

    def test_anomaly_types_shown_in_the_row(self):
        self.event(self.rahul, etype="ip_not_allowed")
        self.event(self.rahul, etype="poor_accuracy", when=at(10, 6))
        row = self.rows(self.get())["rahul"]
        self.assertEqual(row.anomaly_types, ["IP Not Allowed", "Poor GPS Accuracy"])

    def test_staff_filter(self):
        self.assertEqual(self.names(self.get(staff=self.neha.pk)), ["neha"])

    def test_combined_filters(self):
        self.assertEqual(self.names(self.get(status="present", auto_ended="no")), ["amit", "kiran", "rahul"])
        self.assertEqual(self.names(self.get(status="full_day", auto_ended="yes", staff=self.amit.pk)), [])

    def test_bad_or_tampered_values_are_ignored_not_errors(self):
        resp = self.get(status="drop table", staff="abc", verification="x", anomaly="maybe", auto_ended="?", date="not-a-date")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.names(resp), ALL_STAFF)

    def test_date_filter_and_range(self):
        self.rec(self.rahul, at(10, 0), at(19, 0), day=26)
        self.assertEqual(self.names(self.get(date="26/09/2026", status="present")), ["rahul"])
        resp = self.get(date="26/09/2026", date_to="27/09/2026")
        self.assertEqual(len(resp.context["rows"]), 12)  # range view: every staff member x every day (6 x 2)
        self.assertEqual(self.names(self.get(date="26/09/2026", date_to="27/09/2026", status="not_started")), ["sonia"])  # only today can be "not started"
        # reversed range is swapped, not an error
        self.assertEqual(len(self.get(date="27/09/2026", date_to="26/09/2026").context["rows"]), 12)

    def test_iso_dates_also_accepted(self):
        self.assertEqual(self.names(self.get(date="2026-09-27")), ALL_STAFF)

    def test_clear_filters_link_returns_to_default_view(self):
        resp = self.get(status="working", q="rahul")
        self.assertContains(resp, f'href="{reverse("staff_attendance")}"')
        self.assertEqual(self.names(self.get()), ALL_STAFF)  # the bare URL = today, everyone

    def test_selected_filter_values_stay_selected(self):
        resp = self.get(status="working", q="rahul")
        self.assertContains(resp, 'value="working" selected')
        self.assertContains(resp, 'value="rahul"')


# ==================================================================== search
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class SearchTests(Base):
    def test_search_by_name_case_insensitive(self):
        self.assertEqual(self.names(self.get(q="rahul")), ["rahul"])
        self.assertEqual(self.names(self.get(q="RAHUL")), ["rahul"])
        self.assertEqual(self.names(self.get(q="ami")), ["amit"])

    def test_search_by_username(self):
        self.assertEqual(self.names(self.get(q="vik")), ["vikram"])

    def test_search_by_full_name_words(self):
        self.assertEqual(self.names(self.get(q="Neha Sharma")), ["neha"])
        self.assertEqual(self.names(self.get(q="sharma neha")), ["neha"])
        self.assertEqual(self.names(self.get(q="Neha Verma")), [])

    def test_search_by_staff_code(self):
        self.assertEqual(self.names(self.get(q="KM-104")), ["amit"])

    def test_email_and_phone_are_not_searchable(self):
        User.objects.filter(pk=self.rahul.pk).update(email="secret.person@example.com")
        StaffProfile.objects.filter(user=self.rahul).update(phone="9876543210")
        self.assertEqual(self.names(self.get(q="secret.person")), [])
        self.assertEqual(self.names(self.get(q="9876543210")), [])

    def test_no_email_or_phone_rendered_in_the_table(self):
        User.objects.filter(pk=self.rahul.pk).update(email="secret.person@example.com")
        StaffProfile.objects.filter(user=self.rahul).update(phone="9876543210")
        resp = self.get()
        self.assertNotContains(resp, "secret.person@example.com")
        self.assertNotContains(resp, "9876543210")

    def test_search_never_finds_admins(self):
        self.assertEqual(self.names(self.get(q="boss")), [])

    def test_search_combines_with_filters(self):
        self.assertEqual(self.names(self.get(q="a", status="half_day")), ["neha"])

    def test_search_works_in_range_mode(self):
        self.assertEqual(self.names(self.get(q="amit", date="26/09/2026", date_to="27/09/2026")), ["amit", "amit"])  # one row per day


# ==================================================================== pagination / performance
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class PaginationTests(Base):
    def _add_staff(self, n):
        for i in range(n):
            u = self.mk(f"bulk{i:03d}", f"Bulk{i:03d}")
            self.rec(u, at(10, 0), at(19, 0))

    def test_only_one_page_of_rows_is_built(self):
        self._add_staff(40)
        resp = self.get(per_page=10)
        self.assertEqual(len(resp.context["rows"]), 10)
        self.assertEqual(resp.context["page"].paginator.count, 46)
        self.assertEqual(resp.context["page"].paginator.num_pages, 5)

    def test_default_page_size_and_page_navigation(self):
        self._add_staff(40)
        self.assertEqual(len(self.get().context["rows"]), 25)
        p1 = self.names(self.get(per_page=10, page=1))
        p2 = self.names(self.get(per_page=10, page=2))
        self.assertEqual(len(p2), 10)
        self.assertFalse(set(p1) & set(p2))

    def test_out_of_range_page_falls_back_safely(self):
        self.assertEqual(self.get(page=999).status_code, 200)
        self.assertEqual(self.get(page="abc").status_code, 200)

    def test_page_links_keep_the_filters(self):
        self._add_staff(30)
        resp = self.get(per_page=10, status="full_day")
        self.assertContains(resp, "status=full_day")
        self.assertContains(resp, "page=2")

    def test_query_count_does_not_grow_with_the_number_of_staff(self):
        with CaptureQueriesContext(connection) as small:
            self.get(per_page=10)
        self._add_staff(60)
        with CaptureQueriesContext(connection) as big:
            self.get(per_page=10)
        self.assertEqual(len(small), len(big))

    def test_record_mode_is_also_paginated_and_constant_query_count(self):
        self._add_staff(30)
        with CaptureQueriesContext(connection) as q1:
            r = self.get(date="26/09/2026", date_to="27/09/2026", per_page=10)
        self.assertEqual(len(r.context["rows"]), 10)
        with CaptureQueriesContext(connection) as q2:
            self.get(date="26/09/2026", date_to="27/09/2026", per_page=10, page=2)
        self.assertEqual(len(q1), len(q2))

    def test_indexes_exist_for_staff_work_date_and_status(self):
        names = {i.name for i in Attendance._meta.indexes}
        self.assertLessEqual({"att_date_status_idx", "att_status_idx"}, names)
        self.assertTrue(Attendance._meta.get_field("work_date").db_index)   # work_date
        self.assertTrue(Attendance._meta.get_field("user").db_index)        # staff (FK index)
        with connection.cursor() as cur:
            live = connection.introspection.get_constraints(cur, Attendance._meta.db_table)
        live_names = set(live)
        self.assertLessEqual({"att_date_status_idx", "att_status_idx"}, live_names)

    def test_models_and_migrations_are_in_sync(self):
        try:
            call_command("makemigrations", "crm", "--check", "--dry-run", stdout=StringIO(), stderr=StringIO())
        except SystemExit:
            self.fail("Model changes exist that no migration covers (0013 is out of sync with models.py).")


# ==================================================================== detail
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class DetailTests(Base):
    def setUp(self):
        super().setUp()
        self.dev = TrustedDevice.objects.create(user=self.amit, label="Front-desk PC")
        Attendance.objects.filter(pk=self.r_amit.pk).update(
            start_latitude=25.135753, start_longitude=75.823947, start_accuracy=12.0, start_distance_from_office=7.0,
            start_ip="203.0.113.10", start_device=self.dev,
            end_latitude=25.135800, end_longitude=75.824000, end_accuracy=250.0, end_distance_from_office=640.0,
            end_ip="198.51.100.77", end_device=self.dev,
        )

    def detail(self, rec=None, **kw):
        with Clock(at(18, 45)):
            return self.c.get(reverse("staff_attendance_detail", args=[(rec or self.r_amit).pk]), **kw)

    def test_detail_shows_everything_requested(self):
        self.event(self.amit, etype="poor_accuracy", when=at(10, 0))
        resp = self.detail()
        for text in ("Amit", "27/09/2026", "10:00 AM", "06:30 PM", "08h 30m", "PRESENT",
                     "Inside office area", "±12 m", "7 m", "203.0.113.10",
                     "Outside office area", "low GPS accuracy", "±250 m", "640 m", "198.51.100.77",
                     "Front-desk PC", "Poor GPS Accuracy", "Audit history", "Anomalies"):
            self.assertContains(resp, text)

    def test_raw_coordinates_are_never_shown(self):
        resp = self.detail()
        for coord in ("25.135753", "75.823947", "25.1358", "75.824"):
            self.assertNotContains(resp, coord)

    def test_open_record_and_auto_ended_record(self):
        self.assertContains(self.detail(self.r_rahul), "still working")
        self.assertContains(self.detail(self.r_vikram), "Auto ended")

    def test_detail_404_for_unknown_record(self):
        with Clock(at(18, 45)):
            self.assertEqual(self.c.get(reverse("staff_attendance_detail", args=[999999])).status_code, 404)

    def test_detail_without_location_says_so(self):
        self.assertContains(self.detail(self.r_neha), "No location captured")

    def test_events_from_other_staff_or_days_are_not_listed(self):
        self.event(self.neha, etype="ip_not_allowed")
        self.event(self.amit, etype="device_mismatch", when=at(10, 0, day=26))
        resp = self.detail()
        self.assertNotContains(resp, "IP Not Allowed")
        self.assertNotContains(resp, "Device Mismatch")

    def test_link_from_the_table_goes_to_the_detail_page(self):
        self.assertContains(self.get(), reverse("staff_attendance_detail", args=[self.r_amit.pk]))


# ==================================================================== corrections
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class CorrectionTests(Base):
    def fresh(self, rec):
        return Attendance.objects.get(pk=rec.pk)

    def assertUntouched(self, rec, snap):
        cur = self.fresh(rec)
        self.assertEqual((cur.start_time, cur.end_time, cur.worked_duration, cur.attendance_status, cur.auto_ended), snap)
        self.assertFalse(AttendanceCorrection.objects.exists())

    def snap(self, rec):
        r = self.fresh(rec)
        return (r.start_time, r.end_time, r.worked_duration, r.attendance_status, r.auto_ended)

    def test_end_time_correction_recomputes_duration_and_status_and_is_audited(self):
        resp = self.correct(self.r_amit, start_time="10:00", end_time="14:00", status="full_day", reason="Left early, confirmed with manager")
        self.assertEqual(resp.status_code, 302)
        r = self.fresh(self.r_amit)
        self.assertEqual(r.end_time, at(14, 0))
        self.assertEqual(r.worked_duration, datetime.timedelta(hours=4))
        self.assertEqual(r.attendance_status, "half_day")  # status untouched in the form -> follows the corrected times

        rows = {c.field: c for c in AttendanceCorrection.objects.all()}
        self.assertEqual(set(rows), {"end_time", "attendance_status"})
        c = rows["end_time"]
        self.assertEqual((c.admin, c.admin_name, c.staff, c.staff_name), (self.admin, "boss", self.amit, "Amit"))
        self.assertEqual((c.old_value, c.new_value), ("2026-09-27 18:30", "2026-09-27 14:00"))
        self.assertEqual(c.work_date, TODAY)
        self.assertEqual(c.reason, "Left early, confirmed with manager")
        self.assertEqual((rows["attendance_status"].old_value, rows["attendance_status"].new_value), ("Full Day", "Half Day"))
        self.assertIsNotNone(c.created_at)
        self.assertEqual(c.created_at, rows["attendance_status"].created_at)  # one correction, one timestamp

        log = AuditLog.objects.get(action="attendance_correction")
        self.assertEqual(log.actor, self.admin)
        self.assertEqual(log.details["reason"], "Left early, confirmed with manager")
        self.assertEqual({x["field"] for x in log.details["changes"]}, {"end_time", "attendance_status"})
        self.assertIn("Amit", log.summary)

    def test_start_time_correction(self):
        self.correct(self.r_amit, start_time="11:00", end_time="18:30", reason="Arrived late, phone was dead")
        r = self.fresh(self.r_amit)
        self.assertEqual(r.start_time, at(11, 0))
        self.assertEqual(r.worked_duration, datetime.timedelta(hours=7, minutes=30))
        self.assertEqual(r.attendance_status, "full_day")  # 7.5h is within the full-day tolerance (was wrongly half_day)
        self.assertEqual(AttendanceCorrection.objects.filter(field="start_time").count(), 1)

    def test_explicit_status_change_without_touching_times(self):
        self.correct(self.r_neha, start_time="10:00", end_time="15:00", status="full_day", reason="Half day approved as full by HR")
        r = self.fresh(self.r_neha)
        self.assertEqual(r.attendance_status, "full_day")
        self.assertEqual(r.end_time, at(15, 0))  # untouched, seconds preserved
        c = AttendanceCorrection.objects.get()
        self.assertEqual((c.field, c.old_value, c.new_value), ("attendance_status", "Half Day", "Full Day"))

    def test_explicit_status_beats_recompute_when_times_change_too(self):
        # posting the *current* status means "not requested", so the times decide; a different one is an explicit choice:
        self.correct(self.r_kiran, end_time="13:00", status="half_day", reason="Counted as half day per policy")
        self.assertEqual(self.fresh(self.r_kiran).attendance_status, "half_day")

    def test_reason_is_mandatory(self):
        snap = self.snap(self.r_amit)
        for reason in ("", "   ", "abc"):
            resp = self.correct(self.r_amit, end_time="14:00", reason=reason)
            self.assertEqual(resp.status_code, 302)
            self.assertIn("reason", self.last_message(resp).lower())
        self.assertUntouched(self.r_amit, snap)
        self.assertFalse(AuditLog.objects.filter(action="attendance_correction").exists())

    def test_missing_reason_field_entirely(self):
        snap = self.snap(self.r_amit)
        self.correct(self.r_amit, end_time="14:00")
        self.assertUntouched(self.r_amit, snap)

    def test_end_before_start_rejected(self):
        snap = self.snap(self.r_amit)
        resp = self.correct(self.r_amit, end_time="09:00", reason="Typo while testing")
        self.assertIn("before", self.last_message(resp))
        self.assertUntouched(self.r_amit, snap)

    def test_start_after_existing_end_rejected(self):
        snap = self.snap(self.r_amit)
        self.correct(self.r_amit, start_time="19:00", reason="Typo while testing")
        self.assertUntouched(self.r_amit, snap)

    def test_future_time_rejected(self):
        snap = self.snap(self.r_rahul)
        resp = self.correct(self.r_rahul, end_time="19:30", reason="Trying to end in the future")  # clock is 18:45
        self.assertIn("future", self.last_message(resp))
        self.assertUntouched(self.r_rahul, snap)

    def test_malformed_times_rejected(self):
        snap = self.snap(self.r_amit)
        for bad in ("25:99", "abc", "10.30", "24:00"):
            self.correct(self.r_amit, end_time=bad, reason="Bad input from a tampered form")
        self.assertUntouched(self.r_amit, snap)

    def test_closing_an_open_day_sets_end_duration_and_status(self):
        resp = self.correct(self.r_rahul, start_time="10:05", end_time="11:30", reason="Forgot to press End Day")
        self.assertEqual(resp.status_code, 302)
        r = self.fresh(self.r_rahul)
        self.assertEqual(r.end_time, at(11, 30))
        self.assertEqual(r.worked_duration, datetime.timedelta(hours=1, minutes=25))
        self.assertEqual(r.attendance_status, "short_day")
        c = AttendanceCorrection.objects.get(field="end_time")
        self.assertEqual((c.old_value, c.new_value), ("Not ended", "2026-09-27 11:30"))

    def test_open_day_can_have_its_start_corrected_and_stays_open(self):
        self.correct(self.r_rahul, start_time="09:30", reason="Started work before logging in")
        r = self.fresh(self.r_rahul)
        self.assertEqual(r.start_time, at(9, 30))
        self.assertIsNone(r.end_time)
        self.assertEqual(r.attendance_status, "in_progress")

    def test_status_cannot_be_set_on_a_day_that_is_still_open(self):
        snap = self.snap(self.r_rahul)
        resp = self.correct(self.r_rahul, status="full_day", reason="Trying to skip the end time")
        self.assertIn("finished", self.last_message(resp))
        self.assertUntouched(self.r_rahul, snap)

    def test_invalid_status_rejected(self):
        snap = self.snap(self.r_neha)
        for bad in ("in_progress", "absent", "banana"):
            self.correct(self.r_neha, status=bad, reason="Tampered status value")
        self.assertUntouched(self.r_neha, snap)

    def test_changing_the_end_of_an_auto_ended_day_clears_the_auto_flag_and_logs_it(self):
        self.correct(self.r_vikram, end_time="18:00", reason="He actually left at six")
        r = self.fresh(self.r_vikram)
        self.assertFalse(r.auto_ended)
        self.assertEqual(r.end_time, at(18, 0))
        c = AttendanceCorrection.objects.get(field="auto_ended")
        self.assertEqual((c.old_value, c.new_value), ("Yes", "No"))

    def test_status_only_change_keeps_the_auto_flag(self):
        self.correct(self.r_vikram, status="half_day", reason="Policy exception for this day")
        self.assertTrue(self.fresh(self.r_vikram).auto_ended)

    def test_submitting_unchanged_values_is_rejected(self):
        snap = self.snap(self.r_amit)
        resp = self.correct(self.r_amit, start_time="10:00", end_time="18:30", status="full_day", reason="No actual change here")
        self.assertIn("Nothing to change", self.last_message(resp))
        self.assertUntouched(self.r_amit, snap)

    def test_time_is_always_applied_to_the_records_own_date(self):
        old = self.rec(self.rahul, at(10, 0, day=20), at(15, 0, day=20), day=20)
        self.correct(old, end_time="17:00", reason="Corrected from paper register")
        r = self.fresh(old)
        self.assertEqual(r.end_time, at(17, 0, day=20))
        self.assertEqual(r.work_date, datetime.date(2026, 9, 20))

    def test_correction_is_atomic_when_the_audit_write_fails(self):
        snap = self.snap(self.r_amit)
        with mock.patch.object(aa.services, "log_audit", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                with Clock(at(18, 45)):
                    aa.apply_correction(self.r_amit.pk, self.admin, end="14:00", reason="This must roll back")
        self.assertUntouched(self.r_amit, snap)

    def test_history_is_shown_on_the_detail_page(self):
        self.correct(self.r_amit, end_time="14:00", reason="Left early, confirmed")
        with Clock(at(18, 45)):
            resp = self.c.get(reverse("staff_attendance_detail", args=[self.r_amit.pk]))
        for text in ("Left early, confirmed", "2026-09-27 18:30", "2026-09-27 14:00", "boss", "End time"):
            self.assertContains(resp, text)

    def test_correction_form_is_prefilled_and_requires_a_reason(self):
        with Clock(at(18, 45)):
            resp = self.c.get(reverse("staff_attendance_detail", args=[self.r_amit.pk]))
        self.assertContains(resp, 'name="reason"')
        self.assertContains(resp, "required")
        self.assertContains(resp, 'name="start_time" value="10:00"')
        self.assertContains(resp, 'name="end_time" value="18:30"')
        self.assertContains(resp, 'name="status"')
        with Clock(at(18, 45)):
            open_resp = self.c.get(reverse("staff_attendance_detail", args=[self.r_rahul.pk]))
        self.assertNotContains(open_resp, 'name="status"')  # no status on a day still in progress

    def test_correction_rows_are_kept_when_the_admin_account_is_removed(self):
        boss2 = User.objects.create_user("boss2", password="pw", is_superuser=True, is_staff=True)
        AttendanceCorrection.objects.create(admin=boss2, admin_name="boss2", staff=self.amit, staff_name="Amit",
                                            work_date=TODAY, field="end_time", old_value="a", new_value="b", reason="x" * 6)
        boss2.delete()
        c = AttendanceCorrection.objects.get()
        self.assertIsNone(c.admin)
        self.assertEqual(c.admin_name, "boss2")

    def test_correction_of_missing_record_is_404(self):
        with Clock(at(18, 45)):
            resp = self.c.post(reverse("staff_attendance_correct", args=[424242]), {"reason": "whatever it is"})
        self.assertEqual(resp.status_code, 404)

    def test_ordinary_staff_have_no_path_to_edit_attendance(self):
        # The pre-existing staff endpoints only ever start/end the caller's OWN day and accept no times.
        staff = Client()
        staff.force_login(self.rahul)
        snap = self.snap(self.r_amit)
        with Clock(at(18, 45)):
            staff.post(reverse("attendance_end"), {"staff": self.amit.pk, "end_time": "10:30", "user_id": self.amit.pk})
        self.assertEqual(self.snap(self.r_amit), snap)


# ==================================================================== export
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class ExportTests(Base):
    COLUMNS = ["Staff", "Date", "Start", "End", "Worked Hours", "Status", "Auto Ended", "Verification", "Anomaly"]

    def export(self, **params):
        with Clock(at(18, 45)):
            resp = self.c.get(reverse("staff_attendance_export"), params)
        self.assertEqual(resp.status_code, 200)
        return resp

    def csv_rows(self, **params):
        resp = self.export(**params)
        with Clock(at(18, 45)):   # the CSV is generated lazily while streaming
            text = b"".join(resp.streaming_content).decode("utf-8-sig")
        return list(csv.reader(io.StringIO(text)))

    def test_csv_columns_and_content(self):
        self.event(self.rahul)
        rows = self.csv_rows()
        self.assertEqual(rows[0], self.COLUMNS)
        by = {r[0]: r for r in rows[1:]}
        self.assertEqual(len(by), 6)
        self.assertEqual(by["Amit"], ["Amit", "2026-09-27", "10:00", "18:30", "8.5", "PRESENT", "No", "Verified", "No"])
        self.assertEqual(by["Vikram"][6], "Yes")
        self.assertEqual(by["Rahul"][3:6], ["", "", "PRESENT"])  # open day: no end, no final hours
        self.assertEqual(by["Rahul"][8], "Yes")
        self.assertEqual(by["Sonia"], ["Sonia", "2026-09-27", "", "", "", "NOT STARTED", "No", "", "No"])

    def test_csv_response_headers(self):
        resp = self.export()
        self.assertIn("text/csv", resp["Content-Type"])
        self.assertIn("kota-money-staff-attendance-", resp["Content-Disposition"])
        self.assertIn(".csv", resp["Content-Disposition"])

    def test_export_respects_filters(self):
        self.assertEqual({r[0] for r in self.csv_rows(status="present")[1:]}, {"Amit", "Kiran", "Rahul", "Vikram"})
        self.assertEqual({r[0] for r in self.csv_rows(q="neha")[1:]}, {"Neha"})
        self.assertEqual({r[0] for r in self.csv_rows(anomaly="yes")[1:]}, set())

    def test_export_range_mode(self):
        self.rec(self.rahul, at(10, 0), at(19, 0), day=26)
        rows = self.csv_rows(date="26/09/2026", date_to="27/09/2026")
        self.assertEqual(len(rows) - 1, 12)   # 6 staff x 2 days
        self.assertEqual({r[1] for r in rows[1:]}, {"2026-09-26", "2026-09-27"})

    def test_export_is_not_limited_to_one_page(self):
        for i in range(60):
            self.rec(self.mk(f"bulk{i:03d}", f"Bulk{i:03d}"), at(10, 0), at(19, 0))
        self.assertEqual(len(self.csv_rows()) - 1, 66)

    def test_export_never_contains_sensitive_or_location_data(self):
        dev = TrustedDevice.objects.create(user=self.amit, label="Front-desk PC")
        Attendance.objects.filter(pk=self.r_amit.pk).update(
            start_latitude=25.135753, start_longitude=75.823947, start_accuracy=12.0, start_distance_from_office=7.0,
            start_ip="203.0.113.10", start_device=dev, end_ip="198.51.100.77")
        User.objects.filter(pk=self.amit.pk).update(email="amit.private@example.com")
        StaffProfile.objects.filter(user=self.amit).update(phone="9876543210")
        raw = b"".join(self.export().streaming_content).decode("utf-8-sig")
        for secret in ("203.0.113.10", "198.51.100.77", "25.135753", "75.823947", "Front-desk PC",
                       "amit.private@example.com", "9876543210", "password", "token"):
            self.assertNotIn(secret, raw)
        self.assertEqual(csv.reader(io.StringIO(raw)).__next__(), self.COLUMNS)

    def test_formula_injection_is_neutralised(self):
        evil = self.mk("evil", "=HYPERLINK(1)")
        self.rec(evil, at(10, 0), at(19, 0))
        cells = [r[0] for r in self.csv_rows()[1:]]
        self.assertIn("'=HYPERLINK(1)", cells)
        self.assertNotIn("=HYPERLINK(1)", cells)

    def test_excel_export(self):
        import openpyxl

        resp = self.export(format="xlsx")
        self.assertIn("spreadsheetml", resp["Content-Type"])
        ws = openpyxl.load_workbook(io.BytesIO(resp.content)).active
        data = list(ws.iter_rows(values_only=True))
        self.assertEqual(list(data[0]), self.COLUMNS)
        self.assertEqual(len(data) - 1, 6)
        amit = next(r for r in data[1:] if r[0] == "Amit")
        self.assertEqual(amit[4], 8.5)  # a real number, so Excel can sum it

    def test_export_is_audited(self):
        self.export(status="working")
        log = AuditLog.objects.get(action="attendance_export")
        self.assertEqual(log.actor, self.admin)
        self.assertIn("status=working", log.details["query"])

    def test_dashboard_offers_export_links_that_carry_the_filters(self):
        resp = self.get(status="working")
        self.assertContains(resp, reverse("staff_attendance_export"))
        self.assertContains(resp, "format=xlsx")
        self.assertContains(resp, "status=working")


# ==================================================================== responsiveness (static)
@override_settings(ATTENDANCE_WEEKLY_OFF_WEEKDAY=None)  # legacy fixtures treat Sunday 27/09/2026 as a normal day; Sunday is tested in tests_attendance_status
class ResponsivenessTests(Base):
    """
    No browser runs in the test suite, so these guard the pieces mobile layout relies on:
    viewport tag, responsive card grid, horizontally scrollable table, and the media queries.
    """

    def test_markup_has_the_responsive_hooks(self):
        resp = self.get()
        self.assertContains(resp, 'name="viewport"')
        self.assertContains(resp, 'class="sa-cards"')
        self.assertContains(resp, 'class="table-scroll"')
        self.assertContains(resp, 'class="filter-grid"')

    def test_stylesheet_collapses_cards_and_scrolls_the_table_on_small_screens(self):
        css = (settings.BASE_DIR / "static" / "css" / "style.css").read_text()
        self.assertIn(".sa-cards { display: grid; grid-template-columns: repeat(auto-fit", css)
        media = css[css.index("STAFF ATTENDANCE ADMIN"):]
        self.assertIn("@media (max-width: 900px)", media)
        self.assertIn("@media (max-width: 560px)", media)
        self.assertIn(".sa-cards { grid-template-columns: repeat(2", media)
        self.assertRegex(css, r"\.table-scroll\s*\{[^}]*overflow-x:\s*auto")

    def test_detail_page_uses_the_responsive_layout_classes(self):
        with Clock(at(18, 45)):
            resp = self.c.get(reverse("staff_attendance_detail", args=[self.r_amit.pk]))
        self.assertContains(resp, "kv-grid")
        self.assertContains(resp, "sa-correct-grid")
        self.assertContains(resp, "table-scroll")
