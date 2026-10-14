"""
Run:  python manage.py test crm.tests_n8n_ownership --settings=kota_money.settings_test

Regression tests for the inbound n8n ownership policy (see the crm/api.py module docstring):

* ``assigned_to`` / ``reference_by`` in the n8n payload are FREE TEXT. They are stored as typed in
  ``Lead.assigned_to_name`` / ``Lead.reference_by_name`` and are never required to match CRM Staff, never
  rejected, never used to pick the CRM owner (``Lead.assigned_to``), the CRM referrer (``Lead.reference_by``)
  or ``Lead.created_by``.
* CRM ownership comes only from the separate explicit ``crm_owner`` key; without a valid one the lead is
  unowned (admin-only) until an admin assigns it.

Every behaviour is checked for BOTH kinds of nurturing assignee: one whose name equals a CRM staff member
(username / e-mail / full name) and an external person who is not in the CRM at all.
"""
import csv
import io
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import access, services
from .models import Activity, AssignmentHistory, AuditLog, Lead, StaffProfile
from .n8n_integration import build_lead_payload, build_sync_payload
from .settings_store import set_setting

User = get_user_model()

EXTERNAL = "Vikram Partner Desk"
FORM = {
    "form_date": "27/09/2026", "customer_name": "Form Lead", "contact_number": "9811111111",
    "work_profile": "Salaried", "income": "50000", "requirement": "Home Loan", "loan_amount": "2500000",
    "bank_calling": "HDFC", "status": "new", "reference_by_name": "KotaMoney", "assigned_to_name": EXTERNAL,
}


def make_user(username, role=StaffProfile.ROLE_STAFF, status=StaffProfile.STATUS_ACTIVE, **kw):
    user = User.objects.create_user(username, password="pw", **kw)
    profile, _ = StaffProfile.objects.get_or_create(user=user)
    profile.role, profile.status = role, status
    profile.save()
    return user


def client_for(user):
    c = Client()
    c.force_login(user)
    return c


@override_settings(N8N_API_KEY="secret", ATTENDANCE_ENFORCED=False)
class OwnershipTestBase(TestCase):
    def setUp(self):
        self.admin = make_user("boss", role=StaffProfile.ROLE_ADMIN, is_superuser=True, is_staff=True)
        # Registered staff whose name an n8n submitter might type as the nurturing assignee.
        self.anchal = make_user("anchal", first_name="Anchal", last_name="Jain", email="anchal@kota.test")
        self.other = make_user("other", first_name="Other", last_name="Person")
        self.admin_c, self.anchal_c, self.other_c = client_for(self.admin), client_for(self.anchal), client_for(self.other)
        self._n = 0

    def post(self, **extra):
        self._n += 1
        body = {"customer_name": f"Api Lead {self._n}", "contact_number": f"98765{self._n:05d}", **extra}
        return Client().post(reverse("api_lead_create"), data=json.dumps(body), content_type="application/json",
                             HTTP_X_N8N_API_KEY="secret")

    def create_ok(self, **extra):
        r = self.post(**extra)
        self.assertEqual(r.status_code, 201, r.content)
        return Lead.objects.get(pk=r.json()["lead_id"]), r.json()

    def csv_rows(self, client, **params):
        r = client.get(reverse("leads_export"), dict(params, format="csv"))
        self.assertEqual(r.status_code, 200)
        text = b"".join(r.streaming_content).decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))


# ======================================================================================= inbound API
class InboundAssigneeStorageTests(OwnershipTestBase):
    def test_external_assignee_is_accepted_stored_and_creates_no_account(self):
        users, profiles = User.objects.count(), StaffProfile.objects.count()
        lead, body = self.create_ok(assigned_to="  Vikram   Partner Desk ", reference_by="Walk-in  Referrer")
        self.assertEqual(lead.assigned_to_name, EXTERNAL)               # whitespace tidied, spelling kept
        self.assertEqual(lead.reference_by_name, "Walk-in Referrer")
        self.assertIsNone(lead.assigned_to_id)                         # CRM owner: unowned, not guessed
        self.assertIsNone(lead.reference_by_id)
        self.assertIsNone(lead.created_by_id)
        self.assertIsNone(lead.original_assigned_to_id)
        self.assertFalse(body["crm_owner_assigned"])
        self.assertNotIn("warnings", body)                             # nothing went wrong: no crm_owner was sent
        self.assertEqual((User.objects.count(), StaffProfile.objects.count()), (users, profiles))
        self.assertFalse(AssignmentHistory.objects.filter(lead=lead).exists())   # nobody was assigned

    def test_staff_named_assignee_is_stored_as_typed_but_never_becomes_the_owner(self):
        for typed in ("anchal", "ANCHAL", "anchal@kota.test", "Anchal Jain", "anchal jain", "  Anchal   Jain  "):
            with self.subTest(typed=typed):
                lead, _ = self.create_ok(assigned_to=typed, reference_by=typed)
                self.assertEqual(lead.assigned_to_name, " ".join(typed.split()))
                self.assertEqual(lead.reference_by_name, " ".join(typed.split()))
                self.assertIsNone(lead.assigned_to_id)
                self.assertIsNone(lead.reference_by_id)
                self.assertIsNone(lead.created_by_id)
                self.assertFalse(AssignmentHistory.objects.filter(lead=lead).exists())

    def test_admin_name_inactive_staff_and_ambiguous_names_grant_nothing(self):
        make_user("gone", status=StaffProfile.STATUS_INACTIVE, first_name="Gone", last_name="Away")
        make_user("twin1", first_name="Twin", last_name="Name")
        make_user("twin2", first_name="Twin", last_name="Name")
        for typed in ("boss", "gone", "Gone Away", "Twin Name"):
            with self.subTest(typed=typed):
                lead, _ = self.create_ok(assigned_to=typed)
                self.assertEqual(lead.assigned_to_name, typed)
                self.assertIsNone(lead.assigned_to_id)

    def test_only_blank_and_overlong_names_are_the_validation_rules(self):
        lead, _ = self.create_ok()
        self.assertEqual((lead.assigned_to_name, lead.reference_by_name), ("", ""))
        self.assertEqual(self.post(assigned_to="x" * 200).status_code, 201)
        self.assertEqual(self.post(assigned_to="x" * 201).status_code, 400)
        self.assertEqual(self.post(reference_by="y" * 201).status_code, 400)
        self.assertEqual(self.post(assigned_to="राहुल शर्मा (Partner)").status_code, 201)

    def test_api_key_is_still_required(self):
        r = Client().post(reverse("api_lead_create"), data=json.dumps({"customer_name": "x", "contact_number": "9876543210"}),
                          content_type="application/json")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(Lead.objects.count(), 0)

    def test_audit_and_timeline_are_written_for_unowned_api_lead(self):
        lead, _ = self.create_ok(assigned_to=EXTERNAL)
        self.assertTrue(Activity.objects.filter(lead=lead, kind="created").exists())
        audit = AuditLog.objects.get(action="lead_created", target_id=str(lead.pk))
        self.assertEqual(audit.details["assigned_to_name"], EXTERNAL)
        self.assertEqual(audit.details["crm_owner"], "")


class InboundExplicitOwnerTests(OwnershipTestBase):
    def test_valid_crm_owner_sets_owner_history_and_keeps_nurturing_name_separate(self):
        lead, body = self.create_ok(assigned_to=EXTERNAL, crm_owner="anchal")
        self.assertEqual(lead.assigned_to_id, self.anchal.pk)           # CRM owner = the explicit key ...
        self.assertEqual(lead.original_assigned_to_id, self.anchal.pk)
        self.assertEqual(lead.assigned_to_name, EXTERNAL)               # ... nurturing name = free text, untouched
        self.assertIsNone(lead.reference_by_id)
        self.assertTrue(body["crm_owner_assigned"])
        hist = AssignmentHistory.objects.get(lead=lead)
        self.assertEqual((hist.to_user_id, hist.changed_by_name, hist.from_user_id), (self.anchal.pk, "n8n", None))
        self.assertEqual(AuditLog.objects.get(action="lead_created", target_id=str(lead.pk)).details["crm_owner"], "Anchal Jain")

    def test_crm_owner_by_email_and_case_insensitive_username(self):
        self.assertEqual(self.create_ok(crm_owner="ANCHAL@kota.test")[0].assigned_to_id, self.anchal.pk)
        self.assertEqual(self.create_ok(crm_owner=" Anchal ")[0].assigned_to_id, self.anchal.pk)

    def test_unresolvable_crm_owner_never_rejects_or_guesses(self):
        make_user("gone", status=StaffProfile.STATUS_INACTIVE, email="gone@kota.test")
        make_user("dup1", email="dup@kota.test")
        make_user("dup2", email="dup@kota.test")
        deactivated = make_user("off")
        User.objects.filter(pk=deactivated.pk).update(is_active=False)
        for value in ("nobody", "gone", "gone@kota.test", "dup@kota.test", "off", "Anchal Jain", ["anchal"], {"a": 1}, True, 7):
            with self.subTest(value=value):
                lead, body = self.create_ok(assigned_to=EXTERNAL, crm_owner=value)
                self.assertIsNone(lead.assigned_to_id)
                self.assertEqual(lead.assigned_to_name, EXTERNAL)
                self.assertFalse(body["crm_owner_assigned"])
                self.assertEqual(len(body["warnings"]), 1)
                self.assertFalse(AssignmentHistory.objects.filter(lead=lead).exists())

    def test_blank_crm_owner_is_simply_not_given(self):
        lead, body = self.create_ok(crm_owner="   ")
        self.assertIsNone(lead.assigned_to_id)
        self.assertNotIn("warnings", body)

    def test_duplicate_delivery_returns_existing_lead_and_does_not_reassign(self):
        first, _ = self.create_ok(submitted_at="2026-10-01T10:00:00Z", assigned_to=EXTERNAL)
        r = self.post(submitted_at="2026-10-01T10:00:00Z", assigned_to="Someone Else", crm_owner="anchal")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["duplicate"])
        first.refresh_from_db()
        self.assertEqual((first.assigned_to_name, first.assigned_to_id), (EXTERNAL, None))
        self.assertEqual(Lead.objects.count(), 1)


# ============================================================ visibility / permissions / search / filters / exports
class VisibilityAndPermissionTests(OwnershipTestBase):
    def setUp(self):
        super().setUp()
        # Case 1: nurturing assignee == a CRM staff member's name.   Case 2: external assignee.
        self.l_staff_named, _ = self.create_ok(customer_name="StaffNamed Lead", assigned_to="Anchal Jain", reference_by="anchal")
        self.l_external, _ = self.create_ok(customer_name="External Lead", assigned_to=EXTERNAL, reference_by="Walk-in")

    def test_no_non_admin_staff_sees_unowned_api_leads_whatever_the_assignee_name(self):
        for c in (self.anchal_c, self.other_c):
            for url in ("all_leads", "my_leads"):
                self.assertEqual(c.get(reverse(url)).context["total_count"], 0, url)
        for lead in (self.l_staff_named, self.l_external):
            self.assertFalse(access.visible_leads(self.anchal).filter(pk=lead.pk).exists())
            self.assertFalse(access.visible_leads(self.other).filter(pk=lead.pk).exists())

    def test_referrer_keeps_access_does_not_leak_through_a_matching_reference_name(self):
        set_setting("referrer_keeps_access", "1")
        self.assertEqual(self.anchal_c.get(reverse("all_leads")).context["total_count"], 0)
        self.assertFalse(access.can_delete_lead(self.anchal, self.l_staff_named))
        self.assertFalse(access.can_edit_lead(self.anchal, self.l_staff_named))

    def test_unauthorized_staff_get_404_on_detail_edit_delete_and_cannot_post(self):
        for lead in (self.l_staff_named, self.l_external):
            for c in (self.anchal_c, self.other_c):
                self.assertEqual(c.get(reverse("lead_detail", args=[lead.pk])).status_code, 404)
                self.assertEqual(c.get(reverse("lead_edit", args=[lead.pk])).status_code, 404)
                self.assertEqual(c.post(reverse("lead_edit", args=[lead.pk]), dict(FORM, assigned_to_name="Hacked")).status_code, 404)
                self.assertEqual(c.post(reverse("lead_delete", args=[lead.pk])).status_code, 404)
        self.l_external.refresh_from_db()
        self.assertEqual(self.l_external.assigned_to_name, EXTERNAL)
        self.assertEqual(Lead.objects.count(), 2)

    def test_admin_sees_edits_and_can_open_both(self):
        for lead in (self.l_staff_named, self.l_external):
            self.assertEqual(self.admin_c.get(reverse("lead_detail", args=[lead.pk])).status_code, 200)
            self.assertEqual(self.admin_c.get(reverse("lead_edit", args=[lead.pk])).status_code, 200)
            self.assertTrue(access.can_edit_lead(self.admin, lead))
        self.assertEqual(self.admin_c.get(reverse("all_leads")).context["total_count"], 2)

    def test_search_and_assignee_filter_are_scoped_to_authorised_leads(self):
        # Admin: both names are searchable / filterable; the unowned filter finds both leads.
        self.assertEqual(self.admin_c.get(reverse("all_leads"), {"assignee": "vikram partner"}).context["total_count"], 1)
        self.assertEqual(self.admin_c.get(reverse("all_leads"), {"assignee": "anchal jain"}).context["total_count"], 1)
        self.assertEqual(self.admin_c.get(reverse("all_leads"), {"q": "External Lead"}).context["total_count"], 1)
        self.assertEqual(self.admin_c.get(reverse("all_leads"), {"assigned_to": "none"}).context["total_count"], 2)
        self.assertEqual(self.admin_c.get(reverse("all_leads"), {"assigned_to": str(self.anchal.pk)}).context["total_count"], 0)
        # Staff: the same queries find nothing and suggestions never reveal names from leads they cannot see.
        for c in (self.anchal_c, self.other_c):
            self.assertEqual(c.get(reverse("all_leads"), {"assignee": "vikram partner"}).context["total_count"], 0)
            self.assertEqual(c.get(reverse("all_leads"), {"assignee": "anchal jain"}).context["total_count"], 0)
            self.assertEqual(c.get(reverse("all_leads"), {"q": "External Lead"}).context["total_count"], 0)
            self.assertEqual(c.get(reverse("assignee_suggest"), {"q": "vik"}).json()["results"], [])
            self.assertEqual(c.get(reverse("assignee_suggest"), {"q": ""}).json()["results"], [])
        self.assertEqual([x["name"] for x in self.admin_c.get(reverse("assignee_suggest"), {"q": "vik"}).json()["results"]], [EXTERNAL])

    def test_admin_export_has_both_columns_separate_and_staff_export_leaks_nothing(self):
        rows = {r["Name"]: r for r in self.csv_rows(self.admin_c)}
        self.assertEqual(rows["External Lead"]["Assigned To"], EXTERNAL)
        self.assertEqual(rows["External Lead"]["Assigned To (CRM)"], "")
        self.assertEqual(rows["StaffNamed Lead"]["Assigned To"], "Anchal Jain")
        self.assertEqual(rows["StaffNamed Lead"]["Assigned To (CRM)"], "")      # name match did NOT make Anchal the owner
        self.assertEqual(rows["External Lead"]["Reference By"], "")
        self.assertEqual([r["Name"] for r in self.csv_rows(self.admin_c, assignee="vikram")], ["External Lead"])
        set_setting("staff_can_export", "1")
        self.assertEqual(self.csv_rows(self.anchal_c), [])
        self.assertEqual(self.csv_rows(self.anchal_c, assignee="vikram"), [])
        xlsx = self.anchal_c.get(reverse("leads_export"), {"format": "xlsx"})
        self.assertEqual(xlsx.status_code, 200)
        import openpyxl
        ws = openpyxl.load_workbook(io.BytesIO(xlsx.content)).active
        self.assertEqual(ws.max_row, 1)                                          # header only

    def test_outbound_payload_keeps_free_text_names_and_blank_crm_staff(self):
        p = build_sync_payload(self.l_external)
        self.assertEqual(p["assigned_to"], EXTERNAL)
        self.assertEqual(p["assigned_to_staff"], "")
        self.assertEqual(build_lead_payload(self.l_staff_named)["assigned_to"], "Anchal Jain")
        self.assertEqual(build_sync_payload(self.l_staff_named)["assigned_to_staff"], "")


class UnownedLeadPagesRenderTests(OwnershipTestBase):
    """Regression: lead_detail.html dereferenced lead.reference_by.username in a filter argument, which raises (HTTP 500)
    when the referrer FK is empty - i.e. for every n8n lead whose free-text Reference by is not CRM staff."""

    def test_admin_detail_edit_and_lists_render_for_leads_with_no_crm_owner_or_referrer(self):
        for kw in ({"assigned_to": EXTERNAL, "reference_by": "Walk-in"}, {"assigned_to": "Anchal Jain", "reference_by": "anchal"}, {}):
            lead, _ = self.create_ok(**kw)
            self.assertEqual(self.admin_c.get(reverse("lead_detail", args=[lead.pk])).status_code, 200)
            self.assertEqual(self.admin_c.get(reverse("lead_edit", args=[lead.pk])).status_code, 200)
        for name in ("all_leads", "followups"):
            self.assertEqual(self.admin_c.get(reverse(name)).status_code, 200)
        self.assertEqual(self.admin_c.get(reverse("dashboard")).status_code, 200)

    def test_owner_who_may_transfer_can_open_a_lead_that_has_no_referrer(self):
        lead, _ = self.create_ok(assigned_to=EXTERNAL, crm_owner="anchal")      # owned, but reference_by stays empty
        r = self.anchal_c.get(reverse("lead_detail", args=[lead.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Reference By stays unchanged.")


class OwnedApiLeadWorkflowTests(OwnershipTestBase):
    """An API lead given a valid explicit crm_owner (or assigned later by an admin) behaves like any other owned lead."""

    def test_owner_sees_and_edits_others_do_not_and_name_stays_free_text(self):
        lead, _ = self.create_ok(customer_name="Owned Lead", assigned_to=EXTERNAL, crm_owner="anchal")
        self.assertEqual(self.anchal_c.get(reverse("all_leads")).context["total_count"], 1)
        self.assertEqual(self.anchal_c.get(reverse("my_leads")).context["total_count"], 1)
        self.assertEqual(self.other_c.get(reverse("all_leads")).context["total_count"], 0)
        self.assertEqual(self.other_c.get(reverse("lead_edit", args=[lead.pk])).status_code, 404)
        self.assertTrue(access.can_edit_lead(self.anchal, lead))
        r = self.anchal_c.post(reverse("lead_edit", args=[lead.pk]),
                               dict(FORM, customer_name="Owned Lead", contact_number=lead.contact_number, assigned_to_name="Another External"))
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
        lead.refresh_from_db()
        self.assertEqual(lead.assigned_to_name, "Another External")
        self.assertEqual(lead.assigned_to_id, self.anchal.pk)               # editing the nurturing name never moves ownership
        self.assertEqual(self.anchal_c.get(reverse("all_leads"), {"assignee": "another external"}).context["total_count"], 1)
        self.assertEqual([r["Name"] for r in self.csv_rows(self.admin_c, assignee="another")], ["Owned Lead"])

    def test_admin_assigning_an_unowned_api_lead_grants_access_and_keeps_audit_history(self):
        lead, _ = self.create_ok(customer_name="Later Assigned", assigned_to=EXTERNAL)
        self.assertEqual(self.anchal_c.get(reverse("all_leads")).context["total_count"], 0)
        services.assign_lead(lead, self.anchal, self.admin, reason="Triage")
        lead.refresh_from_db()
        self.assertEqual((lead.assigned_to_id, lead.original_assigned_to_id, lead.assigned_to_name),
                         (self.anchal.pk, self.anchal.pk, EXTERNAL))
        hist = AssignmentHistory.objects.get(lead=lead)
        self.assertEqual((hist.from_user_id, hist.to_user_id, hist.changed_by_id), (None, self.anchal.pk, self.admin.pk))
        self.assertTrue(AuditLog.objects.filter(action="lead_assigned", target_id=str(lead.pk)).exists())
        self.assertEqual(self.anchal_c.get(reverse("all_leads")).context["total_count"], 1)

    def test_admin_can_set_the_crm_owner_from_the_edit_form_without_touching_the_nurturing_name(self):
        lead, _ = self.create_ok(customer_name="Form Assigned", assigned_to=EXTERNAL)
        r = self.admin_c.post(reverse("lead_edit", args=[lead.pk]),
                              dict(FORM, customer_name="Form Assigned", contact_number=lead.contact_number,
                                   assigned_to_name=EXTERNAL, assigned_to_user=str(self.anchal.pk)))
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
        lead.refresh_from_db()
        self.assertEqual((lead.assigned_to_id, lead.assigned_to_name), (self.anchal.pk, EXTERNAL))

    def test_transfer_changes_only_the_owner_never_the_nurturing_name(self):
        lead, _ = self.create_ok(assigned_to=EXTERNAL, crm_owner="anchal")
        services.transfer_lead(lead, self.other, self.anchal, "handover")
        lead.refresh_from_db()
        self.assertEqual((lead.assigned_to_id, lead.assigned_to_name, lead.original_assigned_to_id),
                         (self.other.pk, EXTERNAL, self.anchal.pk))
        self.assertEqual(self.anchal_c.get(reverse("all_leads")).context["total_count"], 0)
        self.assertEqual(self.other_c.get(reverse("all_leads")).context["total_count"], 1)


# ====================================================================== native CRM form (existing workflow, unchanged)
class NativeFormOwnershipTests(OwnershipTestBase):
    def test_staff_created_lead_is_owned_by_the_creator_for_staff_and_external_assignee_names_alike(self):
        for i, typed in enumerate((EXTERNAL, "Other Person", "other", "Anchal Jain")):   # last three equal OTHER staff
            with self.subTest(typed=typed):
                r = self.anchal_c.post(reverse("lead_create"), dict(FORM, customer_name=f"N{i}", contact_number=f"98110000{i:02d}",
                                                                  assigned_to_name=typed))
                self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
                lead = Lead.objects.get(customer_name=f"N{i}")
                self.assertEqual(lead.assigned_to_name, typed)
                self.assertEqual((lead.assigned_to_id, lead.reference_by_id, lead.created_by_id),
                                 (self.anchal.pk, self.anchal.pk, self.anchal.pk))
        self.assertEqual(self.other_c.get(reverse("all_leads")).context["total_count"], 0)    # "Other Person" typed -> no access

    def test_admin_picks_the_crm_owner_separately_from_the_nurturing_name(self):
        r = self.admin_c.post(reverse("lead_create"), dict(FORM, assigned_to_name=EXTERNAL, assigned_to_user=str(self.other.pk)))
        self.assertEqual(r.status_code, 302, getattr(r, "content", b"")[:300])
        lead = Lead.objects.get()
        self.assertEqual((lead.assigned_to_id, lead.assigned_to_name, lead.created_by_id), (self.other.pk, EXTERNAL, self.admin.pk))
        self.assertEqual(AssignmentHistory.objects.get(lead=lead).changed_by_id, self.admin.pk)

    def test_native_form_still_requires_assigned_to_text_but_never_a_staff_match(self):
        r = self.anchal_c.post(reverse("lead_create"), dict(FORM, assigned_to_name=""))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Lead.objects.count(), 0)
        r = self.anchal_c.post(reverse("lead_create"), dict(FORM, assigned_to_name="Nobody In Staff"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lead.objects.get().assigned_to_name, "Nobody In Staff")
