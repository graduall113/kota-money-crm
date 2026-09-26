"""CSV / Excel export of an ALREADY-AUTHORIZED queryset (streamed, formula-injection safe)."""
import csv
import io

from django.http import HttpResponse, StreamingHttpResponse
from django.utils import timezone

from .models import user_label

EXCEL_ROW_CAP = 200_000


def _safe(v):
    """Neutralise spreadsheet formula injection (=, +, -, @ prefixes) in exported cells."""
    if v is None:
        return ""
    if not isinstance(v, str):
        return v
    return "'" + v if v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


class _Echo:
    def write(self, value):
        return value


def lead_columns():
    return [
        ("Lead ID", lambda o: o.display_id), ("Name", lambda o: o.customer_name), ("Phone", lambda o: o.contact_number),
        ("Email", lambda o: o.email), ("City", lambda o: o.city), ("Work Profile", lambda o: o.work_profile),
        ("Income", lambda o: o.income), ("Requirement", lambda o: o.requirement),
        ("Loan Amount", lambda o: str(o.loan_amount) if o.loan_amount is not None else ""),
        ("Status", lambda o: o.get_status_display()), ("Source", lambda o: o.source),
        ("Reference By", lambda o: user_label(o.reference_by)), ("Assigned To", lambda o: user_label(o.assigned_to)),
        ("Next Follow-up", lambda o: o.next_followup_date.isoformat() if o.next_followup_date else ""),
        ("Created", lambda o: timezone.localtime(o.created_at).strftime("%Y-%m-%d %H:%M")),
    ]


def contact_columns():
    return [
        ("Contact ID", lambda o: o.pk), ("Name", lambda o: o.name), ("Phone", lambda o: o.phone),
        ("Email", lambda o: o.email), ("City", lambda o: o.city), ("Work Profile", lambda o: o.work_profile),
        ("Income", lambda o: o.income), ("Source", lambda o: o.source), ("Status", lambda o: o.get_status_display()),
        ("Import Batch", lambda o: o.import_batch.code if o.import_batch_id else ""),
        ("Reference By", lambda o: user_label(o.reference_by)),
        ("Assigned To", lambda o: user_label(o.current_assigned_to)),
        ("Created", lambda o: timezone.localtime(o.created_at).strftime("%Y-%m-%d %H:%M")),
    ]


def export_queryset(qs, columns, fmt, filename):
    qs = qs.iterator(chunk_size=2000)
    if fmt == "xlsx":
        import openpyxl

        wb = openpyxl.Workbook(write_only=True)
        ws = wb.create_sheet("Export")
        ws.append([c for c, _ in columns])
        for i, obj in enumerate(qs):
            if i >= EXCEL_ROW_CAP:
                break
            ws.append([_safe(fn(obj)) for _, fn in columns])
        buf = io.BytesIO()
        wb.save(buf)
        resp = HttpResponse(buf.getvalue(), content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        resp["Content-Disposition"] = f'attachment; filename="{filename}.xlsx"'
        return resp

    writer = csv.writer(_Echo())

    def rows():
        yield "\ufeff" + writer.writerow([c for c, _ in columns])
        for obj in qs:
            yield writer.writerow([_safe(fn(obj)) for _, fn in columns])

    resp = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}.csv"'
    return resp
