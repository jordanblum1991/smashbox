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


templates.env.filters["money"] = money
templates.env.filters["pct"] = pct
