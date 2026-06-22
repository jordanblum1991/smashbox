# Mobile Formatting Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the shared nav + the Dashboard / P&L / Sales / Ad-Spend pages render well on a phone — chiefly: replace the horizontally-scrolling nav with a hamburger → vertical tap-menu, and reflow the P&L single-month statement to a clean two-column phone layout.

**Architecture:** Pure Jinja/Tailwind responsive-class changes (compiled Tailwind, no new deps). The nav gets a `hidden md:flex` desktop bar + a `md:hidden` native-`<details>` mobile menu that loops the SAME `{% set %}` link vars. P&L hides its "% of Net Sales" column below `sm`. Container padding goes `px-4 sm:px-6`.

**Tech Stack:** Jinja2 + compiled Tailwind, pytest (render + structural guards). The real acceptance test is the user's eyeball on a phone. Spec: `docs/superpowers/specs/2026-06-22-mobile-formatting-design.md`.

**Branch:** `feature/mobile-formatting` (created; spec committed).

**Conventions:**
- Tests via Bash: `py -m pytest <path> -v 2>&1 | tail -25` (NOT PowerShell/venv).
- Commit: write `.git/COMMIT_MSG_DRAFT.txt` with the **Write tool** (NOT printf — a literal `%` breaks printf), then `git commit -F`. End with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Do NOT change desktop appearance** — every change must be gated so `md+`/`sm+` looks identical to today. New responsive classes only add mobile behavior.
- After editing templates, no server restart needed for tests (Jinja re-parses), but **CSS classes built by string interpolation won't exist** — we only use static utility classes here, which Tailwind's scanner already covers.

---

## File Structure

- **Modify** `app/templates/base.html` — container padding (Task 1).
- **Modify** `app/templates/partials/nav.html` — desktop bar `hidden md:flex` + mobile `<details>` menu (Task 2).
- **Modify** `app/templates/reports/pnl.html` — hide `%` column below `sm`, tighten padding (Task 3).
- **Modify** `app/templates/reports/ad_spend.html`, `reports/sales.html`, `dashboard.html` — padding / wrap / stacking polish (Task 4).
- **Tests:** `tests/test_mobile_layout.py` — render + structural guards.

---

## Task 1: App-wide container padding

**Files:**
- Modify: `app/templates/base.html`
- Test: `tests/test_mobile_layout.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_mobile_layout.py
"""Mobile responsiveness guards. These assert structural markers (responsive
classes / mobile-menu markup) survive future edits. They do NOT validate visual
layout — the acceptance test is a human eyeball on a phone."""
import pytest
from fastapi.testclient import TestClient

from app.db import Base, engine
from app.main import app


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    return TestClient(app)


def test_main_container_uses_responsive_padding(client):
    html = client.get("/").text
    # Mobile gets tighter px-4; sm+ restores px-6.
    assert "px-4 sm:px-6" in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_mobile_layout.py::test_main_container_uses_responsive_padding -v 2>&1 | tail -12`
Expected: FAIL — the page uses `px-6`, not `px-4 sm:px-6`.

- [ ] **Step 3: Edit base.html**

In `app/templates/base.html`, change the `<main>` and `<footer>` padding from `px-6` to `px-4 sm:px-6`:
```html
  <main class="mx-auto max-w-7xl px-4 sm:px-6 py-8 print:max-w-none print:px-0 print:py-0">
```
```html
  <footer class="mx-auto max-w-7xl px-4 sm:px-6 pb-10 text-xs text-slate-400 print:hidden">
```

- [ ] **Step 4: Run the test**

Run: `py -m pytest tests/test_mobile_layout.py -v 2>&1 | tail -12`
Expected: 1 passed.

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
mobile: tighter container padding on phones

base.html main/footer px-6 -> px-4 sm:px-6 for more usable width on a narrow
screen; unchanged at sm and up.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/templates/base.html tests/test_mobile_layout.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 2: Nav — hamburger mobile menu

**Files:**
- Modify: `app/templates/partials/nav.html`
- Test: `tests/test_mobile_layout.py`

This is the main fix. **First READ `app/templates/partials/nav.html` in full** — it has a row of `{% set %}` link vars (`primary_links_left`, `sample_links`, `ad_spend_links`, `inventory_links`) and badge vars (`action_items`, `inv_alerts`, `overdue_ap`, `health_total`, `_user`) already computed near the top, then the desktop bar. The mobile menu **reuses those exact vars** (DRY) so no link is transcribed/lost.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mobile_layout.py`:
```python
def test_nav_has_desktop_bar_and_mobile_menu(client):
    html = client.get("/").text
    # Desktop links are gated to md+ ...
    assert "hidden md:flex" in html
    # ... and a md:hidden <details> hamburger provides the mobile menu.
    assert "md:hidden" in html
    assert 'id="mobile-menu"' in html
    # The mobile menu still surfaces the primary destinations.
    assert html.count('href="/reports/pnl"') >= 2   # desktop bar + mobile menu
    assert html.count('href="/reports/sales"') >= 2


def test_nav_mobile_menu_has_grouped_sections(client):
    html = client.get("/").text
    # Flattened dropdown group labels appear in the mobile menu.
    for label in ("Samples", "Ads", "Inventory"):
        assert label in html
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_mobile_layout.py -k nav -v 2>&1 | tail -15`
Expected: FAIL — no `hidden md:flex` / `id="mobile-menu"` yet.

- [ ] **Step 3: Gate the desktop bar to `md+`**

In `app/templates/partials/nav.html`, find the main links container — the `<div>` that holds all the nav links (the one with classes like `flex items-center gap-1 text-sm`, immediately after the logo `<a>`). Add `hidden md:flex` so it only shows at `md+`. Change its class from e.g.:
```html
    <div class="flex items-center gap-1 text-sm">
```
to:
```html
    <div class="hidden md:flex items-center gap-1 text-sm">
```
(Keep all its inner content exactly as-is — the desktop experience is unchanged.)

- [ ] **Step 4: Add the mobile hamburger + vertical menu**

Immediately AFTER that desktop `<div>` (still inside the outer `<div class="mx-auto flex ... ">`), add the mobile menu. It is a `md:hidden` native `<details>` (zero-JS toggle, the same element the Fiscal dropdown uses). Insert:

```html
      {# ── Mobile menu (hamburger → vertical tap-list). md:hidden; the desktop
         bar above is hidden md:flex. Reuses the same link {% set %} vars. ── #}
      <details id="mobile-menu" class="group md:hidden">
        <summary class="flex cursor-pointer list-none items-center rounded-md p-2 text-slate-600 hover:bg-slate-100">
          <span class="sr-only">Menu</span>
          {{ ui.icon("menu", "h-6 w-6 group-open:hidden") }}
          {{ ui.icon("x", "h-6 w-6 hidden group-open:block") }}
        </summary>
        <div class="absolute inset-x-0 top-full z-30 mt-1 max-h-[80vh] overflow-y-auto border-y border-slate-200 bg-white px-4 py-3 shadow-lg">
          {# Primary links #}
          {% for href, label in primary_links_left %}
          <a href="{{ href }}" class="block rounded-md px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100">{{ label }}</a>
          {% endfor %}
          <a href="/action-center" class="flex items-center justify-between rounded-md px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-100">
            <span>Action Center</span>
            {% if action_items > 0 %}<span class="ml-2 inline-flex h-5 min-w-[1.25rem] items-center justify-center rounded-full bg-rose-600 px-1 text-[10px] font-semibold text-white">{{ action_items }}</span>{% endif %}
          </a>

          {# Samples #}
          <div class="mt-2 px-3 pt-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">Samples</div>
          {% for href, label in sample_links %}
          <a href="{{ href }}" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">{{ label }}</a>
          {% endfor %}

          {# Ads #}
          <div class="mt-2 px-3 pt-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">Ads</div>
          {% for href, label in ad_spend_links %}
          <a href="{{ href }}" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">{{ label }}</a>
          {% endfor %}
          {% if _user and _user.role.value == 'admin' %}
          <a href="/admin/ad-budget" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">Ad Budget</a>
          {% endif %}

          {# Inventory #}
          <div class="mt-2 px-3 pt-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">Inventory</div>
          {% for href, label in inventory_links %}
          <a href="{{ href }}" class="flex items-center justify-between rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">
            <span>{{ label }}</span>
            {% if href == "/reports/demand-planning" and inv_alerts.count > 0 %}<span class="ml-2 inline-flex h-5 min-w-[1.25rem] items-center justify-center rounded-full bg-rose-600 px-1 text-[10px] font-semibold text-white">{{ inv_alerts.count }}</span>{% endif %}
          </a>
          {% endfor %}

          {# Import #}
          <a href="/uploads" class="mt-3 flex items-center gap-2 rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white">
            {{ ui.icon("upload", "h-4 w-4") }} Import
          </a>

          {# Admin section + sign-out (admin only) #}
          {% if _user %}
          {% if _user.role.value == 'admin' %}
          <div class="mt-2 px-3 pt-2 text-[10px] font-bold uppercase tracking-wider text-slate-400">Admin</div>
          <a href="/account" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">User Accounts</a>
          <a href="/admin/catalog" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">Product Catalog</a>
          <a href="/admin/tiktok" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">API Connection</a>
          <a href="/admin/tiktok-ads" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">TikTok Ad Spend</a>
          <a href="/admin/gmv-max-reimbursements" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">GMV Max Reimbursements</a>
          <a href="/admin/invoices" class="flex items-center justify-between rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">
            <span>Invoices &amp; AP</span>
            {% if overdue_ap.count > 0 %}<span class="ml-2 inline-flex h-5 min-w-[1.25rem] items-center justify-center rounded-full bg-rose-600 px-1 text-[10px] font-semibold text-white">{{ overdue_ap.count }}</span>{% endif %}
          </a>
          <a href="/reports/recon-health?tab=recon" class="block rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">Reconciliation</a>
          <a href="/reports/recon-health?tab=data-health" class="flex items-center justify-between rounded-md px-3 py-2 text-sm text-slate-700 hover:bg-slate-100">
            <span>Data Health</span>
            {% if health_total > 0 %}<span class="ml-2 inline-flex h-5 min-w-[1.25rem] items-center justify-center rounded-full bg-rose-600 px-1 text-[10px] font-semibold text-white">{{ health_total }}</span>{% endif %}
          </a>
          {% endif %}
          <form action="/logout" method="post" class="mt-2 border-t border-slate-100 pt-2">
            <button type="submit" class="block w-full rounded-md px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100">Sign out</button>
          </form>
          {% endif %}
        </div>
      </details>
```

NOTES for the implementer:
- The outer `<nav>`/container must be **`relative`** so the menu's `absolute inset-x-0 top-full` panel anchors correctly. If the container `<div class="mx-auto flex ...">` (or the `<nav>`) isn't already `relative`, add `relative` to it.
- Icons: `x.svg` is already committed; `menu.svg` is NOT but the source IS present locally. Vendor it: `cp node_modules/lucide-static/icons/menu.svg app/static/icons/menu.svg` (then it gets `git add`ed in Step 6). The icon-guard test requires a committed SVG for every `ui.icon()` reference, so this must be done before that test passes.
- Match the exact `{% set %}` var names + badge expressions to what nav.html actually defines (you read it in this task). If a var differs (e.g. `inv_alerts.count`), use the real one.
- Confirm `ui` is imported at the top of nav.html (`{% import "partials/ui.html" as ui %}`) — it is.

- [ ] **Step 5: Run the nav tests + icon guard + full render**

Run: `py -m pytest tests/test_mobile_layout.py -v 2>&1 | tail -20`
Expected: all pass.
Run: `py -m pytest -k "icon or nav" -q 2>&1 | tail -10`
Expected: pass (every `ui.icon()` has a committed SVG; nav still renders on every page).

- [ ] **Step 6: Commit**

`.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
mobile: nav hamburger menu (fixes the horizontally-scrolling navbar)

Desktop bar is now hidden md:flex; below md a md:hidden <details> hamburger
opens a full-width vertical tap-menu that reuses the same link vars, with the
dropdown groups flattened into labeled sections and all badges preserved.
Desktop unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/templates/partials/nav.html app/static/icons/ tests/test_mobile_layout.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 3: P&L single-month statement → phone-shaped

**Files:**
- Modify: `app/templates/reports/pnl.html`
- Test: `tests/test_mobile_layout.py`

The income statement is a `label · Amount · % of Net Sales` table rendered via the `line()` and `subtotal()` macros. Hiding the 3rd (`%`) column below `sm` makes it a clean two-column phone statement; labels already wrap (the label cell has no `whitespace-nowrap`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mobile_layout.py`:
```python
def test_pnl_percent_column_hidden_on_mobile(client):
    r = client.get("/reports/pnl")
    assert r.status_code == 200
    # The % column cells/header are gated to sm+ so mobile shows label + amount only.
    assert "hidden sm:table-cell" in r.text
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_mobile_layout.py::test_pnl_percent_column_hidden_on_mobile -v 2>&1 | tail -12`
Expected: FAIL — no `hidden sm:table-cell` yet.

- [ ] **Step 3: Hide the `%` column below `sm` + tighten padding**

In `app/templates/reports/pnl.html`:

1. The statement table header — the `<th>` for "% of Net Sales" (`<th class="py-2 pr-1 text-right">% of Net Sales</th>`) → add `hidden sm:table-cell`:
```html
          <th class="py-2 pr-1 text-right hidden sm:table-cell">% of Net Sales</th>
```

2. The `line()` macro's 3rd `<td>` (the `pct_of_sales` cell) → add `hidden sm:table-cell`:
```html
    <td class="py-1.5 pr-1 text-right text-xs tabular-nums text-slate-400 hidden sm:table-cell">{{ pct_of_sales(value) }}</td>
```

3. The `subtotal()` macro's 3rd `<td>` → add `hidden sm:table-cell`:
```html
    <td class="pt-3 pb-2 pr-1 text-right text-xs font-semibold tabular-nums text-slate-500 hidden sm:table-cell">{{ pct_of_sales(value) }}</td>
```

4. Tighten the statement card's horizontal padding for mobile — the header `<header class="border-b border-slate-200 bg-slate-50/60 px-6 py-3">` and the body `<div class="px-6 py-2">` → change `px-6` to `px-4 sm:px-6` on both.

(The `section()` macro's `<td colspan="3">` stays colspan=3 — browsers render a colspan that exceeds the visible column count as full-width, which is what we want.)

- [ ] **Step 4: Run the test + a P&L render regression**

Run: `py -m pytest tests/test_mobile_layout.py -v 2>&1 | tail -15`
Expected: all pass.
Run: `py -m pytest -k "pnl" -q 2>&1 | tail -10`
Expected: pass (P&L still renders for every scope).

- [ ] **Step 5: Commit**

`.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
mobile: P&L single-month statement is phone-shaped

Hide the "% of Net Sales" column below sm (line/subtotal macros + header) so
the income statement reads as a clean two-column label/amount layout on a
phone; labels wrap; tighten card padding to px-4 sm:px-6. Desktop unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/templates/reports/pnl.html tests/test_mobile_layout.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 4: Ad Spend / Sales / Dashboard polish

**Files:**
- Modify: `app/templates/reports/ad_spend.html`, `app/templates/reports/sales.html`, `app/templates/dashboard.html`
- Test: `tests/test_mobile_layout.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mobile_layout.py`:
```python
@pytest.mark.parametrize("url", ["/reports/ad-spend", "/reports/sales", "/"])
def test_target_pages_render_after_mobile_pass(client, url):
    assert client.get(url).status_code == 200


def test_ad_spend_uses_responsive_padding(client):
    # Ad Spend's heavy px-6 sections get a mobile-tighter variant.
    assert "px-4 sm:px-6" in client.get("/reports/ad-spend").text
```

- [ ] **Step 2: Run to verify it fails**

Run: `py -m pytest tests/test_mobile_layout.py -k "ad_spend or target_pages" -v 2>&1 | tail -12`
Expected: `test_ad_spend_uses_responsive_padding` FAILS (no `px-4 sm:px-6` in ad_spend yet); the render test passes.

- [ ] **Step 3: Ad Spend — responsive padding + wrap**

READ `app/templates/reports/ad_spend.html`. For the **section/card wrappers** that use `px-6` (the page header card, the KPI table card, the controls), change `px-6` → `px-4 sm:px-6`. For the **control bar(s)** (scope toggle + Fiscal dropdown + date-range form), ensure the containing flex row has `flex-wrap` so the controls wrap on a narrow screen (add `flex-wrap` if a control `<div class="... flex items-center gap-...">` lacks it). Do NOT change the tables (already `overflow-x-auto`). Make the minimal edits to get `px-4 sm:px-6` present and the controls wrapping.

- [ ] **Step 4: Sales — verify control bar wraps**

READ `app/templates/reports/sales.html`. Confirm the control bar (`<div class="mb-4 flex flex-wrap items-center gap-1 ...">`) already has `flex-wrap` (it does) and the date-range/fiscal forms use `flex-wrap` (they do). If any control row lacks `flex-wrap`, add it. No change is expected here beyond a verify; if nothing needs changing, note that and move on (do NOT invent edits).

- [ ] **Step 5: Dashboard — confirm sections stack**

READ `app/templates/dashboard.html`. The headline KPI grid is `grid-cols-2 md:grid-cols-5` (already 2-up on mobile — fine). Scan for any other section using a fixed multi-column grid (e.g. `grid-cols-3`/`grid-cols-4` or a flex row) **without** a mobile-friendly base. For any such section, add a mobile base so it stacks (e.g. `grid-cols-1 sm:grid-cols-2 lg:grid-cols-N`, or `flex-wrap`). If everything already has a mobile-sensible base, note that and make no change.

- [ ] **Step 6: Run the tests + render regression**

Run: `py -m pytest tests/test_mobile_layout.py -v 2>&1 | tail -15`
Expected: all pass.
Run: `py -m pytest -k "ad_spend or sales or dashboard" -q 2>&1 | tail -10`
Expected: pass.

- [ ] **Step 7: Commit**

`.git/COMMIT_MSG_DRAFT.txt` (Write tool):
```
mobile: Ad Spend padding + control wrap; Sales/Dashboard stacking verified

Ad Spend section padding px-6 -> px-4 sm:px-6 and control bars flex-wrap on
narrow screens. Sales + Dashboard confirmed to stack/wrap on mobile (minimal
or no change). Desktop unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
Then: `git add app/templates/reports/ad_spend.html app/templates/reports/sales.html app/templates/dashboard.html tests/test_mobile_layout.py && git commit -F .git/COMMIT_MSG_DRAFT.txt 2>&1 | tail -3`

---

## Task 5: Full suite + deploy + phone verification

**Files:** none (verification + ship)

- [ ] **Step 1: Full suite**

Run: `py -m pytest 2>&1 | tail -12`
Expected: all pass (prior baseline 861 + the new mobile-layout tests; 11 skipped).

- [ ] **Step 2: Desktop sanity (no regression)**

Locally or after deploy, confirm a couple of pages look identical on desktop (the changes are all `hidden md:flex` / `hidden sm:table-cell` / `px-4 sm:px-6`, which are no-ops at `md/sm+`). pytest already proved they render.

- [ ] **Step 3: Merge + deploy (local-merge, no PR)**

```bash
git push -u origin feature/mobile-formatting
git checkout main && git pull --ff-only
git merge --no-ff feature/mobile-formatting -m "Merge feature/mobile-formatting"
git push origin main
git branch -d feature/mobile-formatting && git push origin --delete feature/mobile-formatting
fly deploy
```
No schema change → the release `alembic upgrade head` is a no-op. Note: the Dockerfile rebuilds Tailwind CSS, so the new utility classes are included in prod's compiled stylesheet.

- [ ] **Step 4: Phone verification (USER — the real acceptance test)**

Ask the user to open prod on their phone and confirm:
1. The nav no longer scrolls sideways — the hamburger opens a vertical menu with every link.
2. P&L (single month) reads as a clean two-column statement (no horizontal scroll, no `%` column).
3. Dashboard, Sales, Ad Spend read cleanly (cards stack, controls wrap, tables scroll).
This is the acceptance gate — pytest cannot validate layout.

---

## Self-Review

**Spec coverage:**
- Nav hamburger mobile menu (desktop `hidden md:flex` + `md:hidden` `<details>`, flattened sections, badges preserved, reuses set vars) → Task 2. ✓
- App-wide `px-4 sm:px-6` → Task 1. ✓
- P&L single-month: `%` column hidden below `sm`, labels wrap, padding → Task 3. ✓ Multi-month grid keeps its existing `overflow-x-auto` (untouched; horizontal-scroll fallback) → unchanged, consistent with spec. ✓
- Ad Spend padding + control wrap → Task 4. ✓
- Sales control-bar wrap (verify) → Task 4. ✓
- Dashboard sections stack (verify/fix) → Task 4. ✓
- Testing: render + structural guards (nav both presentations, `%`-hidden, padding) → Tasks 1–4; phone eyeball → Task 5. ✓
- Out-of-scope (Reconciliation/admin pages, multi-month reflow, restyle) honored. ✓

**Placeholder scan:** No TBD/TODO; the nav markup is complete. Tasks 4's "verify, change only if needed" steps are explicit about not inventing edits. The icon step has a concrete vendor/fallback instruction.

**Type consistency:** The mobile menu reuses the SAME `{% set %}` vars the desktop bar defines (`primary_links_left`, `sample_links`, `ad_spend_links`, `inventory_links`) + badge vars (`action_items`, `inv_alerts.count`, `overdue_ap.count`, `health_total`, `_user.role.value`) — matched by reading nav.html in Task 2. Test markers (`hidden md:flex`, `id="mobile-menu"`, `hidden sm:table-cell`, `px-4 sm:px-6`) are produced by the exact edits in Tasks 1–4. ✓
