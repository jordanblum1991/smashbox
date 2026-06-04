"""Tests for the Lucide icon renderer (app/icons.py).

render_icon() rewrites a raw lucide-static SVG so the page controls it with
Tailwind: strip the fixed width/height (so h-/w- classes size it), drop the
runtime `lucide lucide-*` class + license comment, inject the caller's classes,
keep stroke="currentColor" + viewBox + path geometry, and mark it aria-hidden.
"""
import pytest

from app.icons import icon, render_icon

RAW = (
    '<!-- @license lucide-static v1.17.0 - ISC -->\n'
    '<svg class="lucide lucide-trending-up" xmlns="http://www.w3.org/2000/svg"\n'
    '  width="24" height="24" viewBox="0 0 24 24" fill="none"\n'
    '  stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">\n'
    '  <path d="M16 7h6v6" />\n'
    '  <path d="m22 7-8.5 8.5-5-5L2 17" />\n'
    '</svg>\n'
)


def test_render_icon_strips_fixed_size():
    out = render_icon(RAW, "h-4 w-4")
    assert 'width="24"' not in out
    assert 'height="24"' not in out


def test_render_icon_keeps_stroke_width_attribute():
    # Only standalone width/height are stripped — stroke-width must survive.
    assert 'stroke-width="2"' in render_icon(RAW, "h-4 w-4")


def test_render_icon_injects_classes_and_drops_lucide_class():
    out = render_icon(RAW, "h-5 w-5 text-sky-500")
    assert 'class="h-5 w-5 text-sky-500"' in out
    assert "lucide-trending-up" not in out
    assert "lucide " not in out


def test_render_icon_preserves_currentcolor_viewbox_and_paths():
    out = render_icon(RAW, "h-4 w-4")
    assert 'stroke="currentColor"' in out
    assert 'viewBox="0 0 24 24"' in out
    assert 'd="M16 7h6v6"' in out
    assert 'd="m22 7-8.5 8.5-5-5L2 17"' in out


def test_render_icon_strips_license_comment_and_adds_aria_hidden():
    out = render_icon(RAW, "h-4 w-4")
    assert "<!--" not in out
    assert 'aria-hidden="true"' in out


def test_icon_reads_a_real_bundled_svg():
    out = icon("trending-up", "h-4 w-4 text-emerald-500")
    assert "<svg" in out
    assert 'class="h-4 w-4 text-emerald-500"' in out
    assert 'width="24"' not in out          # processed, not raw


def test_icon_unknown_name_raises():
    with pytest.raises(ValueError):
        icon("definitely-not-a-real-icon")


def test_every_ui_icon_reference_has_a_committed_svg():
    """Regression guard: every `ui.icon("name")` literal across the templates
    must have a committed app/static/icons/<name>.svg, so a missed copy can't
    ship a 500/blank icon. (Indirect names passed through a variable — e.g.
    module_summary args or `icon_name` set-vars — are covered by page renders.)"""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    icons_dir = root / "app" / "static" / "icons"
    pat = re.compile(r"""ui\.icon\(\s*["']([a-z0-9-]+)["']""")

    referenced: set[str] = set()
    for html in (root / "app" / "templates").rglob("*.html"):
        referenced |= set(pat.findall(html.read_text(encoding="utf-8")))

    assert referenced, "expected to find ui.icon() references in templates"
    missing = sorted(n for n in referenced if not (icons_dir / f"{n}.svg").is_file())
    assert not missing, f"ui.icon() references with no committed SVG: {missing}"
