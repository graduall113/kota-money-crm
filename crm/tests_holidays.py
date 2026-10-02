"""
Holiday management: model + constraints, presets, create / edit / delete /
activate / deactivate, upcoming-current-past tabs, duplicate refusal, overlap
warning (+ explicit confirmation), audit trail, admin-only enforcement
(GET *and* POST, by direct URL), and the attendance banners / cards (the enforcement itself is in tests_holiday_attendance).

The clock is frozen at 27/09/2026 12:00 IST (same convention as the attendance
suites), so "upcoming / current / past" are deterministic.
"""
import datetime
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase
from django.urls import reverse

from . import access, holidays as hr
from .models import Attendance, AuditLog, Holiday
from .tests_attendance import Clock, at

User = get_user_model()
D = datetime.date
TODAY = D(2026, 9, 27)


def now():
    return Clock(at(12))


class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)  # signal -> admin profile
        self.rahul = User.objects.create_user("rahul", password="pw", first_name="Rahul")   # staff, mid-shift
        self.sonia = User.objects.create_user("sonia", password="pw", first_name="Sonia")   # staff, has not started
        Attendance.objects.create(user=self.rahul, work_date=TODAY, start_time=at(10, 0))   # open row -> the gate lets rahul through
        self.c = Client()
        self.c.force_login(self.admin)

    # ---- builders
    def hol(self, name, start, end=None, active=True, reason=""):
        return Holiday.objects.create(name=name, start_date=start, end_date=end or start, is_active=active,
                                      reason=reason, created_by=self.admin)

    @staticmethod
    def payload(**over):
        data = {"name": "Diwali Holiday", "duration": "custom", "start_date": "20/10/2026", "end_date": "27/10/2026",
                "reason": "", "is_active": "on"}
        data.update(over)
        return {k: v for k, v in data.items() if v is not None}

    def create(self, client=None, **over):
        with now():
            return (client or self.c).post(reverse("holiday_create"), self.payload(**over))

    def edit(self, holiday, client=None, **over):
        base = {"name": holiday.name, "duration": "custom", "start_date": f"{holiday.start_date:%d/%m/%Y}",
                "end_date": f"{holiday.end_date:%d/%m/%Y}", "reason": holiday.reason,
                "is_active": "on" if holiday.is_active else None}
        base.update(over)
        with now():
            return (client or self.c).post(reverse("holiday_edit", args=[holiday.pk]),
                                           {k: v for k, v in base.items() if v is not None})

    def get(self, name, *args, client=None, **params):
        with now():
            return (client or self.c).get(reverse(name, args=list(args)), params)

    def msgs(self, resp):
        return " | ".join(str(m) for m in get_messages(resp.wsgi_request))


# ==================================================================== model + constraints
class ModelTests(Base):
    def test_range_is_inclusive_and_every_date_is_a_holiday(self):
        h = self.hol("Diwali Holiday", D(2026, 10, 20), D(2026, 10, 27))
        self.assertEqual(h.duration_days, 8)
        self.assertFalse(h.is_single_day)
        for i in range(8):
            self.assertTrue(hr.is_holiday(D(2026, 10, 20) + datetime.timedelta(days=i)), i)
        self.assertFalse(hr.is_holiday(D(2026, 10, 19)))
        self.assertFalse(hr.is_holiday(D(2026, 10, 28)))
        self.assertEqual(hr.holiday_for(D(2026, 10, 23)).pk, h.pk)

    def test_single_day(self):
        h = self.hol("Republic Day", D(2027, 1, 26))
        self.assertTrue(h.is_single_day)
        self.assertEqual(h.duration_days, 1)
        self.assertTrue(hr.is_holiday(D(2027, 1, 26)))
        self.assertFalse(hr.is_holiday(D(2027, 1, 27)))

    def test_status_relative_to_a_day(self):
        h = self.hol("X", D(2026, 10, 1), D(2026, 10, 3))
        self.assertEqual(h.status_on(D(2026, 9, 30)), "upcoming")
        self.assertEqual(h.status_on(D(2026, 10, 1)), "current")
        self.assertEqual(h.status_on(D(2026, 10, 3)), "current")
        self.assertEqual(h.status_on(D(2026, 10, 4)), "past")

    def test_inactive_holiday_is_ignored_by_every_lookup(self):
        self.hol("Off", D(2026, 10, 5), D(2026, 10, 6), active=False)
        self.assertFalse(hr.is_holiday(D(2026, 10, 5)))
        self.assertIsNone(hr.holiday_for(D(2026, 10, 5)))
        self.assertEqual(hr.holidays_on(D(2026, 10, 5)), [])
        self.assertEqual(hr.holiday_map(D(2026, 10, 1), D(2026, 10, 10)), {})

    def test_holiday_map_clips_to_the_window(self):
        h = self.hol("Long", D(2026, 10, 28), D(2026, 11, 3))
        found = hr.holiday_map(D(2026, 11, 1), D(2026, 11, 2))
        self.assertEqual(sorted(found), [D(2026, 11, 1), D(2026, 11, 2)])
        self.assertEqual(found[D(2026, 11, 1)], [h])

    def test_database_refuses_end_before_start(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Holiday.objects.create(name="Bad", start_date=D(2026, 10, 5), end_date=D(2026, 10, 4))

    def test_database_refuses_an_identical_period(self):
        self.hol("A", D(2026, 10, 5), D(2026, 10, 6))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Holiday.objects.create(name="B", start_date=D(2026, 10, 5), end_date=D(2026, 10, 6))

    def test_full_clean_reports_both_rules(self):
        with self.assertRaises(ValidationError):
            Holiday(name="Bad", start_date=D(2026, 10, 5), end_date=D(2026, 10, 4)).full_clean()
        self.hol("A", D(2026, 10, 5), D(2026, 10, 6))
        with self.assertRaises(ValidationError):
            Holiday(name="B", start_date=D(2026, 10, 5), end_date=D(2026, 10, 6)).full_clean()

    def test_presets(self):
        self.assertEqual(hr.end_for_preset("single", D(2026, 11, 5)), D(2026, 11, 5))
        self.assertEqual(hr.end_for_preset("week", D(2026, 11, 1)), D(2026, 11, 7))
        self.assertEqual(hr.end_for_preset("week", D(2026, 12, 28)), D(2027, 1, 3))        # crosses a year
        self.assertEqual(hr.end_for_preset("month", D(2026, 11, 1)), D(2026, 11, 30))     # 1st -> whole calendar month
        self.assertEqual(hr.end_for_preset("month", D(2026, 11, 10)), D(2026, 12, 9))
        self.assertEqual(hr.end_for_preset("month", D(2026, 12, 15)), D(2027, 1, 14))     # December rolls over
        self.assertEqual(hr.end_for_preset("month", D(2027, 1, 31)), D(2027, 2, 27))      # clamped to a short month
        self.assertEqual(hr.end_for_preset("month", D(2028, 2, 1)), D(2028, 2, 29))       # leap year
        self.assertIsNone(hr.end_for_preset("custom", D(2026, 11, 1)))


# ==================================================================== permissions
class PermissionTests(Base):
    def urls(self, h):
        return [("holiday_list", []), ("holiday_create", []), ("holiday_edit", [h.pk]),
                ("holiday_delete", [h.pk]), ("holiday_toggle", [h.pk])]

    def test_admin_can_open_every_page(self):
        h = self.hol("Diwali Holiday", D(2026, 10, 20), D(2026, 10, 27))
        for name, args in self.urls(h):
            if name == "holiday_toggle":
                continue  # POST only
            self.assertEqual(self.get(name, *args).status_code, 200, name)

    def test_anonymous_is_sent_to_login(self):
        resp = Client().get(reverse("holiday_list"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login/", resp["Location"])
        resp = Client().post(reverse("holiday_create"), self.payload())
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(Holiday.objects.exists())

    def test_staff_gets_403_on_every_url_by_get_and_post_and_nothing_changes(self):
        h = self.hol("Diwali Holiday", D(2026, 10, 20), D(2026, 10, 27), reason="original")
        staff = Client()
        staff.force_login(self.rahul)  # mid-shift: the attendance gate lets him in, so the ADMIN check is what must stop him
        for name, args in self.urls(h):
            with now():
                self.assertEqual(staff.get(reverse(name, args=args)).status_code, 403, f"GET {name}")
        for name, args in self.urls(h):
            with now():
                self.assertEqual(staff.post(reverse(name, args=args), self.payload(name="Hacked")).status_code, 403, f"POST {name}")
        h.refresh_from_db()
        self.assertEqual((h.name, h.reason, h.is_active, h.start_date, h.end_date),
                         ("Diwali Holiday", "original", True, D(2026, 10, 20), D(2026, 10, 27)))
        self.assertEqual(Holiday.objects.count(), 1)  # nothing created, nothing deleted
        self.assertFalse(AuditLog.objects.filter(action__startswith="holiday_").exists())

    def test_staff_who_has_not_started_is_also_blocked(self):
        h = self.hol("Diwali Holiday", D(2026, 10, 20), D(2026, 10, 27))
        staff = Client()
        staff.force_login(self.sonia)
        for name, args in self.urls(h):
            with now():
                self.assertNotEqual(staff.post(reverse(name, args=args), self.payload()).status_code, 200, name)
        self.assertEqual(Holiday.objects.count(), 1)
        self.assertTrue(Holiday.objects.get(pk=h.pk).is_active)

    def test_deactivated_admin_account_cannot_manage(self):
        self.admin.staff_profile.status = "inactive"
        self.admin.staff_profile.save()
        self.assertNotEqual(self.get("holiday_list").status_code, 200)

    def test_helper_matches_role(self):
        self.assertTrue(access.can_manage_holidays(self.admin))
        self.assertFalse(access.can_manage_holidays(self.rahul))

    def test_sidebar_link_is_admin_only(self):
        self.assertContains(self.get("holiday_list"), 'href="/holidays/"')
        staff = Client()
        staff.force_login(self.rahul)
        with now():
            resp = staff.get(reverse("attendance"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, 'href="/holidays/"')

    def test_wrong_methods(self):
        h = self.hol("X", D(2026, 10, 20))
        self.assertEqual(self.get("holiday_toggle", h.pk).status_code, 405)
        with now():
            self.assertEqual(self.c.post(reverse("holiday_list")).status_code, 405)


# ==================================================================== create
class CreateTests(Base):
    def test_diwali_example_date_range(self):
        resp = self.create()
        self.assertRedirects(resp, reverse("holiday_list"), fetch_redirect_response=False)
        h = Holiday.objects.get()
        self.assertEqual((h.name, h.start_date, h.end_date, h.is_active), ("Diwali Holiday", D(2026, 10, 20), D(2026, 10, 27), True))
        self.assertEqual(h.duration_days, 8)
        self.assertEqual(h.created_by, self.admin)
        self.assertIn("Diwali Holiday", self.msgs(resp))
        for day in range(20, 28):
            self.assertTrue(hr.is_holiday(D(2026, 10, day)), day)

    def test_single_day_without_an_end_date(self):
        self.create(name="Gandhi Jayanti", duration="single", start_date="02/10/2026", end_date="")
        h = Holiday.objects.get()
        self.assertEqual((h.start_date, h.end_date), (D(2026, 10, 2), D(2026, 10, 2)))

    def test_week_preset(self):
        self.create(name="Winter break", duration="week", start_date="28/12/2026", end_date="")
        h = Holiday.objects.get()
        self.assertEqual((h.start_date, h.end_date), (D(2026, 12, 28), D(2027, 1, 3)))

    def test_month_preset(self):
        self.create(name="Shutdown", duration="month", start_date="01/11/2026", end_date="")
        h = Holiday.objects.get()
        self.assertEqual((h.start_date, h.end_date), (D(2026, 11, 1), D(2026, 11, 30)))

    def test_preset_ignores_whatever_was_typed_in_the_to_box(self):
        self.create(name="One day", duration="single", start_date="02/10/2026", end_date="garbage")
        h = Holiday.objects.get()
        self.assertEqual(h.end_date, D(2026, 10, 2))

    def test_custom_range_needs_an_end_date(self):
        resp = self.create(end_date="")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("end_date", resp.context["form"].errors)
        self.assertFalse(Holiday.objects.exists())

    def test_start_after_end_is_rejected(self):
        resp = self.create(start_date="27/10/2026", end_date="20/10/2026")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("cannot be before the start date", str(resp.context["form"].errors["end_date"]))
        self.assertFalse(Holiday.objects.exists())

    def test_bad_date_formats_are_rejected_not_crashed(self):
        for bad in ("2026-10-20", "32/13/2026", "20-10-2026", "abc"):
            resp = self.create(start_date=bad)
            self.assertEqual(resp.status_code, 200, bad)
            self.assertIn("start_date", resp.context["form"].errors, bad)
        self.assertFalse(Holiday.objects.exists())

    def test_absurdly_long_or_far_dates_are_rejected(self):
        self.assertEqual(self.create(start_date="01/01/2026", end_date="31/12/2027").status_code, 200)
        self.assertEqual(self.create(start_date="01/01/1990", end_date="02/01/1990").status_code, 200)
        self.assertFalse(Holiday.objects.exists())

    def test_name_is_required_and_whitespace_is_normalised(self):
        self.assertEqual(self.create(name="   ").status_code, 200)
        self.assertFalse(Holiday.objects.exists())
        self.create(name="  Diwali    Holiday  ")
        self.assertEqual(Holiday.objects.get().name, "Diwali Holiday")

    def test_reason_and_inactive_flag_are_saved(self):
        self.create(reason="Festival of lights", is_active=None)
        h = Holiday.objects.get()
        self.assertEqual(h.reason, "Festival of lights")
        self.assertFalse(h.is_active)

    def test_audit_entry(self):
        self.create()
        log = AuditLog.objects.get(action="holiday_created")
        self.assertEqual(log.actor, self.admin)
        self.assertEqual(log.target_type, "holiday")
        self.assertEqual(log.details["start_date"], "2026-10-20")


# ==================================================================== duplicates + overlaps
class DuplicateAndOverlapTests(Base):
    def setUp(self):
        super().setUp()
        self.diwali = self.hol("Diwali Holiday", D(2026, 10, 20), D(2026, 10, 27))

    def test_identical_period_is_refused_even_under_another_name(self):
        resp = self.create(name="Company off")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already exists")
        self.assertEqual(Holiday.objects.count(), 1)

    def test_identical_period_is_refused_when_the_existing_one_is_inactive(self):
        Holiday.objects.filter(pk=self.diwali.pk).update(is_active=False)
        resp = self.create(name="Company off")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Holiday.objects.count(), 1)

    def test_same_name_on_overlapping_dates_is_refused_outright(self):
        resp = self.create(name="diwali holiday", start_date="25/10/2026", end_date="30/10/2026")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "already covers")
        self.assertEqual(resp.context["form"].overlaps, [])   # a refusal, not a confirmable warning
        self.assertEqual(Holiday.objects.count(), 1)

    def test_same_name_in_another_year_is_fine(self):
        self.create(start_date="08/11/2027", end_date="15/11/2027")
        self.assertEqual(Holiday.objects.count(), 2)

    def test_partial_overlap_with_a_different_holiday_warns_and_saves_nothing(self):
        resp = self.create(name="Bhai Dooj", start_date="27/10/2026", end_date="28/10/2026")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([h.pk for h in resp.context["form"].overlaps], [self.diwali.pk])
        self.assertContains(resp, "overlap another active holiday")
        self.assertContains(resp, "Save anyway")
        self.assertContains(resp, "Diwali Holiday")
        self.assertEqual(Holiday.objects.count(), 1)

    def test_overlap_is_saved_once_the_admin_confirms(self):
        resp = self.create(name="Bhai Dooj", start_date="27/10/2026", end_date="28/10/2026", confirm_overlap="1")
        self.assertRedirects(resp, reverse("holiday_list"), fetch_redirect_response=False)
        self.assertEqual(Holiday.objects.count(), 2)

    def test_a_holiday_inside_a_longer_one_also_warns(self):
        resp = self.create(name="Govardhan Puja", start_date="22/10/2026", end_date="22/10/2026")
        self.assertEqual(len(resp.context["form"].overlaps), 1)

    def test_touching_but_not_overlapping_is_fine(self):
        self.create(name="Next day", start_date="28/10/2026", end_date="29/10/2026")
        self.create(name="Day before", start_date="19/10/2026", end_date="19/10/2026")
        self.assertEqual(Holiday.objects.count(), 3)

    def test_overlap_with_an_inactive_holiday_does_not_warn(self):
        Holiday.objects.filter(pk=self.diwali.pk).update(is_active=False)
        self.create(name="Bhai Dooj", start_date="27/10/2026", end_date="28/10/2026")
        self.assertEqual(Holiday.objects.count(), 2)

    def test_an_inactive_new_holiday_never_warns(self):
        self.create(name="Bhai Dooj", start_date="27/10/2026", end_date="28/10/2026", is_active=None)
        self.assertEqual(Holiday.objects.count(), 2)

    def test_editing_a_holiday_does_not_clash_with_itself(self):
        resp = self.edit(self.diwali, reason="updated note")
        self.assertRedirects(resp, reverse("holiday_list"), fetch_redirect_response=False)
        self.assertEqual(Holiday.objects.get(pk=self.diwali.pk).reason, "updated note")

    def test_editing_into_an_overlap_warns_then_saves_when_confirmed(self):
        other = self.hol("Bhai Dooj", D(2026, 11, 5), D(2026, 11, 6))
        resp = self.edit(other, start_date="26/10/2026", end_date="28/10/2026")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.context["form"].overlaps), 1)
        other.refresh_from_db()
        self.assertEqual(other.start_date, D(2026, 11, 5))          # nothing saved yet
        self.edit(other, start_date="26/10/2026", end_date="28/10/2026", confirm_overlap="1")
        other.refresh_from_db()
        self.assertEqual(other.start_date, D(2026, 10, 26))

    def test_editing_into_an_identical_period_is_refused(self):
        other = self.hol("Bhai Dooj", D(2026, 11, 5), D(2026, 11, 6))
        resp = self.edit(other, start_date="20/10/2026", end_date="27/10/2026")
        self.assertEqual(resp.status_code, 200)
        other.refresh_from_db()
        self.assertEqual(other.start_date, D(2026, 11, 5))


# ==================================================================== edit / delete / toggle
class EditDeleteToggleTests(Base):
    def setUp(self):
        super().setUp()
        self.h = self.hol("Diwali Holiday", D(2026, 10, 20), D(2026, 10, 27), reason="lights")

    def test_edit_updates_fields_and_records_what_changed(self):
        resp = self.edit(self.h, name="Diwali Break", end_date="28/10/2026", reason="")
        self.assertRedirects(resp, reverse("holiday_list"), fetch_redirect_response=False)
        self.h.refresh_from_db()
        self.assertEqual((self.h.name, self.h.end_date, self.h.reason), ("Diwali Break", D(2026, 10, 28), ""))
        changes = AuditLog.objects.get(action="holiday_edited").details["changes"]
        self.assertEqual(changes["name"], {"from": "Diwali Holiday", "to": "Diwali Break"})
        self.assertEqual(changes["end_date"]["to"], "2026-10-28")

    def test_edit_page_prefills_dd_mm_yyyy(self):
        resp = self.get("holiday_edit", self.h.pk)
        self.assertContains(resp, 'value="20/10/2026"')
        self.assertContains(resp, 'value="27/10/2026"')

    def test_edit_with_no_changes_writes_no_audit_row(self):
        self.edit(self.h)
        self.assertFalse(AuditLog.objects.filter(action="holiday_edited").exists())

    def test_edit_validation_errors_do_not_change_the_row(self):
        resp = self.edit(self.h, start_date="30/10/2026", end_date="20/10/2026")
        self.assertEqual(resp.status_code, 200)
        self.h.refresh_from_db()
        self.assertEqual((self.h.start_date, self.h.end_date), (D(2026, 10, 20), D(2026, 10, 27)))
        self.assertContains(resp, "Diwali Holiday")  # heading keeps the saved name

    def test_edit_or_delete_of_a_missing_holiday_is_404(self):
        self.assertEqual(self.get("holiday_edit", 99999).status_code, 404)
        self.assertEqual(self.get("holiday_delete", 99999).status_code, 404)
        with now():
            self.assertEqual(self.c.post(reverse("holiday_toggle", args=[99999])).status_code, 404)

    def test_delete_needs_confirmation_then_removes(self):
        resp = self.get("holiday_delete", self.h.pk)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(Holiday.objects.filter(pk=self.h.pk).exists())   # GET never deletes
        with now():
            resp = self.c.post(reverse("holiday_delete", args=[self.h.pk]))
        self.assertRedirects(resp, reverse("holiday_list"), fetch_redirect_response=False)
        self.assertFalse(Holiday.objects.exists())
        self.assertFalse(hr.is_holiday(D(2026, 10, 22)))
        log = AuditLog.objects.get(action="holiday_deleted")
        self.assertEqual(log.details["name"], "Diwali Holiday")

    def test_toggle_flips_and_is_audited(self):
        with now():
            self.c.post(reverse("holiday_toggle", args=[self.h.pk]))
        self.h.refresh_from_db()
        self.assertFalse(self.h.is_active)
        self.assertFalse(hr.is_holiday(D(2026, 10, 22)))         # deactivated -> no longer a holiday
        with now():
            self.c.post(reverse("holiday_toggle", args=[self.h.pk]))
        self.h.refresh_from_db()
        self.assertTrue(self.h.is_active)
        self.assertTrue(hr.is_holiday(D(2026, 10, 22)))
        self.assertEqual(list(AuditLog.objects.filter(action__in=["holiday_activated", "holiday_deactivated"])
                              .order_by("id").values_list("action", flat=True)),
                         ["holiday_deactivated", "holiday_activated"])

    def test_reactivating_into_an_overlap_shows_a_note_but_still_activates(self):
        other = self.hol("Bhai Dooj", D(2026, 10, 27), D(2026, 10, 28), active=False)
        with now():
            resp = self.c.post(reverse("holiday_toggle", args=[other.pk]))
        other.refresh_from_db()
        self.assertTrue(other.is_active)
        self.assertIn("overlaps", self.msgs(resp))
        self.assertIn("Diwali Holiday", self.msgs(resp))

    def test_toggle_redirect_only_follows_local_holiday_urls(self):
        with now():
            resp = self.c.post(reverse("holiday_toggle", args=[self.h.pk]), {"next": "/holidays/?tab=all"})
        self.assertEqual(resp["Location"], "/holidays/?tab=all")
        for bad in ("https://evil.example/holidays/", "//evil.example/holidays/", "/leads/all/"):
            with now():
                resp = self.c.post(reverse("holiday_toggle", args=[self.h.pk]), {"next": bad})
            self.assertEqual(resp["Location"], reverse("holiday_list"), bad)


# ==================================================================== list / tabs
class ListTests(Base):
    def setUp(self):
        super().setUp()
        self.past = self.hol("Raksha Bandhan", D(2026, 9, 10), D(2026, 9, 12))
        self.current = self.hol("Mid-term break", D(2026, 9, 25), D(2026, 9, 29))
        self.soon = self.hol("Gandhi Jayanti", D(2026, 10, 2))
        self.later = self.hol("Diwali Holiday", D(2026, 10, 20), D(2026, 10, 27))
        self.hol("Inactive early", D(2026, 9, 30), active=False)   # earlier than 'soon', but inactive

    def names(self, resp):
        return [r["h"].name for r in resp.context["rows"]]

    def test_default_tab_is_upcoming_in_calendar_order(self):
        resp = self.get("holiday_list")
        self.assertEqual(resp.context["tab"], "upcoming")
        self.assertEqual(self.names(resp), ["Inactive early", "Gandhi Jayanti", "Diwali Holiday"])

    def test_current_tab(self):
        self.assertEqual(self.names(self.get("holiday_list", tab="current")), ["Mid-term break"])

    def test_past_tab_is_most_recent_first(self):
        self.assertEqual(self.names(self.get("holiday_list", tab="past")), ["Raksha Bandhan"])

    def test_all_tab_and_unknown_tab(self):
        self.assertEqual(len(self.names(self.get("holiday_list", tab="all"))), 5)
        self.assertEqual(self.get("holiday_list", tab="nonsense").context["tab"], "upcoming")

    def test_tab_counts(self):
        counts = self.get("holiday_list").context["counts"]
        self.assertEqual((counts["upcoming"], counts["current"], counts["past"], counts["all"]), (3, 1, 1, 5))

    def test_summary_cards_ignore_inactive_holidays(self):
        ctx = self.get("holiday_list").context
        self.assertEqual(ctx["current_holiday"].pk, self.current.pk)
        self.assertEqual(ctx["next_holiday"].pk, self.soon.pk)      # not the inactive 30/09 one
        self.assertEqual(ctx["next_in_days"], 5)
        self.assertEqual(ctx["this_year"], 4)                       # active ones starting in 2026

    def test_row_status_and_inactive_badge(self):
        resp = self.get("holiday_list", tab="all")
        status = {r["h"].name: r["status"] for r in resp.context["rows"]}
        self.assertEqual(status["Raksha Bandhan"], "past")
        self.assertEqual(status["Mid-term break"], "current")
        self.assertEqual(status["Gandhi Jayanti"], "upcoming")
        self.assertContains(resp, "Inactive")

    def test_empty_state(self):
        Holiday.objects.all().delete()
        self.assertContains(self.get("holiday_list"), "Add your first holiday")

    def test_paginates_server_side(self):
        for i in range(30):
            self.hol(f"Bulk {i}", D(2027, 1, 1) + datetime.timedelta(days=i))
        resp = self.get("holiday_list", tab="all")
        self.assertEqual(len(resp.context["rows"]), 25)
        self.assertEqual(resp.context["page"].paginator.count, 35)


# ==================================================================== attendance banners / cards
class AttendanceBannerTests(Base):
    def test_staff_attendance_page_shows_todays_holiday(self):
        self.hol("Mid-term break", D(2026, 9, 25), D(2026, 9, 29))
        staff = Client()
        staff.force_login(self.sonia)     # has not started: may still open the attendance page
        with now():
            resp = staff.get(reverse("attendance"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Today is a holiday")
        self.assertContains(resp, "Mid-term break")

    def test_no_banner_on_a_normal_day_or_for_an_inactive_holiday(self):
        self.hol("Later", D(2026, 10, 20))
        self.hol("Off", D(2026, 9, 27), active=False)
        staff = Client()
        staff.force_login(self.sonia)
        with now():
            resp = staff.get(reverse("attendance"))
        self.assertNotContains(resp, "Today is a holiday")
        self.assertContains(resp, "Start Day")

    def test_crm_stays_locked_for_staff_who_have_not_started_on_a_holiday(self):
        self.hol("Mid-term break", D(2026, 9, 25), D(2026, 9, 29))
        staff = Client()
        staff.force_login(self.sonia)     # cannot start (holiday) -> the deny-by-default gate still applies
        with now():
            resp = staff.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], reverse("attendance"))

    def test_admin_dashboard_banner_follows_the_selected_date(self):
        self.hol("Mid-term break", D(2026, 9, 25), D(2026, 9, 29))
        self.assertContains(self.get("staff_attendance"), "Mid-term break")
        self.assertNotContains(self.get("staff_attendance", date="01/09/2026"), "company holiday")
        self.assertContains(self.get("staff_attendance", date="26/09/2026"), "company holiday")


# ==================================================================== migration
class MigrationTests(TestCase):
    def test_models_and_migrations_agree(self):
        # `--check` exits non-zero if the models need a migration that is not written yet.
        try:
            call_command("makemigrations", "crm", "--check", "--dry-run", stdout=StringIO(), stderr=StringIO())
        except SystemExit:
            self.fail("Holiday (or another crm model) has changes that no migration covers - run makemigrations.")

    def test_holiday_migration_builds_on_0013_and_lead_sync_builds_on_it(self):
        # 0014_holiday builds on 0013; 0015_lead_sync_queue (Lead -> n8n sync queue) builds on 0014
        # and is now the single leaf (this test used to hardcode 0014 as the leaf).
        from django.db.migrations.loader import MigrationLoader
        loader = MigrationLoader(None, ignore_no_migrations=True)
        self.assertIn(("crm", "0013_staff_attendance_admin"), loader.graph.forwards_plan(("crm", "0014_holiday")))
        self.assertIn(("crm", "0014_holiday"), loader.graph.forwards_plan(("crm", "0015_lead_sync_queue")))
        self.assertEqual(loader.graph.leaf_nodes("crm"), [("crm", "0015_lead_sync_queue")])
