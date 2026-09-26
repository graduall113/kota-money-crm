"""
Single place for the CRM's DD/MM/YYYY date convention.

Native <input type="date"> is deliberately NOT used for user-entered dates
anywhere in this app: its on-screen format follows the visiting browser's
locale (often MM/DD/YYYY for US-locale browsers) no matter what the page's
own language is, so it can silently show 24/09/2026 as 09/24/2026. Every
user-entered date field is instead a plain text input with a DD/MM/YYYY
placeholder, parsed here, and every displayed date uses the "d/m/Y" Django
format so what's shown always matches what's typed.

Storage is untouched — Django DateField/DateTimeField columns stay exactly
as they are; only the text people read and type changes.
"""
import datetime

from django import forms

DISPLAY_FORMAT = "d/m/Y"          # Django template date filter format
DISPLAY_FORMAT_DT = "d/m/Y, H:i"  # date + time
INPUT_FORMATS = ["%d/%m/%Y"]
PLACEHOLDER = "DD/MM/YYYY"
INVALID_MESSAGE = "Enter a valid date in DD/MM/YYYY format."


def parse_ddmmyyyy(raw, required=True):
    """
    Parses a DD/MM/YYYY string into a date(). Raises ValueError with
    INVALID_MESSAGE-worthy text on anything malformed (including real
    calendar problems like 32/13/2026) — never raises any other exception,
    never crashes the view.
    """
    raw = (raw or "").strip()
    if not raw:
        if required:
            raise ValueError(INVALID_MESSAGE)
        return None
    try:
        return datetime.datetime.strptime(raw, "%d/%m/%Y").date()
    except ValueError:
        raise ValueError(INVALID_MESSAGE)


def format_ddmmyyyy(value):
    if not value:
        return ""
    return value.strftime("%d/%m/%Y")


class DateTextField(forms.DateField):
    """A DD/MM/YYYY *text* input bound to a real DateField — used everywhere
    a ModelForm needs a user-entered date (never forms.DateInput type=date)."""

    def __init__(self, *, required=True, label=None, placeholder=PLACEHOLDER, **kwargs):
        widget = forms.DateInput(
            format="%d/%m/%Y",
            attrs={"class": "field-input", "type": "text", "placeholder": placeholder, "autocomplete": "off"},
        )
        super().__init__(
            required=required, label=label, input_formats=INPUT_FORMATS, widget=widget,
            error_messages={"invalid": INVALID_MESSAGE}, **kwargs,
        )
