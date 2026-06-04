"""Inline Lucide icons — no runtime JS, no CDN.

Icons are sourced from `lucide-static` (a devDependency) and committed as
individual SVGs under app/static/icons/. `render_icon()` rewrites a raw lucide
SVG so the page controls it with Tailwind instead of the library's defaults:

  - strip the fixed width/height (size comes from h-/w- classes)
  - drop the runtime `lucide lucide-*` class and the license comment
  - inject the caller's classes
  - keep stroke="currentColor" so text-* classes color it; mark aria-hidden

Exposed to templates as the `lucide_icon` Jinja global (see app/templating.py),
wrapped by the `ui.icon(name, classes)` macro.

To add an icon: copy node_modules/lucide-static/icons/<name>.svg into
app/static/icons/ and commit it, then use `ui.icon("<name>", "...")`.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

_ICON_DIR = Path(__file__).resolve().parent / "static" / "icons"

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_SIZE = re.compile(r'\s(?:width|height)="[^"]*"')   # standalone only; not stroke-width
_CLASS = re.compile(r'\sclass="[^"]*"')


def render_icon(svg_text: str, classes: str = "") -> str:
    """Process a raw lucide SVG for inline use with the given Tailwind classes."""
    s = _COMMENT.sub("", svg_text)
    s = _SIZE.sub("", s)
    if _CLASS.search(s):
        s = _CLASS.sub(f' class="{classes}"', s, count=1)
    else:
        s = s.replace("<svg", f'<svg class="{classes}"', 1)
    if "aria-hidden" not in s:
        s = s.replace("<svg", '<svg aria-hidden="true"', 1)
    return s.strip()


@lru_cache(maxsize=None)
def _read(name: str) -> str:
    path = _ICON_DIR / f"{name}.svg"
    if not path.is_file():
        raise ValueError(f"Unknown icon '{name}': no {name}.svg in {_ICON_DIR}")
    return path.read_text(encoding="utf-8")


def icon(name: str, classes: str = "h-4 w-4") -> Markup:
    """Inline the named icon as Markup (safe HTML) with `classes` applied."""
    return Markup(render_icon(_read(name), classes))
