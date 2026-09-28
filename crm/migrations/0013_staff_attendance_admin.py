import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Staff Attendance admin dashboard (Feature 6): correction audit table + attendance indexes. Purely additive."""

    dependencies = [
        ("crm", "0012_attendance_verification"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AttendanceCorrection",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("staff_name", models.CharField(blank=True, max_length=150)),
                ("work_date", models.DateField()),
                ("admin_name", models.CharField(blank=True, max_length=150)),
                ("field", models.CharField(
                    choices=[("start_time", "Start time"), ("end_time", "End time"),
                             ("attendance_status", "Status"), ("auto_ended", "Auto ended")],
                    max_length=20,
                )),
                ("old_value", models.CharField(blank=True, max_length=60)),
                ("new_value", models.CharField(blank=True, max_length=60)),
                ("reason", models.CharField(max_length=500)),
                ("created_at", models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ("admin", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
                ("attendance", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="corrections", to="crm.attendance",
                )),
                ("staff", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"ordering": ["-created_at", "-id"]},
        ),
        migrations.AddIndex(
            model_name="attendancecorrection",
            index=models.Index(fields=["attendance", "created_at"], name="attcorr_att_idx"),
        ),
        migrations.AddIndex(
            model_name="attendancecorrection",
            index=models.Index(fields=["staff", "work_date"], name="attcorr_staff_date_idx"),
        ),
        migrations.AddIndex(
            model_name="attendance",
            index=models.Index(fields=["work_date", "attendance_status"], name="att_date_status_idx"),
        ),
        migrations.AddIndex(
            model_name="attendance",
            index=models.Index(fields=["attendance_status"], name="att_status_idx"),
        ),
    ]
