"""
Contract test for the Google Sheet row-colour automation (google_sheets_row_colour/Code.gs).

The Apps Script finds the row by `lead_reference_id` and colours it from `status`, matching the
status TEXT (Approved / Rejected / Processing). If either key or a label ever changes, the Sheet
colouring silently stops working - so pin them here. No CRM behaviour is changed by this file.
"""
from django.test import TestCase

from .models import Lead
from .n8n_integration import build_sync_payload


class SheetColourPayloadContract(TestCase):
    def _lead(self, status):
        return Lead.objects.create(
            customer_name="Rahul", contact_number="9876543210", status=status,
            reference_by_name="KotaMoney", assigned_to_name="Rahul",
        )

    def test_payload_carries_lead_id_and_colour_status_labels(self):
        expected = {Lead.STATUS_APPROVED: "Approved", Lead.STATUS_REJECTED: "Rejected",
                    Lead.STATUS_PROCESSING: "Processing", Lead.STATUS_NEW: "New"}
        for code, label in expected.items():
            lead = self._lead(code)
            payload = build_sync_payload(lead, "status_changed")
            self.assertEqual(payload["status"], label)
            self.assertEqual(payload["lead_reference_id"], lead.display_id)
            self.assertTrue(payload["lead_reference_id"].startswith("KM-"))
