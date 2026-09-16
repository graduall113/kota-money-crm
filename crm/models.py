from django.conf import settings
from django.db import models


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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.customer_name
