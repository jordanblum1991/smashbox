/** @type {import('tailwindcss').Config} */
module.exports = {
  // Scan every template + the grid helper so no utility class is purged.
  // Adjust the templates path if yours isn't ./app/templates.
  content: [
    "./app/templates/**/*.html",
    "./app/static/js/**/*.js",
  ],

  theme: {
    extend: {
      // Mirrors the inline tailwind.config that was in base.html.
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
      },
    },
  },

  // ── SAFELIST ──────────────────────────────────────────────────────────
  // The Play CDN compiled whatever classes showed up at runtime, so classes
  // your templates BUILD by string interpolation worked automatically —
  // e.g. monthly_pnl.html does `border-{{ sev }}-200 bg-{{ sev }}-50
  // text-{{ sev }}-900` where sev is "rose" or "amber". A real build step
  // can't see those (the scanner only matches whole literal class names),
  // so without this safelist they'd silently disappear and those banners
  // would render unstyled.
  //
  // This pattern keeps the semantic-palette utilities across the shades/
  // utilities the app constructs dynamically. It adds a few KB; the cleaner
  // long-term fix is to replace interpolated classes with full literal
  // strings (a lookup dict of complete class strings, like ui.html already
  // does for its tone macros) and then shrink or delete this safelist.
  safelist: [
    {
      pattern: /^(bg|text|border|ring|from|via|to)-(rose|amber|emerald|sky|indigo|slate)-(50|100|200|300|400|500|600|700|900)$/,
    },
  ],
};
