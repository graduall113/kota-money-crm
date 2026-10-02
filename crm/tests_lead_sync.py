"""
Lead -> n8n -> Google Sheets sync: idempotent upsert keyed by Lead ID.

`FakeSheet` stands in for "n8n webhook + Google Sheets node 'Append or Update Row' matching on the
Lead ID column": it finds the row whose "Lead ID" equals payload["lead_reference_id"], updates it in
place, else appends. So `len(sheet.rows)` per Lead ID is what the real Sheet would hold IF the n8n
node is configured that way (see docs/n8n_lead_sync_setup.md). Nothing here talks to the network.
"""
from datetime import timedelta
from unittest import mock

import requests
from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import lead_sync, services
from .models import Contact, Lead, LeadSyncEvent

User = get_user_model()

VALID = {
    "form_date": "27/09/2026", "customer_name": "Priya Verma", "contact_number": "9876543210",
    "work_profile": "Salaried", "income": "50000", "requirement": "Home Loan", "loan_amount": "2500000",
    "bank_calling": "HDFC", "status": "new", "reference_by_name": "KotaMoney", "assigned_to_name": "Rahul",
    "email": "priya@example.com", "city": "Kota", "source": "Facebook", "interest": "",
}


class FakeSheet:
    """Append-or-Update-Row on the 'Lead ID' column."""

    def __init__(self):
        self.rows, self.calls, self.fail = [], [], False

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.fail:
            raise requests.exceptions.ConnectionError("n8n down")
        row = {
            "Lead ID": json["lead_reference_id"], "Date": json["date"], "Name": json["name"],
            "Contact No.": json["contact_no"], "Work Profile": json["work_profile"], "Income": json["income"],
            "Requirement": json["requirement"], "Loan Amount": json["loan_amount"], "Bank Calling": json["bank_calling"],
            "Status": json["status"], "Reference By": json["reference_by"], "Assigned To": json["assigned_to"],
            "Assigned To (CRM)": json["assigned_to_staff"],
        }
        for existing in self.rows:
            if existing["Lead ID"] == row["Lead ID"]:
                existing.update(row)
                break
        else:
            self.rows.append(row)
        response = mock.Mock(status_code=200)
        response.raise_for_status.return_value = None
        return response

    def row(self, lead_id):
        found = [r for r in self.rows if r["Lead ID"] == lead_id]
        assert len(found) <= 1, f"duplicate Sheet rows for {lead_id}: {found}"
        return found[0] if found else None

    def row_index(self, lead_id):
        return [r["Lead ID"] for r in self.rows].index(lead_id)


@override_settings(ATTENDANCE_ENFORCED=False, CRM_JOBS_INLINE=True, CRM_LEAD_SYNC_AUTODISPATCH=True,
                   CRM_LEAD_SYNC_MIN_INTERVAL=0)
class LeadSyncBase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True)
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.other = User.objects.create_user("amit", password="pw", first_name="Amit")
        self.sheet = FakeSheet()
        for p in (mock.patch("crm.settings_store.get_n8n_webhook_url", return_value="http://n8n.invalid/hook"),
                  mock.patch("crm.n8n_integration.requests.post", side_effect=self.sheet.post)):
            p.start()
            self.addCleanup(p.stop)
        self.c, self.ac = Client(), Client()
        self.c.force_login(self.staff)
        self.ac.force_login(self.admin)

    # every request runs its on_commit callbacks (TestCase would otherwise swallow them)
    def post(self, client, url, data):
        with self.captureOnCommitCallbacks(execute=True):
            return client.post(url, data)

    def add_lead(self, **kw):
        r = self.post(self.c, reverse("lead_create"), dict(VALID, **kw))
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        return Lead.objects.latest("id")

    def edit(self, lead, client=None, **kw):
        r = self.post(client or self.c, reverse("lead_edit", args=[lead.pk]), dict(VALID, **kw))
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        lead.refresh_from_db()
        return lead


class NewLeadTests(LeadSyncBase):
    def test_new_lead_appends_exactly_one_row_with_stable_payload(self):
        lead = self.add_lead()
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertEqual(self.sheet.rows[0]["Lead ID"], lead.display_id)
        call = self.sheet.calls[0]
        body = call["json"]
        self.assertEqual(body["event"], "lead_upsert")
        self.assertEqual(body["lead_id"], lead.pk)                       # numeric, unchanged
        self.assertEqual(body["lead_reference_id"], f"KM-{lead.pk}")
        self.assertEqual(body["idempotency_key"], f"lead-KM-{lead.pk}")
        self.assertEqual(call["headers"]["Idempotency-Key"], f"lead-KM-{lead.pk}")
        self.assertTrue(body["is_new_lead"])
        for key in ("date", "name", "contact_no", "work_profile", "income", "requirement", "loan_amount",
                    "bank_calling", "status", "reference_by", "assigned_to"):
            self.assertIn(key, body)                                      # every legacy key still there
        lead.refresh_from_db()
        self.assertEqual(lead.n8n_sync_status, Lead.N8N_SYNC_SUCCESS)
        # one queued event, delivered once by the view (no second background send)
        self.assertEqual(LeadSyncEvent.objects.filter(lead=lead).count(), 1)
        self.assertEqual(len(self.sheet.calls), 1)

    def test_two_leads_with_the_same_phone_get_two_rows_because_key_is_lead_id(self):
        a, b = self.add_lead(), self.add_lead()
        self.assertNotEqual(a.display_id, b.display_id)
        self.assertEqual(len(self.sheet.rows), 2)


class EditTests(LeadSyncBase):
    def test_five_edits_keep_one_row_and_update_it_in_place(self):
        first = self.add_lead(customer_name="First")
        second = self.add_lead(customer_name="Second")     # sits below the first row
        for i in range(1, 6):
            self.edit(first, customer_name=f"Edit {i}", income=str(60000 + i))
        self.assertEqual(len(self.sheet.rows), 2)                            # NOT 7
        self.assertEqual(self.sheet.row_index(first.display_id), 0)          # same row position
        self.assertEqual(self.sheet.row(first.display_id)["Name"], "Edit 5")
        self.assertEqual(self.sheet.row(first.display_id)["Income"], "60005")
        self.assertEqual(self.sheet.row(second.display_id)["Name"], "Second")
        self.assertEqual(Lead.objects.count(), 2)                            # no new leads / IDs

    def test_unchanged_save_sends_nothing(self):
        lead = self.add_lead()
        calls = len(self.sheet.calls)
        self.edit(lead)                                                      # identical data
        self.assertEqual(len(self.sheet.calls), calls)
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertEqual(LeadSyncEvent.objects.filter(lead=lead).order_by("-id").first().status, LeadSyncEvent.SKIPPED)

    def test_edit_from_my_leads_and_all_leads_update_the_same_row(self):
        # My Leads (owner edits) and All Leads (admin edits) both use the lead_edit view.
        lead = self.add_lead()
        self.assertContains(self.c.get(reverse("my_leads")), reverse("lead_edit", args=[lead.pk]))
        self.assertContains(self.ac.get(reverse("all_leads")), reverse("lead_edit", args=[lead.pk]))
        self.edit(lead, client=self.c, customer_name="By owner")
        self.assertEqual(self.sheet.row(lead.display_id)["Name"], "By owner")
        self.edit(lead, client=self.ac, customer_name="By admin")
        self.assertEqual(self.sheet.row(lead.display_id)["Name"], "By admin")
        self.assertEqual(len(self.sheet.rows), 1)

    def test_status_change_from_edit_and_from_lead_page(self):
        lead = self.add_lead()
        self.edit(lead, status="processing")
        self.assertEqual(self.sheet.row(lead.display_id)["Status"], "Processing")
        self.assertIn("status_changed", self.sheet.calls[-1]["json"]["sync_reason"])
        self.post(self.c, reverse("lead_action", args=[lead.pk]), {"action": "status", "status": "approved"})
        self.assertEqual(self.sheet.row(lead.display_id)["Status"], "Approved")
        self.assertEqual(len(self.sheet.rows), 1)

    def test_reference_by_change(self):
        lead = self.add_lead()
        self.edit(lead, reference_by_name="Neha")
        self.assertEqual(self.sheet.row(lead.display_id)["Reference By"], "Neha")
        self.assertIn("reference_changed", self.sheet.calls[-1]["json"]["sync_reason"])
        self.assertEqual(len(self.sheet.rows), 1)

    def test_assignment_changes_sync_free_text_and_staff_owner(self):
        lead = self.add_lead()
        self.edit(lead, assigned_to_name="Sunita")                           # free-text "Assigned To" column
        self.assertEqual(self.sheet.row(lead.display_id)["Assigned To"], "Sunita")
        with self.captureOnCommitCallbacks(execute=True):                    # CRM staff owner (transfer)
            services.assign_lead(lead, self.other, self.admin, reason="rebalance")
        row = self.sheet.row(lead.display_id)
        self.assertEqual(row["Assigned To (CRM)"], "Amit")
        self.assertEqual(row["Assigned To"], "Sunita")                       # free text untouched
        self.assertEqual(len(self.sheet.rows), 1)

    def test_admin_edit_that_also_reassigns_queues_a_single_event(self):
        lead = self.add_lead()
        self.edit(lead, client=self.ac, customer_name="Reassigned", assigned_to_user=str(self.other.pk))
        lead.refresh_from_db()
        self.assertEqual(lead.assigned_to_id, self.other.pk)
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertEqual(self.sheet.row(lead.display_id)["Assigned To (CRM)"], "Amit")
        self.assertEqual(LeadSyncEvent.objects.filter(lead=lead, status=LeadSyncEvent.PENDING).count(), 0)

    def test_repeated_identical_delivery_is_still_one_row(self):
        lead = self.add_lead()
        for _ in range(3):
            self.assertTrue(lead_sync.sync_lead_now(lead)[0])                # forced re-sends
        self.assertEqual(len(self.sheet.calls), 4)
        self.assertEqual(len(self.sheet.rows), 1)


class ContactConversionTests(LeadSyncBase):
    def setUp(self):
        super().setUp()
        self.contact = Contact.objects.create(
            name="Priya V", phone="9876543210", email="priya@example.com", city="Kota", source="Facebook",
            work_profile="Salaried", income="50000", requirement="Home Loan", loan_amount="2500000",
            current_assigned_to=self.staff, reference_by=self.staff)

    def convert(self):
        r = self.post(self.c, reverse("lead_create"), dict(VALID, from_contact=self.contact.pk))
        self.assertEqual(r.status_code, 302)
        return Lead.objects.get(contact=self.contact)

    def edit_contact(self, **kw):
        data = {"name": "Priya V", "phone": "9876543210", "email": "priya@example.com", "address": "",
                "city": "Kota", "work_profile": "Salaried", "income": "50000", "requirement": "Home Loan",
                "loan_amount": "2500000.00", "source": "Facebook", "status": "new", "notes": ""}
        data.update(kw)
        r = self.post(self.c, reverse("contact_edit", args=[self.contact.pk]), data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        return r

    def test_conversion_appends_one_row(self):
        lead = self.convert()
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertEqual(self.sheet.rows[0]["Lead ID"], lead.display_id)
        self.assertEqual(self.sheet.calls[0]["json"]["sync_reason"], "converted")

    def test_editing_converted_contact_updates_same_lead_and_same_row(self):
        lead = self.convert()
        lead_id, ref = lead.pk, lead.display_id
        self.edit_contact(name="Priya Verma Updated", income="75000", city="Jaipur")
        self.edit_contact(name="Priya Verma Updated 2", income="80000", city="Jaipur")
        self.assertEqual(Lead.objects.count(), 1)                           # no second Lead
        self.assertFalse(Lead.objects.filter(pk__gt=lead_id).exists())      # no KM-(n+1)
        lead.refresh_from_db()
        self.assertEqual((lead.pk, lead.display_id), (lead_id, ref))
        self.assertEqual(lead.customer_name, "Priya Verma Updated 2")
        self.assertEqual(lead.income, "80000")
        self.assertEqual(lead.city, "Jaipur")
        self.assertEqual(len(self.sheet.rows), 1)                           # still ONE Sheet row
        self.assertEqual(self.sheet.row(ref)["Name"], "Priya Verma Updated 2")
        self.assertEqual(self.sheet.row(ref)["Income"], "80000")
        self.assertIn("contact_edited", self.sheet.calls[-1]["json"]["sync_reason"])

    def test_contact_stays_converted_after_edit(self):
        self.convert()
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, Contact.STATUS_CONVERTED)
        self.edit_contact(name="Renamed", status="new")                    # hostile/default status value
        self.contact.refresh_from_db()
        self.assertEqual(self.contact.status, Contact.STATUS_CONVERTED)
        self.assertEqual(self.contact.name, "Renamed")

    def test_only_changed_contact_fields_overwrite_the_lead(self):
        lead = self.convert()
        self.edit(lead, income="99999")                                    # newer edit made on the Lead
        self.edit_contact(city="Ajmer")                                    # contact edit touches city only
        lead.refresh_from_db()
        self.assertEqual(lead.city, "Ajmer")
        self.assertEqual(lead.income, "99999")                             # not clobbered by old contact income
        self.assertEqual(self.sheet.row(lead.display_id)["Income"], "99999")

    def test_saving_converted_contact_without_changes_adds_nothing(self):
        lead = self.convert()
        calls = len(self.sheet.calls)
        self.edit_contact()
        self.assertEqual(Lead.objects.count(), 1)
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertEqual(len(self.sheet.calls), calls)                     # identical -> skipped

    def test_unconverted_contact_edit_touches_no_lead(self):
        self.edit_contact(name="Only a contact")
        self.assertEqual(Lead.objects.count(), 0)
        self.assertEqual(self.sheet.calls, [])

    def test_contact_edit_survives_n8n_failure(self):
        lead = self.convert()
        self.sheet.fail = True
        self.edit_contact(name="Saved Anyway")
        self.contact.refresh_from_db(); lead.refresh_from_db()
        self.assertEqual(self.contact.name, "Saved Anyway")
        self.assertEqual(lead.customer_name, "Saved Anyway")
        self.assertEqual(lead.n8n_sync_status, Lead.N8N_SYNC_FAILED)


class FailureAndRetryTests(LeadSyncBase):
    def test_n8n_down_never_fails_the_save_and_is_retried(self):
        lead = self.add_lead()
        self.sheet.fail = True
        lead = self.edit(lead, customer_name="Saved while n8n is down")
        self.assertEqual(lead.customer_name, "Saved while n8n is down")     # DB save succeeded
        self.assertEqual(lead.n8n_sync_status, Lead.N8N_SYNC_FAILED)
        self.assertIn("n8n down", lead.n8n_error)
        ev = LeadSyncEvent.objects.get(lead=lead, status=LeadSyncEvent.PENDING)
        self.assertEqual(ev.attempts, 1)
        self.assertGreater(ev.next_attempt_at, timezone.now())              # back-off scheduled
        self.assertIn("n8n down", ev.last_error)
        self.assertEqual(self.sheet.row(lead.display_id)["Name"], "Priya Verma")   # Sheet still old

        self.sheet.fail = False
        self.assertEqual(lead_sync.drain_due_events(min_interval=0), 0)      # not due yet
        LeadSyncEvent.objects.filter(pk=ev.pk).update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(lead_sync.drain_due_events(min_interval=0), 1)
        lead.refresh_from_db()
        self.assertEqual(lead.n8n_sync_status, Lead.N8N_SYNC_SUCCESS)
        self.assertEqual(lead.n8n_error, "")
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertEqual(self.sheet.row(lead.display_id)["Name"], "Saved while n8n is down")
        self.assertEqual(LeadSyncEvent.objects.get(pk=ev.pk).status, LeadSyncEvent.SUCCESS)

    def test_retry_sends_latest_data_not_stale_data(self):
        lead = self.add_lead()
        self.sheet.fail = True
        self.edit(lead, customer_name="v2")
        self.edit(lead, customer_name="v3")                                  # coalesces into the pending retry
        self.assertEqual(LeadSyncEvent.objects.filter(lead=lead, status=LeadSyncEvent.PENDING).count(), 1)
        self.sheet.fail = False
        LeadSyncEvent.objects.filter(status=LeadSyncEvent.PENDING).update(next_attempt_at=timezone.now())  # back-off elapsed
        self.assertEqual(lead_sync.drain_due_events(min_interval=0), 1)
        self.assertEqual(self.sheet.row(lead.display_id)["Name"], "v3")

    def test_gives_up_after_max_attempts_but_a_new_edit_starts_over(self):
        lead = self.add_lead()
        self.sheet.fail = True
        self.edit(lead, customer_name="never delivered")
        for _ in range(lead_sync.MAX_ATTEMPTS):
            LeadSyncEvent.objects.filter(status=LeadSyncEvent.PENDING).update(next_attempt_at=timezone.now())
            lead_sync.drain_due_events(min_interval=0)
        ev = LeadSyncEvent.objects.filter(lead=lead).order_by("-id").first()
        self.assertEqual(ev.status, LeadSyncEvent.FAILED)
        self.assertEqual(ev.attempts, lead_sync.MAX_ATTEMPTS)
        self.sheet.fail = False
        self.edit(lead, customer_name="delivered now")
        self.assertEqual(self.sheet.row(lead.display_id)["Name"], "delivered now")
        self.assertEqual(len(self.sheet.rows), 1)

    def test_queueing_error_cannot_break_a_lead_save(self):
        with mock.patch("crm.lead_sync._reference_id", side_effect=RuntimeError("queue broken")):
            lead = Lead.objects.create(customer_name="Still saved", contact_number="9000000001")
        self.assertTrue(Lead.objects.filter(pk=lead.pk).exists())

    def test_disabled_integration_keeps_saving_and_keeps_events_pending(self):
        from .settings_store import set_setting
        lead = self.add_lead()
        set_setting("n8n_enabled", False)
        calls = len(self.sheet.calls)
        self.edit(lead, customer_name="while disabled")
        self.assertEqual(len(self.sheet.calls), calls)
        self.assertEqual(LeadSyncEvent.objects.filter(lead=lead, status=LeadSyncEvent.PENDING).count(), 1)
        set_setting("n8n_enabled", True)
        lead_sync.drain_due_events(min_interval=0)
        self.assertEqual(self.sheet.row(lead.display_id)["Name"], "while disabled")


class QueueSafetyTests(LeadSyncBase):
    def make(self, **kw):
        return Lead.objects.create(customer_name=kw.pop("customer_name", "Q"), contact_number="9000000002", **kw)

    def test_rapid_saves_coalesce_into_one_event_and_one_send(self):
        lead = self.make()
        for i in range(5):                                                   # on_commit never fires inside TestCase
            lead.customer_name = f"n{i}"
            lead.save()
        self.assertEqual(LeadSyncEvent.objects.filter(lead=lead, status=LeadSyncEvent.PENDING).count(), 1)
        lead_sync.drain_due_events(min_interval=0)
        self.assertEqual(len(self.sheet.calls), 1)
        self.assertEqual(self.sheet.rows[0]["Name"], "n4")

    def test_two_events_for_one_lead_never_process_in_parallel(self):
        lead = self.make()
        pending = LeadSyncEvent.objects.get(lead=lead, status=LeadSyncEvent.PENDING)
        # simulate another worker mid-send for the same lead, plus a newer pending event
        LeadSyncEvent.objects.filter(pk=pending.pk).update(status=LeadSyncEvent.PROCESSING, started_at=timezone.now())
        newer = lead_sync.enqueue_lead_sync(lead, "lead_saved")
        ok, err = lead_sync.process_event(newer)
        self.assertFalse(ok)
        self.assertEqual(err, lead_sync.BUSY)
        self.assertEqual(self.sheet.calls, [])
        self.assertEqual(LeadSyncEvent.objects.get(pk=newer.pk).status, LeadSyncEvent.PENDING)

    def test_dead_worker_is_recovered(self):
        lead = self.make()
        ev = LeadSyncEvent.objects.get(lead=lead)
        LeadSyncEvent.objects.filter(pk=ev.pk).update(
            status=LeadSyncEvent.PROCESSING, started_at=timezone.now() - timedelta(minutes=30))
        lead_sync.drain_due_events(min_interval=0)
        self.assertEqual(len(self.sheet.rows), 1)
        self.assertEqual(LeadSyncEvent.objects.get(pk=ev.pk).status, LeadSyncEvent.SUCCESS)

    def test_bulk_status_and_bulk_assign_queue_events(self):
        leads = [self.make(customer_name=f"B{i}") for i in range(3)]
        lead_sync.drain_due_events(min_interval=0)
        self.assertEqual(len(self.sheet.rows), 3)
        pks = [l.pk for l in leads]
        services.bulk_set_status(Lead, pks, Lead.STATUS_APPROVED, self.admin)
        services.apply_plan(Lead, [(self.other, pks)], self.admin, "assign")
        self.assertEqual(LeadSyncEvent.objects.filter(status=LeadSyncEvent.PENDING).count(), 3)   # coalesced
        lead_sync.drain_due_events(min_interval=0)
        self.assertEqual(len(self.sheet.rows), 3)
        for l in leads:
            row = self.sheet.row(l.display_id)
            self.assertEqual(row["Status"], "Approved")
            self.assertEqual(row["Assigned To (CRM)"], "Amit")

    def test_bulk_followup_queues_events(self):
        lead = self.make()
        lead_sync.drain_due_events(min_interval=0)
        import datetime
        services.bulk_set_followup(Lead, [lead.pk], datetime.date(2026, 10, 5), None, "call", self.admin)
        lead_sync.drain_due_events(min_interval=0)
        self.assertEqual(self.sheet.calls[-1]["json"]["next_followup_date"], "05/10/2026")

    def test_n8n_originated_lead_is_not_pushed_back_but_later_edits_sync(self):
        lead = Lead.objects.create(customer_name="From n8n form", contact_number="9000000003",
                                   external_submission_id="2026-09-29T10:00:00Z")
        self.assertEqual(LeadSyncEvent.objects.filter(lead=lead).count(), 0)
        lead.customer_name = "Edited later"
        lead.save()
        self.assertEqual(LeadSyncEvent.objects.filter(lead=lead).count(), 1)

    def test_bookkeeping_saves_do_not_queue_events(self):
        lead = self.make()
        lead_sync.drain_due_events(min_interval=0)
        before = LeadSyncEvent.objects.count()
        lead.last_contacted = timezone.now()
        lead.save(update_fields=["last_contacted", "updated_at"])
        lead.n8n_error = ""
        lead.save(update_fields=["n8n_error"])
        self.assertEqual(LeadSyncEvent.objects.count(), before)

    def test_deleted_lead_leaves_no_orphan_events(self):
        lead = self.make()
        lead.delete()
        self.assertEqual(LeadSyncEvent.objects.count(), 0)
        self.assertEqual(lead_sync.drain_due_events(min_interval=0), 0)
