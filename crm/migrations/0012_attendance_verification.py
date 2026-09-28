import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Attendance anti-fraud / office verification (Feature 5). Purely additive."""

    dependencies = [
        ("crm", "0011_attendance"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TrustedDevice",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("label", models.CharField(blank=True, max_length=100)),
                ("enrollment_code_hash", models.CharField(blank=True, db_index=True, max_length=64)),
                ("enrollment_expires", models.DateTimeField(blank=True, null=True)),
                ("token_hash", models.CharField(blank=True, max_length=64, null=True, unique=True)),
                ("enrolled_at", models.DateTimeField(blank=True, null=True)),
                ("is_active", models.BooleanField(default=True, help_text="Turn off (revoke) to stop this device counting for attendance.")),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("last_seen_at", models.DateTimeField(blank=True, null=True)),
                ("last_ip", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, help_text="Descriptive only; never used for verification.", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("revoked_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="trusted_devices", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [models.Index(fields=["user", "is_active"], name="trusteddev_user_active_idx")],
            },
        ),
        migrations.AddField(
            model_name="attendance",
            name="start_verification",
            field=models.CharField(choices=[("not_checked", "Not checked"), ("verified", "Verified"), ("admin_override", "Admin override")], default="not_checked", max_length=16),
        ),
        migrations.AddField(model_name="attendance", name="start_latitude", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="attendance", name="start_longitude", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(
            model_name="attendance", name="start_accuracy",
            field=models.FloatField(blank=True, help_text="GPS accuracy radius in metres, as reported by the device.", null=True),
        ),
        migrations.AddField(
            model_name="attendance", name="start_distance_from_office",
            field=models.FloatField(blank=True, help_text="Metres. Calculated by the server.", null=True),
        ),
        migrations.AddField(model_name="attendance", name="start_ip", field=models.GenericIPAddressField(blank=True, null=True)),
        migrations.AddField(
            model_name="attendance", name="start_device",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="start_attendances", to="crm.trusteddevice"),
        ),
        migrations.AddField(model_name="attendance", name="end_latitude", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="attendance", name="end_longitude", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="attendance", name="end_accuracy", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="attendance", name="end_distance_from_office", field=models.FloatField(blank=True, null=True)),
        migrations.AddField(model_name="attendance", name="end_ip", field=models.GenericIPAddressField(blank=True, null=True)),
        migrations.AddField(
            model_name="attendance", name="end_device",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="end_attendances", to="crm.trusteddevice"),
        ),
        migrations.AddField(
            model_name="attendance", name="override_by",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL),
        ),
        migrations.AddField(model_name="attendance", name="override_reason", field=models.CharField(blank=True, max_length=255)),
        migrations.CreateModel(
            name="AttendanceEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("user_name", models.CharField(blank=True, max_length=150)),
                ("action", models.CharField(choices=[("start", "Start Day"), ("end", "End Day"), ("enroll", "Device enrolment"), ("admin", "Admin")], max_length=8)),
                ("event_type", models.CharField(choices=[
                    ("verification_failed", "Verification Failed"), ("location_outside", "Location Outside Office"),
                    ("location_unavailable", "Location Unavailable"), ("poor_accuracy", "Poor GPS Accuracy"),
                    ("ip_not_allowed", "IP Not Allowed"), ("device_mismatch", "Device Mismatch"),
                    ("repeated_attempt", "Repeated Attempt"), ("duplicate_start", "Duplicate Start"),
                    ("duplicate_end", "Duplicate End"), ("suspicious_transition", "Unexpected State Transition"),
                    ("unexpected_input", "Unexpected Input Ignored"), ("admin_override", "Admin Override"),
                ], db_index=True, max_length=24)),
                ("outcome", models.CharField(choices=[("rejected", "Rejected"), ("flagged", "Flagged"), ("override", "Override")], max_length=10)),
                ("attempt_key", models.CharField(blank=True, db_index=True, help_text="Groups the events of one attempt.", max_length=32)),
                ("message", models.CharField(blank=True, max_length=300)),
                ("latitude", models.FloatField(blank=True, null=True)),
                ("longitude", models.FloatField(blank=True, null=True)),
                ("accuracy", models.FloatField(blank=True, null=True)),
                ("distance_from_office", models.FloatField(blank=True, null=True)),
                ("ip", models.GenericIPAddressField(blank=True, null=True)),
                ("details", models.JSONField(blank=True, default=dict)),
                ("review_status", models.CharField(choices=[("open", "Open"), ("reviewed", "Reviewed"), ("dismissed", "Dismissed")], db_index=True, default="open", max_length=10)),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_note", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("attendance", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="events", to="crm.attendance")),
                ("device", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="events", to="crm.trusteddevice")),
                ("reviewed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [models.Index(fields=["user", "action", "created_at"], name="attevent_user_action_idx")],
            },
        ),
    ]
