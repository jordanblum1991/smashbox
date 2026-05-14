"""Single source of truth for the Jinja2 environment.

Routers import `templates` from here so every page shares filters and globals.
"""
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


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
templates.env.globals["month_label"] = month_label
templates.env.filters["month_short"] = month_short
