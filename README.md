# kete

Local-first personal finance CLI and TUI for Westpac NZ. It reads only files you export by hand
from Westpac One online banking. No credentials, no scraping, no aggregator.

## Quick start

```sh
uv sync
uv run kete import ~/Downloads/everyday_account.csv ~/Downloads/savings_account.csv
uv run kete transfers detect          # review proposed Savings -> Everyday pairs
uv run kete transfers detect --apply
uv run kete tui
```

Data lives in `~/kete` (override with `KETE_HOME`): `inbox/`, `archive/YYYY/`, `kete.db`, `rules.toml`.
`kete import` with no arguments reads the inbox and moves files to the archive; explicit paths
are copied, not moved.

## Exporting from Westpac One

Desktop browser only → Accounts → select the account → Export transactions → set From/To →
Format: OFX if offered, otherwise CSV (dd/mm/yyyy). Export every account in the same sitting,
with ranges overlapping the previous export by about a week.

## Accounts

Any number of accounts. `everyday`, `savings` and `credit` are built in (created the first time a
file uses them); add more with
`kete accounts add joint --label "Joint" --type savings` (types: everyday, savings, credit, loan,
other). How a file finds its account, in order:

1. OFX: the account number in the file.
2. `kete import --account joint FILE`.
3. Filename prefix: `joint_2026-10.csv` goes to `joint`.
4. Overlap: an unprefixed file that shares rows with exactly one account's earlier imports.
5. Otherwise kete asks. Last time's answer for that filename pattern is offered as the default,
   never applied silently, so two accounts with the same default export name can't be mixed up.
   Non-interactive runs skip the file with a warning.

`kete accounts edit KEY [--label] [--type] [--number] [--rename NEW]` and
`kete accounts remove KEY` (only if it has no transactions).

## Commands

| Command | Does |
|---|---|
| `kete import [PATHS] [--account KEY]` | Import files (default: inbox). Prints new / duplicate / pending counts |
| `kete accounts add/edit/remove` | Manage accounts |
| `kete accounts` | Accounts with newest transaction, age and balance |
| `kete tx [-a everyday] [--from] [--to] [--cat] [-s TEXT] [-u]` | List transactions (`-u` = uncategorised) |
| `kete spend [--month 2026-10] [--by cat\|payee]` | Spending for a month, transfers excluded |
| `kete cashflow [--months 12]` | Income, spend, net per month |
| `kete subs [--all]` | Recurring charges |
| `kete rules apply` / `kete rules test "TEXT"` | Re-run categorisation / show the matching rule |
| `kete categories` / `kete categories rename OLD NEW` | List categories / rename or merge one in transactions and rules.toml |
| `kete transfers detect [--apply]` / `unpair ID` | Pair moves between your accounts |
| `kete balance set ACCOUNT AMOUNT [--as-of]` | Anchor a balance (CSV exports carry none) |
| `kete stale [--days 30]` | Accounts that need a fresh export (exit code 2 if any) |
| `kete sql "SELECT …"` | Read-only SQL |
| `kete dashboard [--demo] [--no-open] [--out FILE]` | Build and open the local HTML dashboard |
| `kete tui` | Terminal UI: Overview, Transactions, Categories, Uncategorised, Import log |

Every read command takes `--json`.

TUI keys: `d` open the dashboard, `1`–`5` tabs, `c` set category, `t` toggle transfer, `n` create a rule from the payee,
`o` raw row, `[`/`]` change month on Categories, Enter drills down, `r` refresh, `q` quit.

## Dashboard

`kete dashboard` writes `~/kete/dashboard.html` and opens it in your browser (`d` in the TUI does
the same). It covers:
- spending by category per month, with a breakdown for any month;
- trends against your own baseline;
- patterns: a spending calendar, day of week, top merchants and frequent small purchases;
- **Where to cut**: suggestions you can tick and adjust, with live monthly and yearly savings;
- a **savings projection** comparing your current path with your chosen changes.

Everything is computed on your machine. The file is a single page with no network access
(enforced by a Content-Security-Policy) and is saved with owner-only permissions.

Tune it in `rules.toml`:

```toml
[dashboard]
discretionary = ["Takeaways", "Eating out", "Entertainment", "Shopping", "Transport"]
fixed_costs = ["Rent", "Utilities", "Groceries", "Insurance"]
default_cut = 0.25       # suggested cut for discretionary categories
interest_rate = 0.03     # used by the projection
savings_goal = 10000     # draws a goal line; 0 = none
```

`kete dashboard --demo` shows the same page with synthetic data. Agents changing the
dashboard should use it, and read [AGENTS.md](AGENTS.md).

## What the Westpac CSV looks like (verified Oct 2026)

```
Date,Amount,Other Party,Description,Reference,Particulars,Analysis Code
```

- Header row present; **no account number and no running balance**, so the account comes from
  the filename and balances come from `kete balance set` (or OFX, if Westpac offers it).
- `Description` is the transaction type (DEBIT, DIRECT CREDIT, ONLINE BANKING, BILL PAYMENT,
  Credit Interest). The merchant is `Other Party`, stored as `payee`.
- Card purchases: `Particulars` = masked card number, `Analysis Code` = card last 4, and Westpac
  appends a day number to the merchant (`Some Merchant 21`); `subs` and `spend --by payee` strip it.
- **Pending card transactions are included** as `EFTPOS TRANSACTION` rows dated the export day,
  with a `dd/mm hh:mm` reference and text like `UBER * PENDING`. They post later as DEBIT rows with
  different text and sometimes a different amount. kete flags them `pending` and deletes them
  when a later export covers that date, so they are never double counted.
- Own-account transfers carry `Particulars = WBC Internet`, `Analysis Code = Transfer`; the
  starter rules mark these as transfers.

## Dedupe

- OFX: `(account, FITID)`.
- CSV: SHA-1 of account, date, cents, normalised text fields, and an occurrence index, so two
  identical $4.50 purchases on one day are both kept.
- Identical files are skipped by SHA-256. CSV↔OFX overlap falls back to date + amount + payee.

## Tests

`uv run pytest`. The fixtures in `tests/fixtures/` are synthetic rows in the exact Westpac shape,
with fake account and card numbers. Never commit real exports.
