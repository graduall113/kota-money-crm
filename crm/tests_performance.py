"""
Regression tests for the performance work (request-scoped settings, aggregate dashboards, cache helper,
staff-list pagination, segment counts, single-user analytics, dropdown lists).

Every test asserts that the OPTIMISED code returns the same business numbers as the straightforward
definition (counts built from plain filters), so a mistake in an aggregate cannot hide.

Run:  python manage.py test crm.tests_performance
"""
import datetime

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import analytics, cache_utils, settings_store
from .models import AssignmentHistory, Contact, ContactSegment, Lead, Segment, Setting
from .settings_store import get_bool, get_setting, set_setting
from .views import lead_source_options

User = get_user_model()
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "perf-tests"}}


def make_lead(owner=None, name="Asha", phone="9876500000", **kw):
    return Lead.objects.create(customer_name=name, contact_number=phone, assigned_to=owner, **kw)


class SettingsSnapshotTests(TestCase):
    def tearDown(self):
        settings_store._end_request()

    def test_outside_a_request_every_read_hits_the_database(self):
        with self.assertNumQueries(2):
            get_setting("staff_can_export")
            get_setting("staff_can_export")

    def test_inside_a_request_the_table_is_read_once(self):
        Setting.objects.create(key="staff_can_export", value="1")
        settings_store._begin_request()
        with self.assertNumQueries(1):
            self.assertTrue(get_bool("staff_can_export"))
            self.assertEqual(get_setting("import_chunk_size"), "2000")  # default, no extra query
            self.assertEqual(get_setting("not_a_real_key", "fallback"), "fallback")

    def test_write_is_visible_immediately_inside_the_same_request(self):
        settings_store._begin_request()
        self.assertFalse(get_bool("staff_can_export"))
        set_setting("staff_can_export", True)
        self.assertTrue(get_bool("staff_can_export"))
        Setting.objects.filter(key="staff_can_export").delete()  # queryset.delete() sends post_delete per row
        self.assertFalse(get_bool("staff_can_export"))

    def test_snapshot_does_not_survive_into_the_next_request(self):
        settings_store._begin_request()
        self.assertFalse(get_bool("staff_can_export"))
        settings_store._end_request()
        Setting.objects.create(key="staff_can_export", value="1")  # e.g. changed by another worker
        settings_store._begin_request()
        self.assertTrue(get_bool("staff_can_export"))


@override_settings(CACHES=LOCMEM)
class CacheHelperTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_value_is_computed_once_then_served(self):
        calls = []
        produce = lambda: calls.append(1) or ["a", "b"]  # noqa: E731
        self.assertEqual(cache_utils.cached("t", (1,), 60, produce), ["a", "b"])
        self.assertEqual(cache_utils.cached("t", (1,), 60, produce), ["a", "b"])
        self.assertEqual(len(calls), 1)

    def test_empty_results_are_cached_too(self):
        calls = []
        produce = lambda: calls.append(1) or []  # noqa: E731
        cache_utils.cached("t", (1,), 60, produce)
        cache_utils.cached("t", (1,), 60, produce)
        self.assertEqual(len(calls), 1)

    def test_keys_are_isolated_per_part(self):
        self.assertEqual(cache_utils.cached("t", ("user", 1), 60, lambda: "mine"), "mine")
        self.assertEqual(cache_utils.cached("t", ("user", 2), 60, lambda: "theirs"), "theirs")

    def test_bump_invalidates_a_namespace_only(self):
        cache_utils.cached("a", (1,), 60, lambda: "old")
        cache_utils.cached("b", (1,), 60, lambda: "keep")
        cache_utils.bump("a")
        self.assertEqual(cache_utils.cached("a", (1,), 60, lambda: "new"), "new")
        self.assertEqual(cache_utils.cached("b", (1,), 60, lambda: "changed"), "keep")

    def test_backend_failure_falls_back_to_computing(self):
        from unittest import mock
        with mock.patch("crm.cache_utils.cache.get", side_effect=RuntimeError("cache down")):
            self.assertEqual(cache_utils.cached("t", (9,), 60, lambda: "fresh"), "fresh")

    def test_lead_save_invalidates_report_cache(self):
        cache_utils.cached("analytics_perf", ("", ""), 60, lambda: "stale")
        make_lead()
        self.assertEqual(cache_utils.cached("analytics_perf", ("", ""), 60, lambda: "fresh"), "fresh")


@override_settings(ATTENDANCE_ENFORCED=False)
class DashboardNumbersTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.amit = User.objects.create_user("amit", password="pw", first_name="Amit")
        today = timezone.localdate()
        day = datetime.timedelta(days=1)
        make_lead(self.rahul, "L1", "9000000001", status=Lead.STATUS_PROCESSING,
                  next_followup_date=today, followup_status="pending")
        make_lead(self.rahul, "L2", "9000000002", status=Lead.STATUS_APPROVED,
                  next_followup_date=today - day, followup_status="pending")
        make_lead(self.rahul, "L3", "9000000003", status=Lead.STATUS_REJECTED,
                  next_followup_date=today + day, followup_status="pending")
        make_lead(self.rahul, "L4", "9000000004", status=Lead.STATUS_PROCESSING,
                  next_followup_date=today, followup_status="done")  # done: must not count as pending
        make_lead(self.amit, "L5", "9000000005", status=Lead.STATUS_APPROVED)
        Contact.objects.create(name="C1", phone="9100000001", current_assigned_to=self.rahul,
                               next_followup_date=today, followup_status="pending")
        Contact.objects.create(name="C2", phone="9100000002", current_assigned_to=self.rahul,
                               next_followup_date=today - day, followup_status="pending")
        Contact.objects.create(name="C3", phone="9100000003", current_assigned_to=self.amit)

    def stats(self, user):
        c = Client()
        c.force_login(user)
        r = c.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 200)
        return {s["label"]: s["value"] for s in r.context["stats"]}

    def test_admin_numbers_match_plain_filters(self):
        s = self.stats(self.admin)
        self.assertEqual(s["Total Leads"], Lead.objects.count())
        self.assertEqual(s["Total Contacts"], Contact.objects.filter(is_deleted=False).count())
        self.assertEqual(s["Today's Leads"], 5)
        self.assertEqual(s["Processing"], Lead.objects.filter(status=Lead.STATUS_PROCESSING).count())
        self.assertEqual(s["Approved"], 2)
        self.assertEqual(s["Rejected"], 1)
        # pending follow-ups = today + overdue for leads (L1, L2) and contacts (C1, C2)
        self.assertEqual(s["Pending Follow-ups"], 4)

    def test_staff_numbers_are_limited_to_their_own_records(self):
        s = self.stats(self.rahul)
        self.assertEqual(s["My Leads"], 4)
        self.assertEqual(s["My Contacts"], 2)
        self.assertEqual(s["Today's Follow-ups"], 2)       # L1 + C1
        self.assertEqual(s["Overdue Follow-ups"], 2)       # L2 + C2
        self.assertEqual(s["Processing"], 2)
        self.assertEqual(s["Approved"], 1)
        self.assertEqual(s["Rejected"], 1)

    def test_my_leads_stat_cards(self):
        c = Client()
        c.force_login(self.rahul)
        r = c.get(reverse("my_leads"))
        got = {s["label"]: s["value"] for s in r.context["stats"]}
        self.assertEqual(got["Assigned to me"], 4)
        self.assertEqual(got["Referred by me"], 0)
        self.assertEqual(got["Approved"], 1)

    def test_followups_page_counts(self):
        c = Client()
        c.force_login(self.rahul)
        r = c.get(reverse("followups"))
        by_key = {s["key"]: s for s in r.context["sections"]}
        self.assertEqual((by_key["today"]["lead_count"], by_key["today"]["contact_count"]), (1, 1))
        self.assertEqual((by_key["overdue"]["lead_count"], by_key["overdue"]["contact_count"]), (1, 1))
        self.assertEqual((by_key["upcoming"]["lead_count"], by_key["upcoming"]["contact_count"]), (1, 0))


@override_settings(ATTENDANCE_ENFORCED=False)
class StaffListPaginationTests(TestCase):
    def test_every_account_is_reachable_exactly_once(self):
        admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        for i in range(37):
            User.objects.create_user(f"user{i:02d}", password="pw", first_name="Same")
        c = Client()
        c.force_login(admin)
        seen, page_no = [], 1
        while True:
            r = c.get(reverse("staff_list"), {"per_page": 10, "page": page_no})
            self.assertEqual(r.status_code, 200)
            page = r.context["page"]
            seen += [u.pk for u in page.object_list]
            if not page.has_next():
                break
            page_no += 1
        self.assertEqual(len(seen), 38)
        self.assertEqual(len(set(seen)), 38)  # no duplicates, nothing skipped
        self.assertEqual(page.paginator.count, User.objects.count())


@override_settings(ATTENDANCE_ENFORCED=False)
class SegmentCountTests(TestCase):
    def test_staff_only_count_contacts_they_may_see(self):
        admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        rahul = User.objects.create_user("rahul", password="pw")
        amit = User.objects.create_user("amit", password="pw")
        seg_a = Segment.objects.create(name="A", created_by=admin)
        seg_b = Segment.objects.create(name="B", created_by=admin)  # empty for everybody
        mine1 = Contact.objects.create(name="m1", phone="9111111111", current_assigned_to=rahul)
        mine2 = Contact.objects.create(name="m2", phone="9222222222", current_assigned_to=rahul)
        theirs = Contact.objects.create(name="t1", phone="9333333333", current_assigned_to=amit)
        gone = Contact.objects.create(name="gone", phone="9444444444", current_assigned_to=rahul, is_deleted=True)
        for contact in (mine1, mine2, theirs, gone):
            ContactSegment.objects.create(contact=contact, segment=seg_a)
        c = Client()
        c.force_login(rahul)
        counts = {s.name: s.n for s in c.get(reverse("segment_list")).context["segments"]}
        self.assertEqual(counts, {"A": 2, "B": 0})
        c.force_login(admin)
        counts = {s.name: s.n for s in c.get(reverse("segment_list")).context["segments"]}
        # Admin count now follows access.visible_contacts too: the soft-deleted contact ("gone") is not counted anywhere,
        # exactly as the segment page's table already hides it. (It used to be the raw membership count, 4.)
        self.assertEqual(counts["A"], 3)


class SingleUserPerformanceTests(TestCase):
    def test_one_user_scope_gives_the_same_row_as_the_full_report(self):
        admin = User.objects.create_user("boss", password="pw", is_superuser=True)
        rahul = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        amit = User.objects.create_user("amit", password="pw", first_name="Amit")
        make_lead(rahul, "L1", "9000000001", status=Lead.STATUS_APPROVED, last_contacted=timezone.now())
        make_lead(rahul, "L2", "9000000002")
        make_lead(amit, "L3", "9000000003", status=Lead.STATUS_REJECTED)
        Contact.objects.create(name="C1", phone="9100000001", current_assigned_to=rahul, status=Contact.STATUS_INTERESTED)
        Contact.objects.create(name="C2", phone="9100000002", current_assigned_to=amit)
        lead = Lead.objects.get(customer_name="L1")
        AssignmentHistory.objects.create(lead=lead, action=AssignmentHistory.ACTION_TRANSFER, from_user=rahul, to_user=amit)

        full = {r["user"].pk: r for r in analytics.staff_performance({})}
        for u in (rahul, amit):
            only = analytics.staff_performance({}, user_ids=[u.pk])
            self.assertEqual(len(only), 1)
            strip = lambda row: {k: v for k, v in row.items() if k != "user"}  # noqa: E731
            self.assertEqual(strip(only[0]), strip(full[u.pk]))
        self.assertEqual(analytics.staff_performance({}, user_ids=[admin.pk]), [])  # admins are not in the report


class DropdownOptionTests(TestCase):
    def test_lead_sources_are_distinct_and_sorted(self):
        admin = User.objects.create_user("boss", password="pw", is_superuser=True)
        for i in range(3):
            make_lead(None, f"F{i}", f"900000010{i}", source="Facebook")
        make_lead(None, "G", "9000000200", source="Google")
        make_lead(None, "None", "9000000300", source="")
        self.assertEqual(lead_source_options(admin), ["Facebook", "Google"])
