"""
Staff Activity live assigned-call channel (additive only - no data is changed or removed):
  * new table StaffLiveCallActivity (temporary live-call state; NOT call history)
  * StaffInactivityPeriod.device_notified_at (one Android warning per inactivity period)
  * StaffInactivityPeriod.ended_by gains the "live_call" choice (choices only, no column change)
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("crm", "0018_reclassify_attendance_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="staffinactivityperiod",
            name="device_notified_at",
            field=models.DateTimeField(blank=True, null=True, help_text="When the Android app acknowledged showing the 15-minute warning for this period (one warning per period)."),
        ),
        migrations.AlterField(
            model_name="staffinactivityperiod",
            name="ended_by",
            field=models.CharField(blank=True, max_length=10, choices=[("activity", "CRM activity resumed"), ("lunch", "Lunch started"), ("day_ended", "Day ended"), ("call", "Covered by a synced call"), ("live_call", "Assigned customer call")]),
        ),
        migrations.CreateModel(
            name="StaffLiveCallActivity",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("session_id", models.CharField(max_length=64)),
                ("call_state", models.CharField(choices=[("started", "Started / ringing"), ("active", "Connected"), ("ended", "Ended")], default="started", max_length=8)),
                ("is_qualifying", models.BooleanField(default=False, help_text="Number belongs to a lead/contact assigned to THIS staff member (decided server-side).")),
                ("started_at", models.DateTimeField()),
                ("connected_at", models.DateTimeField(blank=True, null=True)),
                ("last_seen_at", models.DateTimeField()),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("device", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="live_call_sessions", to="crm.calldevice")),
                ("matched_contact", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="crm.contact")),
                ("matched_lead", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="crm.lead")),
                ("staff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="live_call_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "indexes": [models.Index(fields=["staff", "ended_at", "last_seen_at"], name="livecall_staff_open_idx")],
                "constraints": [models.UniqueConstraint(fields=("staff", "session_id"), name="uniq_livecall_staff_session")],
            },
        ),
    ]
