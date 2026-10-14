"""
Global KPI / summary-card synchronisation.

Rule under test: wherever a page has filters, the summary cards describe the SAME authorised, filtered dataset the
table lists (never just the current page), clearing the filters restores the defaults, and cards that are
intentionally global stay global.

Every expectation below is computed from plain, independent ORM filters (the "straightforward definition"), never
from the code under test, so a mistake in an aggregate cannot hide.

Run:  python manage.py test crm.tests_summary_sync
"""
import csv
import io
import datetime

from django.contrib.auth import get_user_model
from django.http import QueryDict
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import analytics, attendance_admin as aa, filters, jobs, services
from .models import CallRecord, Contact, ContactSegment, DocumentType, Lead, LeadDocument, Segment
from .tests_attendance import Clock, at
from .tests_staff_attendance import Base as AttendanceBase

User = get_user_model()


def stats(resp):
    return {s["label"]: s["value"] for s in resp.context["stats"]}


def lead(name, phone, **kw):
    return Lead.objects.create(customer_name=name, contact_number=phone, **kw)


# ============================================================================================ My Leads
@override_settings(ATTENDANCE_ENFORCED=False)
class MyLeadsCardsFollowFiltersTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.amit = User.objects.create_user("amit", password="pw", first_name="Amit")
        R, A = self.rahul, self.amit
        # assigned to Rahul
        lead("Asha Mehta", "9000000001", assigned_to=R, reference_by=R, status=Lead.STATUS_DOCS_PENDING, source="Facebook")
        lead("Bina Shah", "9000000002", assigned_to=R, reference_by=A, status=Lead.STATUS_DOCS_COMPLETE, source="Google")
        lead("Chetan Rao", "9000000003", assigned_to=R, reference_by=A, status=Lead.STATUS_MEETING_REQUESTED, source="Facebook")
        lead("Deepa Jain", "9000000004", assigned_to=R, reference_by=R, status=Lead.STATUS_APPROVED, source="Facebook")
        # referred by Rahul, now handled by Amit (still visible: referrer_keeps_access defaults on)
        lead("Esha Nair", "9000000005", assigned_to=A, reference_by=R, status=Lead.STATUS_APPROVED, source="Google")
        # nobody Rahul may see
        lead("Farhan Ali", "9000000006", assigned_to=A, reference_by=A, status=Lead.STATUS_APPROVED, source="Facebook")
        self.c = Client()
        self.c.force_login(R)

    def get(self, **params):
        return self.c.get(reverse("my_leads"), params)

    def expected(self, **flt):
        """Independent definition: authorised leads narrowed by plain ORM filters, then counted per card."""
        from .access import visible_leads
        qs = visible_leads(self.rahul).filter(**flt)
        s = Lead
        return {
            "Assigned to me": qs.filter(assigned_to=self.rahul).count(),
            "Referred by me": qs.filter(reference_by=self.rahul).count(),
            "Documents Pending": qs.filter(status=s.STATUS_DOCS_PENDING).count(),
            "Documents Complete": qs.filter(status=s.STATUS_DOCS_COMPLETE).count(),
            "Meeting Requested": qs.filter(status=s.STATUS_MEETING_REQUESTED).count(),
            "Approved": qs.filter(status=s.STATUS_APPROVED).count(),
        }

    def test_default_cards_cover_everything_the_user_may_see_and_nothing_else(self):
        got = stats(self.get())
        self.assertEqual(got, self.expected())
        self.assertEqual(got["Approved"], 2)      # Farhan's approved lead is NOT counted (tenant isolation)

    def test_status_filter_narrows_the_cards(self):
        self.assertEqual(stats(self.get(status=Lead.STATUS_APPROVED)), self.expected(status=Lead.STATUS_APPROVED))
        self.assertEqual(stats(self.get(status=Lead.STATUS_APPROVED))["Documents Pending"], 0)

    def test_search_narrows_the_cards(self):
        got = stats(self.get(q="Asha"))
        self.assertEqual(got["Assigned to me"], 1)
        self.assertEqual(got["Documents Pending"], 1)
        self.assertEqual(got["Approved"], 0)

    def test_source_filter_narrows_the_cards(self):
        self.assertEqual(stats(self.get(source="Google")), self.expected(source__iexact="Google"))

    def test_several_filters_at_once(self):
        got = stats(self.get(source="Facebook", status=Lead.STATUS_APPROVED))
        self.assertEqual(got, self.expected(source__iexact="Facebook", status=Lead.STATUS_APPROVED))
        self.assertEqual((got["Assigned to me"], got["Referred by me"], got["Approved"]), (1, 1, 1))

    def test_scope_tab_narrows_the_cards(self):
        got = stats(self.get(scope="referred"))
        self.assertEqual(got, self.expected(reference_by=self.rahul))
        got = stats(self.get(scope="assigned"))
        self.assertEqual(got, self.expected(assigned_to=self.rahul))

    def test_scope_and_filter_together(self):
        got = stats(self.get(scope="assigned", status=Lead.STATUS_APPROVED))
        self.assertEqual(got, self.expected(assigned_to=self.rahul, status=Lead.STATUS_APPROVED))

    def test_empty_result_gives_zeros_not_stale_numbers(self):
        got = stats(self.get(q="no such customer"))
        self.assertEqual(set(got.values()), {0})

    def test_clearing_filters_restores_the_defaults(self):
        self.get(status=Lead.STATUS_APPROVED)
        self.assertEqual(stats(self.get()), self.expected())

    def test_cards_are_not_limited_to_the_current_page(self):
        for i in range(30):                                   # 36 leads in total -> My Leads paginates at 25
            lead(f"Bulk {i}", f"9111111{i:03d}", assigned_to=self.rahul, reference_by=self.rahul, status=Lead.STATUS_APPROVED)
        for page in (1, 2):
            resp = self.get(page=page)
            self.assertEqual(stats(resp)["Approved"], 2 + 30)
            self.assertEqual(resp.context["total_count"], 5 + 30)
        self.assertEqual(len(self.get(page=1).context["leads"]), 25)

    def test_cards_match_the_table_total_when_filtered(self):
        resp = self.get(status=Lead.STATUS_APPROVED)
        self.assertEqual(resp.context["total_count"], 2)
        self.assertEqual(stats(resp)["Approved"], resp.context["total_count"])

    def test_the_scope_tab_survives_applying_and_clearing_filters(self):
        resp = self.get(scope="referred")
        self.assertContains(resp, 'name="scope" value="referred"')
        self.assertContains(resp, "?scope=referred")

    def test_unknown_scope_is_ignored(self):
        self.assertEqual(stats(self.get(scope="everyone")), self.expected())

    def test_a_staff_member_never_sees_counts_from_another_users_records(self):
        c = Client(); c.force_login(self.amit)
        got = stats(c.get(reverse("my_leads")))
        self.assertEqual(got["Assigned to me"], 2)            # Esha + Farhan
        self.assertEqual(got["Referred by me"], 3)            # Bina + Chetan + Farhan


# ================================================================ export / bulk use the same scope definition
@override_settings(ATTENDANCE_ENFORCED=False)
class ScopeIsPartOfTheSharedFilterTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = User.objects.create_user("rahul", password="pw")
        self.amit = User.objects.create_user("amit", password="pw")
        self.l_assigned = lead("Assigned", "9000000011", assigned_to=self.rahul, reference_by=self.amit)
        self.l_referred = lead("Referred", "9000000012", assigned_to=self.amit, reference_by=self.rahul)

    def test_filter_leads_applies_the_scope_only_when_given_a_user(self):
        from .access import visible_leads
        base = visible_leads(self.rahul)
        got = filters.filter_leads(base, QueryDict("scope=assigned"), user=self.rahul)
        self.assertEqual([l.pk for l in got], [self.l_assigned.pk])
        got = filters.filter_leads(base, QueryDict("scope=referred"), user=self.rahul)
        self.assertEqual([l.pk for l in got], [self.l_referred.pk])
        self.assertEqual(filters.filter_leads(base, QueryDict("scope=assigned")).count(), 2)  # no user -> unchanged

    def test_bulk_select_all_matching_resolves_the_same_set_as_the_scoped_list(self):
        scope = {"type": "filter", "query": "scope=referred"}
        got = jobs.resolve_queryset("lead", self.rahul, scope)
        self.assertEqual([l.pk for l in got], [self.l_referred.pk])

    def test_export_of_a_scoped_list_contains_only_that_scope(self):
        c = Client(); c.force_login(self.rahul)
        from .settings_store import set_setting
        set_setting("staff_can_export", "1")
        resp = c.get(reverse("leads_export"), {"scope": "assigned"})
        body = b"".join(resp.streaming_content).decode() if resp.streaming else resp.content.decode()
        rows = list(csv.reader(io.StringIO(body)))
        text = " ".join(" ".join(r) for r in rows[1:])
        self.assertIn("Assigned", text)
        self.assertNotIn("Referred", text)


# ============================================================================================ Segments
@override_settings(ATTENDANCE_ENFORCED=False)
class SegmentSizesTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = User.objects.create_user("rahul", password="pw")
        self.amit = User.objects.create_user("amit", password="pw")
        self.seg = Segment.objects.create(name="Hot", created_by=self.admin)
        mine = Contact.objects.create(name="m", phone="9111111111", current_assigned_to=self.rahul)
        theirs = Contact.objects.create(name="t", phone="9222222222", current_assigned_to=self.amit)
        gone = Contact.objects.create(name="g", phone="9333333333", current_assigned_to=self.rahul, is_deleted=True)
        for c in (mine, theirs, gone):
            ContactSegment.objects.create(contact=c, segment=self.seg)

    def test_staff_detail_total_counts_only_contacts_they_may_see(self):
        c = Client(); c.force_login(self.rahul)
        resp = c.get(reverse("segment_detail", args=[self.seg.pk]))
        self.assertEqual(resp.context["total_all"], 1)         # not 3: no leaking Amit's contact or the deleted one
        self.assertEqual(resp.context["total_count"], 1)

    def test_admin_detail_total_excludes_soft_deleted_contacts(self):
        c = Client(); c.force_login(self.admin)
        resp = c.get(reverse("segment_detail", args=[self.seg.pk]))
        self.assertEqual(resp.context["total_all"], 2)
        self.assertEqual(resp.context["total_count"], 2)

    def test_total_all_stays_the_segment_size_while_total_count_follows_filters(self):
        c = Client(); c.force_login(self.admin)
        resp = c.get(reverse("segment_detail", args=[self.seg.pk]), {"q": "no match"})
        self.assertEqual((resp.context["total_all"], resp.context["total_count"]), (2, 0))

    def test_list_count_and_detail_agree_for_every_role(self):
        for user in (self.admin, self.rahul, self.amit):
            c = Client(); c.force_login(user)
            listed = {s.name: s.n for s in c.get(reverse("segment_list")).context["segments"]}["Hot"]
            detail = c.get(reverse("segment_detail", args=[self.seg.pk])).context["total_all"]
            self.assertEqual(listed, detail, user.username)


# ================================================================================ Document analytics
@override_settings(ATTENDANCE_ENFORCED=False)
class DocumentAnalyticsCardsTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        DocumentType.objects.get_or_create(name="PAN card", defaults={"is_default": True, "is_required": True})
        self.done = lead("Done", "9000000021")
        self.empty1 = lead("Empty 1", "9000000022")
        self.empty2 = lead("Empty 2", "9000000023")
        for dt in services.active_doc_types().filter(is_required=True, is_default=True):
            LeadDocument.objects.update_or_create(lead=self.done, document_type=dt, defaults={"received": True})

    def report(self, **params):
        return analytics.document_report(params)

    def test_no_filter_cards_cover_all_leads(self):
        r = self.report()
        self.assertEqual((r["total"], r["complete"], r["not_started"], r["pending"]), (3, 1, 2, 2))

    def test_status_filter_narrows_the_cards_like_the_tables(self):
        r = self.report(doc_status="complete")
        self.assertEqual((r["total"], r["complete"], r["not_started"], r["partial"], r["pending"]), (1, 1, 0, 0, 0))
        self.assertEqual(sum(row["n"] for row in r["staff_rows"]), 0)    # unassigned leads: not in the staff table
        r = self.report(doc_status="not_started")
        self.assertEqual((r["total"], r["complete"], r["not_started"], r["pending"]), (2, 0, 2, 2))

    def test_staff_filter_narrows_the_cards(self):
        rahul = User.objects.create_user("rahul", password="pw")
        Lead.objects.filter(pk=self.empty1.pk).update(assigned_to=rahul)
        r = self.report(staff=str(rahul.pk))
        self.assertEqual((r["total"], r["not_started"], r["complete"]), (1, 1, 0))

    def test_empty_result(self):
        r = self.report(staff="999999")
        self.assertEqual((r["total"], r["complete"], r["partial"], r["not_started"], r["pending"]), (0, 0, 0, 0, 0))

    def test_page_renders_with_a_filter(self):
        c = Client(); c.force_login(self.admin)
        resp = c.get(reverse("document_analytics"), {"doc_status": "complete"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["r"]["total"], 1)


# ================================================================================ Calling analytics
@override_settings(ATTENDANCE_ENFORCED=False)
class CallingTotalsFollowFiltersTests(TestCase):
    """Already correct before this work (cards and list share analytics.overview_queryset); pinned so it stays so."""

    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = User.objects.create_user("rahul", password="pw")
        self.amit = User.objects.create_user("amit", password="pw")
        now = datetime.datetime.now(datetime.timezone.utc)
        n = 0
        for staff, status, days_ago in ((self.rahul, "answered", 0), (self.rahul, "missed", 0), (self.amit, "answered", 0),
                                        (self.rahul, "answered", 3)):
            n += 1
            CallRecord.objects.create(staff=staff, phone_number=f"900000{n:04d}", direction="outgoing", status=status,
                                      started_at=now - datetime.timedelta(days=days_ago), duration_seconds=60,
                                      external_call_id=f"ext-{n}")

    def test_staff_filter_and_range_both_apply(self):
        c = Client(); c.force_login(self.admin)
        t = c.get(reverse("calling_dashboard"), {"range": "all"}).context["totals"]
        self.assertEqual((t["total"], t["answered"]), (4, 3))
        t = c.get(reverse("calling_dashboard"), {"range": "all", "staff": self.rahul.pk}).context["totals"]
        self.assertEqual((t["total"], t["answered"], t["missed"]), (3, 2, 1))
        t = c.get(reverse("calling_dashboard"), {"range": "today", "staff": self.rahul.pk}).context["totals"]
        self.assertEqual((t["total"], t["answered"]), (2, 1))

    def test_a_staff_member_only_ever_sees_their_own_calls(self):
        c = Client(); c.force_login(self.amit)
        t = c.get(reverse("calling_dashboard"), {"range": "all", "staff": self.rahul.pk}).context["totals"]
        self.assertEqual(t["total"], 1)


# ============================================================================ Staff attendance cards
class AttendanceCardsFollowFiltersTests(AttendanceBase):
    """Fixture (see tests_staff_attendance.Base): 27/09/2026 18:45 IST, 6 eligible staff, 5 records today."""

    def test_unfiltered_cards_are_unchanged_for_today(self):
        s = self.get().context["summary"]
        self.assertEqual((s["total"], s["started"], s["not_started"]), (6, 5, 1))
        self.assertFalse(s["is_range"])

    def test_staff_filter_narrows_the_cards(self):
        s = self.get(staff=self.sonia.pk).context["summary"]      # Sonia has no record today
        self.assertEqual((s["total"], s["started"], s["not_started"], s["present"]), (1, 0, 1, 0))

    def test_search_narrows_the_cards(self):
        s = self.get(q="rahul").context["summary"]
        self.assertEqual((s["total"], s["started"], s["working"], s["present"]), (1, 1, 1, 1))

    def test_date_filter_moves_the_cards_to_that_day(self):
        resp = self.get(date="26/09/2026")                         # nobody has a record that day; the day is over
        s = resp.context["summary"]
        self.assertEqual((s["started"], s["absent"], s["not_started"]), (0, 6, 0))
        self.assertEqual(sum(1 for r in resp.context["rows"] if r.status == "absent"), s["absent"])

    def test_status_filter_refines_the_table_not_the_cards(self):
        everything = self.get().context["summary"]
        resp = self.get(status="half_day")
        self.assertEqual(resp.context["summary"], everything)
        self.assertEqual([r.user.username for r in resp.context["rows"]], ["neha"])

    def test_range_cards_are_derived_from_the_same_rows_as_the_table(self):
        resp = self.get(date="26/09/2026", date_to="27/09/2026")
        s = resp.context["summary"]
        rows = resp.context["rows"]
        self.assertTrue(s["is_range"])
        self.assertEqual(len(rows), 12)                            # 6 staff x 2 days, one page
        from collections import Counter
        c = Counter(r.status for r in rows)
        self.assertEqual((s["present"], s["half_day"], s["absent"], s["not_started"]),
                         (c["present"], c["half_day"], c["absent"], c["not_started"]))
        self.assertEqual(s["started"], sum(1 for r in rows if r.rec))
        self.assertEqual(s["total"], 6)

    def test_range_cards_follow_staff_and_ignore_the_status_breakdown(self):
        base = self.get(date="26/09/2026", date_to="27/09/2026", staff=self.neha.pk)
        s = base.context["summary"]
        self.assertEqual((s["total"], s["started"], s["half_day"], s["absent"]), (1, 1, 1, 1))
        refined = self.get(date="26/09/2026", date_to="27/09/2026", staff=self.neha.pk, status="half_day")
        self.assertEqual(refined.context["summary"], s)
        self.assertEqual(len(refined.context["rows"]), 1)

    def test_deactivated_staff_are_still_excluded_from_range_cards(self):
        self.rec(self.inactive, at(10, 0), at(19, 0), day=26)
        s = self.get(date="26/09/2026", date_to="27/09/2026").context["summary"]
        self.assertEqual(s["total"], 6)

    def test_card_links_keep_the_population_filters(self):
        resp = self.get(q="neha", date="27/09/2026")
        self.assertContains(resp, "date=27%2F09%2F2026&amp;q=neha&status=half_day")

    def test_future_dates_show_no_absentees(self):
        s = self.get(date="30/09/2026").context["summary"]
        self.assertEqual((s["started"], s["absent"], s["not_started"]), (0, 0, 0))


# ===================================================================== intentionally global dashboards
@override_settings(ATTENDANCE_ENFORCED=False)
class GlobalDashboardsStayGlobalTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = User.objects.create_user("rahul", password="pw")
        lead("A", "9000000031", assigned_to=self.rahul, status=Lead.STATUS_APPROVED)
        lead("B", "9000000032", assigned_to=self.rahul, status=Lead.STATUS_REJECTED)

    def test_dashboard_ignores_query_string_filters(self):
        c = Client(); c.force_login(self.admin)
        plain = {s["label"]: s["value"] for s in c.get(reverse("dashboard")).context["stats"]}
        filtered = {s["label"]: s["value"] for s in c.get(reverse("dashboard"), {"status": "approved", "q": "A"}).context["stats"]}
        self.assertEqual(plain, filtered)
        self.assertEqual(plain["Total Leads"], 2)

    def test_reports_page_is_a_global_status_breakdown(self):
        c = Client(); c.force_login(self.admin)
        resp = c.get(reverse("reports"), {"status": "approved"})
        self.assertEqual(resp.context["total"], 2)
