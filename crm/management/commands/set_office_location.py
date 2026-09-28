from django.core.management.base import BaseCommand, CommandError

from crm.attendance_verify import _to_float
from crm.settings_store import set_setting


class Command(BaseCommand):
    help = (
        "Stores the office coordinates (and optionally the radius) in the admin settings table, "
        "so they live in the database, not in source code. Does NOT switch enforcement on - do that "
        "under Settings -> Attendance Verification. Example: "
        "python manage.py set_office_location --lat <latitude> --lng <longitude> --radius 200"
    )

    def add_arguments(self, parser):
        parser.add_argument("--lat", required=True)
        parser.add_argument("--lng", required=True)
        parser.add_argument("--radius", type=int, default=None, help="metres (30-2000)")

    def handle(self, *args, **opts):
        lat, lng = _to_float(opts["lat"], -90, 90), _to_float(opts["lng"], -180, 180)
        if lat is None or lng is None:
            raise CommandError("Latitude must be -90..90 and longitude -180..180.")
        if opts["radius"] is not None and not (30 <= opts["radius"] <= 2000):
            raise CommandError("Radius must be between 30 and 2000 metres.")
        set_setting("att_office_lat", f"{lat:.6f}")
        set_setting("att_office_lng", f"{lng:.6f}")
        if opts["radius"] is not None:
            set_setting("att_geofence_radius_m", opts["radius"])
        self.stdout.write(self.style.SUCCESS(f"Office location saved: {lat:.6f}, {lng:.6f}. Enforcement is still controlled in Settings."))
