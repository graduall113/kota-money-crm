"""
ONE place where "who may see / change what" is decided.

Every view, export, bulk action and AJAX endpoint must start from
visible_leads(user) / visible_contacts(user) — never from Lead.objects /
Contact.objects — so filters, search, pagination and manipulated query
strings can only ever narrow an already-authorized set.
"""
from django.db.models import Q
from django.http import Http404

from .settings_store import get_bool


def is_admin(user):
    profile = getattr(user, "staff_profile", None)
    return bool(user.is_authenticated and profile and profile.is_admin)


def visible_leads(user):
    from .models import Lead

    qs = Lead.objects.all()
    if is_admin(user):
        return qs
    cond = Q(assigned_to=user)
    if get_bool("referrer_keeps_access"):
        cond |= Q(reference_by=user)
    return qs.filter(cond)


def visible_contacts(user):
    from .models import Contact

    # Soft-deleted contacts (currently only ever produced by "Undo Import")
    # never show up anywhere a normal user or admin looks — lists, search,
    # exports, segments, counts, bulk actions. Restoring the import is the
    # only way back.
    qs = Contact.objects.filter(is_deleted=False)
    if is_admin(user):
        return qs
    cond = Q(current_assigned_to=user)
    if get_bool("referrer_keeps_access"):
        cond |= Q(reference_by=user)
    return qs.filter(cond)


def get_visible_lead_or_404(user, pk):
    """Not-visible leads look exactly like non-existent ones (404, no info leak)."""
    try:
        return visible_leads(user).select_related("reference_by", "assigned_to", "original_assigned_to").get(pk=pk)
    except Exception:
        raise Http404("Lead not found")


def get_visible_contact_or_404(user, pk):
    try:
        return visible_contacts(user).select_related(
            "reference_by", "current_assigned_to", "import_batch"
        ).get(pk=pk)
    except Exception:
        raise Http404("Contact not found")


def can_edit_lead(user, lead):
    if is_admin(user):
        return True
    # The current owner edits. An unassigned lead can still be edited by its referrer.
    if lead.assigned_to_id == user.id:
        return True
    return lead.assigned_to_id is None and lead.reference_by_id == user.id


def can_edit_contact(user, contact):
    if is_admin(user):
        return True
    if contact.current_assigned_to_id == user.id:
        return True
    return contact.current_assigned_to_id is None and contact.reference_by_id == user.id


def can_delete_lead(user, lead):
    if is_admin(user):
        return True
    return lead.reference_by_id == user.id and lead.assigned_to_id in (None, user.id)


def can_delete_contact(user, contact):
    if is_admin(user):
        return True
    return contact.reference_by_id == user.id and contact.current_assigned_to_id in (None, user.id)


def can_transfer_lead(user, lead):
    if is_admin(user):
        return True
    return get_bool("staff_can_transfer") and can_edit_lead(user, lead)


def can_export(user):
    return is_admin(user) or get_bool("staff_can_export")


# ---------------------------------------------------------------- segments
# Segments are visible (read-only) to every active user, scoped to the
# contacts they're authorized to see. Creating, editing, deleting a segment,
# and changing who's in it (including the bulk "Add to Segment" action and
# segment-wise staff assignment) is Admin-only.
def can_manage_segments(user):
    return is_admin(user)


def visible_segment_contacts(user, segment):
    return visible_contacts(user).filter(segments=segment)
