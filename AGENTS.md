# Working on kete (for AI agents)

kete is a local-first finance tool. The owner's real data lives in `~/kete` (or `$KETE_HOME`).

## Privacy rules: read these first

- **Never read, open, query or print anything from `~/kete`**: not `kete.db`, `rules.toml`,
  `archive/`, `dashboard.html`, `reports/`, and no `kete accounts/tx/spend/sql` or other commands that print
  real data. The owner chose kete so their finances never reach a model.
- Work against **synthetic data only**: `kete dashboard --demo`, `kete report --demo` and the tests.
- Never add network calls, analytics, CDNs or remote fonts. The dashboard's
  Content-Security-Policy (`default-src 'none'`) must stay, and tests check it.
- Never commit bank exports or databases. `.gitignore` blocks `*.csv`/`*.ofx`/`*.db` outside
  `tests/fixtures/`, and the fixtures are synthetic.

## Reports architecture

```
kete report [--month 2026-09 | --year 2026 | --tax-year 2026] [--demo] [--json]
```

| File | Role |
|---|---|
| `src/kete/report/detectors.py` | Detectors: `@detector` functions returning `Finding`s |
| `src/kete/report/__init__.py` | Periods, comparisons, tax interest, assembling the model, rendering |
| `src/kete/report/assets/report.{html,css,js}` | The page (reuses `dashboard/assets/style.css` tokens) |

To add a detector: write `def my_check(ctx: Context) -> list[Finding]` in `detectors.py` and
decorate it with `@detector`. Rules for findings:

- Give each one a **stable id** (detector + what it's about, e.g. `price:<merchant>:<date>`), so
  dismissals stick.
- List the transactions that justify it in `evidence`.
- Keep thresholds in `DEFAULT_CONFIG` (overridable from `[report]` in rules.toml).
- Plant an example in `kete/demo.py` (the "last complete month" block) and assert it in
  `tests/test_kete.py`.
- Guard against false positives on short histories: much of the data may start recently.

## Dashboard architecture

```
kete dashboard          -> ~/kete/dashboard.html (real data, opened in the browser)
kete dashboard --demo   -> $TMPDIR/kete-demo/dashboard.html (synthetic data from kete/demo.py)
kete dashboard --demo --json   -> prints the data model, for seeing its shape
```

| File | Role | Edit it to… |
|---|---|---|
| `src/kete/dashboard/model.py` | SQLite → one JSON-able dict (all analysis) | add metrics, insights, suggestions |
| `src/kete/dashboard/assets/template.html` | Page skeleton and sections | add or reorder sections |
| `src/kete/dashboard/assets/style.css` | Design tokens (light/dark), layout | restyle |
| `src/kete/dashboard/assets/app.js` | Renders the model with DOM + SVG, no libraries | add charts and interactions |
| `src/kete/dashboard/__init__.py` | Inlines model + assets into one file | rarely |
| `src/kete/demo.py` | Synthetic transactions | add patterns your feature needs to show |

The HTML is self-contained: `render()` replaces `/*__STYLE__*/`, `/*__SCRIPT__*/` and `__DATA__`
in the template. The page reads the model from `<script id="kete-data" type="application/json">`.

### The model (top-level keys)

`demo`, `today`, `config`, `windows` (`recent`, `baseline`, `complete` month lists), `series`
(up to 7 categories + `"Other"`, in colour-slot order), `kpis`, `monthly[]` (per month:
`status` complete|partial|incomplete, `income`, `spend`, `net`, `by_series`, `by_category`),
`categories[]` (`recent_avg`, `baseline_avg`, `change_pct`, `monthly[]`, `discretionary`,
`top_merchants`), `merchants[]`, `frequent_small[]`, `weekday[7]`, `daily[]`, `subscriptions[]`,
`opportunities[]` (`kind` cut|restore|avoid|cancel, `base_monthly`, `cut`, `enabled`,
`adjustable`; the saving is `base_monthly × cut`), `insights[]` (`tone` good|warn|info),
`projection` (`monthly_net`, `start_balance`), `data_quality`. Money is in dollars. Run
`kete dashboard --demo --json` for a full example.

### Rules for changes

- Analysis goes in `model.py` (Python, testable); `app.js` only presents. Keep suggestions
  explainable: every number shown should be traceable to transactions.
- Merchant and category names are untrusted. In JS, insert them with `textContent` or
  `createTextNode` (the `h()` / `s()` helpers do this), never `innerHTML`.
- Charts follow the palette in `style.css`: categorical `--s1`…`--s7` in fixed order by series
  index, `--other` for the fold, `--seq0..5` for magnitude, status colours only with an icon.
  One y-axis per chart, 2px lines, bars ≤ 24px with 4px rounded ends, 2px gaps between stacked
  segments, a tooltip on every mark, a table view for dense charts, and both light and dark themes.
- Respect `prefers-reduced-motion`. Only the first render animates.
- Personal settings belong in the `[dashboard]` table of the owner's `rules.toml`
  (see `DEFAULT_CONFIG` in `model.py`), not hardcoded.

## Checks before you finish

```sh
uv run pytest                         # includes dashboard model, CSP and escaping tests
uv run kete dashboard --demo          # open and look at it, in light and dark mode
```
