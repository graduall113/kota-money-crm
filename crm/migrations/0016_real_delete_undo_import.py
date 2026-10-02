"""
Real-deletion support. Additive / non-destructive only:
  * ImportBatch gets four history counters (default 0) recording the outcome of an Undo.
  * ImportReviewRow.existing_contact: CASCADE -> SET_NULL, now nullable, so deleting a contact
    can never silently erase another import's parked duplicate-review row.
No data is deleted or rewritten by this migration.
"""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("crm", "0015_lead_sync_queue"),
    ]

    operations = [
        migrations.AddField(model_name="importbatch", name="undo_selected_count",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="importbatch", name="undo_deleted_count",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="importbatch", name="undo_protected_count",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AddField(model_name="importbatch", name="undo_leads_detached",
                            field=models.PositiveIntegerField(default=0)),
        migrations.AlterField(
            model_name="importreviewrow", name="existing_contact",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="+", to="crm.contact"),
        ),
    ]
