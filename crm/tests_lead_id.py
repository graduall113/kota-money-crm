"""
Lead ID (Lead.display_id, e.g. "KM-1058") — automatic, unique, permanent,
server-generated, never editable, and sent to n8n as ``lead_reference_id``.
"""
import json
import re
import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import OperationalError, connection, connections
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from .forms import LeadForm
from .models import Contact, Lead
from .n8n_integration import build_lead_payload, send_lead_to_n8n

User = get_user_model()

ID_RE = re.compile(r"^KM-\d+$")

VALID = {
    "form_date": "27/09/2026", "customer_name": "Priya Verma", "contact_number": "9876543210",
    "work_profile": "Salaried", "income": "50000", "requirement": "Home Loan", "loan_amount": "2500000",
    "bank_calling": "HDFC", "status": "new", "reference_by_name": "KotaMoney", "assigned_to_name": "Rahul",
    "email": "priya@example.com", "city": "Kota", "source": "Facebook", "interest": "",
}

# The payload keys the existing n8n workflow already relies on. None may be
# removed or renamed; "lead_reference_id" is the only addition.
LEGACY_PAYLOAD_KEYS = {
    "lead_id", "date", "name", "contact_no", "work_profile", "income", "requirement",
    "loan_amount", "bank_calling", "status", "reference_by", "assigned_to",
}


def make_lead(**kw):
    data = dict(customer_name="Test Lead", contact_number="9000000000")
    data.update(kw)
    return Lead.objects.create(**data)


@override_settings(ATTENDANCE_ENFORCED=False)  # these tests predate attendance gating
class LeadIdModelTests(TestCase):
    def test_format_and_uses_existing_convention(self):
        lead = make_lead()
        self.assertRegex(lead.display_id, ID_RE)
        self.assertEqual(lead.display_id, f"KM-{lead.pk}")

    def test_unsaved_lead_has_no_id(self):
        self.assertEqual(Lead(customer_name="x", contact_number="1").display_id, "")

    def test_multiple_leads_get_unique_increasing_ids(self):
        leads = [make_lead(customer_name=f"L{i}") for i in range(25)]
        ids = [l.display_id for l in leads]
        self.assertEqual(len(set(ids)), 25)
        pks = [l.pk for l in leads]
        self.assertEqual(pks, sorted(pks))

    def test_id_is_read_only(self):
        lead = make_lead()
        with self.assertRaises(AttributeError):
            lead.display_id = "KM-999999"

    def test_id_never_changes_on_update(self):
        lead = make_lead()
        before = lead.display_id
        lead.customer_name = "Renamed"
        lead.contact_number = "9111111111"
        lead.status = Lead.STATUS_APPROVED
        lead.save()
        lead.refresh_from_db()
        self.assertEqual(lead.display_id, before)

    def test_deleted_lead_number_is_not_reused(self):
        first = make_lead()
        old_id = first.display_id
        first.delete()
        second = make_lead()
        self.assertNotEqual(second.display_id, old_id)

    def test_id_is_not_derived_from_phone(self):
        a = make_lead(contact_number="9876543210")
        b = make_lead(contact_number="9876543210")
        self.assertNotEqual(a.display_id, b.display_id)
        self.assertNotIn("9876543210", a.display_id)


@override_settings(ATTENDANCE_ENFORCED=False)  # these tests predate attendance gating
class LeadIdConcurrencyTests(TransactionTestCase):
    """Simultaneous creation from several threads must never yield a duplicate ID."""

    THREADS = 8
    PER_THREAD = 10

    def test_concurrent_creation_yields_unique_ids(self):
        results, errors = [], []
        lock = threading.Lock()

        def worker(n):
            try:
                for i in range(self.PER_THREAD):
                    for attempt in range(50):  # SQLite may briefly lock; a retry is not an ID collision
                        try:
                            lead = Lead.objects.create(customer_name=f"T{n}-{i}", contact_number="9000000000")
                            break
                        except OperationalError:
                            continue
                    else:
                        raise RuntimeError("could not create lead")
                    with lock:
                        results.append(lead.display_id)
            except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
                errors.append(exc)
            finally:
                connections.close_all()

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(self.THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        expected = self.THREADS * self.PER_THREAD
        self.assertEqual(len(results), expected)
        self.assertEqual(len(set(results)), expected, "duplicate Lead ID generated under concurrency")
        self.assertEqual(Lead.objects.count(), expected)
        self.assertEqual(len({l.display_id for l in Lead.objects.all()}), expected)


@override_settings(ATTENDANCE_ENFORCED=False)  # these tests predate attendance gating
class LeadIdFormTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.c = Client()
        self.c.force_login(self.staff)

    def test_form_class_has_no_id_field(self):
        form = LeadForm()
        for name in ("id", "pk", "display_id", "lead_id", "lead_reference_id"):
            self.assertNotIn(name, form.fields)

    def test_add_page_has_no_editable_id_input_and_no_fake_id(self):
        html = self.c.get(reverse("lead_create")).content.decode()
        self.assertNotRegex(html, r'<input[^>]+name="(id|pk|display_id|lead_id|lead_reference_id)"')
        self.assertIn("Generated automatically when you save", html)
        self.assertNotRegex(html, r"KM-(\d+|None)")  # must not pretend an ID exists yet

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_add_new_lead_generates_id_and_shows_it(self, n8n):
        r = self.c.post(reverse("lead_create"), VALID)
        lead = Lead.objects.get()
        self.assertRedirects(r, reverse("lead_detail", args=[lead.pk]))
        self.assertRegex(lead.display_id, ID_RE)
        page = self.c.get(r["Location"])
        self.assertIn(lead.display_id, page.content.decode())  # detail page shows the generated ID

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_success_message_contains_lead_id(self, n8n):
        r = self.c.post(reverse("lead_create"), VALID, follow=True)
        lead = Lead.objects.get()
        self.assertIn(lead.display_id, " ".join(str(m) for m in r.context["messages"]))

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_client_supplied_id_is_ignored_on_create(self, n8n):
        forged = dict(VALID, id="424242", pk="424242", display_id="KM-424242",
                      lead_id="424242", lead_reference_id="KM-424242")
        self.c.post(reverse("lead_create"), forged)
        lead = Lead.objects.get()
        self.assertNotEqual(lead.pk, 424242)
        self.assertNotEqual(lead.display_id, "KM-424242")

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_lead_id_cannot_be_edited(self, n8n):
        self.c.post(reverse("lead_create"), VALID)
        lead = Lead.objects.get()
        original_pk, original_id = lead.pk, lead.display_id

        edit_url = reverse("lead_edit", args=[lead.pk])
        page = self.c.get(edit_url).content.decode()
        self.assertIn(original_id, page)  # shown...
        self.assertNotRegex(page, r'<input[^>]+name="(id|pk|display_id|lead_id|lead_reference_id)"')  # ...but not editable

        forged = dict(VALID, customer_name="Edited", id="777", pk="777", display_id="KM-777",
                      lead_id="777", lead_reference_id="KM-777")
        r = self.c.post(edit_url, forged)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Lead.objects.count(), 1)
        lead.refresh_from_db()
        self.assertEqual(lead.customer_name, "Edited")  # the real edit went through
        self.assertEqual(lead.pk, original_pk)
        self.assertEqual(lead.display_id, original_id)  # the ID did not

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_edit_does_not_resend_or_change_id(self, n8n):
        self.c.post(reverse("lead_create"), VALID)
        lead = Lead.objects.get()
        self.c.post(reverse("lead_edit", args=[lead.pk]), dict(VALID, city="Jaipur"))
        self.assertEqual(n8n.call_count, 1)  # only the creation sends to n8n


@override_settings(ATTENDANCE_ENFORCED=False)  # these tests predate attendance gating
class ConvertContactLeadIdTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.contact = Contact.objects.create(
            name="Priya V", phone="9876543210", current_assigned_to=self.staff, reference_by=self.staff)
        self.c = Client()
        self.c.force_login(self.staff)

    def test_convert_page_has_no_editable_id(self):
        html = self.c.get(reverse("lead_create") + f"?from_contact={self.contact.pk}").content.decode()
        self.assertNotRegex(html, r'<input[^>]+name="(id|pk|display_id|lead_id|lead_reference_id)"')
        self.assertIn("Generated automatically when you save", html)

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_conversion_generates_id_and_shows_it(self, n8n):
        r = self.c.post(reverse("lead_create"), dict(VALID, from_contact=self.contact.pk, display_id="KM-1"),
                        follow=True)
        lead = Lead.objects.get(contact=self.contact)
        self.assertRegex(lead.display_id, ID_RE)
        self.assertNotEqual(lead.display_id, "KM-1")
        self.assertIn(lead.display_id, " ".join(str(m) for m in r.context["messages"]))
        n8n.assert_called_once_with(lead)

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_converted_and_added_leads_share_one_unique_sequence(self, n8n):
        self.c.post(reverse("lead_create"), VALID)
        self.c.post(reverse("lead_create"), dict(VALID, from_contact=self.contact.pk))
        ids = [l.display_id for l in Lead.objects.all()]
        self.assertEqual(len(ids), 2)
        self.assertEqual(len(set(ids)), 2)

    @mock.patch("crm.views.send_lead_to_n8n", return_value=(True, ""))
    def test_conversion_payload_carries_lead_id(self, n8n):
        self.c.post(reverse("lead_create"), dict(VALID, from_contact=self.contact.pk))
        lead = Lead.objects.get(contact=self.contact)
        payload = build_lead_payload(lead)
        self.assertEqual(payload["lead_reference_id"], lead.display_id)
        self.assertEqual(payload["lead_id"], lead.pk)


@override_settings(ATTENDANCE_ENFORCED=False)  # these tests predate attendance gating
class N8nPayloadTests(TestCase):
    def setUp(self):
        self.lead = make_lead(customer_name="Priya Verma", contact_number="9876543210",
                              work_profile="Salaried", loan_amount="2500000")

    def test_payload_has_reference_id_and_keeps_numeric_lead_id(self):
        p = build_lead_payload(self.lead)
        self.assertEqual(p["lead_reference_id"], f"KM-{self.lead.pk}")
        self.assertIsInstance(p["lead_id"], int)  # must NOT become a string
        self.assertEqual(p["lead_id"], self.lead.pk)

    def test_payload_keeps_every_legacy_key(self):
        p = build_lead_payload(self.lead)
        self.assertEqual(set(p), LEGACY_PAYLOAD_KEYS | {"lead_reference_id"})
        self.assertEqual(p["name"], "Priya Verma")
        self.assertEqual(p["contact_no"], "9876543210")
        json.dumps(p)  # still JSON-serialisable

    def _send(self, lead):
        with mock.patch("crm.settings_store.get_n8n_webhook_url", return_value="http://n8n.invalid/hook"), \
             mock.patch("crm.n8n_integration.requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            post.return_value.status_code = 200
            ok, err = send_lead_to_n8n(lead)
        return ok, post

    def test_webhook_request_body_and_idempotency_header(self):
        ok, post = self._send(self.lead)
        self.assertTrue(ok)
        kwargs = post.call_args.kwargs
        self.assertEqual(kwargs["json"]["lead_reference_id"], self.lead.display_id)
        self.assertEqual(kwargs["json"]["lead_id"], self.lead.pk)
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], f"lead-{self.lead.display_id}")

    def test_retry_sends_identical_key_and_lead_id(self):
        """A repeated delivery must be recognisable as the SAME lead (so the Sheet upserts, not appends)."""
        _, first = self._send(self.lead)
        _, second = self._send(self.lead)
        a, b = first.call_args.kwargs, second.call_args.kwargs
        self.assertEqual(a["headers"]["Idempotency-Key"], b["headers"]["Idempotency-Key"])
        self.assertEqual(a["json"]["lead_reference_id"], b["json"]["lead_reference_id"])
        self.assertEqual(a["json"], b["json"])

    def test_failed_delivery_keeps_lead_and_id(self):
        import requests as rq
        before = self.lead.display_id
        with mock.patch("crm.settings_store.get_n8n_webhook_url", return_value="http://n8n.invalid/hook"), \
             mock.patch("crm.n8n_integration.requests.post", side_effect=rq.exceptions.ConnectionError("boom")):
            ok, err = send_lead_to_n8n(self.lead)
        self.assertFalse(ok)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.n8n_sync_status, Lead.N8N_SYNC_FAILED)
        self.assertEqual(self.lead.display_id, before)

    def test_unsaved_lead_is_never_sent(self):
        with mock.patch("crm.n8n_integration.requests.post") as post:
            ok, err = send_lead_to_n8n(Lead(customer_name="x", contact_number="1"))
        self.assertFalse(ok)
        post.assert_not_called()


@override_settings(N8N_API_KEY="secret")
@override_settings(ATTENDANCE_ENFORCED=False)  # these tests predate attendance gating
class InboundApiLeadIdTests(TestCase):
    def test_api_returns_reference_id_and_ignores_supplied_one(self):
        r = Client().post(
            reverse("api_lead_create"),
            data=json.dumps({"customer_name": "Api Lead", "contact_number": "9876543210",
                             "lead_id": 999999, "lead_reference_id": "KM-999999", "display_id": "KM-999999"}),
            content_type="application/json", HTTP_X_N8N_API_KEY="secret")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        lead = Lead.objects.get()
        self.assertEqual(body["lead_id"], lead.pk)
        self.assertNotEqual(lead.pk, 999999)
        self.assertEqual(body["lead_reference_id"], lead.display_id)
