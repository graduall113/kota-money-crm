"""Tests for the Calling Device revoke / re-enable / delete flow and the Android sync API."""
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import AuditLog, CallDevice, CallRecord, Lead, StaffProfile

User = get_user_model()


@override_settings(ATTENDANCE_ENFORCED=False)  # these tests predate attendance gating
class CallDeviceDeleteTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True)  # signal -> admin role
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.other_staff = User.objects.create_user("amit", password="pw")
        self.lead = Lead.objects.create(customer_name="Cust", contact_number="9876543210", assigned_to=self.staff)

        self.device = CallDevice.objects.create(staff=self.staff, label="Rahul's phone", created_by=self.admin)
        self.device.start_pairing()
        self.token = self.device.complete_pairing("android-1")
        self.record = CallRecord.objects.create(
            staff=self.staff, device=self.device, lead=self.lead, phone_number="9876543210",
            phone_normalized="9876543210", direction="outgoing", status="answered",
            started_at=timezone.now(), duration_seconds=30, external_call_id="android-1:1",
        )
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.admin)

    # helpers -------------------------------------------------------
    def _csrf_post(self, client, url):
        client.get(reverse("calling_devices"))  # sets csrftoken cookie
        token = client.cookies["csrftoken"].value
        return client.post(url, {"csrfmiddlewaretoken": token}, follow=False)

    def _delete_url(self, pk=None):
        return reverse("calling_device_delete", args=[pk or self.device.pk])

    def _sync(self, token=None, ext="android-1:2"):
        body = {"calls": [{"external_call_id": ext, "phone_number": "9876543210", "direction": "outgoing",
                           "status": "missed", "duration_seconds": 10,
                           "started_at": int(timezone.now().timestamp() * 1000)}]}
        return Client().post(reverse("api_calling_sync"), json.dumps(body), content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {token or self.token}")

    # UI ------------------------------------------------------------
    def test_both_buttons_visible_for_paired_and_revoked(self):
        html = self.client.get(reverse("calling_devices")).content.decode()
        self.assertIn("Revoke", html)
        self.assertIn(self._delete_url(), html)
        self.assertIn("This device is currently paired.", html)
        self.client.post(reverse("calling_device_toggle", args=[self.device.pk]),
                         {"csrfmiddlewaretoken": self.client.get(reverse("calling_devices")).cookies["csrftoken"].value})
        html = self.client.get(reverse("calling_devices")).content.decode()
        self.assertIn("Re-enable", html)
        self.assertIn(self._delete_url(), html)

    # revoke / re-enable still work ---------------------------------
    def test_revoke_and_reenable(self):
        r = self._csrf_post(self.client, reverse("calling_device_toggle", args=[self.device.pk]))
        self.assertEqual(r.status_code, 302)
        self.device.refresh_from_db()
        self.assertFalse(self.device.is_active)
        self.assertEqual(self._sync().status_code, 401)
        self._csrf_post(self.client, reverse("calling_device_toggle", args=[self.device.pk]))
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_active)
        self.assertEqual(self._sync().status_code, 201)

    def test_toggle_rejects_get(self):
        self.assertEqual(self.client.get(reverse("calling_device_toggle", args=[self.device.pk])).status_code, 405)
        self.device.refresh_from_db()
        self.assertTrue(self.device.is_active)

    # delete --------------------------------------------------------
    def test_delete_paired_device_preserves_everything(self):
        self.assertTrue(self.device.is_paired and self.device.is_active)
        r = self._csrf_post(self.client, self._delete_url())
        self.assertEqual(r.status_code, 302)
        self.assertFalse(CallDevice.objects.filter(pk=self.device.pk).exists())
        # history / staff / leads survive
        self.record.refresh_from_db()
        self.assertIsNone(self.record.device_id)
        self.assertEqual(CallRecord.objects.count(), 1)
        self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())
        self.assertTrue(Lead.objects.filter(pk=self.lead.pk).exists())
        # token is dead
        self.assertEqual(self._sync().status_code, 401)
        # audit trail
        log = AuditLog.objects.get(action="call_device_deleted")
        self.assertEqual(log.actor_id, self.admin.pk)
        self.assertEqual(log.target_id, str(self.device.pk))
        self.assertEqual(log.details["staff_id"], self.staff.pk)
        self.assertEqual(log.details["call_records_preserved"], 1)
        self.assertTrue(log.details["was_paired"])

    def test_delete_revoked_device(self):
        self.device.is_active = False
        self.device.save()
        self._csrf_post(self.client, self._delete_url())
        self.assertFalse(CallDevice.objects.filter(pk=self.device.pk).exists())
        self.assertEqual(CallRecord.objects.count(), 1)

    def test_delete_device_without_history_and_unpaired(self):
        d = CallDevice.objects.create(staff=self.other_staff, label="new")
        d.start_pairing()
        self._csrf_post(self.client, self._delete_url(d.pk))
        self.assertFalse(CallDevice.objects.filter(pk=d.pk).exists())
        # its pairing code can no longer be redeemed
        r = Client().post(reverse("api_calling_pair"), json.dumps({"pairing_code": d.pairing_code, "device_id": "x"}),
                          content_type="application/json")
        self.assertEqual(r.status_code, 404)

    def test_double_delete_and_missing_device_are_graceful(self):
        self._csrf_post(self.client, self._delete_url())
        r = self._csrf_post(self.client, self._delete_url())  # already deleted
        self.assertEqual(r.status_code, 302)
        r = self._csrf_post(self.client, self._delete_url(999999))  # never existed
        self.assertEqual(r.status_code, 302)
        page = self.client.get(reverse("calling_devices")).content.decode()
        self.assertNotIn("Traceback", page)
        self.assertEqual(AuditLog.objects.filter(action="call_device_deleted").count(), 1)

    def test_invalid_id_is_404_not_500(self):
        self.assertEqual(self.client.post("/calling/devices/abc/delete/").status_code, 404)

    # security ------------------------------------------------------
    def test_get_cannot_delete(self):
        self.assertEqual(self.client.get(self._delete_url()).status_code, 405)
        self.assertTrue(CallDevice.objects.filter(pk=self.device.pk).exists())

    def test_csrf_required(self):
        self.assertEqual(self.client.post(self._delete_url()).status_code, 403)
        self.assertTrue(CallDevice.objects.filter(pk=self.device.pk).exists())

    def _post_with_manual_csrf(self, client, url):
        client.cookies["csrftoken"] = "a" * 32
        return client.post(url, {"csrfmiddlewaretoken": "a" * 32})

    def test_anonymous_cannot_delete(self):
        r = self._post_with_manual_csrf(Client(enforce_csrf_checks=True), self._delete_url())
        self.assertEqual(r.status_code, 302)
        self.assertIn("login", r["Location"])
        self.assertTrue(CallDevice.objects.filter(pk=self.device.pk).exists())

    def test_non_admin_staff_cannot_delete(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.staff)
        r = self._post_with_manual_csrf(c, self._delete_url())
        self.assertEqual(r.status_code, 403)
        self.assertTrue(CallDevice.objects.filter(pk=self.device.pk).exists())
        self.assertEqual(AuditLog.objects.filter(action="call_device_deleted").count(), 0)

    # sync race -----------------------------------------------------
    def test_sync_after_delete_returns_401_not_500(self):
        self.device.delete()
        self.assertEqual(self._sync().status_code, 401)
