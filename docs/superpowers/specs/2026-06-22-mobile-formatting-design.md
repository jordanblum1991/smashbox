# Mobile Formatting Fix — Design

**Date:** 2026-06-22
**Status:** Approved (design)

## Problem

The app is used on a phone and the formatting is poor. The biggest concrete
symptom (user-confirmed): the **nav bar scrolls horizontally** — it's a single
non-wrapping flex row of ~9 items (Dashboard · P&L · Sales · Action Center ·
Samples▾ · Ads▾ · Inventory▾ · Import · user chip) that overflows a phone width.
Wide report tables also overflow.

The viewport meta tag is already correct (`width=device-width, initial-scale=1`),
so this is a per-component responsiveness gap, not a global misconfiguration.

## Scope (from brainstorming)

- **Shared nav** (helps every page) — the priority.
- **Dashboard**, **P&L** (`/reports/pnl`), **Sales** (`/reports/sales`), **Ad
  Spend** (`/reports/ad-spend`) — the pages the user opens on mobile.
- **Out of scope:** Reconciliation + admin/upload pages (not used on mobile); any
  non-responsive restyle. This is a responsiveness pass only.

## Current state (measured)

| Page | State | Gap |
|---|---|---|
| Nav | flat horizontal row, ~2 responsive classes, no mobile menu | **no mobile collapse → horizontal scroll** |
| `base.html` | `<main>`/`<footer>` use `px-6` | a bit wide on a phone |
| P&L | 4 `<table>`, only 1 `overflow-x-auto` wrapper, 4 `whitespace-nowrap` | 3 tables can force the page wider than the screen |
| Ad Spend | 3 tables all already wrapped in `overflow-x-auto`; 15 `px-6` | heavy section padding; verify control bar wraps |
| Sales | cards `grid-cols-2 sm:grid-cols-3 lg:grid-cols-5`, table wrapped | already responsive; verify control bar wraps |
| Dashboard | KPI grid `grid-cols-2 md:grid-cols-5`, no tables | mostly fine; confirm every section stacks |

## Design

### 1. Shared nav — mobile hamburger menu (`app/templates/partials/nav.html`)

The single biggest fix. Restructure into two presentations:

- **`md+` (desktop):** the existing horizontal bar, unchanged, wrapped so it only
  shows at `md` and up (`hidden md:flex`). No behavior change on desktop.
- **`< md` (mobile):** a compact bar with the **logo + a hamburger button**. The
  hamburger is the `<summary>` of a `md:hidden` native **`<details>`** element
  (zero JS, touch-friendly — the same `<details>` pattern the Fiscal dropdown
  already uses); opening it reveals a **full-width vertical menu**.

The vertical menu contains **every destination**, with the hover dropdowns
**flattened into labeled sections** so touch users don't need hover:
- Primary links: Dashboard, P&L, Sales, Action Center (with its count badge).
- **Samples** section → Sample Report (+ the disabled "By Creator").
- **Ads** section → Ad Spend Report, Ad Spend Reimbursements, Ad Budget (admin).
- **Inventory** section → Inventory Report, Demand Planning (+ reorder badge),
  Planner Accuracy, Purchase Orders.
- **Import** action.
- **Admin** section (only when `_user.role == 'admin'`): User Accounts, Product
  Catalog, API Connection, TikTok Ad Spend, GMV Max Reimbursements, Invoices & AP
  (+ overdue badge), Reconciliation, Data Health (+ issue badge) — i.e. the items
  currently in the top-right user dropdown.
- The user chip + **Sign out**.

All badges (`action_items`, `inv_alerts`, `overdue_ap`, `health_total`) and the
admin gate are preserved. No links are lost — they're stacked and tappable. The
`{% set %}` context vars (`_user`, badge counts) are already defined at the top of
nav.html, so both presentations read them.

### 2. App-wide container padding (`app/templates/base.html`)

`<main>` and `<footer>`: `px-6` → `px-4 sm:px-6`. More usable width on a phone,
unchanged at `sm+`. One change, every page benefits.

### 3. P&L — phone-shaped (`app/templates/reports/pnl.html`)

The user reads a **single month** on mobile, so make the **income statement**
itself phone-shaped (not a wide scroll), and let the rarely-used multi-month grid
fall back to scroll.

- **Single-period income statement** (the `label · Amount · % of Net Sales` table,
  rendered via the `line()` / `subtotal()` macros): the **"% of Net Sales" 3rd
  column is hidden on mobile** — mark its `<th>` and each row's `%` `<td>`
  `hidden sm:table-cell`. So on a phone it's a clean two-column statement:
  **label left, amount right**. Let long labels **wrap** (the label cell must not
  be `whitespace-nowrap`; keep the amount cell `whitespace-nowrap` + right-aligned
  so figures never break). Reduce cell padding on mobile (`px-3 sm:px-6` where
  cells use wide padding). The nested fee-detail + adjustments sub-tables (also
  `line()`-based, label+amount) inherit the same treatment. Result: a readable
  narrow statement with **no horizontal scroll**.
- **Multi-month comparison table** (year/YTD/range, where each *column* is a
  month): wrap in `<div class="overflow-x-auto">` as a **horizontal-scroll
  fallback** — it stays a wide grid (rarely opened on a phone; reflow to per-month
  cards is explicitly out of scope per the user's "single month" answer).
- The trend charts already render responsively (inline-SVG viewBox).

### 4. Ad Spend (`app/templates/reports/ad_spend.html`)

Tables already scroll. Reduce the heavy section/card `px-6` to `px-4 sm:px-6`, and
ensure the scope toggle + Fiscal dropdown + date-range form sit in `flex-wrap`
containers so they wrap onto multiple rows on a narrow screen instead of
overflowing.

### 5. Sales (`app/templates/reports/sales.html`)

Already responsive. Verify the control bar (granularity toggle + Fiscal ▾ +
date-range form) is `flex-wrap` and doesn't overflow at ~360px; adjust only if it
does. The chart + table already handle narrow widths.

### 6. Dashboard (`app/templates/dashboard.html`)

Confirm every section collapses to 1–2 columns on mobile (the headline KPI grid is
already `grid-cols-2`). Fix any section using a fixed multi-column grid without a
mobile breakpoint so it stacks.

## Testing

- **pytest (regression):** every touched page (`/`, `/reports/pnl`,
  `/reports/sales`, `/reports/ad-spend`) still returns 200 and renders without a
  Jinja error after the nav restructure.
- **Structural guard:** a test asserting the rendered nav contains the mobile
  menu markup (a `md:hidden` `<details>` hamburger) AND still contains the
  `md:`-gated desktop bar — so a future edit can't silently drop the mobile menu.
- **The real acceptance test is the user's eyeball on a phone** (per the
  "HTTP 200 ≠ visual verification" rule). After each piece, the user checks it on
  prod at a real phone width; pytest cannot validate layout.

## Out of scope (YAGNI)

- Reconciliation + admin/upload pages.
- Reflowing the P&L **multi-month** grid into per-month cards (user reads a single
  month on mobile; that grid gets horizontal-scroll fallback only).
- A JS framework / Alpine — the `<details>` hamburger is zero-JS.
- Any visual restyle beyond making the existing design fit a phone.

## Success criteria

1. The nav no longer scrolls horizontally on a phone — it's a hamburger →
   vertical tap-menu below `md`, unchanged on desktop, with every link + badge
   preserved.
2. P&L's single-month income statement reads as a clean two-column (label /
   amount) phone layout — no horizontal scroll, % column hidden, labels wrap; the
   multi-month grid scrolls within the page as a fallback.
3. Dashboard, Sales, and Ad Spend read cleanly at ~360px (cards stack, controls
   wrap, tables scroll).
4. Full pytest suite green; user confirms on their phone.
