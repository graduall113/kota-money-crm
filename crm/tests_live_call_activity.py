"""Staff Activity: live assigned-customer call, stale safety, 15-min Android warning, 50-min lunch.
Separate from the calling-sync tests: CallRecord must never be created or touched here."""
import datetime
import json

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, override_settings
from django.urls import reverse

from . import attendance, live_call, staff_activity as sa, tests_staff_activity as T
from .models import CallDevice, CallRecord, Lead, PrivilegedNumber, StaffInactivityPeriod, StaffLiveCallActivity
from .tests_attendance import Clock

User = get_user_model()
at = T.at
MINE, THEIRS, UNKNOWN = "9876543210", "9123456780", "9000000001"


class LiveBase(T.Base):
    def setUp(self):
        super().setUp()  # staff "rahul" started day 10:00 (IST, 27 Sep 2026)
        Lead.objects.create(customer_name="Mine", contact_number=MINE, assigned_to=self.staff)
        Lead.objects.create(customer_name="Theirs", contact_number=THEIRS, assigned_to=self.other)
        self.device, self.token = self.pair(self.staff)

    def pair(self, user):
        d = CallDevice.objects.create(staff=user, label="phone")
        d.start_pairing()
        return d, d.complete_pairing("android-" + user.username)

    def post(self, name, when, body, token="__default__"):
        token = self.token if token == "__default__" else token
        extra = {"HTTP_AUTHORIZATION": f"Bearer {token}"} if token else {}
        with Clock(when):
            return Client().post(reverse(name), json.dumps(body), content_type="application/json", **extra)

    def ev(self, when, state, phone=None, sid="sess-0001-aaaa", token="__default__"):
        body = {"session_id": sid, "state": state}
        if phone:
            body["phone_number"] = phone
        return self.post("api_staff_activity_live_call", when, body, token)

    def status(self, when, ack=None):
        body = {"ack_warning_id": ack} if ack else {}
        return self.post("api_staff_activity_status", when, body).json()


class AssignmentTests(LiveBase):
    def test_assigned_call_is_active_even_with_crm_hidden(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 3), "visibility", vis="hidden")
        r = self.ev(at(10, 5), "CALL_ACTIVE", MINE).json()
        self.assertTrue(r["success"] and r["qualifying_assigned_call"])
        d = self.state(at(10, 6))
        self.assertEqual((d["state"], d["reason"], d["on_assigned_call"]), (sa.ACTIVE, "assigned_call", True))
        self.assertEqual(d["reason_label"], "Assigned customer call")
        self.assertNotIn(MINE, json.dumps(d, default=str))  # no customer number in the state

    def test_unassigned_call_does_not_override(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 3), "visibility", vis="hidden")
        self.assertFalse(self.ev(at(10, 5), "CALL_ACTIVE", UNKNOWN).json()["qualifying_assigned_call"])
        self.assertEqual(self.state(at(10, 6))["state"], sa.BACKGROUND)

    def test_call_to_customer_assigned_to_someone_else_does_not_qualify(self):
        self.sig(at(10, 3), "visibility", vis="hidden")
        self.assertFalse(self.ev(at(10, 5), "CALL_ACTIVE", THEIRS).json()["qualifying_assigned_call"])
        self.assertEqual(self.state(at(10, 6))["state"], sa.BACKGROUND)

    def test_privileged_number_does_not_qualify(self):
        PrivilegedNumber.objects.create(phone_number=MINE, label="Owner")
        self.assertFalse(self.ev(at(10, 5), "CALL_ACTIVE", MINE).json()["qualifying_assigned_call"])

    def test_ringing_not_yet_connected_does_not_count(self):
        self.sig(at(10, 3), "visibility", vis="hidden")
        self.assertTrue(self.ev(at(10, 5), "CALL_STARTED", MINE).json()["qualifying_assigned_call"])  # it IS an assigned customer...
        self.assertNotEqual(self.state(at(10, 5, 30))["reason"], "assigned_call")  # ...but nobody is talking yet

    def test_no_call_background_is_normal(self):
        self.sig(at(10, 3), "visibility", vis="hidden")
        self.assertEqual(self.state(at(10, 4))["state"], sa.BACKGROUND)

    def test_calling_sync_data_never_touched(self):
        before = Lead.objects.get(contact_number=MINE).status
        self.ev(at(10, 5), "CALL_ACTIVE", MINE)
        self.ev(at(10, 9), "CALL_ENDED")
        self.assertEqual(CallRecord.objects.count(), 0)
        self.assertEqual(Lead.objects.get(contact_number=MINE).status, before)


class WarningTests(LiveBase):
    def test_14_minutes_no_warning_15_minutes_warning(self):
        self.sig(at(10, 1), "activity")
        d = self.state(at(10, 15, 59))  # 14:59 after activity
        self.assertFalse(d["warning_due"])
        self.assertIsNone(self.status(at(10, 15, 59))["warning"])
        self.assertTrue(self.state(at(10, 16))["warning_due"])
        w = self.status(at(10, 16))["warning"]
        self.assertEqual(w["title"], "Kota Money Activity Warning")
        self.assertIn("15 minutes", w["body"])

    def test_one_warning_per_period_after_ack(self):
        self.sig(at(10, 1), "activity")
        w = self.status(at(10, 16))["warning"]
        self.assertEqual(self.status(at(10, 17))["warning"]["id"], w["id"])  # not acked yet -> still pending
        self.assertIsNone(self.status(at(10, 18), ack=w["id"])["warning"])
        self.assertIsNone(self.status(at(10, 40))["warning"])
        self.assertEqual(StaffInactivityPeriod.objects.filter(user=self.staff).count(), 1)

    def test_next_poll_wakes_at_threshold(self):
        self.sig(at(10, 1), "activity")
        self.assertEqual(self.status(at(10, 15, 40))["next_poll_seconds"], 21)

    def test_assigned_call_at_14_minutes_suppresses_warning(self):
        self.sig(at(10, 1), "activity")
        self.ev(at(10, 15), "CALL_ACTIVE", MINE)
        for t in (at(10, 16), at(10, 20)):
            self.ev(t, "CALL_ACTIVE", MINE)
        self.assertIsNone(self.status(at(10, 21))["warning"])
        self.assertEqual(self.state(at(10, 21))["state"], sa.ACTIVE)
        self.assertEqual(self.periods(), [])

    def test_call_end_restarts_normal_calculation(self):
        self.sig(at(10, 1), "activity")
        self.ev(at(10, 15), "CALL_ACTIVE", MINE)
        self.ev(at(10, 20), "CALL_ENDED")
        d = self.state(at(10, 21))
        self.assertFalse(d["on_assigned_call"])
        self.assertFalse(self.state(at(10, 34, 59))["warning_due"])
        self.assertTrue(self.state(at(10, 35))["warning_due"])  # 15 min after the call ended

    def test_stale_live_call_stops_counting(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 3), "visibility", vis="hidden")
        self.ev(at(10, 5), "CALL_ACTIVE", MINE)  # then the phone dies: no more refreshes
        ttl = settings.ACTIVITY_LIVE_CALL_TTL_SECONDS
        self.assertTrue(self.state(at(10, 5) + datetime.timedelta(seconds=ttl))["on_assigned_call"])
        late = at(10, 5) + datetime.timedelta(seconds=ttl + 1)
        self.assertFalse(self.state(late)["on_assigned_call"])
        self.assertEqual(self.state(late)["state"], sa.BACKGROUND)
        # the stale call is trusted only up to its last refresh: inactivity counts from 10:05
        self.assertTrue(self.state(at(10, 20))["warning_due"])
        self.assertFalse(self.state(at(10, 19, 59))["warning_due"])


class LunchTests(LiveBase):
    def test_default_lunch_allowance_is_50_minutes(self):
        self.assertEqual(settings.ACTIVITY_LUNCH_MAX_MINUTES, 50)

    def test_lunch_30_minutes_no_warning(self):
        self.sig(at(10, 1), "activity")
        with Clock(at(10, 5)):
            sa.start_lunch(self.staff)
        self.assertEqual(self.state(at(10, 35))["state"], sa.LUNCH)
        self.assertIsNone(self.status(at(10, 35))["warning"])

    def test_lunch_exactly_50_minutes_still_inside_allowance(self):
        self.sig(at(10, 1), "activity")
        with Clock(at(10, 5)):
            sa.start_lunch(self.staff)
        self.assertEqual(self.state(at(10, 55))["state"], sa.LUNCH)
        self.assertIsNone(self.status(at(10, 55))["warning"])

    def test_after_50_minutes_normal_inactivity_resumes(self):
        self.sig(at(10, 1), "activity")
        with Clock(at(10, 5)):
            sa.start_lunch(self.staff)
        self.assertNotEqual(self.state(at(10, 55, 1))["state"], sa.LUNCH)
        self.assertFalse(self.state(at(11, 9, 59))["warning_due"])
        self.assertTrue(self.state(at(11, 10))["warning_due"])  # 10:55 + 15 min


class ApiRobustnessTests(LiveBase):
    def sessions(self):
        return StaffLiveCallActivity.objects.filter(staff=self.staff)

    def test_duplicate_started_is_idempotent(self):
        for t in (at(10, 5), at(10, 5, 2)):
            self.ev(t, "CALL_STARTED", MINE)
        self.assertEqual(self.sessions().count(), 1)

    def test_network_retry_of_active_creates_no_second_session(self):
        for t in (at(10, 5), at(10, 5, 1), at(10, 5, 9)):
            self.ev(t, "CALL_ACTIVE", MINE)
        self.assertEqual(self.sessions().count(), 1)
        self.assertEqual(self.sessions().get().connected_at, at(10, 5))

    def test_duplicate_ended_is_idempotent(self):
        self.ev(at(10, 5), "CALL_ACTIVE", MINE)
        self.ev(at(10, 9), "CALL_ENDED")
        first = self.sessions().get().ended_at
        self.ev(at(10, 12), "CALL_ENDED")
        self.assertEqual((self.sessions().count(), self.sessions().get().ended_at), (1, first))

    def test_ended_before_delayed_active_cannot_resurrect(self):
        self.ev(at(10, 9), "CALL_ENDED")
        self.assertFalse(self.ev(at(10, 10), "CALL_ACTIVE", MINE).json()["qualifying_assigned_call"])
        self.assertEqual(self.sessions().count(), 1)
        self.assertFalse(self.state(at(10, 10, 30))["on_assigned_call"])

    def test_unauthorized_and_revoked_devices_rejected(self):
        self.assertEqual(self.ev(at(10, 5), "CALL_ACTIVE", MINE, token="wrong").status_code, 401)
        self.assertEqual(self.ev(at(10, 5), "CALL_ACTIVE", MINE, token="").status_code, 401)
        CallDevice.objects.filter(pk=self.device.pk).update(is_active=False)
        self.assertEqual(self.ev(at(10, 5), "CALL_ACTIVE", MINE).status_code, 401)
        self.assertEqual(self.post("api_staff_activity_status", at(10, 5), {}).status_code, 401)
        self.assertEqual(self.sessions().count(), 0)

    def test_bad_input_rejected(self):
        self.assertEqual(self.ev(at(10, 5), "CALL_ACTIVE", MINE, sid="x").status_code, 400)
        self.assertEqual(self.ev(at(10, 5), "TELEPORT", MINE).status_code, 400)

    def test_phone_number_is_not_stored(self):
        self.ev(at(10, 5), "CALL_ACTIVE", MINE)
        row = self.sessions().get()
        self.assertNotIn(MINE, json.dumps({f.name: str(getattr(row, f.name)) for f in row._meta.fields}))

    def test_attendance_not_started_never_active(self):
        dev, tok = self.pair(self.other)
        Lead.objects.create(customer_name="Amit's", contact_number=UNKNOWN, assigned_to=self.other)
        self.ev(at(10, 5), "CALL_ACTIVE", UNKNOWN, token=tok)
        with Clock(at(10, 6)):
            self.assertEqual(sa.compute_state(self.other, at(10, 6))["state"], sa.NOT_WORKING)

    def test_end_day_never_active(self):
        with Clock(at(12, 0)):
            attendance.end_day(self.staff)
        self.ev(at(12, 5), "CALL_ACTIVE", MINE)
        with Clock(at(12, 6)):
            self.assertEqual(sa.compute_state(self.staff, at(12, 6))["state"], sa.NOT_WORKING)
