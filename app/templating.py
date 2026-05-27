"""Single source of truth for the Jinja2 environment.

Routers import `templates` from here so every page shares filters and globals.
"""
import re
from decimal import Decimal
from pathlib import Path

from fastapi.templating import Jinja2Templates

BASE_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=BASE_DIR / "templates")


def money(value) -> str:
    if value is None:
        return "—"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    sign = "-" if value < 0 else ""
    abs_val = abs(value)
    return f"{sign}${abs_val:,.2f}"


def pct(value) -> str:
    if value is None:
        return "—"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"{value * 100:.1f}%"


def month_label(year: int, month: int) -> str:
    """Human-readable month header used across every P&L report: 'April 2026'."""
    import calendar  # stdlib; cheap to import per call
    return f"{calendar.month_name[month]} {year}"


def month_short(month_val) -> str:
    """Short three-letter month name for compact YTD column headers: 'Apr'.
    Accepts either a date/datetime or an integer 1–12."""
    import calendar
    if hasattr(month_val, "month"):
        return calendar.month_abbr[month_val.month]
    return calendar.month_abbr[int(month_val)]


# Matches a trailing parenthesized chunk preceded by optional whitespace.
# \xa0 is included explicitly because the TikTok master sheet uses NBSP
# before the size paren on some rows (e.g. PHOTO FINISH OIL CONTROL ...).
_TRAILING_SIZE_RE = re.compile(r"[\s\xa0]*\(([^)]*)\)\s*$")


def strip_size(value) -> str:
    """Remove a trailing parenthesized size chunk from a product name.
    Returns the input unchanged when no trailing paren is present."""
    if not value:
        return value
    return _TRAILING_SIZE_RE.sub("", str(value))


def extract_size(value) -> str:
    """Return the trailing parenthesized size (without parens), or '—'."""
    if not value:
        return "—"
    match = _TRAILING_SIZE_RE.search(str(value))
    return match.group(1).strip() if match else "—"


def title_case(value) -> str:
    """Title-case a string via str.title(); pass-through for falsy input."""
    if not value:
        return value
    return str(value).title()


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
templates.env.globals["month_label"] = month_label
templates.env.filters["month_short"] = month_short
templates.env.filters["strip_size"] = strip_size
templates.env.filters["extract_size"] = extract_size
templates.env.filters["title_case"] = title_case
