from django.core.management.base import BaseCommand

from crm import attendance


class Command(BaseCommand):
    help = (
        "Ends every still-open staff attendance record whose work-end time (default 19:00 "
        "Asia/Kolkata) has passed: end_time = official work end, auto_ended=True, duration + "
        "status calculated. Idempotent — schedule it as often as you like."
    )

    def handle(self, *args, **options):
        n = attendance.auto_end_overdue()
        self.stdout.write(self.style.SUCCESS(f"Auto-ended {n} attendance record(s)."))
