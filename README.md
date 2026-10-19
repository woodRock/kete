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

### Other banks and cards

Exports kete can't read on its own (another bank, a credit card that shows charges as positive
numbers, US-style dates) need their columns described once:

```sh
kete accounts add visa --type credit
kete import --map ~/Downloads/visa_statement.csv   # asks which column is which, then imports
```

The layout is saved in `~/kete/formats.toml`, matched by the file's header row, so later exports
import normally. Without prompts: `kete formats add visa --from FILE --date "Date" --amount
"Amount" --payee "Details" --negate`. A CSV whose dates look month-first is refused rather than
imported on the wrong dates.

### Automatic import (macOS)

`kete autoimport install` sets up a LaunchAgent that watches `~/Downloads` (or `--folder`). A bank
export downloaded there is moved into the inbox and imported within seconds, and a macOS
notification says how it went. Only files downloaded in the last day that look like exports (OFX,
or a CSV with date, amount and payee columns) are touched. A file whose account kete can't tell
stays in the inbox for `kete import`. `kete autoimport status` / `uninstall`. If nothing happens,
allow Python to read the folder in System Settings → Privacy & Security → Files and Folders.

### Cash and missing transactions

`kete accounts add cash --type other`, then `kete add cash -12.50 "Market stall" [--date] [-c Groceries]`.

## Commands

| Command | Does |
|---|---|
| `kete import [PATHS] [--account KEY]` | Import files (default: inbox). Prints new / duplicate / pending counts |
| `kete accounts add/edit/remove` | Manage accounts |
| `kete accounts` | Accounts with newest transaction, age and balance |
| `kete tx [-a everyday] [--from] [--to] [--cat] [-s TEXT] [-u]` | List transactions (`-u` = uncategorised) |
| `kete spend [--month 2026-10 \| --period P] [--compare] [--by cat\|payee]` | Spending for a month or any period (`2026`, `2026-01..2026-03`, `since 2026-02-01`, `last 90 days`, `tax 2026`); `--compare` adds a year earlier |
| `kete cashflow [--months 12]` | Income, spend, net per month |
| `kete subs [--all]` | Recurring charges |
| `kete rules apply` / `kete rules test "TEXT"` | Re-run categorisation / show the matching rule |
| `kete rules suggest [--apply]` / `rules decline PAYEE` | Rules learnt from payees you always categorise the same way by hand |
| `kete categorise CATEGORY ID... \| --search TEXT` | Categorise several transactions at once |
| `kete add ACCOUNT AMOUNT PAYEE` | Add a transaction by hand |
| `kete undo [--force]` / `kete history` | Undo the last change to categories, transfers, shares or rules; list recent changes |
| `kete bills` / `bills add NAME --match TEXT [--min] [--max] [--cadence]` / `bills remove NAME` | Bills you name yourself, for ones detection misses (several payee spellings, a new price) |
| `kete bills rename NAME NEW` / `bills cancel NAME [--undo]` | Rename any recurring payment; mark one cancelled |
| `kete owed` / `owed add ID NAME [AMOUNT]` / `owed list` / `owed remove ID` | Money others owe you on shared payments, and their repayments |
| `kete cycle` | This pay cycle: spent so far, bills due before payday, safe to spend |
| `kete export csv\|hledger\|beancount [--period P] [-o FILE]` | Export for an accountant or plain-text accounting |
| `kete formats` / `formats add` / `formats remove` | Saved CSV layouts for other banks and cards |
| `kete autoimport install\|status\|uninstall` | Import exports as soon as they're downloaded (macOS) |
| `kete categories` / `kete categories rename OLD NEW` | List categories / rename or merge one in transactions and rules.toml |
| `kete transfers detect [--apply]` / `unpair ID` | Pair moves between your accounts |
| `kete balance set ACCOUNT AMOUNT [--as-of]` | Anchor a balance (CSV exports carry none) |
| `kete stale [--days 30]` | Accounts that need a fresh export (exit code 2 if any) |
| `kete sql "SELECT …"` | Read-only SQL |
| `kete dashboard [--demo] [--no-open] [--out FILE]` | Build and open the local HTML dashboard |
| `kete report [--month M \| --year Y \| --tax-year Y] [--demo]` | Findings report for a period (default: last month) |
| `kete report dismiss ID` / `restore ID` | Hide or bring back a finding in future reports |
| `kete tui [--private]` | Terminal UI: Overview, Transactions, Categories, Uncategorised, Import log |

Every read command takes `--json`.

### TUI

The **Overview** shows your accounts with a 90-day balance trend, the current pay cycle and what's
safe to spend, spending by month (orange months are 15% or more above average), this month's
categories against a typical month, regular charges due in the next 14 days, and who owes you.
`v` swaps the monthly chart for a table.

| Keys | Where | Does |
|---|---|---|
| `1`–`5` | everywhere | Switch tabs |
| `c` | Transactions, Uncategorised | Set category (all selected rows, if any) |
| `space` / `a` / `x` | Transactions, Uncategorised | Select a row / select all shown / clear |
| `n` | Transactions, Uncategorised | Create a rule from the payee |
| `b` | Transactions, Uncategorised | Track this payment as a monthly bill, at about this amount |
| `s` | Transactions, Uncategorised | Shared: `Sam half`, `Sam 40`, `Sam 1/3`; on money in, who paid you back |
| `t` / `o` | Transactions, Uncategorised | Mark or unmark as a transfer between your own accounts (selected rows too) / raw row |
| `[` `]` / `g` | Categories | Previous or next month / any period, compared with a year earlier |
| Enter / Esc | Categories | Show a category's transactions / go back |
| `u` | everywhere | Undo the last change |
| `h` | everywhere | Hide amounts and payees, for screen sharing |
| `d` / `p` | everywhere | Open the dashboard / last month's report |

After you categorise a payee by hand the same way twice, the TUI offers to make it a rule. If
you're correcting another rule (say the own-account transfer rule caught a payment to someone
else's account), the new rule goes above it in `rules.toml` so it wins.

Westpac labels every online-banking transfer "WBC Internet / Transfer", including payments to
other people's accounts, and the starter rule treats them all as moves between your own
accounts. Those are left out of spending, subscriptions and Coming up. Unmark them with `t`, then
categorise them.

### Bills in Coming up

Coming up lists regular charges kete detects: the same payee, similar amounts, a regular
interval. Monthly bills paid by hand on different days are detected too (marked ≈), and a company
that charges two different amounts (an internet plan and a mobile top-up) is judged per amount.
For anything still missing, name it: `b` on one of its payments in the TUI, or

```sh
kete bills add Internet --match telco --min 80            # ignore the smaller mobile charges
kete bills add "AI app" --match aico --match aicorp --min 30  # two payee spellings
```

A named bill's next date is its latest payment (pending ones included) plus one cycle, and its
amount is the latest payment, so a price change shows straight away.

**Recurring tab** (`6`): everything that leaves your accounts regularly, biggest first, with the
monthly and yearly total split into bills, subscriptions and regular spending. Enter shows the
payments, `e` renames (only the name shown changes), `k` marks one cancelled.

**When something stops.** A recurring payment counts as stopped about two cycles after its last
charge (monthly: about 65 days), so one late payment or a gap between exports doesn't end it. It's
then dimmed and left out of totals, and that month's report says "X has stopped" with what you
save a year. Mark it cancelled (`k`, or `kete bills cancel NAME`) and it leaves the totals and
Coming up straight away; if it charges again, the Recurring tab shows it in red and the report
flags "charged you after you cancelled". **When a price changes**, the new price is used once two
charges agree on it (named bills use the latest payment straight away), and the report notes the
rise.

### Shared costs

Mark the part of a payment someone else owes (`s` in the TUI, or `kete owed add ID Sam half`).
Spending totals everywhere then count only your share. When they pay you back, mark that incoming
payment the same way so it isn't counted as income. `kete owed` shows who still owes what.

### Pay cycle

`kete cycle` finds your pay from regular income and groups spending payday to payday. "Safe to
spend" is your typical pay, minus what you've spent this cycle, minus subscriptions and bills
expected before the next payday, minus any savings you set aside:

```toml
[paycycle]
payee = "Acme Ltd"     # optional: whose payments are your pay
save_per_cycle = 300   # optional
```

## Dashboard

`kete dashboard` writes `~/kete/dashboard.html` and opens it in your browser (`d` in the TUI does
the same). It covers:
- spending by category per month, with a breakdown for any month or run of months (shift-click,
  or pick From/To), compared with the same months a year earlier;
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

## Reports

`kete report` checks a period (default: last complete month; `--year 2026`, or `--tax-year 2026`
for 1 April 2025 to 31 March 2026) and writes `~/kete/reports/<period>.html`. Each finding lists the
transactions behind it. The checks are:

- unusually large charges at a familiar merchant
- possible double charges
- price rises on regular charges
- new subscriptions
- large first-time payments to a new merchant
- categories well above or below your normal
- fees
- spending more than came in
- data gaps and uncategorised spending

Year and tax-year reports add a month-by-month chart and an interest summary (gross interest
and RWT withheld, read from Westpac's interest entries). Tax-year reports also total payments
categorised as Donations for the donation tax credit (IR526: 33⅓% of receipts of $5 or more). The
starter rules put anything mentioning "donation" there; set other categories with
`donation_categories = ["Donations", "Charity"]` under `[report]`. Dismiss a finding on the page, and run the
`kete report dismiss ID` command it shows to hide it in future reports too. Everything runs
locally, with the same no-network page as the dashboard. Tune the thresholds in `rules.toml`:

```toml
[report]
spike_ratio = 1.4          # flag a category at 1.4x its normal...
spike_min = 60             # ...when it's at least $60 over
unusual_multiple = 2.5     # a charge 2.5x a merchant's usual amount
large_new_merchant = 150   # first payments above $150 to a new merchant
```

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

## Export

`kete export csv|hledger|beancount [--period "tax 2026"] [-o FILE]`. In the ledgers, accounts are
`Assets:<Account>` (credit cards and loans `Liabilities:`), categories `Expenses:<Category>` and
`Income:<Category>`, money owed to you `Assets:Receivable:<Name>`, and a paired transfer is one
entry between your two accounts. Exported files are written readable only by you.

## Tests

`uv run pytest`. The fixtures in `tests/fixtures/` are synthetic rows in the exact Westpac shape,
with fake account and card numbers. Never commit real exports.

The tests also fail if any of kete's code imports networking modules, or if the dashboard or
report pages could make a request. GitHub Actions runs them on every push, along with a check that
no bank exports, databases or real-looking account numbers are committed.
