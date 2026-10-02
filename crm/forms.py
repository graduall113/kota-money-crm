from django import forms
from django.contrib.auth.forms import AuthenticationForm, UserCreationForm
from django.contrib.auth.models import User

from .datefmt import DateTextField
from . import holidays as holiday_rules
from .models import Contact, Holiday, Lead, Segment, StaffProfile

TEXT_WIDGET = {"class": "field-input"}


class RegisterForm(UserCreationForm):
    """
    Public self-registration. Always creates a STAFF account —
    role promotion to admin only happens through Staff Management,
    never through this form.
    """

    full_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs=TEXT_WIDGET))
    email = forms.EmailField(widget=forms.EmailInput(attrs=TEXT_WIDGET))
    phone = forms.CharField(max_length=20, required=False, widget=forms.TextInput(attrs=TEXT_WIDGET))
    reference_code = forms.CharField(max_length=50, required=False, widget=forms.TextInput(attrs=TEXT_WIDGET))

    class Meta:
        model = User
        fields = ["full_name", "email", "phone", "reference_code", "password1", "password2"]

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def save(self, commit=True):
        user = super().save(commit=False)
        full_name = self.cleaned_data["full_name"].strip()
        first, _, last = full_name.partition(" ")
        user.first_name = first
        user.last_name = last
        user.email = self.cleaned_data["email"]
        user.username = self.cleaned_data["email"]  # login by email
        if commit:
            user.save()
            # The post_save signal already created a StaffProfile (role=staff
            # by default) — just fill in the extra fields the form collected.
            profile = user.staff_profile
            profile.phone = self.cleaned_data.get("phone", "")
            profile.reference_code = self.cleaned_data.get("reference_code", "")
            profile.role = StaffProfile.ROLE_STAFF
            profile.save()
        return user

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["password1"].widget.attrs.update(TEXT_WIDGET)
        self.fields["password2"].widget.attrs.update(TEXT_WIDGET)


class EmailAuthenticationForm(AuthenticationForm):
    """Standard AuthenticationForm, restyled to match the CRM's inputs."""

    remember_me = forms.BooleanField(required=False, initial=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].widget.attrs.update(TEXT_WIDGET)
        self.fields["username"].label = "Email / Username"
        self.fields["password"].widget.attrs.update(TEXT_WIDGET)


class ProfileForm(forms.Form):
    """
    Self-service profile edit. Deliberately has no role field —
    staff (and admins editing themselves here) cannot self-promote.
    """

    full_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs=TEXT_WIDGET))
    email = forms.EmailField(widget=forms.EmailInput(attrs=TEXT_WIDGET))
    phone = forms.CharField(max_length=20, required=False, widget=forms.TextInput(attrs=TEXT_WIDGET))

    def __init__(self, *args, user=None, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        if user and not self.is_bound:
            self.fields["full_name"].initial = user.get_full_name()
            self.fields["email"].initial = user.email
            self.fields["phone"].initial = getattr(user, "staff_profile", None) and user.staff_profile.phone

    def save(self):
        full_name = self.cleaned_data["full_name"].strip()
        first, _, last = full_name.partition(" ")
        self.user.first_name = first
        self.user.last_name = last
        self.user.email = self.cleaned_data["email"]
        self.user.save()
        profile = self.user.staff_profile
        profile.phone = self.cleaned_data.get("phone", "")
        profile.save()
        return self.user


class StaffCreateForm(forms.Form):
    """Admin-only: create a brand-new staff/admin account."""

    full_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs=TEXT_WIDGET))
    email = forms.EmailField(widget=forms.EmailInput(attrs=TEXT_WIDGET))
    phone = forms.CharField(max_length=20, required=False, widget=forms.TextInput(attrs=TEXT_WIDGET))
    password = forms.CharField(widget=forms.PasswordInput(attrs=TEXT_WIDGET))
    role = forms.ChoiceField(choices=StaffProfile.ROLE_CHOICES, widget=forms.Select(attrs=TEXT_WIDGET))
    status = forms.ChoiceField(choices=StaffProfile.STATUS_CHOICES, widget=forms.Select(attrs=TEXT_WIDGET))

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def save(self):
        full_name = self.cleaned_data["full_name"].strip()
        first, _, last = full_name.partition(" ")
        user = User.objects.create_user(
            username=self.cleaned_data["email"],
            email=self.cleaned_data["email"],
            password=self.cleaned_data["password"],
            first_name=first,
            last_name=last,
        )
        profile = user.staff_profile
        profile.phone = self.cleaned_data.get("phone", "")
        profile.role = self.cleaned_data["role"]
        profile.status = self.cleaned_data["status"]
        profile.save()
        return user


class StaffEditForm(forms.Form):
    """Admin-only: edit an existing staff member's role/status/contact info."""

    full_name = forms.CharField(max_length=150, widget=forms.TextInput(attrs=TEXT_WIDGET))
    email = forms.EmailField(widget=forms.EmailInput(attrs=TEXT_WIDGET))
    phone = forms.CharField(max_length=20, required=False, widget=forms.TextInput(attrs=TEXT_WIDGET))
    role = forms.ChoiceField(choices=StaffProfile.ROLE_CHOICES, widget=forms.Select(attrs=TEXT_WIDGET))
    status = forms.ChoiceField(choices=StaffProfile.STATUS_CHOICES, widget=forms.Select(attrs=TEXT_WIDGET))

    def __init__(self, *args, instance=None, **kwargs):
        self.instance = instance
        super().__init__(*args, **kwargs)
        if instance and not self.is_bound:
            self.fields["full_name"].initial = instance.get_full_name()
            self.fields["email"].initial = instance.email
            self.fields["phone"].initial = instance.staff_profile.phone
            self.fields["role"].initial = instance.staff_profile.role
            self.fields["status"].initial = instance.staff_profile.status

    def clean_email(self):
        email = self.cleaned_data["email"]
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise forms.ValidationError("Another account already uses this email.")
        return email

    def save(self):
        full_name = self.cleaned_data["full_name"].strip()
        first, _, last = full_name.partition(" ")
        self.instance.first_name = first
        self.instance.last_name = last
        self.instance.email = self.cleaned_data["email"]
        self.instance.save()
        profile = self.instance.staff_profile
        profile.phone = self.cleaned_data.get("phone", "")
        profile.role = self.cleaned_data["role"]
        profile.status = self.cleaned_data["status"]
        profile.save()
        return self.instance


class LeadForm(forms.ModelForm):
    """
    The CRM's own native Lead Form — recreated field-for-field to match
    the original n8n "Leads Form" (order, field types, required rules):

      1. Date (DD/MM/YYYY)  — plain text input, NOT a date picker
      2. Full Name          — text input
      3. Contact No.        — text input
      4. Work Profile       — dropdown
      5. Income             — text input
      6. Requirement        — dropdown
      7. Loan Amount        — text input
      8. Bank Calling       — text input
      9. Status             — dropdown
      10. Reference by      — text input
      11. Assigned To       — text input

    NOTE on Work Profile / Requirement dropdown options: the reference n8n
    form (https://soyacil.app.n8n.cloud/form/664c5f8c-5fda-4f3d-9a1f-d5116a7dd74d)
    is private — robots.txt blocks automated access — and no option list
    for these two fields exists anywhere in this project (models.py,
    api.py, n8n_integration.py, templates, README, git history). Per the
    explicit instruction to never invent/guess dropdown options, these two
    stay as required free-text inputs until you paste/screenshot the real
    option list — then it's a one-line change each to forms.ChoiceField.
    Status already has a real, existing dropdown (unchanged below).

    "Reference by" / "Assigned To" here are the free-text fields the n8n
    form itself collects (e.g. "KotaMoney") — separate from the CRM's own
    internal reference_by/assigned_to staff ownership FKs, which the view
    sets automatically and are not part of this form. See Lead model docs.
    """

    form_date = DateTextField(label="Date")
    next_followup_date = DateTextField(label="Next follow-up date", required=False)
    loan_amount = forms.DecimalField(
        label="Loan Amount",
        min_value=0,
        widget=forms.TextInput(attrs={**TEXT_WIDGET, "inputmode": "decimal", "placeholder": "e.g. 29828"}),
        error_messages={"invalid": "Enter a valid loan amount."},
    )

    class Meta:
        model = Lead
        fields = [
            "form_date",
            "customer_name",
            "contact_number",
            "work_profile",
            "income",
            "requirement",
            "loan_amount",
            "bank_calling",
            "status",
            "reference_by_name",
            "assigned_to_name",
            "email",
            "city",
            "source",
            "interest",
            "next_followup_date",
            "next_followup_time",
            "followup_notes",
        ]
        labels = {
            "next_followup_date": "Next follow-up date",
            "next_followup_time": "Next follow-up time",
            "followup_notes": "Follow-up notes",
            "source": "Lead source",
            "contact_number": "Contact No.",
            "bank_calling": "Bank Calling",
            "reference_by_name": "Reference by",
            "assigned_to_name": "Assigned To",
        }
        widgets = {
            "customer_name": forms.TextInput(attrs=TEXT_WIDGET),
            "contact_number": forms.TextInput(attrs={**TEXT_WIDGET, "type": "tel", "inputmode": "tel"}),
            # See the class docstring: real option lists needed before these
            # two can safely become dropdowns without guessing values.
            "work_profile": forms.TextInput(attrs=TEXT_WIDGET),
            "income": forms.TextInput(attrs=TEXT_WIDGET),
            "requirement": forms.TextInput(attrs=TEXT_WIDGET),
            "bank_calling": forms.TextInput(attrs=TEXT_WIDGET),
            "status": forms.Select(attrs=TEXT_WIDGET),
            "reference_by_name": forms.TextInput(attrs=TEXT_WIDGET),
            "assigned_to_name": forms.TextInput(attrs=TEXT_WIDGET),
            "email": forms.EmailInput(attrs=TEXT_WIDGET),
            "city": forms.TextInput(attrs=TEXT_WIDGET),
            "source": forms.TextInput(attrs=TEXT_WIDGET),
            "interest": forms.Select(attrs=TEXT_WIDGET),
            "next_followup_time": forms.TimeInput(format="%H:%M", attrs={**TEXT_WIDGET, "type": "time"}),
            "followup_notes": forms.Textarea(attrs={**TEXT_WIDGET, "rows": 2}),
        }

    def __init__(self, *args, is_admin=False, **kwargs):
        super().__init__(*args, **kwargs)
        # Admin-only: pick the CRM staff owner (the real, permission-driving assignee).
        self.fields["assigned_to_user"] = forms.ModelChoiceField(
            label="Assign to staff (CRM owner)", required=False,
            queryset=User.objects.filter(staff_profile__status="active").order_by("first_name", "username"),
            widget=forms.Select(attrs=TEXT_WIDGET), empty_label="— Unassigned —",
        )
        self.is_admin_form = is_admin
        if not is_admin:
            del self.fields["assigned_to_user"]
        elif self.instance and self.instance.pk:
            self.fields["assigned_to_user"].initial = self.instance.assigned_to_id

        # The n8n form makes every single field mandatory. Several of these
        # Lead model fields carry blank=True (to stay lenient for the
        # separate n8n -> Django inbound API in crm/api.py), so a plain
        # ModelForm would otherwise mark them optional. Force them required
        # here so this form matches the n8n form's behavior exactly: Django
        # validation AND the HTML5 "required" attribute (added automatically
        # by Django for any field with required=True) both apply.
        always_required = [
            "form_date",
            "customer_name",
            "contact_number",
            "work_profile",
            "income",
            "requirement",
            "loan_amount",
            "bank_calling",
            "status",
            "reference_by_name",
            "assigned_to_name",
        ]
        for field_name in always_required:
            self.fields[field_name].required = True

    def clean_work_profile(self):
        value = self.cleaned_data["work_profile"].strip()
        if not value:
            raise forms.ValidationError("Work Profile is required.")
        return value

    def clean_income(self):
        value = self.cleaned_data["income"].strip()
        if not value:
            raise forms.ValidationError("Income is required.")
        return value

    def clean_requirement(self):
        value = self.cleaned_data["requirement"].strip()
        if not value:
            raise forms.ValidationError("Requirement is required.")
        return value

    def clean_bank_calling(self):
        value = self.cleaned_data["bank_calling"].strip()
        if not value:
            raise forms.ValidationError("Bank Calling is required.")
        return value

    def clean_reference_by_name(self):
        value = self.cleaned_data["reference_by_name"].strip()
        if not value:
            raise forms.ValidationError("Reference by is required.")
        return value

    def clean_assigned_to_name(self):
        value = self.cleaned_data["assigned_to_name"].strip()
        if not value:
            raise forms.ValidationError("Assigned To is required.")
        return value

    def clean_customer_name(self):
        name = self.cleaned_data["customer_name"].strip()
        if not name:
            raise forms.ValidationError("Customer name is required.")
        return name

    def clean_contact_number(self):
        contact = self.cleaned_data["contact_number"].strip()
        digits = "".join(ch for ch in contact if ch.isdigit())
        if len(digits) < 7:
            raise forms.ValidationError("Enter a valid contact number.")
        # Preserve exactly what was typed (including any leading zero /
        # formatting) — never coerce this into a number.
        return contact


class TransferForm(forms.Form):
    to_user = forms.ModelChoiceField(
        queryset=User.objects.filter(staff_profile__status="active").order_by("first_name", "username"),
        widget=forms.Select(attrs=TEXT_WIDGET), label="Transfer to",
    )
    reason = forms.CharField(max_length=300, required=False, label="Reason (optional)",
                             widget=forms.TextInput(attrs={**TEXT_WIDGET, "placeholder": "e.g. Customer requested senior assistance"}))


class DocumentTypeForm(forms.ModelForm):
    class Meta:
        from .models import DocumentType as _DT
        model = _DT
        fields = ["name", "category", "is_required", "is_default", "is_active"]
        widgets = {"name": forms.TextInput(attrs=TEXT_WIDGET), "category": forms.TextInput(attrs=TEXT_WIDGET)}

    def clean_name(self):
        from .models import DocumentType
        name = self.cleaned_data["name"].strip()
        qs = DocumentType.objects.filter(name__iexact=name, is_archived=False)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A document with this name already exists.")
        return name


class ImportUploadForm(forms.Form):
    file = forms.FileField(label="CSV or Excel file")
    source_label = forms.CharField(max_length=100, required=False, label="Source label (optional)",
                                   widget=forms.TextInput(attrs={**TEXT_WIDGET, "placeholder": "e.g. Facebook Ads Sept"}))


class SegmentForm(forms.ModelForm):
    class Meta:
        model = Segment
        fields = ["name", "description"]
        widgets = {
            "name": forms.TextInput(attrs={**TEXT_WIDGET, "placeholder": "e.g. Jaipur Leads"}),
            "description": forms.Textarea(attrs={**TEXT_WIDGET, "rows": 3, "placeholder": "Optional"}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Segment.objects.filter(name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise forms.ValidationError("A segment with this name already exists.")
        return name


class ContactForm(forms.ModelForm):
    """
    Manual single-contact Add / Edit form. Reuses the existing Contact
    model fields exactly as the importer populates them — no new fields,
    no new model. Admin-only fields (assignment) are added dynamically by
    the view, since a plain staff member creating a contact always owns it.
    """

    class Meta:
        model = Contact
        fields = [
            "name", "phone", "email", "address", "city", "work_profile",
            "income", "requirement", "loan_amount", "source", "status", "notes",
        ]
        widgets = {
            "name": forms.TextInput(attrs={**TEXT_WIDGET, "placeholder": "Full name"}),
            "phone": forms.TextInput(attrs={**TEXT_WIDGET, "type": "tel", "inputmode": "tel", "placeholder": "e.g. 98765 43210"}),
            "email": forms.EmailInput(attrs=TEXT_WIDGET),
            "address": forms.TextInput(attrs=TEXT_WIDGET),
            "city": forms.TextInput(attrs=TEXT_WIDGET),
            "work_profile": forms.TextInput(attrs=TEXT_WIDGET),
            "income": forms.TextInput(attrs=TEXT_WIDGET),
            "requirement": forms.TextInput(attrs=TEXT_WIDGET),
            "loan_amount": forms.NumberInput(attrs={**TEXT_WIDGET, "inputmode": "decimal", "step": "0.01"}),
            "source": forms.TextInput(attrs={**TEXT_WIDGET, "placeholder": "e.g. Walk-in, Referral"}),
            "status": forms.Select(attrs=TEXT_WIDGET),
            "notes": forms.Textarea(attrs={**TEXT_WIDGET, "rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Never let this form create/edit a "converted" contact by hand —
        # that transition only happens through the real convert-to-lead flow.
        self.fields["status"].choices = [c for c in Contact.STATUS_CHOICES if c[0] != Contact.STATUS_CONVERTED]
        # An ALREADY-converted contact must stay converted when it is edited and saved. Its status is
        # not among the choices above, so without this the dropdown would silently fall back to "New"
        # on save. Show it, lock it (disabled fields ignore POSTed values).
        if self.instance is not None and self.instance.pk and self.instance.status == Contact.STATUS_CONVERTED:
            self.fields["status"].choices = Contact.STATUS_CHOICES
            self.fields["status"].disabled = True
        self.fields["name"].required = True
        self.fields["phone"].required = True

    def clean_phone(self):
        phone = self.cleaned_data.get("phone", "").strip()
        if not phone:
            raise forms.ValidationError("Enter a contact number.")
        return phone


class HolidayForm(forms.ModelForm):
    """
    Add / Edit a holiday. Admin-only (the views enforce that; this form only validates).

    "Holiday type" is a convenience that fills in the end date on the server, so it
    works without JavaScript: Single day -> end = start, One week -> start + 6 days,
    One month -> start .. same date next month - 1 day. "Date range" uses the typed end date.

    Rules:
      * end date before start date              -> error on the End date field
      * same-name holiday on overlapping dates  -> refused (almost certainly entered twice)
      * exactly the same dates as another row   -> refused (uniq_holiday_period constraint)
      * overlaps another ACTIVE holiday         -> warning; saved only once the admin confirms
        with "Save anyway" (`confirm_overlap`). `self.overlaps` lists the clashing holidays.
    """

    duration = forms.ChoiceField(
        label="Holiday type", choices=holiday_rules.DURATION_CHOICES, initial=holiday_rules.DURATION_CUSTOM,
        widget=forms.Select(attrs=TEXT_WIDGET),
    )
    start_date = DateTextField(label="From")
    end_date = DateTextField(required=False, label="To")
    confirm_overlap = forms.BooleanField(required=False)

    class Meta:
        model = Holiday
        fields = ["name", "start_date", "end_date", "reason", "is_active"]
        labels = {"name": "Holiday name", "reason": "Reason / note", "is_active": "Active"}
        widgets = {
            "name": forms.TextInput(attrs={**TEXT_WIDGET, "placeholder": "e.g. Diwali Holiday"}),
            "reason": forms.Textarea(attrs={**TEXT_WIDGET, "rows": 2, "placeholder": "Optional - shown to admins only"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.overlaps = []
        if self.instance.pk and not self.is_bound and self.instance.is_single_day:
            self.initial["duration"] = holiday_rules.DURATION_SINGLE

    def clean_name(self):
        return " ".join(self.cleaned_data["name"].split())

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        if start is None:
            return cleaned  # the field itself already reported the problem

        # 1. Work out the end date.
        kind = cleaned.get("duration") or holiday_rules.DURATION_CUSTOM
        end = holiday_rules.end_for_preset(kind, start)
        if end is not None:
            self.errors.pop("end_date", None)  # a preset ignores whatever was typed in the To box
        else:
            end = cleaned.get("end_date")
            if end is None:
                if "end_date" not in self.errors:
                    self.add_error("end_date", "Enter the last day of the holiday, or choose \u201cSingle day\u201d.")
                return cleaned
        cleaned["end_date"] = end

        # 2. Basic sanity.
        if end < start:
            self.add_error("end_date", "The end date cannot be before the start date.")
            return cleaned
        if (end - start).days + 1 > holiday_rules.MAX_SPAN_DAYS:
            self.add_error("end_date", f"A single holiday can span at most {holiday_rules.MAX_SPAN_DAYS} days. Check the dates, or split it into two.")
        if start.year < holiday_rules.MIN_YEAR or end.year > holiday_rules.MAX_YEAR:
            self.add_error("start_date", f"Enter a year between {holiday_rules.MIN_YEAR} and {holiday_rules.MAX_YEAR}.")
        if self.errors:
            return cleaned

        # 3. Accidental duplicates / overlaps.
        pk = self.instance.pk
        name = cleaned.get("name")
        if name:
            clash = holiday_rules.find_same_name_conflict(name, start, end, pk)
            # (identical dates are reported by the uniq_holiday_period constraint instead - don't say it twice)
            if clash and (clash.start_date, clash.end_date) != (start, end):
                raise forms.ValidationError(
                    f"A holiday named \u201c{clash.name}\u201d already covers {clash.start_date:%d/%m/%Y} - {clash.end_date:%d/%m/%Y}"
                    f"{'' if clash.is_active else ' (currently inactive)'}. Edit or re-activate that one instead of adding a duplicate."
                )
        if cleaned.get("is_active") and not cleaned.get("confirm_overlap"):
            overlaps = [h for h in holiday_rules.find_overlaps(start, end, pk) if (h.start_date, h.end_date) != (start, end)]
            if overlaps:
                self.overlaps = overlaps
                raise forms.ValidationError("These dates overlap another active holiday. Nothing has been saved yet.", code="overlap")
        return cleaned

