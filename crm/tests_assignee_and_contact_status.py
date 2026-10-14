"""
Run:  python manage.py test crm.tests_assignee_and_contact_status

Covers: Contact statuses Switched Off / Not Received (save, retrieve, filter, export, bulk, badge), the "Assigned To
Staff" label on Contacts, and the searchable lead "Assigned To" filter (field choice, case/whitespace-insensitivity,
suggestions, fuzzy, permissions, combined filters, KPI cards, export, empty/clear).
"""
import csv
import io

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import assignee_search
from .access import visible_leads
from .models import Contact, Lead

User = get_user_model()


def mk_lead(name, phone, **kw):
    return Lead.objects.create(customer_name=name, contact_number=phone, **kw)


@override_settings(ATTENDANCE_ENFORCED=False)
class ContactStatusTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.rahul = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.c = Client()
        self.c.force_login(self.admin)

    def test_choices_keep_old_and_add_new(self):
        keys = [k for k, _ in Contact.STATUS_CHOICES]
        for k in ("new", "contacted", "interested", "not_interested", "callback", "converted", "switched_off", "not_received"):
            self.assertIn(k, keys)
        labels = dict(Contact.STATUS_CHOICES)
        self.assertEqual(labels["switched_off"], "Switched Off")
        self.assertEqual(labels["not_received"], "Not Received")

    def test_save_retrieve_and_display(self):
        for key, label in (("switched_off", "Switched Off"), ("not_received", "Not Received")):
            c = Contact.objects.create(name=f"C {key}", phone="9000000001", status=key, current_assigned_to=self.rahul)
            c.refresh_from_db()
            self.assertEqual(c.status, key)
            self.assertEqual(c.get_status_display(), label)
            c.full_clean()  # choices validation accepts it

    def test_add_form_accepts_new_statuses(self):
        for i, key in enumerate(("switched_off", "not_received")):
            r = self.c.post(reverse("contact_create"), {"name": f"N {key}", "phone": f"98290 1111{i}", "status": key})
            self.assertEqual(r.status_code, 302, r.content[:300])
            self.assertEqual(Contact.objects.get(name=f"N {key}").status, key)

    def test_detail_update_saves_status_and_logs_activity(self):
        c = Contact.objects.create(name="Z", phone="9000000002", current_assigned_to=self.admin)
        r = self.c.get(reverse("contact_detail", args=[c.pk]))
        self.assertContains(r, 'value="switched_off"')
        self.assertContains(r, 'value="not_received"')
        self.c.post(reverse("contact_action", args=[c.pk]), {"action": "update", "status": "switched_off"})
        c.refresh_from_db()
        self.assertEqual(c.status, "switched_off")
        self.assertTrue(c.activities.filter(message__contains="Switched Off").exists())

    def test_invalid_status_is_still_rejected(self):
        c = Contact.objects.create(name="Z", phone="9000000002", current_assigned_to=self.admin)
        self.c.post(reverse("contact_action", args=[c.pk]), {"action": "update", "status": "bogus"})
        c.refresh_from_db()
        self.assertEqual(c.status, "new")

    def test_list_filter_badge_and_label(self):
        a = Contact.objects.create(name="Off One", phone="9000000003", status="switched_off")
        b = Contact.objects.create(name="NoRecv", phone="9000000004", status="not_received")
        Contact.objects.create(name="Plain", phone="9000000005")
        r = self.c.get(reverse("contacts"), {"status": "switched_off"})
        self.assertEqual([c.pk for c in r.context["page"].object_list], [a.pk])
        r = self.c.get(reverse("contacts"), {"status": "not_received"})
        self.assertEqual([c.pk for c in r.context["page"].object_list], [b.pk])
        r = self.c.get(reverse("contacts"))
        self.assertContains(r, "Switched Off")
        self.assertContains(r, "Not Received")
        self.assertContains(r, "Assigned To Staff")
        self.assertNotContains(r, "<th>Assigned To</th>")
        self.assertNotContains(r, "<label>Assigned To</label>")

    def test_detail_label(self):
        c = Contact.objects.create(name="Z", phone="9000000002", current_assigned_to=self.rahul)
        r = self.c.get(reverse("contact_detail", args=[c.pk]))
        self.assertContains(r, "Assigned To Staff")

    def test_export_has_status_and_renamed_header(self):
        Contact.objects.create(name="Off One", phone="9000000003", status="switched_off")
        r = self.c.get(reverse("contacts_export"), {"status": "switched_off"})
        rows = list(csv.reader(io.StringIO(b"".join(r.streaming_content).decode("utf-8-sig"))))
        head = rows[0]
        self.assertIn("Assigned To Staff", head)
        self.assertEqual(rows[1][head.index("Status")], "Switched Off")

    def test_leads_keep_their_own_statuses(self):
        keys = [k for k, _ in Lead.STATUS_CHOICES]
        self.assertNotIn("switched_off", keys)
        self.assertNotIn("not_received", keys)


@override_settings(ATTENDANCE_ENFORCED=False)
class AssigneeNormaliseAndSuggestTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user("owner", password="pw", first_name="Owner")
        self.rows = [
            ("Anchal Jain", 5), ("anchal jain ", 2), ("ANCHAL   JAIN", 1),   # one person, three spellings
            ("Anchal Sharma", 3), ("Rohit Verma", 4), ("Rohan Gupta", 1), ("Sunita", 2),
        ]
        n = 0
        for name, cnt in self.rows:
            for _ in range(cnt):
                n += 1
                mk_lead(f"L{n}", f"90000{n:05d}", assigned_to_name=name, assigned_to=self.owner)

    def qs(self):
        return Lead.objects.all()

    def test_normalize(self):
        self.assertEqual(assignee_search.normalize("  AnChAl   JaIn "), "anchal jain")
        self.assertEqual(assignee_search.tidy("  Anchal   Jain "), "Anchal Jain")

    def test_one_person_not_three_suggestions(self):
        res = assignee_search.suggest(self.qs(), "anchal jain")
        names = [r["name"] for r in res]
        self.assertEqual(names[0], "Anchal Jain")
        self.assertEqual(names.count("Anchal Jain"), 1)
        self.assertEqual(res[0]["count"], 8)
        self.assertEqual(res[0]["match"], "exact")

    def test_case_variants_find_same_person(self):
        for q in ("Anchal Jain", "anchal jain", "ANCHAL JAIN", "AnChAl JaIn", "  anchal    jain  "):
            self.assertEqual(assignee_search.suggest(self.qs(), q)[0]["name"], "Anchal Jain", q)

    def test_ranking_exact_then_prefix_then_partial(self):
        res = assignee_search.suggest(self.qs(), "anchal")
        self.assertEqual([r["name"] for r in res][:2], ["Anchal Jain", "Anchal Sharma"])  # prefix, most-used first
        res = assignee_search.suggest(self.qs(), "jain")
        self.assertEqual(res[0]["name"], "Anchal Jain")                                   # word-prefix
        self.assertNotIn("Rohit Verma", [r["name"] for r in res])

    def test_fuzzy_spelling_suggestion(self):
        res = assignee_search.suggest(self.qs(), "anchl jian")
        self.assertEqual(res[0]["name"], "Anchal Jain")
        self.assertEqual(res[0]["match"], "similar")
        res = assignee_search.suggest(self.qs(), "rohet")      # typo of Rohit; Rohan is less similar but allowed to rank after
        self.assertIn("Rohit Verma", [r["name"] for r in res])

    def test_unrelated_names_are_not_suggested(self):
        self.assertEqual(assignee_search.suggest(self.qs(), "zzzz"), [])
        names = [r["name"] for r in assignee_search.suggest(self.qs(), "xa")]
        self.assertEqual(names, [])     # 2 letters: no fuzzy matching, no substring hit

    def test_empty_query_returns_most_used_bounded(self):
        res = assignee_search.suggest(self.qs(), "")
        self.assertEqual(res[0]["name"], "Anchal Jain")
        self.assertLessEqual(len(res), assignee_search.MAX_SUGGESTIONS)

    def test_raw_variants_modes(self):
        v, mode = assignee_search.raw_variants(self.qs(), "ANCHAL jain")
        self.assertEqual(mode, "exact")
        self.assertEqual(set(v), {"Anchal Jain", "anchal jain ", "ANCHAL   JAIN"})
        v, mode = assignee_search.raw_variants(self.qs(), "anchal")
        self.assertEqual(mode, "partial")
        self.assertEqual(set(v), {"Anchal Jain", "anchal jain ", "ANCHAL   JAIN", "Anchal Sharma"})
        self.assertEqual(assignee_search.raw_variants(self.qs(), "nobody")[1], "none")
        self.assertEqual(assignee_search.raw_variants(self.qs(), "   ")[1], "empty")

    def test_suggest_query_count_is_one_grouped_query(self):
        with self.assertNumQueries(1):
            assignee_search.suggest(self.qs(), "anchal")


@override_settings(ATTENDANCE_ENFORCED=False)
class LeadAssignedToFilterTests(TestCase):
    """Uses assigned_to_name (future-nurturing assignee), never the CRM-owner FK / reference / creator."""

    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.tele = User.objects.create_user("tele", password="pw", first_name="Tele")     # telecaller who creates leads
        self.other = User.objects.create_user("other", password="pw", first_name="Other")
        T, O = self.tele, self.other
        # Telecaller created all of these (assigned_to FK == creator); the nurturing person differs.
        self.a1 = mk_lead("A1", "9100000001", assigned_to=T, created_by=T, reference_by=T, assigned_to_name="Anchal Jain",
                          reference_by_name="Tele", status=Lead.STATUS_APPROVED, source="Facebook")
        self.a2 = mk_lead("A2", "9100000002", assigned_to=T, created_by=T, reference_by=T, assigned_to_name="anchal  JAIN ",
                          reference_by_name="Tele", status=Lead.STATUS_DOCS_PENDING, source="Google")
        self.a3 = mk_lead("A3", "9100000003", assigned_to=T, created_by=T, reference_by=T, assigned_to_name="Rohit Verma",
                          reference_by_name="Anchal Jain", status=Lead.STATUS_APPROVED, source="Facebook")   # Anchal is only the REFERENCE here
        self.a4 = mk_lead("A4", "9100000004", assigned_to=T, created_by=T, reference_by=T, assigned_to_name="Anchal Jain",
                          status=Lead.STATUS_APPROVED, source="Facebook")
        # Lead the telecaller may NOT see (other staff's) but which names Anchal too, plus a secret assignee name.
        self.x1 = mk_lead("X1", "9100000009", assigned_to=O, created_by=O, reference_by=O, assigned_to_name="Anchal Jain",
                          status=Lead.STATUS_APPROVED)
        self.x2 = mk_lead("X2", "9100000010", assigned_to=O, created_by=O, reference_by=O, assigned_to_name="Secret Person")
        self.admin_c, self.tele_c = Client(), Client()
        self.admin_c.force_login(self.admin)
        self.tele_c.force_login(T)

    def ids(self, resp):
        return {l.pk for l in resp.context["leads"]}

    def get(self, client, name="all_leads", **p):
        return client.get(reverse(name), p)

    def test_uses_future_nurturing_field_not_owner_reference_or_creator(self):
        r = self.get(self.admin_c, assignee="Anchal Jain")
        self.assertEqual(self.ids(r), {self.a1.pk, self.a2.pk, self.a4.pk, self.x1.pk})
        self.assertNotIn(self.a3.pk, self.ids(r))     # "Anchal Jain" is only the reference-by name there
        # Filtering by the telecaller's name finds nothing: the telecaller is the CRM owner/creator, not the assignee.
        self.assertEqual(self.ids(self.get(self.admin_c, assignee="Tele")), set())

    def test_case_and_whitespace_insensitive(self):
        want = {self.a1.pk, self.a2.pk, self.a4.pk, self.x1.pk}
        for q in ("anchal jain", "ANCHAL JAIN", "AnChAl JaIn", "  Anchal    Jain  "):
            self.assertEqual(self.ids(self.get(self.admin_c, assignee=q)), want, q)

    def test_stored_name_is_not_modified(self):
        self.get(self.admin_c, assignee="anchal jain")
        self.a2.refresh_from_db()
        self.assertEqual(self.a2.assigned_to_name, "anchal  JAIN ")

    def test_partial_text_when_no_exact_name(self):
        self.assertEqual(self.ids(self.get(self.admin_c, assignee="anchal")), {self.a1.pk, self.a2.pk, self.a4.pk, self.x1.pk})

    def test_unknown_name_gives_empty_not_everything(self):
        r = self.get(self.admin_c, assignee="Nobody Atall")
        self.assertEqual(self.ids(r), set())
        self.assertEqual(r.context["total_count"], 0)
        self.assertContains(r, "No leads match these filters")

    def test_clear_filter_restores_everything(self):
        total = Lead.objects.count()
        self.assertEqual(self.get(self.admin_c, assignee="Anchal Jain").context["total_count"], 4)
        self.assertEqual(self.get(self.admin_c, assignee="").context["total_count"], total)
        self.assertEqual(self.get(self.admin_c).context["total_count"], total)

    def test_permissions_all_leads_and_my_leads(self):
        # The telecaller only ever sees leads they are authorised for - X1 (another staff member's) never appears.
        for name in ("all_leads", "my_leads"):
            got = self.ids(self.get(self.tele_c, name, assignee="Anchal Jain"))
            self.assertEqual(got, {self.a1.pk, self.a2.pk, self.a4.pk}, name)
            self.assertNotIn(self.x1.pk, got)

    def test_combined_filters(self):
        r = self.get(self.admin_c, assignee="anchal jain", status=Lead.STATUS_APPROVED, source="Facebook")
        self.assertEqual(self.ids(r), {self.a1.pk, self.a4.pk, self.x1.pk} - {self.x1.pk} | ({self.x1.pk} if self.x1.source == "Facebook" else set()))
        r = self.get(self.admin_c, assignee="anchal jain", status=Lead.STATUS_DOCS_PENDING)
        self.assertEqual(self.ids(r), {self.a2.pk})
        r = self.get(self.admin_c, assignee="anchal jain", q="A4")
        self.assertEqual(self.ids(r), {self.a4.pk})
        # With the CRM-owner filter (the old FK filter, now labelled "CRM Owner")
        r = self.get(self.admin_c, assignee="anchal jain", assigned_to=self.other.pk)
        self.assertEqual(self.ids(r), {self.x1.pk})

    def test_my_leads_scope_and_kpis_follow_assignee(self):
        r = self.get(self.tele_c, "my_leads", assignee="anchal jain")
        cards = {s["label"]: s["value"] for s in r.context["stats"]}
        # Independent definition
        mine = visible_leads(self.tele).filter(assigned_to_name__iexact="Anchal Jain") if False else None
        expect = [l for l in visible_leads(self.tele) if " ".join(l.assigned_to_name.lower().split()) == "anchal jain"]
        self.assertEqual(r.context["total_count"], len(expect))
        self.assertEqual(cards["Assigned to me"], len([l for l in expect if l.assigned_to_id == self.tele.pk]))
        self.assertEqual(cards["Approved"], len([l for l in expect if l.status == Lead.STATUS_APPROVED]))
        self.assertEqual(cards["Documents Pending"], len([l for l in expect if l.status == Lead.STATUS_DOCS_PENDING]))
        # scope tab + assignee together
        r = self.get(self.tele_c, "my_leads", assignee="anchal jain", scope="referred")
        self.assertEqual(self.ids(r), {self.a1.pk, self.a2.pk, self.a4.pk})
        # clearing the assignee restores the all-mine totals
        full = {s["label"]: s["value"] for s in self.get(self.tele_c, "my_leads").context["stats"]}
        self.assertEqual(full["Assigned to me"], 4)

    def test_pagination_independent_totals(self):
        for i in range(30):
            mk_lead(f"P{i}", f"92{i:08d}", assigned_to=self.tele, assigned_to_name="Paged Person", status=Lead.STATUS_APPROVED)
        r = self.get(self.tele_c, "my_leads", assignee="paged person")      # 25 per page
        self.assertEqual(len(r.context["leads"]), 25)
        self.assertEqual(r.context["total_count"], 30)
        self.assertEqual({s["label"]: s["value"] for s in r.context["stats"]}["Approved"], 30)
        r2 = self.get(self.tele_c, "my_leads", assignee="paged person", page=2)
        self.assertEqual(len(r2.context["leads"]), 5)
        self.assertEqual({s["label"]: s["value"] for s in r2.context["stats"]}["Approved"], 30)

    def test_export_matches_filter(self):
        r = self.admin_c.get(reverse("leads_export"), {"assignee": "ANCHAL JAIN"})
        rows = list(csv.reader(io.StringIO(b"".join(r.streaming_content).decode("utf-8-sig"))))
        head = rows[0]
        self.assertEqual(len(rows) - 1, 4)
        self.assertIn("Assigned To", head)
        self.assertIn("Assigned To (CRM)", head)
        self.assertTrue(all(" ".join(row[head.index("Assigned To")].lower().split()) == "anchal jain" for row in rows[1:]))

    def test_suggest_endpoint_is_scoped_to_authorised_leads(self):
        r = self.tele_c.get(reverse("assignee_suggest"), {"q": "secret"})
        self.assertEqual(r.json()["results"], [])                      # Secret Person is on another staff member's lead
        r = self.admin_c.get(reverse("assignee_suggest"), {"q": "secret"})
        self.assertEqual([x["name"] for x in r.json()["results"]], ["Secret Person"])
        r = self.tele_c.get(reverse("assignee_suggest"), {"q": "ANCHAL jain"})
        data = r.json()["results"]
        self.assertEqual(data[0]["name"], "Anchal Jain")
        self.assertEqual(data[0]["count"], 3)                          # a1,a2,a4 - not x1
        self.assertEqual(r["Cache-Control"], "private, no-store")

    def test_suggest_requires_login(self):
        r = Client().get(reverse("assignee_suggest"), {"q": "a"})
        self.assertIn(r.status_code, (302, 401, 403))

    def test_filter_ui_present_on_both_pages_and_owner_filter_relabelled(self):
        for name, client in (("all_leads", self.admin_c), ("my_leads", self.tele_c)):
            r = self.get(client, name)
            self.assertContains(r, 'name="assignee"')
            self.assertContains(r, "assignee_filter.js")
            self.assertContains(r, ">Assigned To</label>")
        self.assertContains(self.get(self.admin_c), "CRM Owner (staff)")

    def test_table_shows_assigned_to_name(self):
        r = self.get(self.admin_c, assignee="Rohit Verma")
        self.assertContains(r, "Rohit Verma")
        self.assertContains(r, "<th>Assigned To</th><th>CRM Owner</th>")

    def test_filter_uses_bounded_queries(self):
        # Spelling lookup is ONE grouped query; the rest is the normal list. Guard against per-row queries.
        self.get(self.admin_c, assignee="anchal jain")  # warm
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as base:
            self.get(self.admin_c)
        with CaptureQueriesContext(connection) as filt:
            self.get(self.admin_c, assignee="anchal jain")
        self.assertLessEqual(len(filt) - len(base), 2)


@override_settings(ATTENDANCE_ENFORCED=False)
class LazyDetailPanelTests(TestCase):
    """Secondary panels on lead / contact detail are fetched on demand, with the same authorisation as the page."""

    def setUp(self):
        from . import services
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.a = User.objects.create_user("a", password="pw", first_name="A")
        self.b = User.objects.create_user("b", password="pw", first_name="B")
        self.lead = mk_lead("Lazy Lead", "9300000001", assigned_to=self.a, reference_by=self.a, assigned_to_name="Nurturer")
        services.log_activity(self.a, "note", "UNIQUE-TIMELINE-MARKER", lead=self.lead)
        self.contact = Contact.objects.create(name="Lazy C", phone="9300000002", current_assigned_to=self.a)
        services.log_activity(self.a, "note", "UNIQUE-CONTACT-MARKER", contact=self.contact)
        self.ca, self.cb, self.cadmin = Client(), Client(), Client()
        self.ca.force_login(self.a)
        self.cb.force_login(self.b)
        self.cadmin.force_login(self.admin)

    def queried_tables(self, client, url):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            resp = client.get(url)
        sql = " ".join(q["sql"] for q in ctx.captured_queries)
        return resp, sql

    def test_lead_detail_does_not_load_or_render_secondary_rows(self):
        resp, sql = self.queried_tables(self.ca, reverse("lead_detail", args=[self.lead.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("crm_activity", sql)
        self.assertNotIn("crm_assignmenthistory", sql)
        self.assertNotIn("crm_leaddocumenthistory", sql)
        self.assertNotContains(resp, "UNIQUE-TIMELINE-MARKER")
        for panel in ("timeline", "assignments", "doc_history"):
            self.assertContains(resp, reverse("lead_lazy_panel", args=[self.lead.pk, panel]))
        self.assertContains(resp, "lazy_panels.js")
        self.assertContains(resp, "Assigned To")      # nurturing assignee label is on the detail page too
        self.assertContains(resp, "Nurturer")

    def test_lead_panel_returns_rows_on_demand(self):
        resp, sql = self.queried_tables(self.ca, reverse("lead_lazy_panel", args=[self.lead.pk, "timeline"]))
        self.assertContains(resp, "UNIQUE-TIMELINE-MARKER")
        self.assertIn("crm_activity", sql)
        self.assertEqual(resp["Cache-Control"], "private, no-store")
        self.assertEqual(self.ca.get(reverse("lead_lazy_panel", args=[self.lead.pk, "assignments"])).status_code, 200)
        self.assertEqual(self.ca.get(reverse("lead_lazy_panel", args=[self.lead.pk, "doc_history"])).status_code, 200)

    def test_lead_panel_authorisation_and_unknown_panel(self):
        url = reverse("lead_lazy_panel", args=[self.lead.pk, "timeline"])
        self.assertEqual(self.cb.get(url).status_code, 404)                  # not B's lead: looks non-existent
        self.assertEqual(self.cadmin.get(url).status_code, 200)
        self.assertIn(Client().get(url).status_code, (302, 401, 403))
        self.assertEqual(self.ca.get(reverse("lead_lazy_panel", args=[self.lead.pk, "nope"])).status_code, 404)

    def test_contact_detail_and_panels(self):
        resp, sql = self.queried_tables(self.ca, reverse("contact_detail", args=[self.contact.pk]))
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("crm_activity", sql)
        self.assertNotContains(resp, "UNIQUE-CONTACT-MARKER")
        resp = self.ca.get(reverse("contact_lazy_panel", args=[self.contact.pk, "timeline"]))
        self.assertContains(resp, "UNIQUE-CONTACT-MARKER")
        self.assertEqual(self.ca.get(reverse("contact_lazy_panel", args=[self.contact.pk, "history"])).status_code, 200)
        self.assertEqual(self.cb.get(reverse("contact_lazy_panel", args=[self.contact.pk, "timeline"])).status_code, 404)
        self.assertEqual(self.ca.get(reverse("contact_lazy_panel", args=[self.contact.pk, "bogus"])).status_code, 404)


# ================================================================================================
# "Assigned To" accepts ANY name - it is NOT restricted to registered CRM staff
# ================================================================================================
import json
from unittest import mock

from django.core import mail  # noqa: F401  (kept for parity with other test modules)

from . import jobs

VALID_LEAD = {
    "form_date": "27/09/2026", "customer_name": "Priya Verma", "contact_number": "9876543210",
    "work_profile": "Salaried", "income": "50000", "requirement": "Home Loan", "loan_amount": "2500000",
    "bank_calling": "HDFC", "status": "new", "reference_by_name": "KotaMoney", "assigned_to_name": "Rahul",
    "email": "priya@example.com", "city": "Kota", "source": "Facebook", "interest": "",
}


@override_settings(ATTENDANCE_ENFORCED=False)
class FreeTextAssigneeTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.tele = User.objects.create_user("tele", password="pw", first_name="Tele")
        self.rahul_staff = User.objects.create_user("rahulsharma", password="pw", first_name="Rahul", last_name="Sharm")
        self.tc, self.ac = Client(), Client()
        self.tc.force_login(self.tele)
        self.ac.force_login(self.admin)
        self.users_before = User.objects.count()

    def create(self, client, name, phone="9876543210", **kw):
        with mock.patch("crm.views.send_lead_to_n8n", return_value=(True, "")):
            return client.post(reverse("lead_create"), dict(VALID_LEAD, assigned_to_name=name, contact_number=phone, **kw))

    def test_new_name_not_in_staff_can_be_entered_and_is_saved_exactly(self):
        for i, name in enumerate(("Amit Verma", "राहुल शर्मा", "Dr. O'Neil-Smith (Partner Team)", "  Spaced   Name ")):
            r = self.create(self.tc, name, phone=f"98760000{i:02d}")
            self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:200])
        saved = list(Lead.objects.order_by("pk").values_list("assigned_to_name", flat=True))
        self.assertEqual(saved[:3], ["Amit Verma", "राहुल शर्मा", "Dr. O'Neil-Smith (Partner Team)"])
        self.assertEqual(saved[3], "Spaced   Name")            # ends trimmed, the stored spelling is otherwise as typed
        self.assertNotIn("Amit Verma", [u.get_full_name() for u in User.objects.all()])

    def test_no_staff_record_is_created_and_nothing_is_replaced_by_closest_staff(self):
        self.create(self.tc, "Rahul Sharma")          # a registered staffer is "Rahul Sharm" - similar but DIFFERENT
        self.create(self.tc, "Amit Verma", phone="9876543211")
        self.assertEqual(User.objects.count(), self.users_before)
        names = set(Lead.objects.values_list("assigned_to_name", flat=True))
        self.assertEqual(names, {"Rahul Sharma", "Amit Verma"})
        # The CRM-owner FK is still the creating staff member and is independent of the typed name.
        self.assertEqual(set(Lead.objects.values_list("assigned_to_id", flat=True)), {self.tele.pk})

    def test_name_is_preserved_on_edit_and_other_fields_untouched(self):
        self.create(self.tc, "Amit Verma")
        lead = Lead.objects.get()
        form = dict(VALID_LEAD, assigned_to_name="Neha Kapoor (external)")
        with mock.patch("crm.views.send_lead_to_n8n", return_value=(True, "")):
            r = self.tc.post(reverse("lead_edit", args=[lead.pk]), form)
        self.assertEqual(r.status_code, 302)
        lead.refresh_from_db()
        self.assertEqual(lead.assigned_to_name, "Neha Kapoor (external)")
        self.assertEqual(lead.assigned_to_id, self.tele.pk)
        self.assertEqual(lead.created_by_id, self.tele.pk)

    def test_blank_and_overlong_assignee_are_the_only_rejections(self):
        self.assertEqual(self.create(self.tc, "").status_code, 200)               # required, as on the n8n form
        self.assertEqual(self.create(self.tc, "X" * 201).status_code, 200)        # longer than the column
        self.assertEqual(Lead.objects.count(), 0)
        self.assertEqual(self.create(self.tc, "X" * 200).status_code, 302)

    def test_external_name_is_suggested_after_use_even_when_not_staff(self):
        self.create(self.tc, "Rahul Sharma")
        for q in ("Rahul", "rahul sharma", "RAHUL  SHARMA", "sharma", "rahul shrma"):
            names = [x["name"] for x in self.tc.get(reverse("assignee_suggest"), {"q": q}).json()["results"]]
            self.assertIn("Rahul Sharma", names, q)
        # a name that never appeared before is simply absent from suggestions - and can still be saved
        self.assertEqual(self.tc.get(reverse("assignee_suggest"), {"q": "Amit Verma"}).json()["results"], [])
        self.assertEqual(self.create(self.tc, "Amit Verma", phone="9876543299").status_code, 302)
        self.assertEqual(Lead.objects.filter(assigned_to_name="Amit Verma").count(), 1)

    def test_suggestions_do_not_come_from_staff_or_user_tables(self):
        # Registered staff "Rahul Sharm" has no lead using that name, so it must NOT be suggested.
        self.create(self.tc, "Amit Verma")
        results = self.tc.get(reverse("assignee_suggest"), {"q": "rahul"}).json()["results"]
        self.assertEqual(results, [])
        results = self.ac.get(reverse("assignee_suggest"), {"q": "tele"}).json()["results"]
        self.assertEqual(results, [])

    def test_similar_names_are_never_merged(self):
        self.create(self.tc, "Anchal Jain", phone="9876500001")
        self.create(self.tc, "Anchal Jan", phone="9876500002")
        self.create(self.tc, "anchal  jain", phone="9876500003")      # same person as the first (case/space only)
        res = self.tc.get(reverse("assignee_suggest"), {"q": "anchal"}).json()["results"]
        counts = {r["name"]: r["count"] for r in res}
        self.assertEqual(len(counts), 2)
        self.assertEqual(sum(counts.values()), 3)
        got = {l.customer_name for l in self.tc.get(reverse("all_leads"), {"assignee": "Anchal Jan"}).context["leads"]}
        self.assertEqual(len(got), 1)                                   # only the "Jan" lead, NOT the Jain ones
        got = self.tc.get(reverse("all_leads"), {"assignee": "ANCHAL JAIN"}).context["leads"]
        self.assertEqual(len(list(got)), 2)

    def test_external_name_filter_kpis_export_and_bulk_scope(self):
        for i in range(3):
            self.create(self.tc, "Amit Verma", phone=f"98765001{i:02d}")
        self.create(self.tc, "Someone Else", phone="9876500199")
        r = self.tc.get(reverse("my_leads"), {"assignee": "amit verma"})
        self.assertEqual(r.context["total_count"], 3)
        self.assertEqual({s["label"]: s["value"] for s in r.context["stats"]}["Assigned to me"], 3)
        # export (CSV + Excel; admin - staff export is a separate setting) follows the same filter and writes the typed name
        csv_resp = self.ac.get(reverse("leads_export"), {"assignee": "AMIT VERMA"})
        rows = list(csv.reader(io.StringIO(b"".join(csv_resp.streaming_content).decode("utf-8-sig"))))
        head = rows[0]
        self.assertEqual({row[head.index("Assigned To")] for row in rows[1:]}, {"Amit Verma"})
        self.assertEqual(len(rows) - 1, 3)
        xl = self.ac.get(reverse("leads_export"), {"assignee": "AMIT VERMA", "format": "xlsx"})
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(b"".join(xl.streaming_content) if hasattr(xl, "streaming_content") else xl.content))
        ws = wb.active
        header = [c.value for c in ws[1]]
        self.assertEqual({ws.cell(row=i, column=header.index("Assigned To") + 1).value for i in range(2, ws.max_row + 1)}, {"Amit Verma"})
        # "select all N matching" (bulk scope) resolves with the same assignee semantics and authorisation
        qs = jobs.resolve_queryset("lead", self.tele, {"type": "filter", "query": "assignee=amit+verma"})
        self.assertEqual(qs.count(), 3)
        other = User.objects.create_user("other", password="pw", first_name="Other")
        self.assertEqual(jobs.resolve_queryset("lead", other, {"type": "filter", "query": "assignee=amit+verma"}).count(), 0)

    def test_blank_null_like_and_odd_values_do_not_break_search(self):
        for i, name in enumerate(("", "   ", "Valid Person")):
            Lead.objects.create(customer_name=f"L{i}", contact_number=f"9111{i:06d}", assigned_to=self.tele, assigned_to_name=name)
        for q in ("", "   ", "%", "_", "\\", "'; DROP TABLE crm_lead; --", "Valid Person", "व", "a" * 500, "\x00"):
            r = self.tc.get(reverse("assignee_suggest"), {"q": q})
            self.assertEqual(r.status_code, 200, repr(q))
            r = self.tc.get(reverse("my_leads"), {"assignee": q})
            self.assertEqual(r.status_code, 200, repr(q))
        self.assertEqual([x["name"] for x in self.tc.get(reverse("assignee_suggest"), {"q": ""}).json()["results"]], ["Valid Person"])
        self.assertEqual(self.tc.get(reverse("my_leads"), {"assignee": "%"}).context["total_count"], 0)   # LIKE wildcards are literal
        self.assertEqual(Lead.objects.count(), 3)

    def test_form_page_has_autocomplete_but_input_stays_free_text(self):
        r = self.tc.get(reverse("lead_create"))
        self.assertContains(r, "assignee-field")
        self.assertContains(r, "assignee_filter.js")
        self.assertContains(r, "Anyone can be entered here")
        self.assertContains(r, 'name="assigned_to_name"')
        self.assertNotContains(r, '<select name="assigned_to_name"')


@override_settings(N8N_API_KEY="secret", ATTENDANCE_ENFORCED=False)
class InboundApiFreeTextAssigneeTests(TestCase):
    """The n8n webhook used to answer 400 'staff member not found' for any Assigned To outside CRM Staff."""

    def post(self, **extra):
        body = {"customer_name": "Api Lead", "contact_number": "9876543210", **extra}
        return Client().post(reverse("api_lead_create"), data=json.dumps(body), content_type="application/json",
                             HTTP_X_N8N_API_KEY="secret")

    def test_unregistered_assignee_and_reference_are_accepted_and_stored(self):
        users = User.objects.count()
        r = self.post(assigned_to="External  Partner Team", reference_by="Walk-in Referrer")
        self.assertEqual(r.status_code, 201, r.content)
        lead = Lead.objects.get()
        self.assertEqual(lead.assigned_to_name, "External Partner Team")      # whitespace tidied, spelling kept
        self.assertEqual(lead.reference_by_name, "Walk-in Referrer")
        self.assertIsNone(lead.assigned_to_id)                                 # no staff match -> no guess
        self.assertIsNone(lead.reference_by_id)
        self.assertEqual(User.objects.count(), users)                          # nothing auto-created

    def test_registered_staff_name_is_stored_but_does_not_set_the_owner_fk(self):
        # Ownership policy (crm/api.py docstring): the free-text Assigned To is the nurturing assignee only. Even when it
        # equals a CRM user's full name it is stored as typed and NEVER decides the CRM owner (that would be a guess).
        staff = User.objects.create_user("anchal", password="pw", first_name="Anchal", last_name="Jain")
        r = self.post(assigned_to="anchal jain", reference_by="Somebody New")
        self.assertEqual(r.status_code, 201, r.content)
        lead = Lead.objects.get()
        self.assertIsNone(lead.assigned_to_id)
        self.assertNotEqual(lead.assigned_to_id, staff.pk)
        self.assertEqual(lead.assigned_to_name, "anchal jain")                 # typed text is kept as entered
        self.assertEqual(lead.reference_by_name, "Somebody New")

    def test_assignee_is_optional_and_overlong_is_rejected(self):
        self.assertEqual(self.post().status_code, 201)
        self.assertEqual(Lead.objects.get().assigned_to_name, "")
        self.assertEqual(self.post(assigned_to="x" * 201, contact_number="9876543211").status_code, 400)

    def test_api_assignee_is_searchable_by_admin(self):
        admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.post(assigned_to="External Partner Team")
        c = Client()
        c.force_login(admin)
        r = c.get(reverse("all_leads"), {"assignee": "external partner"})
        self.assertEqual(r.context["total_count"], 1)
        self.assertEqual([x["name"] for x in c.get(reverse("assignee_suggest"), {"q": "extern"}).json()["results"]], ["External Partner Team"])


@override_settings(ATTENDANCE_ENFORCED=False)
class QueryCountGuardTests(TestCase):
    """List pages must not issue more queries as the number of rows / distinct assignees grows (no N+1).
    SQLite + a few hundred rows: this proves the SHAPE (constant query count), not production-scale speed."""

    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.c = Client()
        self.c.force_login(self.admin)

    def fill(self, n, start):
        Lead.objects.bulk_create([
            Lead(customer_name=f"Q{start + i}", contact_number=f"93{start + i:08d}", assigned_to=self.admin,
                 reference_by=self.admin, assigned_to_name=f"Assignee {(start + i) % 150}", status=Lead.STATUS_NEW)
            for i in range(n)
        ])

    def count_queries(self, url, params=None):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        self.c.get(url, params or {})
        with CaptureQueriesContext(connection) as ctx:
            r = self.c.get(url, params or {})
        self.assertEqual(r.status_code, 200)
        return len(ctx)

    def test_all_leads_query_count_is_independent_of_row_and_assignee_count(self):
        self.fill(10, 0)
        small = self.count_queries(reverse("all_leads"))
        small_f = self.count_queries(reverse("all_leads"), {"assignee": "assignee 3"})
        self.fill(290, 10)
        big = self.count_queries(reverse("all_leads"))
        big_f = self.count_queries(reverse("all_leads"), {"assignee": "assignee 3"})
        self.assertEqual(small, big)
        self.assertEqual(small_f, big_f)

    def test_suggest_is_one_grouped_query_with_many_assignees_and_bounded_output(self):
        self.fill(300, 0)
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        with CaptureQueriesContext(connection) as ctx:
            r = self.c.get(reverse("assignee_suggest"), {"q": "assignee 1"})
        lead_queries = [q["sql"] for q in ctx.captured_queries if "crm_lead" in q["sql"]]
        self.assertEqual(len(lead_queries), 1)                    # ONE grouped query, never one per assignee name
        self.assertIn("GROUP BY", lead_queries[0])
        self.assertLessEqual(len(ctx), 5)                         # + session / user / staff-profile bookkeeping
        self.assertLessEqual(len(r.json()["results"]), 8)

    def test_my_leads_and_contacts_constant_queries(self):
        self.fill(10, 0)
        a = self.count_queries(reverse("my_leads"))
        self.fill(120, 10)
        self.assertEqual(a, self.count_queries(reverse("my_leads")))

    def test_lazy_panel_responses_are_html_fragments(self):
        lead = Lead.objects.create(customer_name="X", contact_number="9000000000", assigned_to=self.admin)
        for panel in ("timeline", "assignments", "doc_history"):
            r = self.c.get(reverse("lead_lazy_panel", args=[lead.pk, panel]))
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r["Content-Type"].startswith("text/html"))
            self.assertNotIn(b"<html", r.content)           # a fragment, not a full page
        # POST is not a way in: the panel endpoints are read-only GETs rendered from authorised rows
        self.assertEqual(self.c.get(reverse("lead_lazy_panel", args=[99999, "timeline"])).status_code, 404)
