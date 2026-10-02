import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Holiday management: one new table. Purely additive - no existing table is touched."""

    dependencies = [
        ("crm", "0013_staff_attendance_admin"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Holiday",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=150)),
                ("start_date", models.DateField()),
                ("end_date", models.DateField()),
                ("reason", models.CharField(blank=True, max_length=500)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name="+", to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={
                "ordering": ["start_date", "end_date", "id"],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("end_date__gte", models.F("start_date"))),
                        name="holiday_start_lte_end",
                        violation_error_message="The start date cannot be after the end date.",
                    ),
                    models.UniqueConstraint(
                        fields=("start_date", "end_date"),
                        name="uniq_holiday_period",
                        violation_error_message="A holiday for exactly these dates already exists. Edit that one (or re-activate it) instead of adding a duplicate.",
                    ),
                ],
            },
        ),
    ]
