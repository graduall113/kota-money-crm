"""Contact -> Convert to Lead: same LeadForm/view as Add New Lead, plus conversion guarantees."""
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from .forms import LeadForm
from .models import Activity, AuditLog, Contact, Lead

User = get_user_model()

VALID = {
    "form_date": "27/09/2026", "customer_name": "Priya Verma", "contact_number": "9876543210",
    "work_profile": "Salaried", "income": "50000", "requirement": "Home Loan", "loan_amount": "2500000",
    "bank_calling": "HDFC", "status": "new", "reference_by_name": "KotaMoney", "assigned_to_name": "Rahul",
    "email": "priya@example.com", "city": "Kota", "source": "Facebook", "interest": "",
}


@override_settings(ATTENDANCE_ENFORCED=False)  # these tests predate attendance gating
class ConvertContactTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True)
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.other = User.objects.create_user("amit", password="pw")
        self.contact = Contact.objects.create(
            name="Priya V", phone="9876543210", email="priya@example.com", city="Kota", source="Facebook",
            work_profile="Salaried", income="50000", requirement="Home Loan", loan_amount="2500000",
            current_assigned_to=self.staff, reference_by=self.staff,
        )
        self.c = Client()
        self.c.force_login(self.staff)
        self.url = reverse("lead_create") + f"?from_contact={self.contact.pk}"

    def post(self, data=None, client=None, url=None):
        d = dict(VALID); d["from_contact"] = self.contact.pk
        d.update(data or {})
        return (client or self.c).post(url or reverse("lead_create"), d)

    # 1-3 form reuse + prefill ---------------------------------------
    def test_get_uses_same_leadform_and_prefills_contact(self):
        r = self.c.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertIsInstance(r.context["form"], LeadForm)
        self.assertTemplateUsed(r, "leads/lead_form.html")
        html = r.content.decode()
        self.assertIn("Convert Contact to Lead", html)
        self.assertIn("Priya V", html)
        f = r.context["form"]
        self.assertEqual(f.initial["customer_name"], "Priya V")
        self.assertEqual(f.initial["contact_number"], "9876543210")
        self.assertEqual(f.initial["email"], "priya@example.com")
        self.assertEqual(f.initial["city"], "Kota")
        self.assertEqual(f.initial["source"], "Facebook")

    def test_field_list_and_order_identical_to_add_new_lead(self):
        conv = list(self.c.get(self.url).context["form"].fields)
        add = list(self.c.get(reverse("lead_create")).context["form"].fields)
        self.assertEqual(conv, add)
        conv_req = {n: f.required for n, f in self.c.get(self.url).context["form"].fields.items()}
        add_req = {n: f.required for n, f in self.c.get(reverse("lead_create")).context["form"].fields.items()}
        self.assertEqual(conv_req, add_req)

    # 4-8 conversion --------------------------------------------------
    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_convert_edited_fields_links_and_marks_converted(self, n8n):
        r = self.post({"customer_name": "Priya Verma (edited)", "city": "Jaipur"})
        lead = Lead.objects.get(contact=self.contact)
        self.assertRedirects(r, reverse("lead_detail", args=[lead.pk]))
        self.assertEqual(lead.customer_name, "Priya Verma (edited)")  # user's edit wins
        self.assertEqual(lead.city, "Jaipur")
        self.assertEqual(lead.contact_id, self.contact.pk)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, Contact.STATUS_CONVERTED)
        self.assertTrue(Activity.objects.filter(contact=self.contact, kind="converted").exists())
        self.assertTrue(AuditLog.objects.filter(action="contact_converted", target_id=str(self.contact.pk)).exists())
        self.assertTrue(AuditLog.objects.filter(action="lead_created", target_id=str(lead.pk)).exists())
        self.assertTrue(Contact.objects.filter(pk=self.contact.pk).exists())  # contact kept

    def test_all_leadform_fields_saved(self):
        with mock.patch("crm.views.send_lead_to_n8n", return_value=(True, "")):
            self.post({"interest": "interested", "next_followup_date": "01/10/2026", "next_followup_time": "10:30",
                       "followup_notes": "call after lunch"})
        lead = Lead.objects.get(contact=self.contact)
        for k in ("customer_name", "contact_number", "work_profile", "income", "requirement", "bank_calling",
                  "reference_by_name", "assigned_to_name", "email", "city", "source"):
            self.assertEqual(getattr(lead, k), VALID[k], k)
        self.assertEqual(str(lead.loan_amount), "2500000.00")
        self.assertEqual(lead.status, "new")
        self.assertEqual(lead.interest, "interested")
        self.assertEqual(lead.followup_notes, "call after lunch")
        self.assertEqual(lead.next_followup_date.isoformat(), "2026-10-01")

    # 5 validation ----------------------------------------------------
    def test_validation_errors_do_not_convert(self):
        r = self.post({"customer_name": "", "loan_amount": "abc", "bank_calling": ""})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["form"].errors)
        self.assertEqual(Lead.objects.count(), 0)
        self.contact.refresh_from_db()
        self.assertNotEqual(self.contact.status, Contact.STATUS_CONVERTED)
        self.assertIn("Convert Contact to Lead", r.content.decode())  # re-rendered as the convert page

    # 9-10 n8n --------------------------------------------------------
    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_n8n_called_once_with_new_lead(self, n8n):
        self.post()
        lead = Lead.objects.get(contact=self.contact)
        n8n.assert_called_once_with(lead)

    def test_n8n_down_keeps_lead_and_conversion(self):
        with mock.patch("crm.settings_store.get_n8n_webhook_url", return_value="http://n8n.invalid/hook"), \
             mock.patch("crm.n8n_integration.requests.post", side_effect=requests.exceptions.ConnectionError("boom")):
            r = self.post(); follow = self.c.get(r["Location"])
        lead = Lead.objects.get(contact=self.contact)
        self.assertEqual(lead.n8n_sync_status, Lead.N8N_SYNC_FAILED)
        self.assertIn("boom", lead.n8n_error)
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, Contact.STATUS_CONVERTED)
        self.assertIn("automation sync is pending", " ".join(str(m) for m in follow.context["messages"]))

    def test_n8n_unexpected_exception_still_keeps_lead(self):
        with mock.patch("crm.views.send_lead_to_n8n", side_effect=RuntimeError("kaboom")):
            r = self.post()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lead.objects.filter(contact=self.contact).count(), 1)

    # 11-12 duplicates ------------------------------------------------
    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_second_conversion_blocked_with_link_to_existing_lead(self, n8n):
        self.post()
        lead = Lead.objects.get(contact=self.contact)
        r = self.c.get(self.url)
        self.assertRedirects(r, reverse("lead_detail", args=[lead.pk]))
        r = self.post({"customer_name": "Second"})
        self.assertRedirects(r, reverse("lead_detail", args=[lead.pk]))
        self.assertEqual(Lead.objects.filter(contact=self.contact).count(), 1)
        self.assertEqual(n8n.call_count, 1)

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_double_submit_race_creates_one_lead(self, n8n):
        """Second request passed the pre-check (stale page/race): the atomic claim must still stop it."""
        self.post()
        with mock.patch("crm.views._converted_lead_for", return_value=(False, None)):
            r = self.post({"customer_name": "Dupe"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lead.objects.filter(contact=self.contact).count(), 1)
        self.assertFalse(Lead.objects.filter(customer_name="Dupe").exists())
        self.assertEqual(n8n.call_count, 1)

    def test_lead_failure_rolls_back_contact_status(self):
        with mock.patch("crm.views.services.log_audit", side_effect=RuntimeError("db hiccup")), \
             mock.patch("crm.views.send_lead_to_n8n") as n8n:
            with self.assertRaises(RuntimeError):
                self.post()
        self.contact.refresh_from_db()
        self.assertNotEqual(self.contact.status, Contact.STATUS_CONVERTED)  # atomic: nothing half-done
        self.assertEqual(Lead.objects.count(), 0)
        n8n.assert_not_called()

    def test_existing_lead_link_blocks_even_if_status_was_changed_back(self):
        Lead.objects.create(customer_name="Old", contact_number="1", contact=self.contact)
        self.assertEqual(self.contact.status, Contact.STATUS_NEW)
        r = self.c.get(self.url)
        self.assertEqual(r.status_code, 302)

    # 13 permissions --------------------------------------------------
    def test_other_staff_cannot_convert_or_see(self):
        c = Client(); c.force_login(self.other)
        self.assertEqual(c.get(self.url).status_code, 404)
        self.assertEqual(self.post(client=c).status_code, 404)
        self.assertEqual(Lead.objects.count(), 0)

    def test_anonymous_redirected_to_login(self):
        r = Client().post(reverse("lead_create"), {"from_contact": self.contact.pk, **VALID})
        self.assertEqual(r.status_code, 302); self.assertIn("login", r["Location"])
        self.assertEqual(Lead.objects.count(), 0)

    def test_visible_but_not_editable_contact_is_403(self):
        Contact.objects.filter(pk=self.contact.pk).update(current_assigned_to=self.other, reference_by=self.staff)
        with mock.patch("crm.access.get_bool", return_value=True):  # referrer keeps read access only
            r = self.c.get(self.url)
        self.assertEqual(r.status_code, 403)

    def test_admin_can_convert_any_contact(self):
        c = Client(); c.force_login(self.admin)
        with mock.patch("crm.views.send_lead_to_n8n", return_value=(True, "")):
            r = self.post(client=c)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lead.objects.filter(contact=self.contact).count(), 1)

    def test_invalid_contact_id_404(self):
        self.assertEqual(self.c.get(reverse("lead_create") + "?from_contact=abc").status_code, 404)
        self.assertEqual(self.c.get(reverse("lead_create") + "?from_contact=99999").status_code, 404)

    # Add New Lead unaffected -----------------------------------------
    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_plain_add_new_lead_still_works(self, n8n):
        r = self.c.post(reverse("lead_create"), VALID)
        lead = Lead.objects.get()
        self.assertRedirects(r, reverse("lead_detail", args=[lead.pk]))
        self.assertIsNone(lead.contact_id)
        n8n.assert_called_once_with(lead)
        self.assertIn("Add New Lead", self.c.get(reverse("lead_create")).content.decode())
