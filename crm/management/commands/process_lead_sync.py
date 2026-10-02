"""
Deliver / retry the Lead -> n8n -> Google Sheets sync queue.

    python manage.py process_lead_sync                 # send everything that is due, then exit
    python manage.py process_lead_sync --loop          # keep running (worker process), every 30 s
    python manage.py process_lead_sync --status        # queue counts + recent failures, sends nothing
    python manage.py process_lead_sync --requeue-failed   # give up-for-good events one more round
    python manage.py process_lead_sync --enqueue KM-1050,KM-1051   # force a fresh send of these leads
    python manage.py process_lead_sync --prune-days 30 # delete finished events older than 30 days

Normal saves already deliver in the background; this command is the guaranteed retry driver
(schedule it every minute in cron / a Render cron job) and the manual repair tool.
"""
import time
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count
from django.utils import timezone

from crm import lead_sync
from crm.models import Lead, LeadSyncEvent


class Command(BaseCommand):
    help = "Deliver and retry queued Lead -> n8n -> Google Sheets sync events."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running instead of exiting.")
        parser.add_argument("--interval", type=int, default=30, help="Seconds between passes with --loop.")
        parser.add_argument("--limit", type=int, default=200, help="Max events per pass.")
        parser.add_argument("--status", action="store_true", help="Show queue status and exit.")
        parser.add_argument("--requeue-failed", action="store_true", help="Reset 'failed' events to pending.")
        parser.add_argument("--enqueue", default="", help="Comma-separated Lead IDs (KM-1050) to force-resync.")
        parser.add_argument("--prune-days", type=int, default=0, help="Delete success/skipped/failed events older than N days.")

    def handle(self, *args, **o):
        if o["status"]:
            return self._status()
        if o["prune_days"]:
            cutoff = timezone.now() - timedelta(days=o["prune_days"])
            n = LeadSyncEvent.objects.filter(
                status__in=[LeadSyncEvent.SUCCESS, LeadSyncEvent.SKIPPED, LeadSyncEvent.FAILED], created_at__lt=cutoff).delete()[0]
            self.stdout.write(f"Pruned {n} old sync events.")
        if o["requeue_failed"]:
            n = 0
            for ev in LeadSyncEvent.objects.filter(status=LeadSyncEvent.FAILED):
                if LeadSyncEvent.objects.filter(lead_id=ev.lead_id, status=LeadSyncEvent.PENDING).exists():
                    continue  # a newer pending event already covers this lead
                ev.status, ev.attempts, ev.next_attempt_at = LeadSyncEvent.PENDING, 0, timezone.now()
                ev.save(update_fields=["status", "attempts", "next_attempt_at"])
                n += 1
            self.stdout.write(f"Re-queued {n} failed events.")
        if o["enqueue"]:
            for code in [c.strip() for c in o["enqueue"].split(",") if c.strip()]:
                prefix = Lead.LEAD_ID_PREFIX + "-"
                if not code.upper().startswith(prefix) or not code[len(prefix):].isdigit():
                    raise CommandError(f"'{code}' is not a Lead ID like {prefix}1050.")
                pk = int(code[len(prefix):])
                if not Lead.objects.filter(pk=pk).exists():
                    raise CommandError(f"Lead {code} does not exist.")
                lead_sync.enqueue_lead_sync(pk, "manual_resync", force=True)
                self.stdout.write(f"Queued {code}.")

        while True:
            handled = lead_sync.drain_due_events(limit=o["limit"])
            if handled or not o["loop"]:
                self.stdout.write(f"Processed {handled} sync events.")
            if not o["loop"]:
                return
            time.sleep(o["interval"])

    def _status(self):
        counts = dict(LeadSyncEvent.objects.values_list("status").annotate(c=Count("id")).values_list("status", "c"))
        for key, label in LeadSyncEvent.STATUS_CHOICES:
            self.stdout.write(f"{label:<11}{counts.get(key, 0)}")
        recent = LeadSyncEvent.objects.exclude(last_error="").order_by("-id")[:10]
        for ev in recent:
            self.stdout.write(f"  {ev.lead_reference_id} [{ev.status}] attempts={ev.attempts} {ev.last_error[:120]}")
