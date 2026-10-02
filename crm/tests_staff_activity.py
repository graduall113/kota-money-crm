"""Staff CRM activity monitoring (mobile): heartbeat vs activity, 15-minute rule, states, lunch, calls."""
import datetime
from io import StringIO
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from . import attendance, staff_activity as sa
from .models import CallRecord, LunchBreak, StaffInactivityPeriod, StaffPresence
from .tests_attendance import Clock

User = get_user_model()
IST = ZoneInfo("Asia/Kolkata")


def at(h, m=0, s=0, day=27, month=9, year=2026):
    return datetime.datetime(year, month, day, h, m, s, tzinfo=IST)


class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("boss", password="pw", is_superuser=True, is_staff=True)
        self.staff = User.objects.create_user("rahul", password="pw", first_name="Rahul")
        self.other = User.objects.create_user("amit", password="pw")
        with Clock(at(10, 0)):
            attendance.start_day(self.staff)  # day starts 10:00 -> inactivity clock starts here

    def sig(self, when, kind="heartbeat", vis="visible", user=None):
        with Clock(when):
            return sa.record_signal(user or self.staff, kind, vis, "sess-key")

    def state(self, when, user=None):
        with Clock(when):
            return sa.compute_state(user or self.staff, when)

    def periods(self):
        return list(StaffInactivityPeriod.objects.filter(user=self.staff).order_by("started_at"))


class HeartbeatVsActivityTests(Base):
    def test_heartbeat_is_not_meaningful_activity(self):
        for minute in range(1, 11):
            self.sig(at(10, minute), "heartbeat")
        p = StaffPresence.objects.get(user=self.staff)
        self.assertIsNotNone(p.last_heartbeat_at)
        self.assertIsNone(p.last_activity_at)
        self.assertEqual(p.active_seconds, 0)

    def test_activity_updates_last_activity_and_credits_time(self):
        self.sig(at(10, 1), "activity")
        p = StaffPresence.objects.get(user=self.staff)
        self.assertEqual(p.last_activity_at, at(10, 1))
        self.assertEqual(p.active_seconds, 60)
        self.sig(at(10, 1, 20), "activity")  # continuous use is credited by real elapsed time, not 60 s each
        self.assertEqual(StaffPresence.objects.get(user=self.staff).active_seconds, 80)

    def test_hidden_page_activity_is_ignored(self):
        self.sig(at(10, 1), "activity", vis="hidden")
        self.assertIsNone(StaffPresence.objects.get(user=self.staff).last_activity_at)

    def test_activity_faster_than_server_minimum_is_not_double_counted(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 1, 2), "activity")
        self.assertEqual(StaffPresence.objects.get(user=self.staff).last_activity_at, at(10, 1))

    def test_nothing_recorded_when_day_not_started(self):
        with Clock(at(10, 5)):
            d = sa.record_signal(self.other, "activity", "visible", "s")
        self.assertEqual(d["state"], sa.NOT_WORKING)
        self.assertFalse(StaffPresence.objects.filter(user=self.other).exists())


class InactivityRuleTests(Base):
    def test_inactive_after_15_minutes_without_activity_even_with_heartbeats(self):
        self.sig(at(10, 1), "activity")
        for minute in range(2, 20):
            self.sig(at(10, minute), "heartbeat")  # heartbeat keeps coming: must not reset the clock
        (period,) = self.periods()
        self.assertEqual(period.last_activity_at, at(10, 1))
        self.assertEqual(period.started_at, at(10, 16))
        self.assertIsNone(period.ended_at)
        self.assertEqual(self.state(at(10, 19))["staff_status"], "inactive")

    def test_not_inactive_before_15_minutes(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 15, 59), "heartbeat")
        self.assertEqual(self.periods(), [])
        self.assertEqual(self.state(at(10, 15, 59))["staff_status"], "working")

    def test_day_start_counts_as_the_first_activity(self):
        self.sig(at(10, 14), "heartbeat")
        self.assertEqual(self.periods(), [])
        self.sig(at(10, 16), "heartbeat")
        self.assertEqual(self.periods()[0].started_at, at(10, 15))

    def test_activity_resets_timer_and_closes_period(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 20), "heartbeat")
        self.sig(at(10, 30), "activity")
        (period,) = self.periods()
        self.assertEqual(period.ended_at, at(10, 30))
        self.assertEqual(period.ended_by, StaffInactivityPeriod.END_ACTIVITY)
        self.assertEqual(self.state(at(10, 30, 5))["staff_status"], "working")

    def test_period_is_recorded_retroactively_when_the_phone_was_silent(self):
        self.sig(at(10, 1), "activity")          # then: browser closed / phone asleep, nothing for an hour
        self.sig(at(11, 5), "activity")          # first real CRM use back
        (period,) = self.periods()
        self.assertEqual(period.started_at, at(10, 16))  # the REAL start, not the time it was noticed
        self.assertEqual(period.ended_at, at(11, 5))

    def test_repeated_evaluation_never_duplicates(self):
        self.sig(at(10, 1), "activity")
        for s in range(0, 10):
            self.sig(at(10, 30, s), "heartbeat")
        self.assertEqual(StaffInactivityPeriod.objects.filter(user=self.staff).count(), 1)

    def test_only_one_open_period_per_user_in_the_database(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 20), "heartbeat")
        with self.assertRaises(IntegrityError), transaction.atomic():
            StaffInactivityPeriod.objects.create(user=self.staff, work_date=at(10).date(),
                                                 last_activity_at=at(10, 2), started_at=at(10, 17))

    def test_period_records_state_and_visibility_when_detected(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 12), "visibility", vis="hidden")     # then silence (phone locked / app switched)
        with Clock(at(10, 16)):
            call_command("monitor_staff_activity", stdout=StringIO())
        p = self.periods()[0]
        self.assertEqual(p.visibility_state, "hidden")
        self.assertEqual(p.session_state, sa.BACKGROUND)

    def test_warning_flag_before_threshold(self):
        self.sig(at(10, 1), "activity")
        d = self.state(at(10, 13, 30))
        self.assertTrue(d["warn"])
        self.assertEqual(d["staff_status"], "working")
        self.assertEqual(d["seconds_to_inactive"], 150)
        self.assertFalse(self.state(at(10, 5))["warn"])

    def test_day_end_closes_open_period(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 20), "heartbeat")
        with Clock(at(11, 0)):
            attendance.end_day(self.staff)
        with Clock(at(11, 30)):
            call_command("monitor_staff_activity", stdout=StringIO())
        (period,) = self.periods()
        self.assertEqual(period.ended_at, at(11, 0))
        self.assertEqual(period.ended_by, StaffInactivityPeriod.END_DAY)


class CrmStateTests(Base):
    def test_active_then_temporarily_inactive(self):
        self.sig(at(10, 1), "activity")
        self.assertEqual(self.state(at(10, 1, 30))["state"], sa.ACTIVE)
        self.sig(at(10, 4), "heartbeat")
        self.assertEqual(self.state(at(10, 4, 30))["state"], sa.TEMP_INACTIVE)  # heartbeat alone is not activity

    def test_hidden_page_is_background_not_an_app_name(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 2), "visibility", vis="hidden")
        d = self.state(at(10, 3))
        self.assertEqual(d["state"], sa.BACKGROUND)
        self.assertNotIn("whatsapp", d["label"].lower())
        self.sig(at(10, 8), "visibility", vis="visible")
        self.assertEqual(self.state(at(10, 8, 5))["state"], sa.TEMP_INACTIVE)  # visible again, but not ACTIVE until real use
        self.sig(at(10, 9), "activity")
        self.assertEqual(self.state(at(10, 9, 10))["state"], sa.ACTIVE)

    def test_background_time_is_not_active_time(self):
        self.sig(at(10, 1), "activity")
        self.sig(at(10, 2), "visibility", vis="hidden")
        self.sig(at(10, 12), "visibility", vis="visible")
        self.assertEqual(StaffPresence.objects.get(user=self.staff).active_seconds, 60)

    def test_short_network_loss_is_not_disconnection(self):
        self.sig(at(10, 1), "activity")
        d = self.state(at(10, 1, 50))  # ~50 s without a ping (one missed heartbeat)
        self.assertEqual(d["state"], sa.ACTIVE)
        self.assertEqual(self.state(at(10, 2, 30))["state"] in (sa.ACTIVE, sa.TEMP_INACTIVE), True)

    def test_silence_beyond_grace_is_unknown_then_disconnected(self):
        self.sig(at(10, 6), "heartbeat")
        self.assertEqual(self.state(at(10, 9))["state"], sa.UNKNOWN)         # 180 s: not enough information
        self.assertEqual(self.state(at(10, 12))["state"], sa.DISCONNECTED)   # 360 s: session disconnected

    def test_reconnect_restores_state(self):
        self.sig(at(10, 1), "activity")
        self.assertEqual(self.state(at(10, 10))["state"], sa.DISCONNECTED)
        self.sig(at(10, 10, 5), "activity")
        self.assertEqual(self.state(at(10, 10, 10))["state"], sa.ACTIVE)

    def test_no_signal_yet_is_unknown(self):
        self.assertEqual(self.state(at(10, 3))["state"], sa.UNKNOWN)

    def test_state_before_day_start_is_not_working(self):
        self.assertEqual(self.state(at(10, 3), user=self.other)["state"], sa.NOT_WORKING)


class LunchTests(Base):
    def lunch_start(self, when):
        with Clock(when):
            return sa.start_lunch(self.staff)

    def lunch_end(self, when):
        with Clock(when):
            return sa.end_lunch(self.staff)

    def test_lunch_state_and_no_inactivity_during_valid_lunch(self):
        self.sig(at(10, 1), "activity")
        self.lunch_start(at(10, 5))
        self.sig(at(10, 40), "heartbeat")
        self.assertEqual(self.periods(), [])
        self.assertEqual(self.state(at(10, 40))["state"], sa.LUNCH)
        self.assertEqual(self.state(at(10, 40))["staff_status"], "lunch")

    def test_clock_restarts_after_lunch(self):
        self.sig(at(10, 1), "activity")
        self.lunch_start(at(10, 5))
        self.lunch_end(at(10, 50))
        self.sig(at(11, 0), "heartbeat")
        self.assertEqual(self.periods(), [])
        self.sig(at(11, 6), "heartbeat")  # 16 min after lunch ended with no activity
        self.assertEqual(self.periods()[0].started_at, at(11, 5))

    def test_idle_time_before_lunch_is_recorded_and_closed_at_lunch(self):
        self.sig(at(10, 1), "activity")
        self.lunch_start(at(10, 30))
        (period,) = self.periods()
        self.assertEqual((period.started_at, period.ended_at, period.ended_by), (at(10, 16), at(10, 30), StaffInactivityPeriod.END_LUNCH))

    def test_overlong_lunch_stops_excusing_inactivity(self):
        self.sig(at(10, 1), "activity")
        self.lunch_start(at(10, 5))
        self.sig(at(11, 30), "heartbeat")  # 50-minute lunch allowance ended 10:55; +15 min = 11:10
        self.assertEqual(self.periods()[0].started_at, at(11, 10))
        self.assertNotEqual(self.state(at(11, 30))["state"], sa.LUNCH)

    def test_one_lunch_per_day_and_must_have_started_day(self):
        self.lunch_start(at(10, 5))
        self.lunch_end(at(10, 30))
        with self.assertRaises(sa.LunchError):
            self.lunch_start(at(12, 0))
        with self.assertRaises(sa.LunchError), Clock(at(12, 0)):
            sa.start_lunch(self.other)  # day not started
        self.assertEqual(LunchBreak.objects.filter(user=self.staff).count(), 1)

    def test_end_lunch_without_lunch(self):
        with self.assertRaises(sa.LunchError):
            self.lunch_end(at(10, 5))


class CallCoverageTests(Base):
    def call(self, start, end, n="1"):
        return CallRecord.objects.create(
            staff=self.staff, direction="outgoing", status="answered", started_at=start, ended_at=end,
            duration_seconds=int((end - start).total_seconds()), external_call_id=f"dev:{n}", phone_number="9876543210")

    def test_call_synced_after_the_fact_covers_inactivity(self):
        self.sig(at(10, 20), "heartbeat")                     # period opened at 10:15 (call not synced yet)
        self.assertEqual(self.periods()[0].started_at, at(10, 15))
        with Clock(at(10, 25)):
            self.call(at(10, 0), at(10, 20))                  # Android app syncs it at 10:25
        (period,) = self.periods()
        self.assertEqual(period.ended_at, at(10, 20))
        self.assertEqual(period.ended_by, StaffInactivityPeriod.END_CALL)
        self.assertEqual(period.call_overlap_seconds, 300)

    def test_already_synced_call_resets_the_clock(self):
        with Clock(at(10, 12)):
            self.call(at(10, 5), at(10, 12))
        self.sig(at(10, 25), "heartbeat")                     # 13 min after the call ended
        self.assertEqual(self.periods(), [])
        self.sig(at(10, 28), "heartbeat")
        self.assertEqual(self.periods()[0].started_at, at(10, 27))

    def test_no_live_call_state_exists(self):
        self.assertNotIn("on_call", sa.LABELS)


class EndpointTests(Base):
    def setUp(self):
        super().setUp()
        self.c = Client()
        self.c.force_login(self.staff)
        self.url = reverse("activity_signal")

    def post(self, when, data, client=None):
        with Clock(when):
            return (client or self.c).post(self.url, data)

    def test_login_required_returns_json_401(self):
        r = self.post(at(10, 1), {"kind": "heartbeat"}, client=Client())
        self.assertEqual(r.status_code, 401)

    def test_get_not_allowed(self):
        self.assertEqual(self.c.get(self.url).status_code, 405)

    def test_identity_comes_from_the_session_not_the_body(self):
        r = self.post(at(10, 1), {"kind": "activity", "visibility": "visible", "user": self.other.pk, "user_id": self.other.pk,
                                  "timestamp": "2020-01-01T00:00:00"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(StaffPresence.objects.filter(user=self.staff).exists())
        self.assertFalse(StaffPresence.objects.filter(user=self.other).exists())
        self.assertEqual(StaffPresence.objects.get(user=self.staff).last_activity_at, at(10, 1))  # server clock

    def test_response_shape_and_bad_input(self):
        d = self.post(at(10, 13), {"kind": "heartbeat", "visibility": "visible"}).json()
        self.assertEqual(d["state"], sa.TEMP_INACTIVE)  # just heard from it, but no real use for 13 min
        self.assertIn("heartbeat_seconds", d)
        self.assertNotIn("last_activity_at", d)
        self.assertEqual(self.post(at(10, 14), {"kind": "teleport"}).status_code, 400)
        self.assertEqual(self.post(at(10, 14), {"kind": "heartbeat", "visibility": "banana"}).status_code, 400)

    def test_warning_numbers_are_server_computed(self):
        self.post(at(10, 1), {"kind": "activity", "visibility": "visible"})
        d = self.post(at(10, 13), {"kind": "heartbeat", "visibility": "visible"}).json()
        self.assertTrue(d["warn"])
        self.assertEqual(d["seconds_to_inactive"], 180)

    def test_admins_are_not_monitored(self):
        c = Client()
        c.force_login(self.admin)
        r = self.post(at(10, 1), {"kind": "activity"}, client=c)
        self.assertEqual(r.json(), {"monitored": False})
        self.assertFalse(StaffPresence.objects.filter(user=self.admin).exists())

    def test_day_not_started_gets_not_working_and_is_not_redirected(self):
        c = Client()
        c.force_login(self.other)
        r = self.post(at(10, 1), {"kind": "activity"}, client=c)
        self.assertEqual(r.status_code, 200)  # allow-listed past the attendance gate
        self.assertEqual(r.json()["state"], sa.NOT_WORKING)

    @override_settings(ACTIVITY_MONITORING_ENABLED=False)
    def test_master_switch(self):
        self.assertEqual(self.post(at(10, 1), {"kind": "activity"}).json(), {"monitored": False})

    def test_lunch_endpoints(self):
        with Clock(at(10, 5)):
            self.assertEqual(self.c.post(reverse("lunch_start")).status_code, 302)
        self.assertTrue(LunchBreak.objects.filter(user=self.staff, ended_at__isnull=True).exists())
        with Clock(at(10, 40)):
            self.assertEqual(self.c.post(reverse("lunch_end")).status_code, 302)
        self.assertFalse(LunchBreak.objects.filter(user=self.staff, ended_at__isnull=True).exists())
        self.assertEqual(self.c.get(reverse("lunch_start")).status_code, 405)

    def test_widget_renders_activity_script_for_working_staff(self):
        with Clock(at(10, 5)):
            r = self.c.get(reverse("dashboard"))
        self.assertContains(r, 'id="actCfg"')
        self.assertContains(r, "staff_activity.js")
        self.assertContains(r, "Start Lunch")


class AdminBoardAndCommandTests(Base):
    def test_board_is_admin_only(self):
        c = Client()
        c.force_login(self.staff)
        with Clock(at(10, 30)):
            self.assertEqual(c.get(reverse("staff_activity")).status_code, 403)
        c.force_login(self.admin)
        with Clock(at(10, 30)):
            r = c.get(reverse("staff_activity"))
        self.assertContains(r, "Rahul")
        self.assertContains(r, "cannot see which other phone app")

    def test_board_shows_inactive_staff(self):
        self.sig(at(10, 1), "activity")
        c = Client()
        c.force_login(self.admin)
        with Clock(at(10, 20)):
            r = c.get(reverse("staff_activity"))
        self.assertContains(r, "CRM inactive for 19 min")

    def test_command_records_silent_staff_and_is_idempotent(self):
        with Clock(at(10, 30)):
            call_command("monitor_staff_activity", stdout=StringIO())
            call_command("monitor_staff_activity", stdout=StringIO())
        (period,) = self.periods()
        self.assertEqual(period.started_at, at(10, 15))
        self.assertIsNone(period.ended_at)
