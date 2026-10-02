"""
CSV / Excel import engine.

Pipeline (each phase runs OUTSIDE the web request, streaming the file so a
500k-row sheet is never loaded into memory or the browser):

    upload -> map columns -> analyze (validate + detect duplicates)
           -> configure -> import in chunks (bulk_create / bulk_update)
           -> [review duplicates] -> report

Progress is written to the ImportBatch row after every chunk; the UI polls it.
"""
import csv
import datetime
import decimal
import logging
import re
import threading
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.db import connection, transaction
from django.utils import timezone

from . import services
from .models import (
    AssignmentHistory, Contact, ContactSegment, ImportBatch, ImportReviewRow, ImportRowError, normalize_phone,
    user_label,
)
from .settings_store import get_int

logger = logging.getLogger("crm.import")

# (key, label, required, aliases used for automatic column detection)
TARGET_FIELDS = [
    ("name", "Full Name", True, ["name", "full name", "customer name", "customer", "client name", "party name", "contact name", "applicant name"]),
    ("phone", "Contact No.", True, ["phone", "mobile", "mobile no", "mobile number", "contact", "contact no", "contact number", "phone number", "whatsapp", "cell", "mob"]),
    ("email", "Email", False, ["email", "e-mail", "email id", "mail", "email address"]),
    ("address", "Address", False, ["address", "full address", "home address", "residential address",
                                    "current address", "permanent address", "location address", "addr"]),
    ("city", "City", False, ["city", "town", "location", "district"]),
    ("work_profile", "Work Profile", False, ["work profile", "occupation", "profession", "job", "employment", "work"]),
    ("income", "Income", False, ["income", "salary", "monthly income", "annual income", "earning"]),
    ("requirement", "Requirement", False, ["requirement", "service", "product", "need", "loan type", "interested in"]),
    ("loan_amount", "Loan Amount", False, ["loan amount", "required loan", "loan", "amount", "loan required", "requirement amount"]),
    ("source", "Source", False, ["source", "lead source", "channel", "campaign"]),
    ("notes", "Notes", False, ["notes", "remarks", "comment", "comments", "remark"]),
    ("assigned_to", "Assigned To (staff name / email)", False, ["assigned to", "assigned", "telecaller", "agent", "caller"]),
    ("reference_by", "Reference By (staff name / email)", False, ["reference by", "reference", "referred by", "ref by"]),
]
FIELD_LABELS = {k: label for k, label, _, _ in TARGET_FIELDS}
MAXLEN = {"name": 200, "phone": 30, "email": 254, "address": 500, "city": 100, "work_profile": 200,
          "income": 100, "requirement": 200, "source": 100}

# Columns that get a *recognized, labeled* place in extra_data (rather than
# their raw spreadsheet header) when there's no dedicated model field for
# them. Anything not in this list is still preserved — just under its
# original spreadsheet column name (see suggest_mapping/parse_row below).
EXTRA_FIELD_ALIASES = {
    "State": ["state"],
    "Pincode": ["pincode", "pin code", "zip", "zipcode", "postal code", "pin"],
    "Company": ["company", "company name", "employer", "organisation", "organization"],
    "Gender": ["gender", "sex"],
    "DOB": ["dob", "date of birth", "birth date", "birthdate"],
    "PAN": ["pan", "pan number", "pan card"],
    "Aadhaar": ["aadhaar", "aadhar", "aadhaar number", "aadhar number", "aadhaar no", "uid"],
    "Designation": ["designation", "job title", "title"],
}
_EXTRA_ALIAS_LOOKUP = {alias: label for label, aliases in EXTRA_FIELD_ALIASES.items() for alias in aliases}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ALLOWED_EXT = {".csv", ".xlsx", ".xls"}


class ImportProblem(Exception):
    pass


# ------------------------------------------------------------------ reading files
def upload_dir():
    d = Path(getattr(settings, "IMPORT_UPLOAD_DIR", settings.BASE_DIR / "import_uploads"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    if isinstance(v, (datetime.datetime, datetime.date)):
        return v.isoformat()
    return str(v).strip()


def _csv_encoding(path):
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(path, encoding=enc, newline="") as fh:
                while fh.read(1 << 20):
                    pass
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def iter_rows(path, file_type):
    """Yields (row_number, [cells]) — row 1 is the header. Streams; O(1) memory."""
    if file_type == "csv":
        csv.field_size_limit(1 << 24)
        enc = _csv_encoding(path)
        with open(path, encoding=enc, newline="") as fh:
            sample = fh.read(8192)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            for n, row in enumerate(csv.reader(fh, dialect), start=1):
                yield n, [c.strip() for c in row]
    elif file_type == "xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            for n, row in enumerate(ws.iter_rows(values_only=True), start=1):
                yield n, [_cell(c) for c in row]
        finally:
            wb.close()
    elif file_type == "xls":
        try:
            import xlrd
        except ImportError:
            raise ImportProblem("Legacy .xls files need the optional 'xlrd' package. "
                                "Install it (pip install xlrd) or save the file as .xlsx / .csv.")
        book = xlrd.open_workbook(path, on_demand=True)
        sheet = book.sheet_by_index(0)
        for i in range(sheet.nrows):
            yield i + 1, [_cell(c) for c in sheet.row_values(i)]
        book.release_resources()
    else:
        raise ImportProblem("Unsupported file type.")


def read_preview(path, file_type, limit=6):
    """Header + first rows, without touching the rest of the file."""
    header, rows = None, []
    for n, row in iter_rows(path, file_type):
        if header is None:
            header = row
            continue
        rows.append((n, row))
        if len(rows) >= limit:
            break
    if not header or not any(header):
        raise ImportProblem("The file has no header row.")
    return header, rows


def suggest_mapping(headers):
    """Best-effort auto-detection: exact alias match first, then contains."""
    norm = [re.sub(r"[^a-z0-9 ]", " ", h.lower()).strip() for h in headers]
    used, mapping = set(), {}
    for key, _, _, aliases in TARGET_FIELDS:
        found = None
        for i, h in enumerate(norm):
            if i not in used and h in aliases:
                found = i
                break
        if found is None:
            for i, h in enumerate(norm):
                if i not in used and h and any(len(a) > 3 and a in h for a in aliases):
                    found = i
                    break
        if found is not None:
            mapping[key] = found
            used.add(found)
    return mapping


def classify_headers(headers, mapping):
    """
    For the mapping-review UI: for every spreadsheet column, says whether it
    maps to a CRM field, a recognized "Additional Information" label, or will
    be preserved under its own original column name. Nothing is ever dropped.
    """
    mapped_idx = {v: k for k, v in mapping.items()}
    rows = []
    for i, header in enumerate(headers or []):
        if i in mapped_idx:
            rows.append({"header": header, "kind": "field", "target": FIELD_LABELS[mapped_idx[i]]})
            continue
        norm = re.sub(r"[^a-z0-9 ]", " ", (header or "").lower()).strip()
        label = _EXTRA_ALIAS_LOOKUP.get(norm)
        if label:
            rows.append({"header": header, "kind": "extra_known", "target": label})
        elif (header or "").strip():
            rows.append({"header": header, "kind": "extra_raw", "target": header.strip()})
        else:
            rows.append({"header": header, "kind": "blank", "target": ""})
    return rows


def new_batch_code():
    now = timezone.localtime()
    prefix = f"KM-{now.strftime('%b').upper()}-{now.year}-"
    seq = ImportBatch.objects.filter(code__startswith=prefix).count() + 1
    while ImportBatch.objects.filter(code=f"{prefix}{seq:03d}").exists():
        seq += 1
    return f"{prefix}{seq:03d}"


# ------------------------------------------------------------------ row parsing
class StaffResolver:
    def __init__(self):
        self.by_key = {}
        for u in User.objects.select_related("staff_profile"):
            for key in (u.username, u.email, u.get_full_name()):
                if key:
                    self.by_key[key.strip().lower()] = u

    def find(self, raw):
        return self.by_key.get((raw or "").strip().lower())


def build_extra_data(cells, mapping, headers):
    """
    Everything NOT claimed by a TARGET_FIELDS column index is preserved here
    instead of being discarded. Recognized-but-fieldless columns (State,
    Pincode, Company, Gender, DOB, PAN, Aadhaar, Designation, ...) get their
    clean label; anything else keeps its original spreadsheet header as the
    key. Blank cells are skipped so extra_data doesn't fill up with clutter.
    """
    mapped_idx = set(mapping.values())
    extra = {}
    for i, header in enumerate(headers or []):
        if i in mapped_idx or i >= len(cells):
            continue
        val = (cells[i] or "").strip()
        if not val or not (header or "").strip():
            continue
        norm = re.sub(r"[^a-z0-9 ]", " ", header.lower()).strip()
        key = _EXTRA_ALIAS_LOOKUP.get(norm, header.strip())
        # Don't silently overwrite an earlier column that mapped to the same label
        if key in extra:
            key = header.strip()
        extra[key[:100]] = val[:500]
    return extra


def parse_row(cells, mapping, resolver, headers=None):
    """
    Returns (data, errors). errors = [(field, problem, reason)]. Never raises.
    `mapping` = {target_key: column_index}. `headers` (optional) lets any
    column NOT claimed by mapping be preserved in data["extra_data"] instead
    of being thrown away.
    """
    def get(key):
        idx = mapping.get(key)
        if idx is None or idx >= len(cells):
            return ""
        return (cells[idx] or "").strip()

    data, errors = {}, []
    for key in ("name", "phone", "email", "address", "city", "work_profile", "income", "requirement", "source", "notes"):
        val = get(key)
        if key in MAXLEN:
            val = val[:MAXLEN[key]]
        data[key] = val
    data["extra_data"] = build_extra_data(cells, mapping, headers) if headers is not None else {}
    if not data["name"]:
        errors.append(("name", "Missing name", "Full Name is required"))
    digits = re.sub(r"\D", "", data["phone"])
    if not data["phone"]:
        errors.append(("phone", "Missing phone number", "Contact No. is required"))
    elif len(digits) < 7:
        errors.append(("phone", "Invalid phone number", f"'{data['phone']}' has fewer than 7 digits"))
    elif len(digits) > 15:
        errors.append(("phone", "Invalid phone number", f"'{data['phone'][:30]}' has more than 15 digits"))
    if data["email"]:
        data["email"] = data["email"].lower()
        if not EMAIL_RE.match(data["email"]):
            errors.append(("email", "Invalid email", f"'{data['email'][:60]}' is not a valid email address"))
    raw_amt = get("loan_amount")
    data["loan_amount"] = None
    if raw_amt:
        cleaned = re.sub(r"[₹,\s]|rs\.?|inr", "", raw_amt.lower())
        try:
            amt = decimal.Decimal(cleaned)
            if amt < 0 or amt >= decimal.Decimal("1e10"):
                raise decimal.InvalidOperation
            data["loan_amount"] = str(amt)
        except decimal.InvalidOperation:
            errors.append(("loan_amount", "Invalid loan amount", f"'{raw_amt[:40]}' is not a valid number"))
    for key in ("assigned_to", "reference_by"):
        raw = get(key)
        data[key] = None
        if raw:
            user = resolver.find(raw)
            if user is None:
                errors.append((key, "Unknown staff member", f"No staff named '{raw[:60]}'"))
            else:
                data[key] = user.pk
    data["phone_normalized"] = normalize_phone(data["phone"])
    return data, errors


# ------------------------------------------------------------------ helpers for phases
def _beat(batch, **fields):
    batch.heartbeat = timezone.now()
    for k, v in fields.items():
        setattr(batch, k, v)
    batch.save(update_fields=["heartbeat", *fields.keys()])


def _existing_map(phones, emails):
    """{'p:<phone>': pk, 'e:<email>': pk} for contacts already in the CRM."""
    found = {}
    phones, emails = list(phones), list(emails)
    for i in range(0, len(phones), 500):
        for pk, ph in Contact.objects.filter(phone_normalized__in=phones[i:i + 500]).order_by("-id").values_list("pk", "phone_normalized"):
            found.setdefault(f"p:{ph}", pk)
    for i in range(0, len(emails), 500):
        for pk, em in Contact.objects.filter(email__in=emails[i:i + 500]).order_by("-id").values_list("pk", "email"):
            found.setdefault(f"e:{em}", pk)
    return found


def _dup_key(data, found, chunk_first):
    """Match on normalized phone first, then email. Returns ('db'|'chunk', ref) or None."""
    for prefix, val in (("p", data["phone_normalized"]), ("e", data["email"])):
        if not val:
            continue
        k = f"{prefix}:{val}"
        if k in found:
            return "db", found[k]
        if k in chunk_first:
            return "chunk", chunk_first[k]
    return None


def chunked_rows(batch, size, skip=0):
    """Yields lists of (row_number, cells), skipping the header and `skip` data rows."""
    buf, seen = [], 0
    for n, cells in iter_rows(batch.stored_path, batch.file_type):
        if n == 1:
            continue
        if not any(cells):  # fully blank spreadsheet row
            continue
        seen += 1
        if seen <= skip:
            continue
        buf.append((n, cells))
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf


def _count_data_rows(batch):
    return sum(1 for n, cells in iter_rows(batch.stored_path, batch.file_type) if n > 1 and any(cells))


# ------------------------------------------------------------------ PHASE 1: analyze
def analyze_batch(batch_id):
    batch = ImportBatch.objects.get(pk=batch_id)
    resolver = StaffResolver()
    size = get_int("import_chunk_size", 2000)
    batch.row_errors.filter(stage="analyze").delete()
    _beat(batch, status=ImportBatch.STATUS_ANALYZING, processed_rows=0, error_message="",
          total_rows=_count_data_rows(batch))
    new = dup = invalid = 0
    seen = set()
    for chunk in chunked_rows(batch, size):
        errs, valid = [], []
        for n, cells in chunk:
            data, errors = parse_row(cells, batch.mapping, resolver, headers=batch.headers)
            if errors:
                invalid += 1
                for field, problem, reason in errors:
                    errs.append(ImportRowError(batch=batch, row_number=n, field=field, problem=problem,
                                               reason=reason, raw=cells[:60], stage="analyze"))
            else:
                valid.append((n, data))
        found = _existing_map({d["phone_normalized"] for _, d in valid}, {d["email"] for _, d in valid if d["email"]})
        for _, d in valid:
            is_dup = False
            for prefix, val in (("p", d["phone_normalized"]), ("e", d["email"])):
                if val and (f"{prefix}:{val}" in found or f"{prefix}:{val}" in seen):
                    is_dup = True
            for prefix, val in (("p", d["phone_normalized"]), ("e", d["email"])):
                if val:
                    seen.add(f"{prefix}:{val}")
            if is_dup:
                dup += 1
            else:
                new += 1
        ImportRowError.objects.bulk_create(errs, batch_size=1000)
        _beat(batch, processed_rows=batch.processed_rows + len(chunk))
    _beat(batch, status=ImportBatch.STATUS_ANALYZED, analysis_new=new, analysis_duplicates=dup, analysis_invalid=invalid,
          error_count=invalid, processed_rows=batch.total_rows)


# ------------------------------------------------------------------ PHASE 2: import
def _contact_from(data, batch, assign_user_id):
    assigned = data.get("assigned_to") or assign_user_id
    return Contact(
        name=data["name"], phone=data["phone"], phone_normalized=data["phone_normalized"],
        email=data["email"], address=data.get("address", ""), city=data["city"],
        work_profile=data["work_profile"], income=data["income"],
        requirement=data["requirement"], loan_amount=data["loan_amount"], notes=data["notes"],
        source=data["source"] or batch.source_label or Path(batch.file_name).stem[:100],
        import_batch=batch, created_by=batch.uploaded_by,
        reference_by_id=data.get("reference_by"),
        current_assigned_to_id=assigned, original_assigned_to_id=assigned,
        extra_data=data.get("extra_data") or {},
    )


UPDATE_FIELDS = ["name", "phone", "phone_normalized", "email", "address", "city", "work_profile", "income",
                 "requirement", "loan_amount", "notes", "extra_data", "updated_at"]


def _merge(contact, data):
    """Copies non-empty incoming values onto an existing contact (never blanks anything out)."""
    for f in ("name", "phone", "email", "address", "city", "work_profile", "income", "requirement", "notes"):
        if data.get(f):
            setattr(contact, f, data[f])
    if data.get("loan_amount") not in (None, ""):
        contact.loan_amount = data["loan_amount"]
    if data.get("extra_data"):
        # merge, never drop previously-stored extra fields
        merged = dict(contact.extra_data or {})
        merged.update(data["extra_data"])
        contact.extra_data = merged
    contact.phone_normalized = normalize_phone(contact.phone)
    contact.updated_at = timezone.now()


def _add_to_segment(contact_ids, batch):
    """
    Adds contacts to batch.add_to_segment, if the admin chose one.

    Documented behavior: new contacts and "import as new" duplicates join the
    segment; duplicates merged via the Update policy also join (the imported
    row still represents that contact); duplicates that were Skipped, or left
    Skipped/undecided in the Review screen, never join. bulk_create with
    ignore_conflicts=True is what actually prevents duplicate membership rows
    — safe to call with contacts already in the segment.
    """
    if not batch.add_to_segment_id or not contact_ids:
        return
    ContactSegment.objects.bulk_create(
        [ContactSegment(contact_id=pk, segment_id=batch.add_to_segment_id, added_by=batch.uploaded_by) for pk in contact_ids],
        batch_size=1000, ignore_conflicts=True,
    )


def _assignment_rows(contacts, batch):
    return [
        AssignmentHistory(
            contact=c, action=AssignmentHistory.ACTION_ASSIGN, to_user_id=c.current_assigned_to_id,
            to_name=user_label(User.objects.filter(pk=c.current_assigned_to_id).first()),
            changed_by=batch.uploaded_by, changed_by_name=user_label(batch.uploaded_by),
            reason=f"Import {batch.code}",
        ) for c in contacts if c.current_assigned_to_id
    ]


def import_batch(batch_id):
    batch = ImportBatch.objects.get(pk=batch_id)
    resolver = StaffResolver()
    size = get_int("import_chunk_size", 2000)
    policy = batch.duplicate_policy
    assign_id = batch.assign_to_on_import_id
    resume = batch.status == ImportBatch.STATUS_IMPORTING and batch.processed_rows > 0
    if not resume:
        batch.review_rows.all().delete()
        _beat(batch, status=ImportBatch.STATUS_IMPORTING, processed_rows=0, imported_count=0, updated_count=0,
              skipped_count=0, duplicate_count=0, error_count=batch.analysis_invalid, error_message="")
    else:
        _beat(batch, status=ImportBatch.STATUS_IMPORTING)
    name_cache = {}

    for chunk in chunked_rows(batch, size, skip=batch.processed_rows):
        with transaction.atomic():
            valid = []
            for n, cells in chunk:
                data, errors = parse_row(cells, batch.mapping, resolver, headers=batch.headers)
                if not errors:
                    valid.append((n, data))
            found = _existing_map({d["phone_normalized"] for _, d in valid}, {d["email"] for _, d in valid if d["email"]})
            chunk_first, new_rows, dup_rows = {}, [], []
            for n, d in valid:
                hit = _dup_key(d, found, chunk_first)
                if hit and policy != ImportBatch.DUP_NEW:
                    dup_rows.append((n, d, hit))
                    continue
                if hit:  # policy = import as new: still a duplicate for reporting
                    dup_rows.append((n, d, None))
                idx = len(new_rows)
                new_rows.append((n, d))
                for prefix, val in (("p", d["phone_normalized"]), ("e", d["email"])):
                    if val:
                        chunk_first.setdefault(f"{prefix}:{val}", idx)
            contacts = [_contact_from(d, batch, assign_id) for _, d in new_rows]
            Contact.objects.bulk_create(contacts, batch_size=1000)
            if contacts and contacts[0].pk is None:  # DB without RETURNING support
                by_phone = {c.phone_normalized: c.pk for c in Contact.objects.filter(import_batch=batch)}
                for c in contacts:
                    c.pk = by_phone.get(c.phone_normalized)
            AssignmentHistory.objects.bulk_create(_assignment_rows(contacts, batch), batch_size=1000)

            skipped = updated = duplicates = 0
            updates, reviews = {}, []
            for n, d, hit in dup_rows:
                duplicates += 1
                if hit is None:
                    continue  # imported as new (already created above)
                kind, ref = hit
                pk = ref if kind == "db" else contacts[ref].pk
                if policy == ImportBatch.DUP_SKIP:
                    skipped += 1
                elif policy == ImportBatch.DUP_UPDATE:
                    updates.setdefault(pk, []).append(d)
                elif policy == ImportBatch.DUP_REVIEW:
                    reviews.append(ImportReviewRow(batch=batch, row_number=n, existing_contact_id=pk, data=d))
            if updates:
                objs = list(Contact.objects.filter(pk__in=updates.keys()))
                for c in objs:
                    for d in updates[c.pk]:
                        _merge(c, d)
                Contact.objects.bulk_update(objs, UPDATE_FIELDS, batch_size=500)
                updated = sum(len(v) for v in updates.values())
            ImportReviewRow.objects.bulk_create(reviews, batch_size=1000)

            # New contacts + duplicates merged via "Update" join the chosen segment;
            # Skipped duplicates never do (see _add_to_segment docstring).
            _add_to_segment([c.pk for c in contacts] + list(updates.keys()), batch)

            batch.imported_count += len(contacts)
            batch.updated_count += updated
            batch.skipped_count += skipped
            batch.duplicate_count += duplicates
            batch.processed_rows += len(chunk)
            batch.heartbeat = timezone.now()
            batch.save(update_fields=["imported_count", "updated_count", "skipped_count", "duplicate_count",
                                      "processed_rows", "heartbeat"])
    _finish_import(batch)


def _finish_import(batch):
    pending_review = batch.review_rows.filter(applied=False).exists()
    batch.status = ImportBatch.STATUS_REVIEW if pending_review else ImportBatch.STATUS_COMPLETED
    batch.completed_at = None if pending_review else timezone.now()
    batch.processed_rows = batch.total_rows
    _beat(batch, status=batch.status, completed_at=batch.completed_at, processed_rows=batch.total_rows)
    if not pending_review:
        services.log_audit(
            batch.uploaded_by, "import_completed",
            f"Import {batch.code}: {batch.imported_count:,} imported, {batch.updated_count:,} updated, "
            f"{batch.duplicate_count:,} duplicates, {batch.error_count:,} errors",
            {"batch": batch.code, "imported": batch.imported_count, "duplicates": batch.duplicate_count,
             "errors": batch.error_count}, "import", batch.pk)


# ------------------------------------------------------------------ PHASE 3: apply review decisions
def apply_review(batch_id, finish=False):
    batch = ImportBatch.objects.get(pk=batch_id)
    _beat(batch, status=ImportBatch.STATUS_IMPORTING)
    qs = batch.review_rows.filter(applied=False).exclude(decision="")
    done_ids = []
    while True:
        rows = list(qs.exclude(pk__in=done_ids).select_related("existing_contact")[:1000])
        if not rows:
            break
        with transaction.atomic():
            updates, creates = {}, []
            orphaned_skips = 0
            for r in rows:
                if r.decision == ImportReviewRow.DECISION_UPDATE:
                    if r.existing_contact_id is None:
                        # The contact this row was matched against has since been deleted, so
                        # there is nothing to update. Skip it explicitly (counted below) rather
                        # than crash or silently create a record the admin didn't choose.
                        orphaned_skips += 1
                        continue
                    updates.setdefault(r.existing_contact_id, (r.existing_contact, []))[1].append(r.data)
                elif r.decision == ImportReviewRow.DECISION_NEW:
                    creates.append(_contact_from(r.data, batch, batch.assign_to_on_import_id))
            for c, datas in updates.values():
                for d in datas:
                    _merge(c, d)
            Contact.objects.bulk_update([c for c, _ in updates.values()], UPDATE_FIELDS, batch_size=500)
            Contact.objects.bulk_create(creates, batch_size=1000)
            AssignmentHistory.objects.bulk_create(_assignment_rows(creates, batch), batch_size=1000)
            _add_to_segment([c.pk for c, _ in updates.values()] + [c.pk for c in creates], batch)
            batch.updated_count += len(updates)
            batch.imported_count += len(creates)
            batch.skipped_count += sum(1 for r in rows if r.decision == ImportReviewRow.DECISION_SKIP) + orphaned_skips
            batch.save(update_fields=["updated_count", "imported_count", "skipped_count"])
            ImportReviewRow.objects.filter(pk__in=[r.pk for r in rows]).update(applied=True)
        done_ids.extend(r.pk for r in rows)
    if finish:  # undecided rows are skipped
        n = batch.review_rows.filter(applied=False).update(applied=True, decision=ImportReviewRow.DECISION_SKIP)
        batch.skipped_count += n
        batch.save(update_fields=["skipped_count"])
    _finish_import(batch)


# ------------------------------------------------------------------ runner
def _run(batch_id, phase, close=True, **kw):
    try:
        {"analyze": analyze_batch, "import": import_batch, "review": apply_review}[phase](batch_id, **kw)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Import %s failed in phase %s", batch_id, phase)
        batch = ImportBatch.objects.filter(pk=batch_id).first()
        if batch:
            batch.status = ImportBatch.STATUS_FAILED
            batch.error_message = str(exc)[:1000]
            batch.save(update_fields=["status", "error_message"])
            services.log_audit(batch.uploaded_by, "import_failed", f"Import {batch.code} failed: {str(exc)[:200]}",
                               {"batch": batch.code, "phase": phase}, "import", batch.pk)
    finally:
        if close:
            connection.close()


def start(batch_id, phase, **kw):
    """Runs a phase in a daemon thread (or inline when CRM_JOBS_INLINE, e.g. tests)."""
    if getattr(settings, "CRM_JOBS_INLINE", False):
        _run(batch_id, phase, close=False, **kw)  # inline: keep the caller's connection open
        return
    threading.Thread(target=_run, args=(batch_id, phase), kwargs=kw, daemon=True).start()
