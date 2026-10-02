"""
Data-only migration (no schema change): re-derive the stored status of FINISHED attendance days from their
stored worked_duration using the corrected thresholds (full day >= expected - tolerance, e.g. 6h30), so
old ~7h days that were wrongly saved as "half_day" now read PRESENT.

Safe by design: nothing is deleted; start/end/duration are untouched; only `attendance_status` of rows whose
derived value differs is updated; rows an admin has hand-corrected (a status/start/end correction exists in
the audit table) are left exactly as the admin set them. Not reversible (the old, wrong value is not kept).
"""
from django.conf import settings
from django.db import migrations


def reclassify(apps, schema_editor):
    Attendance = apps.get_model("crm", "Attendance")
    Correction = apps.get_model("crm", "AttendanceCorrection")
    full_min = settings.ATTENDANCE_FULL_DAY_MIN_MINUTES
    half_min = settings.ATTENDANCE_HALF_DAY_MIN_MINUTES
    corrected = set(Correction.objects.filter(attendance__isnull=False,
                    field__in=["attendance_status", "start_time", "end_time"]).values_list("attendance_id", flat=True))
    qs = Attendance.objects.filter(end_time__isnull=False, worked_duration__isnull=False)
    for rec in qs.only("id", "worked_duration", "attendance_status").iterator():
        if rec.id in corrected:
            continue
        minutes = rec.worked_duration.total_seconds() / 60
        new = "full_day" if minutes >= full_min else ("half_day" if minutes >= half_min else "short_day")
        if new != rec.attendance_status:
            Attendance.objects.filter(pk=rec.pk).update(attendance_status=new)


class Migration(migrations.Migration):

    dependencies = [("crm", "0017_staff_activity_monitoring")]

    operations = [migrations.RunPython(reclassify, migrations.RunPython.noop)]
