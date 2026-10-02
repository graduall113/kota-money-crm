"""
Staff CRM activity monitoring. Additive only (three new tables); nothing existing is altered.
  * StaffPresence          latest heartbeat / meaningful-activity / visibility facts per staff member
  * StaffInactivityPeriod  "CRM inactive for N minutes" records
  * LunchBreak             one lunch break per staff member per business day
"""
import django.db.models.deletion
import django.utils.timezone  # noqa: F401
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("crm", "0016_real_delete_undo_import"),
    ]

    operations = [
        migrations.CreateModel(
            name="StaffPresence",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("work_date", models.DateField(blank=True, help_text="Business date the counters below belong to.", null=True)),
                ("last_heartbeat_at", models.DateTimeField(blank=True, null=True)),
                ("last_activity_at", models.DateTimeField(blank=True, null=True)),
                ("reported_visibility", models.CharField(choices=[("visible", "Visible"), ("hidden", "Hidden")], default="visible", max_length=7)),
                ("visibility_changed_at", models.DateTimeField(blank=True, null=True)),
                ("session_hash", models.CharField(blank=True, help_text="Short hash of the session key (never the key).", max_length=16)),
                ("active_seconds", models.PositiveIntegerField(default=0)),
                ("credited_until", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="presence", to=settings.AUTH_USER_MODEL)),
            ],
            options={"verbose_name": "staff presence"},
        ),
        migrations.CreateModel(
            name="StaffInactivityPeriod",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("work_date", models.DateField(db_index=True)),
                ("last_activity_at", models.DateTimeField(help_text="Last meaningful CRM activity before the period.")),
                ("started_at", models.DateTimeField(help_text="last_activity_at + the inactivity threshold.")),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("ended_by", models.CharField(blank=True, choices=[("activity", "CRM activity resumed"), ("lunch", "Lunch started"), ("day_ended", "Day ended"), ("call", "Covered by a synced call")], max_length=10)),
                ("session_state", models.CharField(blank=True, help_text="CRM state when the period was recorded.", max_length=20)),
                ("visibility_state", models.CharField(blank=True, max_length=7)),
                ("call_overlap_seconds", models.PositiveIntegerField(default=0, help_text="Seconds covered by synced CallRecords.")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="inactivity_periods", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-started_at"],
                "indexes": [models.Index(fields=["user", "started_at"], name="inact_user_started_idx")],
            },
        ),
        migrations.AddConstraint(
            model_name="staffinactivityperiod",
            constraint=models.UniqueConstraint(condition=models.Q(("ended_at__isnull", True)), fields=("user",), name="uniq_open_inactivity_per_user"),
        ),
        migrations.AddConstraint(
            model_name="staffinactivityperiod",
            constraint=models.UniqueConstraint(fields=("user", "started_at"), name="uniq_inactivity_user_started"),
        ),
        migrations.CreateModel(
            name="LunchBreak",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("work_date", models.DateField(db_index=True)),
                ("started_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("auto_ended", models.BooleanField(default=False)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="lunch_breaks", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-started_at"]},
        ),
        migrations.AddConstraint(
            model_name="lunchbreak",
            constraint=models.UniqueConstraint(fields=("user", "work_date"), name="uniq_lunch_user_work_date"),
        ),
        migrations.AddConstraint(
            model_name="lunchbreak",
            constraint=models.CheckConstraint(check=models.Q(("ended_at__isnull", True), ("ended_at__gte", models.F("started_at")), _connector="OR"), name="lunch_end_not_before_start"),
        ),
    ]
