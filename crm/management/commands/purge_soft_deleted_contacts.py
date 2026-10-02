"""
OPTIONAL clean-up for legacy rows only.

Before real deletion existed, "Undo Import" hid contacts with is_deleted=True and kept them in the
database. This command can permanently remove those legacy rows. It is never run automatically
(not by migrations, deploys or the app) and it is a DRY RUN unless --confirm is passed:

    python manage.py purge_soft_deleted_contacts                   # report only
    python manage.py purge_soft_deleted_contacts --batch 25        # report one batch only
    python manage.py purge_soft_deleted_contacts --confirm         # actually delete

Leads are never deleted: a converted Lead survives with Lead.contact set to NULL.
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from crm import services
from crm.models import Contact, Lead


class Command(BaseCommand):
    help = "Dry-run by default. Permanently deletes legacy soft-deleted contacts only with --confirm."

    def add_arguments(self, parser):
        parser.add_argument("--confirm", action="store_true", help="Actually delete. Without it nothing is changed.")
        parser.add_argument("--batch", type=int, help="Only contacts of this ImportBatch id.")

    def handle(self, *args, **opts):
        qs = Contact.objects.filter(is_deleted=True)
        if opts.get("batch"):
            qs = qs.filter(import_batch_id=opts["batch"])
        ids = list(qs.order_by("pk").values_list("pk", flat=True))
        with_leads = Lead.objects.filter(contact_id__in=ids).values("contact").distinct().count() if ids else 0
        self.stdout.write(f"{len(ids)} legacy soft-deleted contact(s) found; {with_leads} have a Lead (kept, detached).")
        if not opts["confirm"]:
            self.stdout.write(self.style.WARNING("DRY RUN — nothing was deleted. Re-run with --confirm to delete permanently."))
            return
        with transaction.atomic():
            result = services.delete_records(Contact, ids)
            services.log_audit(None, "purge_soft_deleted", f"Purged {result['deleted']:,} legacy soft-deleted contacts",
                               {"selected": result["selected"], "deleted": result["deleted"],
                                "protected": len(result["protected"]), "leads_kept_detached": result["leads_detached"],
                                "batch": opts.get("batch")}, "contact", "")
        self.stdout.write(self.style.SUCCESS(f"Permanently deleted {result['deleted']} contact(s); {len(result['protected'])} skipped."))
