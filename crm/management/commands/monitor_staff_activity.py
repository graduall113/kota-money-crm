"""
Record "CRM inactive" periods for staff who went silent (closed browser, locked / sleeping phone,
no network) and therefore sent no request that would have triggered the lazy check.

    python manage.py monitor_staff_activity            # one pass
    python manage.py monitor_staff_activity --loop     # worker, every 60 s

Correctness never depends on this command: every period is derived from timestamps, so a later
request (or the next run) records the same period with the same start time. Running it just makes
the admin board current. Safe to run any number of times, even concurrently.
"""
import time

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from crm import attendance, staff_activity
from crm.models import Attendance, StaffInactivityPeriod


class Command(BaseCommand):
    help = "Record CRM inactivity periods for staff with an open day (or an unclosed period)."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=int, default=60)

    def handle(self, *args, **o):
        while True:
            n = self.run_once()
            self.stdout.write(f"Checked {n} staff.")
            if not o["loop"]:
                return
            time.sleep(o["interval"])

    @staticmethod
    def run_once():
        if not staff_activity.settings.ACTIVITY_MONITORING_ENABLED:
            return 0
        ids = set(Attendance.objects.filter(end_time__isnull=True).values_list("user_id", flat=True))
        ids |= set(StaffInactivityPeriod.objects.filter(ended_at__isnull=True).values_list("user_id", flat=True))
        User = get_user_model()
        checked = 0
        for user in User.objects.filter(pk__in=ids):
            if not staff_activity.is_monitored(user):
                continue
            with transaction.atomic():
                get_user_model().objects.select_for_update().get(pk=user.pk)
                _state, rec = attendance.get_state(user)
                if rec is None:  # no record to judge against: close a leftover open period
                    for p in StaffInactivityPeriod.objects.filter(user=user, ended_at__isnull=True):
                        staff_activity._close(p, staff_activity._now(), StaffInactivityPeriod.END_DAY)
                else:
                    staff_activity.settle(user, record=rec)
            checked += 1
        return checked
