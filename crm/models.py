import hashlib
import re
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def normalize_phone(raw):
    """
    Canonical phone key used for duplicate detection. Keeps digits only,
    drops a leading 00 / country code 91 / trunk 0 for Indian numbers, and
    returns the last 10 digits when longer. Shorter numbers are returned as
    digits so landlines and bad data don't collapse into each other.
    """
    digits = re.sub(r"\D", "", str(raw or ""))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) > 10:
        digits = digits[-10:]
    return digits


def user_label(user):
    if not user:
        return ""
    return user.get_full_name() or user.username


class StaffProfile(models.Model):
    """
    One-to-one extension of Django's built-in User model.
    Holds the CRM-specific role/status that drive permissions —
    the User model itself stays untouched and standard.
    """

    ROLE_ADMIN = "admin"
    ROLE_STAFF = "staff"
    ROLE_CHOICES = [
        (ROLE_ADMIN, "Admin"),
        (ROLE_STAFF, "Staff"),
    ]

    STATUS_ACTIVE = "active"
    STATUS_INACTIVE = "inactive"
    STATUS_CHOICES = [
        (STATUS_ACTIVE, "Active"),
        (STATUS_INACTIVE, "Inactive"),
    ]

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="staff_profile",
    )
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default=ROLE_STAFF)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    phone = models.CharField(max_length=20, blank=True)
    reference_code = models.CharField(max_length=50, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["user__first_name", "user__username"]

    def __str__(self):
        return f"{self.user.get_full_name() or self.user.username} ({self.role})"

    @property
    def is_admin(self):
        return self.role == self.ROLE_ADMIN

    @property
    def is_account_active(self):
        return self.status == self.STATUS_ACTIVE

    @property
    def initials(self):
        name = self.user.get_full_name() or self.user.username
        parts = [p for p in name.split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][0].upper()
        return (parts[0][0] + parts[-1][0]).upper()


class Lead(models.Model):
    """
    'reference_by' and 'assigned_to' are intentionally separate FKs:
    reference_by = who referred / generated the lead
    assigned_to  = who is responsible for handling it
    They must never be merged or treated as the same thing.
    SET_NULL keeps historical leads intact if a staff account is removed.
    """

    STATUS_NEW = "new"
    STATUS_DOCS_PENDING = "docs_pending"
    STATUS_DOCS_COMPLETE = "docs_complete"
    STATUS_MEETING_REQUESTED = "meeting_requested"
    STATUS_PROCESSING = "processing"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_DOCS_PENDING, "Documents Pending"),
        (STATUS_DOCS_COMPLETE, "Documents Complete"),
        (STATUS_MEETING_REQUESTED, "Meeting Requested"),
        (STATUS_PROCESSING, "Processing"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
    ]

    # Maps loose/human status text (as it might arrive from the n8n form)
    # to the canonical status codes above. Keys are lowercased+stripped
    # before lookup, so casing/spacing from the form doesn't matter.
    STATUS_ALIASES = {
        "new": STATUS_NEW,
        "documents pending": STATUS_DOCS_PENDING,
        "docs pending": STATUS_DOCS_PENDING,
        "document pending": STATUS_DOCS_PENDING,
        "documents complete": STATUS_DOCS_COMPLETE,
        "docs complete": STATUS_DOCS_COMPLETE,
        "document complete": STATUS_DOCS_COMPLETE,
        "meeting requested": STATUS_MEETING_REQUESTED,
        "meeting request": STATUS_MEETING_REQUESTED,
        "processing": STATUS_PROCESSING,
        "in process": STATUS_PROCESSING,
        "approved": STATUS_APPROVED,
        "approve": STATUS_APPROVED,
        "rejected": STATUS_REJECTED,
        "reject": STATUS_REJECTED,
    }

    customer_name = models.CharField(max_length=200)
    contact_number = models.CharField(max_length=20)
    work_profile = models.CharField(max_length=200, blank=True)
    income = models.CharField(max_length=100, blank=True)
    requirement = models.CharField(max_length=200, blank=True)
    loan_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    bank_calling = models.CharField(max_length=200, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW)

    # DD/MM/YYYY "Date" field captured on the n8n form itself — kept
    # separate from created_at (which is when the Django row was made,
    # possibly moments later once n8n's automation forwards it here).
    form_date = models.DateField(null=True, blank=True)

    # Identifies a single n8n form submission so retried/duplicate
    # webhook deliveries don't create a second Lead. Populated from the
    # form's submittedAt (or a value derived from it) by the API view.
    external_submission_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, db_index=True
    )

    reference_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="referred_leads",
        help_text=(
            "Internal CRM ownership only (drives 'My Leads' + edit/delete "
            "permissions). Auto-set to the logged-in staff member who "
            "creates the lead — not shown on the Add/Edit Lead form."
        ),
    )
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_leads",
        help_text=(
            "Internal CRM staff assignment (optional, not on the n8n-matching "
            "form). Set manually elsewhere if/when needed; unrelated to the "
            "free-text 'Assigned To' field the original n8n form collects."
        ),
    )

    # ---------------------------------------------------------------
    # Free-text "Reference by" / "Assigned To" fields — these are the
    # actual visible fields on the original n8n Leads Form (field types:
    # plain text input, e.g. "KotaMoney"), and are what gets sent back
    # out to the n8n webhook. Deliberately separate from the reference_by
    # / assigned_to FKs above, which are internal CRM ownership plumbing
    # unrelated to what the n8n form itself collects.
    # ---------------------------------------------------------------
    reference_by_name = models.CharField(max_length=200, blank=True)
    assigned_to_name = models.CharField(max_length=200, blank=True)

    # ---------------------------------------------------------------
    # n8n outbound sync tracking (CRM → n8n webhook, see crm/n8n_integration.py)
    # ---------------------------------------------------------------
    N8N_SYNC_PENDING = "pending"
    N8N_SYNC_SUCCESS = "success"
    N8N_SYNC_FAILED = "failed"
    N8N_SYNC_CHOICES = [
        (N8N_SYNC_PENDING, "Pending"),
        (N8N_SYNC_SUCCESS, "Success"),
        (N8N_SYNC_FAILED, "Failed"),
    ]

    n8n_sync_status = models.CharField(
        max_length=10, choices=N8N_SYNC_CHOICES, default=N8N_SYNC_PENDING, blank=True
    )
    n8n_last_sync = models.DateTimeField(null=True, blank=True)
    n8n_error = models.TextField(blank=True)

    # ---------------------------------------------------------------
    # Added in the role-based-access / import upgrade
    # ---------------------------------------------------------------
    email = models.EmailField(blank=True, db_index=True)
    phone_normalized = models.CharField(max_length=20, blank=True, db_index=True)
    city = models.CharField(max_length=100, blank=True)
    source = models.CharField(max_length=100, blank=True, db_index=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="created_leads",
        help_text="Who physically created the row. Never changes.",
    )
    # First person the lead was ever assigned to. Written once, never edited.
    original_assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="originally_assigned_leads",
    )
    contact = models.ForeignKey(
        "Contact", on_delete=models.SET_NULL, null=True, blank=True, related_name="leads",
    )
    import_batch = models.ForeignKey(
        "ImportBatch", on_delete=models.SET_NULL, null=True, blank=True, related_name="leads",
    )

    INTEREST_UNKNOWN = ""
    INTEREST_INTERESTED = "interested"
    INTEREST_NOT_INTERESTED = "not_interested"
    INTEREST_CHOICES = [
        (INTEREST_UNKNOWN, "Not recorded"),
        (INTEREST_INTERESTED, "Interested"),
        (INTEREST_NOT_INTERESTED, "Not interested"),
    ]
    interest = models.CharField(max_length=20, choices=INTEREST_CHOICES, blank=True, default="")

    FOLLOWUP_PENDING = "pending"
    FOLLOWUP_DONE = "done"
    FOLLOWUP_CHOICES = [(FOLLOWUP_PENDING, "Pending"), (FOLLOWUP_DONE, "Done")]
    next_followup_date = models.DateField(null=True, blank=True, db_index=True)
    next_followup_time = models.TimeField(null=True, blank=True)
    followup_notes = models.TextField(blank=True)
    followup_status = models.CharField(max_length=10, choices=FOLLOWUP_CHOICES, blank=True, default="")
    last_contacted = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["assigned_to", "status"], name="lead_assignee_status_idx"),
            models.Index(fields=["reference_by", "status"], name="lead_reference_status_idx"),
        ]

    def __str__(self):
        return self.customer_name

    @property
    def display_id(self):
        """Human Lead ID (KM-1058). The DB pk stays the real identifier —
        it is also what n8n / Google Sheets already receive as ``lead_id``."""
        return f"KM-{self.pk}"

    def save(self, *args, **kwargs):
        self.phone_normalized = normalize_phone(self.contact_number)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "contact_number" in update_fields:
            kwargs["update_fields"] = list(update_fields) + ["phone_normalized"]
        super().save(*args, **kwargs)


# =========================================================
# SETTINGS (admin-editable, replaces editing source code)
# =========================================================


class Setting(models.Model):
    key = models.CharField(max_length=80, unique=True)
    value = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    def __str__(self):
        return self.key


# =========================================================
# IMPORTS + CONTACTS
# =========================================================


class ImportBatch(models.Model):
    STATUS_UPLOADED = "uploaded"
    STATUS_MAPPED = "mapped"
    STATUS_ANALYZING = "analyzing"
    STATUS_ANALYZED = "analyzed"
    STATUS_IMPORTING = "importing"
    STATUS_REVIEW = "review"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_UPLOADED, "Uploaded"),
        (STATUS_MAPPED, "Columns mapped"),
        (STATUS_ANALYZING, "Validating"),
        (STATUS_ANALYZED, "Ready to import"),
        (STATUS_IMPORTING, "Importing"),
        (STATUS_REVIEW, "Duplicates need review"),
        (STATUS_COMPLETED, "Completed"),
        (STATUS_FAILED, "Failed"),
    ]

    DUP_SKIP = "skip"
    DUP_UPDATE = "update"
    DUP_NEW = "new"
    DUP_REVIEW = "review"
    DUP_CHOICES = [
        (DUP_SKIP, "Skip duplicates"),
        (DUP_UPDATE, "Update existing record"),
        (DUP_NEW, "Import as new record"),
        (DUP_REVIEW, "Review duplicates"),
    ]

    code = models.CharField(max_length=30, unique=True)
    file_name = models.CharField(max_length=255)
    stored_path = models.CharField(max_length=500, blank=True)
    file_type = models.CharField(max_length=10, blank=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="import_batches"
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_UPLOADED, db_index=True)
    headers = models.JSONField(default=list, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    source_label = models.CharField(max_length=100, blank=True)
    duplicate_policy = models.CharField(max_length=10, choices=DUP_CHOICES, default=DUP_SKIP)
    assign_to_on_import = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # Segment every successfully-imported (new or updated) contact from this
    # batch is added to, if the admin chose one on the import-settings step.
    add_to_segment = models.ForeignKey(
        "Segment", on_delete=models.SET_NULL, null=True, blank=True, related_name="import_batches"
    )

    # Progress / results
    total_rows = models.PositiveIntegerField(default=0)
    processed_rows = models.PositiveIntegerField(default=0)
    analysis_new = models.PositiveIntegerField(default=0)
    analysis_duplicates = models.PositiveIntegerField(default=0)
    analysis_invalid = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    updated_count = models.PositiveIntegerField(default=0)
    skipped_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    error_message = models.TextField(blank=True)
    heartbeat = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.code

    @property
    def progress_percent(self):
        if not self.total_rows:
            return 0
        return min(100, round(self.processed_rows * 100 / self.total_rows, 1))

    @property
    def is_busy(self):
        return self.status in (self.STATUS_ANALYZING, self.STATUS_IMPORTING)

    @property
    def is_stalled(self):
        """Busy but no heartbeat for 2 minutes — the worker died (server restart)."""
        return bool(self.is_busy and self.heartbeat and (timezone.now() - self.heartbeat).total_seconds() > 120)

    @property
    def assignment_status(self):
        total = self.contacts.count()
        if not total:
            return "—"
        assigned = self.contacts.exclude(current_assigned_to=None).count()
        if assigned == 0:
            return "Unassigned"
        if assigned == total:
            return "Fully assigned"
        return f"{assigned:,} / {total:,} assigned"


class ImportRowError(models.Model):
    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="row_errors")
    row_number = models.PositiveIntegerField()
    field = models.CharField(max_length=60, blank=True)
    problem = models.CharField(max_length=200)
    reason = models.CharField(max_length=300, blank=True)
    raw = models.JSONField(default=list, blank=True)
    stage = models.CharField(max_length=10, default="analyze")  # analyze | import

    class Meta:
        ordering = ["row_number", "id"]
        indexes = [models.Index(fields=["batch", "stage"], name="rowerr_batch_stage_idx")]


class ImportReviewRow(models.Model):
    """Duplicate rows parked for a human decision (policy = Review)."""

    DECISION_PENDING = ""
    DECISION_SKIP = "skip"
    DECISION_UPDATE = "update"
    DECISION_NEW = "new"
    DECISION_CHOICES = [
        (DECISION_PENDING, "Undecided"),
        (DECISION_SKIP, "Skip"),
        (DECISION_UPDATE, "Update existing"),
        (DECISION_NEW, "Import as new"),
    ]

    batch = models.ForeignKey(ImportBatch, on_delete=models.CASCADE, related_name="review_rows")
    row_number = models.PositiveIntegerField()
    existing_contact = models.ForeignKey("Contact", on_delete=models.CASCADE, related_name="+")
    data = models.JSONField(default=dict)
    decision = models.CharField(max_length=10, choices=DECISION_CHOICES, blank=True, default="")
    applied = models.BooleanField(default=False)

    class Meta:
        ordering = ["row_number"]
        indexes = [models.Index(fields=["batch", "decision"], name="review_batch_decision_idx")]


class Contact(models.Model):
    STATUS_NEW = "new"
    STATUS_CONTACTED = "contacted"
    STATUS_INTERESTED = "interested"
    STATUS_NOT_INTERESTED = "not_interested"
    STATUS_CALLBACK = "callback"
    STATUS_CONVERTED = "converted"
    STATUS_CHOICES = [
        (STATUS_NEW, "New"),
        (STATUS_CONTACTED, "Contacted"),
        (STATUS_INTERESTED, "Interested"),
        (STATUS_NOT_INTERESTED, "Not interested"),
        (STATUS_CALLBACK, "Call back"),
        (STATUS_CONVERTED, "Converted to lead"),
    ]

    name = models.CharField(max_length=200, db_index=True)
    phone = models.CharField(max_length=30, blank=True)
    phone_normalized = models.CharField(max_length=20, blank=True, db_index=True)
    email = models.EmailField(blank=True, db_index=True)
    address = models.CharField(max_length=500, blank=True)
    city = models.CharField(max_length=100, blank=True)
    work_profile = models.CharField(max_length=200, blank=True)
    income = models.CharField(max_length=100, blank=True)
    requirement = models.CharField(max_length=200, blank=True)
    loan_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    source = models.CharField(max_length=100, blank=True, db_index=True)
    notes = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_NEW, db_index=True)
    # Any imported spreadsheet column that has no dedicated field above is kept
    # here as {"Original Column Name": "value"} — nothing from an import is
    # ever discarded. Works the same on SQLite and Postgres (both have a
    # native JSON column type; Django's JSONField never needs raw SQL).
    extra_data = models.JSONField(default=dict, blank=True)

    import_batch = models.ForeignKey(
        ImportBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="contacts"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="created_contacts"
    )
    # Two different concepts — never merged (see Lead docstring).
    reference_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="referred_contacts"
    )
    current_assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_contacts"
    )
    original_assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    next_followup_date = models.DateField(null=True, blank=True, db_index=True)
    next_followup_time = models.TimeField(null=True, blank=True)
    followup_notes = models.TextField(blank=True)
    followup_status = models.CharField(max_length=10, blank=True, default="")
    last_contacted = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["current_assigned_to", "status"], name="contact_assignee_status_idx"),
            models.Index(fields=["reference_by"], name="contact_reference_idx"),
            models.Index(fields=["import_batch", "current_assigned_to"], name="contact_batch_assignee_idx"),
        ]

    def __str__(self):
        return self.name or self.phone

    def save(self, *args, **kwargs):
        self.phone_normalized = normalize_phone(self.phone)
        super().save(*args, **kwargs)


# =========================================================
# ASSIGNMENT / TRANSFER HISTORY, ACTIVITY, AUDIT, JOBS
# =========================================================


class AssignmentHistory(models.Model):
    ACTION_ASSIGN = "assign"
    ACTION_REASSIGN = "reassign"
    ACTION_TRANSFER = "transfer"
    ACTION_CHOICES = [
        (ACTION_ASSIGN, "Assigned"),
        (ACTION_REASSIGN, "Reassigned"),
        (ACTION_TRANSFER, "Transferred"),
    ]

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, null=True, blank=True, related_name="assignment_history")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, null=True, blank=True, related_name="assignment_history")
    action = models.CharField(max_length=10, choices=ACTION_CHOICES)
    from_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    to_user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    # Names are snapshotted so history stays readable if a staff account is removed.
    from_name = models.CharField(max_length=150, blank=True)
    to_name = models.CharField(max_length=150, blank=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    changed_by_name = models.CharField(max_length=150, blank=True)
    reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["lead", "created_at"], name="assignhist_lead_idx"),
            models.Index(fields=["contact", "created_at"], name="assignhist_contact_idx"),
            models.Index(fields=["from_user", "action"], name="assignhist_from_action_idx"),
        ]


class Activity(models.Model):
    """Chronological timeline entries for a lead (or a contact)."""

    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, null=True, blank=True, related_name="activities")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, null=True, blank=True, related_name="activities")
    kind = models.CharField(max_length=30)
    message = models.CharField(max_length=500)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    actor_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["lead", "created_at"], name="activity_lead_idx"),
            models.Index(fields=["contact", "created_at"], name="activity_contact_idx"),
        ]


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    actor_name = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=40, db_index=True)
    summary = models.CharField(max_length=500)
    details = models.JSONField(default=dict, blank=True)
    target_type = models.CharField(max_length=30, blank=True)
    target_id = models.CharField(max_length=40, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class BackgroundJob(models.Model):
    """Tracks long-running bulk operations (assign / reassign / status / delete)."""

    STATUS_QUEUED = "queued"
    STATUS_RUNNING = "running"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    kind = models.CharField(max_length=30)
    title = models.CharField(max_length=200)
    status = models.CharField(max_length=10, default=STATUS_QUEUED)
    total = models.PositiveIntegerField(default=0)
    processed = models.PositiveIntegerField(default=0)
    params = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    heartbeat = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def progress_percent(self):
        return min(100, round(self.processed * 100 / self.total, 1)) if self.total else 0


# =========================================================
# DOCUMENT CHECKLIST (tracking only — NO files are stored)
# =========================================================


class DocumentType(models.Model):
    """
    Master checklist item, edited by Admin. Lead-level ticks live in
    LeadDocument and reference this row, so renaming / disabling / archiving
    a type never corrupts historical ticks.
    """

    name = models.CharField(max_length=100)
    category = models.CharField(max_length=60, blank=True)
    sort_order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)
    is_required = models.BooleanField(default=True, help_text="Counts toward completion.")
    is_default = models.BooleanField(
        default=True, help_text="Appears on every lead. Non-default items can be added per lead."
    )
    is_archived = models.BooleanField(default=False, help_text="Soft-deleted: hidden everywhere but history keeps it.")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["sort_order", "id"]

    def __str__(self):
        return self.name


class LeadDocument(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="documents")
    document_type = models.ForeignKey(DocumentType, on_delete=models.PROTECT, related_name="lead_documents")
    received = models.BooleanField(default=False)
    received_at = models.DateTimeField(null=True, blank=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["lead", "document_type"], name="uniq_lead_document_type"),
        ]
        indexes = [models.Index(fields=["document_type", "received"], name="leaddoc_type_received_idx")]


class DocumentHistory(models.Model):
    lead = models.ForeignKey(Lead, on_delete=models.CASCADE, related_name="document_history")
    document_type = models.ForeignKey(DocumentType, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    document_name = models.CharField(max_length=100)  # snapshot at time of change
    old_received = models.BooleanField(default=False)
    new_received = models.BooleanField(default=False)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    changed_by_name = models.CharField(max_length=150, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


# =========================================================
# SEGMENTS — simple named groups of contacts (no rules/conditions)
# =========================================================


class Segment(models.Model):
    """A plain named container of contacts. Membership is always manual —
    there is no rule/condition engine here by design."""

    name = models.CharField(max_length=150, unique=True)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="segments_created"
    )
    contacts = models.ManyToManyField(Contact, through="ContactSegment", related_name="segments", blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ContactSegment(models.Model):
    """Through-model for Contact<->Segment. The unique constraint is what
    actually prevents duplicate membership — not application-level checks."""

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="segment_links")
    segment = models.ForeignKey(Segment, on_delete=models.CASCADE, related_name="contact_links")
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["contact", "segment"], name="uniq_contact_segment")]
        indexes = [models.Index(fields=["segment", "contact"], name="contactsegment_seg_contact_idx")]


# =========================================================
# CALLING ANALYTICS — Android companion app -> CallRecord
# =========================================================
#
# CallDevice pairs exactly one Android device to exactly one staff
# account, so every synced call can be attributed correctly and a lost/
# stolen/reissued phone can be revoked without touching anyone else's
# device. Pairing is a short-lived one-time code (generated by an admin
# in the CRM); once paired, the device authenticates every request with
# a permanent bearer token. Only the SHA-256 hash of that token is
# stored — the plaintext is shown to the device exactly once, at
# pairing time, the same way a password would be.


def _new_pairing_code():
    return f"{secrets.randbelow(1_000_000):06d}"


def _new_token():
    return secrets.token_hex(32)


def hash_token(raw_token):
    return hashlib.sha256((raw_token or "").encode("utf-8")).hexdigest()


class CallDevice(models.Model):
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="call_devices",
    )
    label = models.CharField(max_length=100, blank=True, help_text="e.g. phone model — for the admin's eyes only.")
    android_device_id = models.CharField(max_length=100, blank=True, db_index=True)

    # One-time pairing (admin generates it, the Android app redeems it once).
    pairing_code = models.CharField(max_length=6, blank=True, db_index=True)
    pairing_code_expires = models.DateTimeField(null=True, blank=True)

    # Permanent device credential once paired. Only the hash is stored.
    token_hash = models.CharField(max_length=64, blank=True, unique=True, null=True)
    paired_at = models.DateTimeField(null=True, blank=True)

    is_active = models.BooleanField(default=True, help_text="Turn off to instantly revoke a lost/replaced device.")
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["pairing_code"], name="calldevice_pairing_idx"),
            models.Index(fields=["staff", "is_active"], name="calldevice_staff_active_idx"),
        ]

    def __str__(self):
        return f"{self.label or 'Device'} — {user_label(self.staff)}"

    @property
    def is_paired(self):
        return bool(self.token_hash)

    def start_pairing(self):
        """(Re)issue a fresh 10-minute pairing code and clear any old token."""
        self.pairing_code = _new_pairing_code()
        self.pairing_code_expires = timezone.now() + timezone.timedelta(minutes=10)
        self.token_hash = None
        self.paired_at = None
        self.save(update_fields=["pairing_code", "pairing_code_expires", "token_hash", "paired_at"])
        return self.pairing_code

    def complete_pairing(self, android_device_id):
        """Redeems the pairing code. Returns the plaintext token — shown once, never stored."""
        raw_token = _new_token()
        self.token_hash = hash_token(raw_token)
        self.android_device_id = (android_device_id or "")[:100]
        self.paired_at = timezone.now()
        self.pairing_code = ""
        self.pairing_code_expires = None
        self.save(update_fields=[
            "token_hash", "android_device_id", "paired_at", "pairing_code", "pairing_code_expires",
        ])
        return raw_token

    @classmethod
    def authenticate(cls, raw_token):
        if not raw_token:
            return None
        try:
            device = cls.objects.select_related("staff", "staff__staff_profile").get(
                token_hash=hash_token(raw_token), is_active=True,
            )
        except cls.DoesNotExist:
            return None
        profile = getattr(device.staff, "staff_profile", None)
        if not (profile and profile.is_account_active):
            return None
        return device


class PrivilegedNumber(models.Model):
    """
    Owner/Jiju/other excluded numbers, configurable by an admin — never
    hard-coded into the Android app. A call to/from any active number here
    is completely excluded from calling analytics, call history, and the
    NEW -> CONTACTED automation, no matter which staff/device it came from.
    """

    phone_number = models.CharField(max_length=20)
    phone_normalized = models.CharField(max_length=20, blank=True, db_index=True)
    label = models.CharField(max_length=100, blank=True, help_text="e.g. Owner, Jiju — for the admin's eyes only.")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["phone_normalized"], name="privnum_phone_idx")]

    def __str__(self):
        return f"{self.label or 'Privileged'} — {self.phone_number}"

    def save(self, *args, **kwargs):
        self.phone_normalized = normalize_phone(self.phone_number)
        super().save(*args, **kwargs)

    @classmethod
    def active_normalized_set(cls):
        """One cheap query — call once per sync batch, never per-event."""
        return set(cls.objects.filter(is_active=True).exclude(phone_normalized="").values_list(
            "phone_normalized", flat=True,
        ))


class CallRecord(models.Model):
    """
    One real phone-call event, synced automatically from a paired Android
    device's call log. Never created by a CRM button click — see
    crm/calling_api.py, the only writer of this table besides admin/data
    fixes. Kept deliberately separate from the generic `Activity` timeline
    (Activity still gets an auto-generated entry when a call matches a
    lead, but the authoritative analytics numbers always come from here).
    """

    DIRECTION_OUTGOING = "outgoing"
    DIRECTION_INCOMING = "incoming"
    DIRECTION_CHOICES = [
        (DIRECTION_OUTGOING, "Outgoing"),
        (DIRECTION_INCOMING, "Incoming"),
    ]

    STATUS_ANSWERED = "answered"
    STATUS_MISSED = "missed"
    STATUS_NOT_CONNECTED = "not_connected"
    STATUS_REJECTED = "rejected"
    STATUS_BUSY = "busy"
    STATUS_FAILED = "failed"
    STATUS_UNKNOWN = "unknown"
    STATUS_CHOICES = [
        (STATUS_ANSWERED, "Answered"),
        (STATUS_MISSED, "Missed"),
        (STATUS_NOT_CONNECTED, "Not Connected"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_BUSY, "Busy"),
        (STATUS_FAILED, "Failed"),
        (STATUS_UNKNOWN, "Unknown"),
    ]

    staff = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="call_records")
    device = models.ForeignKey(CallDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name="call_records")

    lead = models.ForeignKey(Lead, on_delete=models.SET_NULL, null=True, blank=True, related_name="call_records")
    contact = models.ForeignKey(Contact, on_delete=models.SET_NULL, null=True, blank=True, related_name="call_records")

    phone_number = models.CharField(max_length=20, blank=True)
    phone_normalized = models.CharField(max_length=20, blank=True, db_index=True)
    direction = models.CharField(max_length=10, choices=DIRECTION_CHOICES)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_UNKNOWN)

    started_at = models.DateTimeField(db_index=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.PositiveIntegerField(default=0)

    # "<android_device_id>:<CallLog.Calls._ID>" — the Android app's natural
    # dedup key. A unique DB constraint (not just app-level checking) is
    # what actually makes re-sent/retried sync batches idempotent.
    external_call_id = models.CharField(max_length=150, unique=True, db_index=True)
    source = models.CharField(max_length=20, default="android")

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["staff", "started_at"], name="callrecord_staff_started_idx"),
            models.Index(fields=["phone_normalized"], name="callrecord_phone_idx"),
            models.Index(fields=["lead"], name="callrecord_lead_idx"),
        ]

    def __str__(self):
        return f"{self.phone_number} ({self.get_status_display()}) — {user_label(self.staff)}"

    @property
    def duration_label(self):
        m, s = divmod(int(self.duration_seconds or 0), 60)
        return f"{m}:{s:02d}"

    def save(self, *args, **kwargs):
        if not self.phone_normalized:
            self.phone_normalized = normalize_phone(self.phone_number)
        super().save(*args, **kwargs)
