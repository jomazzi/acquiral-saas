# Acquiral SaaS — Multi-Tenant Architecture (Phases 1–7)

## Tenancy model

Every customer of Acquiral is an `Organization` row (`app/models/tenant.py`).
Everything else that belongs to a customer — chart of accounts, funders,
projects, journal entries/lines, and (in later phases) fixed assets,
employees, payroll, invoices — carries an `organization_id` foreign key,
added automatically by inheriting `TenantScopedMixin`.

Isolation between tenants is enforced at the **database layer** using
PostgreSQL Row-Level Security (RLS), not just by the application adding a
`WHERE organization_id = ...` clause to every query. This means even a
forgotten filter, a raw SQL query, or a bug in a future feature cannot
leak another tenant's data — Postgres itself refuses to return or modify
rows outside the current tenant.

## How tenant context is applied

1. The app connects as `acquiral_app`, an ordinary (non-superuser,
   non-BYPASSRLS) Postgres role. **This is required** — RLS has zero
   effect for superusers or BYPASSRLS roles, and a table's own owner
   bypasses RLS by default too (which is why every policy is created
   with `FORCE ROW LEVEL SECURITY`, not just `ENABLE`).
2. `app/__init__.py` defines `set_tenant(organization_id)`, which runs
   `SET LOCAL app.current_tenant = '<uuid>'`. `SET LOCAL` scopes the
   setting to the *current transaction only* — it automatically reverts
   once that transaction commits or rolls back.
3. `set_tenant()` is called explicitly, by hand, in two places:
   - Once per request, in `before_request`, for the logged-in user's org.
   - Again, manually, anywhere a route commits mid-request and then needs
     to run more tenant-scoped queries afterward (e.g. `auth/routes.py`
     `signup()`, which must commit the new `Organization` and `User` rows
     — to get their generated ids — before it can seed that org's chart
     of accounts).

   This is **deliberately not wired up via an ORM event hook**
   (`after_begin` etc.) — an earlier attempt at that had unpredictable
   firing timing relative to when queries actually ran, which is a bad
   trade for something security-critical. An explicit call fails loudly
   (RLS denies everything if you forget it) rather than silently applying
   stale or wrong context.

4. Every RLS policy (`scripts/setup_rls.sql`) reads that setting via
   `NULLIF(current_setting('app.current_tenant', true), '')::uuid`. The
   `NULLIF` matters: after a transaction ends, Postgres resets a
   `SET LOCAL`-only custom GUC to `''` (empty string), **not** `NULL`. A
   bare `::uuid` cast of `''` raises an error; wrapping it in `NULLIF`
   turns that into a real `NULL` first, which then compares false
   against every row's `organization_id` — so a forgotten or expired
   tenant setting safely **denies all access**, rather than crashing or
   (far worse) silently allowing everything through.

## The `users` table is NOT row-secured — on purpose

Logging in requires looking up a `User` row to discover which
organization they belong to. If `users` were row-secured, that lookup
itself would need `app.current_tenant` already set — which requires
knowing the user first. That's circular.

The standard way real multi-tenant systems handle this: **exclude the
identity table from RLS** and enforce isolation there by hand, in
application code, instead.

Concretely:
- `scripts/setup_rls.sql` ends with `ALTER TABLE users DISABLE ROW LEVEL
  SECURITY` and does **not** create a policy for it.
- The `User` model's docstring in `app/models/tenant.py` repeats this
  warning.
- **Every route that queries `User` (other than the login lookup itself,
  which by definition doesn't have a tenant yet) MUST filter by
  `organization_id` by hand.** See `app/blueprints/auth/routes.py` —
  `users_list`, `user_toggle`, `user_reset_password` all do
  `.filter_by(organization_id=current_user.organization_id, ...)`
  explicitly. Grep for `User.query` before adding a new route that
  touches users.

`Organization` itself is also not row-secured, for the obvious reason
that it's the tenant table, not a tenant-scoped one — `signup()` has to
be able to create a new one with no tenant context at all.

## Adding a new tenant-scoped table

1. Inherit `TenantScopedMixin` (adds `organization_id` + index
   automatically).
2. Add a matching block to `scripts/setup_rls.sql` — either add the table
   name to the `ARRAY[...]` in the existing `DO $$ ... $$` block (it
   loops through and applies `ENABLE`/`FORCE`/`CREATE POLICY`
   identically to all of them), or write one by hand if it needs a
   different policy shape.
3. Always set `organization_id=current_user.organization_id` explicitly
   when constructing a new row — RLS's `WITH CHECK` clause will reject an
   `INSERT`/`UPDATE` that doesn't match the current tenant context, which
   is the correct behavior (it means a bug that forgot to set it fails
   loudly instead of inserting into the wrong tenant).
4. Run `flask db migrate -m "..."` / `flask db upgrade`, then re-apply
   `scripts/setup_rls.sql` (`sudo -u postgres psql -d <db> -f
   scripts/setup_rls.sql` — it's idempotent).
5. Re-run `scripts/rls_adversarial_test.py` after any schema change that
   touches tenant-scoped tables.

## Known fixes baked into this build

- `expire_on_commit=False` is passed directly to the `SQLAlchemy()`
  constructor in `app/extensions.py` (Flask-SQLAlchemy 3.x does **not**
  read a `SQLALCHEMY_SESSION_OPTIONS` config key — confirmed by testing;
  setting it in `config.py` is silently ignored). Without this, touching
  an attribute on a committed object triggers a fresh, silent `SELECT` in
  a brand-new transaction — which needs tenant context re-applied, and a
  silent extra query is an easy place to forget that.
- Flask-Login's `get_id()`/`user_loader` contract passes/returns a
  **string**, but primary keys are UUID objects. `db.session.get(User,
  user_id)` does not coerce the string for you — it silently returns
  `None` on the mismatch, which looks exactly like "wrong password"
  rather than a bug. `app/__init__.py`'s `load_user()` casts explicitly
  with `uuid.UUID(user_id)`.
- Multi-currency exchange-rate math (ported from the single-tenant app,
  see `app/blueprints/accounting/routes.py: journal_new`): never round
  the intermediate native-currency amount before converting to base
  currency — round only the final displayed/summed totals. Rounding the
  intermediate value first introduced up to ~₦1.20 mismatches on odd
  exchange rates.

## Testing

- `scripts/smoke_test.py` — functional test through the Flask test
  client: two organizations sign up independently, create their own
  data (funders, accounts, a fixed asset bought on payable, a
  depreciation run, an employee, a full payroll run generated AND
  posted), and are confirmed unable to see each other's data through
  ordinary application routes, including direct-object-reference
  attempts by guessed/known UUID against funders and payroll runs.
- `scripts/rls_adversarial_test.py` — bypasses the ORM and the
  application entirely, issuing raw SQL as the `acquiral_app` role, to
  prove isolation is enforced by Postgres itself. Runs the same three
  attacks against `funders`, `fixed_assets`, `employees`, `payroll_runs`,
  `customers`, `invoices`, `invoice_lines`, `bank_statement_imports`,
  and `bank_transactions`:
  1. An unfiltered `SELECT * FROM <table>` under the wrong tenant's
     context returns zero rows from the other tenant, even with no
     `WHERE organization_id` clause written at all.
  2. `SELECT ... WHERE id = <known other-tenant row id>` under the wrong
     tenant context returns zero rows (blocks direct-object access, not
     just list filtering) — with a sanity check that the same query
     *does* return the row under its own tenant's context.
  3. `SELECT` with **no tenant context set at all** returns zero rows
     (proves the `NULLIF` fix denies-by-default rather than erroring or
     silently allowing everything).

Run both after any change to models, routes, or `setup_rls.sql`.

## Phase 3: Fixed Assets & Payroll

Adds `fixed_assets`, `employees`, `payroll_runs`, `payslips` — all
`TenantScopedMixin`, all RLS-protected identically to the Phase 2 tables
(same `ARRAY[...]` loop in `scripts/setup_rls.sql`).

This phase was flagged in advance as the highest-risk area for a dropped
tenant-context bug, because these routes are **multi-step, multi-commit**
in a way Phase 2's weren't:

- `assets.asset_new`: insert `FixedAsset` → `flush()` → build a
  `JournalEntry` referencing it → `flush()` again → `commit()`.
- `assets.run_depreciation`: loop over every asset, build one combined
  journal entry, `commit()`.
- `payroll.payroll_new`: insert `PayrollRun` → `flush()` → loop over every
  active `Employee`, insert one `Payslip` each → `commit()`.
- `payroll.payroll_post`: build a journal entry from all of a run's
  payslips → `flush()` → `commit()`.

None of these call `set_tenant()` themselves — they rely entirely on the
single `before_request` call having set `app.current_tenant` for the
whole request, and on that setting surviving across the route's own
internal `flush()`/`commit()` calls within that request's transaction.
This is exactly the scenario the adversarial tests below were extended to
cover, and it holds: RLS isolation is per-transaction via `SET LOCAL`,
and Flask-SQLAlchemy's request-scoped session only starts a genuinely new
transaction after a full `commit()` ends the previous one — at which
point `before_request` isn't going to run again mid-request, so this
pattern only stays safe as long as each request either does all its
tenant-scoped work in one transaction, or (like `auth.signup`) calls
`set_tenant()` again explicitly after any mid-request commit. Every
multi-commit route in this phase does all of its writes as flushes within
a single transaction, committing only once at the end — so this doesn't
come up in practice yet, but it's the thing to check first if a future
route commits more than once per request.

The multi-currency "don't round the intermediate amount" fix (see below)
is also reused here: both `asset_new` (buying an asset from a
foreign-currency account) and `payroll_post` (paying net salaries from
one) compute `native_amount = amount / rate` without rounding it before
using it in the journal line.

## Phase 4: Invoicing & Payslip PDFs

Adds `customers`, `invoices`, `invoice_lines` (`app/models/invoicing.py`) —
all `TenantScopedMixin`, all RLS-protected identically to every prior
tenant-scoped table (same `ARRAY[...]` loop in `scripts/setup_rls.sql`).
Also adds account `2230 VAT Payable` to the seeded chart of accounts.

**Customer vs Funder — deliberately separate entities.** A `Funder`
(Phase 2) is who gives an NGO grant money; a `Customer` is who Acquiral
bills for commercial/consulting work (e.g. Admiral Sentinel's freelance
clients). Conflating them would force one side's fields/workflow onto the
other as the product grows, so they're modeled and RLS-protected as two
independent tables from the start, at the user's explicit request.

**Invoice lifecycle and journal postings — draft is free, send/pay are
not.** An `Invoice` starts as `draft`: fully editable, discardable, with
*zero* accounting impact — no journal entry exists yet. Only two actions
touch the general ledger:
- **Send** (`invoicing.invoice_send`): posts `Dr Accounts Receivable
  (2300)` / `Cr Service Income (4200)` for the subtotal, plus
  `Cr VAT Payable (2230)` for the VAT amount if the invoice has a nonzero
  `vat_rate`. Status flips `draft -> sent`.
- **Record payment** (`invoicing.invoice_record_payment`): posts
  `Dr Bank/Cash` / `Cr Accounts Receivable (2300)` for the invoice total.
  Status flips `sent -> paid`. Reuses the same multi-currency
  "don't round the intermediate `native_amount` before converting" rule
  as payroll/asset postings, for a foreign-currency receiving account.

A **draft** invoice can be voided directly (`invoicing.invoice_void`,
admin-only) since it has no postings to reverse. A **sent** or **paid**
invoice cannot be voided from the Invoicing screen — it already has real
journal entries, so undoing it needs a deliberate reversing entry via the
Journal module, not a silent status flip that would orphan postings.

VAT is modeled as a **per-invoice field** (`Invoice.vat_rate`), not
hardcoded — it defaults to `DEFAULT_VAT_RATE = 7.5` (Nigeria's standard
rate) but can be overridden or zeroed per invoice (e.g. VAT-exempt
customers/services).

**Invoice numbering** (`next_invoice_number()` in
`app/models/invoicing.py`) counts existing invoices for the tenant and
formats `INV-0001`, `INV-0002`, etc. — scoped per-organization, so two
tenants both have their own `INV-0001`.

### Payslip & Invoice PDF generation

Both are generated with **reportlab** (`app/pdf/common.py`,
`invoice_pdf.py`, `payslip_pdf.py`), not WeasyPrint or wkhtmltopdf.
reportlab is pure-Python with no system library/binary dependency, so the
generated-PDF feature runs unmodified on the single-tenant app's
documented Windows *and* Linux deployments — a system-library dependency
(WeasyPrint needs Pango/Cairo/GDK-Pixbuf; wkhtmltopdf needs its own
binary) would work in this cloud container but break or need extra setup
on a plain Windows install. This reasoning is also recorded as a code
comment in `app/pdf/common.py`.

Both PDFs are built with `reportlab.platypus` (`SimpleDocTemplate`,
`Table`, `Paragraph`) styled to match the app's navy/gold brand palette,
and streamed back via Flask's `send_file(..., as_attachment=True)` —
`payroll.payslip_pdf` and `invoicing.invoice_pdf` respectively. Neither
route requires any additional tenant-scoping beyond the normal
`.filter_by(id=...).first_or_404()` RLS-backed lookup — a payslip/invoice
belonging to another tenant simply 404s before PDF generation is ever
reached (verified in `scripts/smoke_test.py`).

### UI: left sidebar + responsive hamburger menu

`app/templates/base.html` was restructured from a top navbar to a
persistent left sidebar at desktop widths (`≥992px`, Bootstrap's `lg`
breakpoint) that collapses into an off-canvas panel triggered by a
hamburger button on a slim mobile top bar below that width — built with
Bootstrap 5.3.3's `offcanvas-lg` component, which needs zero custom JS to
switch behavior at the breakpoint. Two non-obvious things had to be fixed
during this rebuild, both found only by actually rendering the page with
Playwright screenshots rather than by reading the markup:
- Bootstrap's own responsive-offcanvas CSS resets `.offcanvas-lg` to
  `background-color: transparent !important` once it becomes a static
  column at `≥992px` (by design, so an "always open" offcanvas blends
  into the page) — this silently overrode the sidebar's own background
  color. Fixed with a matching `!important` inside the same
  `@media (min-width: 992px)` block.
- The mobile top bar must sit **outside** `.app-shell` (the flex-row
  container holding the sidebar and main content), not as a sibling
  inside it — a flex row's default `align-items: stretch` stretches any
  child without an explicit height to fill the full cross-axis, which
  turned the intended slim horizontal bar into a full-height invisible
  third "column" pushing its own content off-screen.

Bootstrap and Bootstrap Icons are now vendored locally under
`app/static/vendor/` (`npm install bootstrap bootstrap-icons`, copied in)
rather than loaded from a CDN — this both fixed CDN access issues in the
build/test environment and removes a third-party runtime dependency,
which matters given variable internet reliability at the NGO's
Port Harcourt location.

## Testing (updated)

`scripts/smoke_test.py` and `scripts/rls_adversarial_test.py` were
extended for this phase to cover `customers`, `invoices`, and
`invoice_lines` with the same coverage as every prior tenant-scoped
table: ordinary-route tenant isolation, direct-object-reference 404s on
known UUIDs, the full draft → send → paid lifecycle (asserting the actual
journal postings happen and the status transitions correctly), real PDF
byte-content checks (`%PDF` magic bytes, `200`/`application/pdf`) for
both invoice and payslip downloads, and all three raw-SQL RLS attacks
(unfiltered select, known-id lookup under the wrong tenant, no tenant
context at all). All pass with zero regressions to prior phases.

## Phase 5: Bank Transactions & Reconciliation

Adds `bank_statement_imports` and `bank_transactions`
(`app/models/banking.py`) — both `TenantScopedMixin`, RLS-protected
identically to every prior table. Deliberately **not** in scope, per the
agreed roadmap: live bank-API connections and image/OCR extraction — a
PDF is parsed by reading its actual text/table structure, never by
rasterizing and OCR'ing the page, so a scanned image-only PDF will not
parse. This is intentional: OCR introduces silent transcription errors,
which is a bad trade in financial data.

### Import: CSV / Excel / PDF only

`app/banking/statement_parser.py` normalizes all three formats to the
same shape — a list of `{date, description, amount}` rows, where
`amount` is **signed** (positive = money in, negative = money out).
- **CSV**: Python's built-in `csv` module.
- **Excel (.xlsx)**: `openpyxl`.
- **PDF**: `pdfplumber`'s table extraction, tried with its default
  ruled-line strategy first, then falling back to a text-alignment
  strategy for statements with no visible grid.

Column headers are matched against common aliases (`Date`/`Transaction
Date`/`Value Date`; `Description`/`Narration`/`Particulars`;
`Debit`/`Withdrawal` and `Credit`/`Deposit`, or a single signed
`Amount` column) — covering the shape of most Nigerian bank statement
exports (GTBank, Zenith, UBA, Access, First Bank, etc.) without needing
per-bank templates. Rows that don't parse as a valid date (opening-
balance lines, subtotals, footers) are silently skipped rather than
erroring the whole import.

**Known limitation, documented rather than hidden**: the PDF
text-alignment fallback (used only when a statement has no ruled grid)
is a heuristic and was found, in testing, to occasionally misjudge a
column boundary on a sparse row (e.g. one where Debit is blank) and
silently drop that row. This is not a correctness risk to the books
themselves — a parsed row only ever becomes an `unmatched`
`BankTransaction` sitting in the reconciliation queue for a human to
review; nothing is ever posted to the journal automatically from an
import. A dropped row just means the reconciliation won't balance
against the real bank balance, which is exactly the kind of discrepancy
reconciliation exists to surface — the fix is to re-import from CSV/
Excel (deterministic, no guessing), which the import screen recommends.

### Reconciliation: three ways to resolve an unmatched row

`app/blueprints/banking/routes.py`, per bank account:
1. **Match to an existing journal line** (`match_existing`) — for a
   transaction already recorded manually before the statement arrived.
   Only journal lines against that same account, not already linked to
   another bank transaction, are offered as candidates.
2. **Post a new journal entry and match it** (`match_new`) — for
   anything with no counterpart yet (bank charges, interest, an
   unrecorded deposit). A deposit (positive amount) debits the bank
   account and credits the chosen contra account; a withdrawal credits
   the bank account and debits the contra account.
3. **Ignore** (`ignore_txn`) — for duplicate rows or statement lines
   that shouldn't hit the books at all. Reversible (`unmatch_txn`),
   which only removes the reconciliation link — it never deletes or
   reverses a journal entry a match may have created; that goes through
   the Journal module like any other correction.

`BankTransaction.matched_journal_line_id` is a `UNIQUE` foreign key to
`journal_lines.id`, so a journal line can never be claimed by two bank
transactions at once — enforced at the database level, not just by the
route's own candidate-list query.

## Phase 6: Cloud Deployment

Packaged as a standard Docker image (`Dockerfile`) so the same artifact
runs on Render (the recommended host — see `DEPLOYMENT.md`), Railway,
Fly.io, DigitalOcean App Platform, or a plain VPS, with only the
environment-variable setup differing between them.

**Config now has an explicit production mode**, selected via
`APP_ENV=production` (`config.py`'s `get_config()`, used by
`app/__init__.py`):
- `ProductionConfig` turns on `SESSION_COOKIE_SECURE`,
  `SESSION_COOKIE_HTTPONLY`, and `SESSION_COOKIE_SAMESITE=Lax`.
- **Refuses to start** if `APP_ENV=production` and `SECRET_KEY` is still
  the checked-into-source dev default — verified by testing (see
  `get_config()`'s `RuntimeError`). Without this, an internet-facing
  deployment that forgot to set a real secret would boot "successfully"
  with every session/login/CSRF token forgeable by anyone who's read
  this repo, which is a silent failure mode worth refusing outright
  rather than just warning about.
- `BEHIND_PROXY` (defaults `true` in production, `false` in dev) wires
  up Werkzeug's `ProxyFix` middleware. Cloud platforms terminate HTTPS
  in front of the app and forward plain HTTP internally with
  `X-Forwarded-*` headers — without `ProxyFix`, Flask thinks every
  request is plain HTTP, which breaks secure cookies and
  `url_for(..., _external=True)`. Confirmed working under gunicorn
  locally with `APP_ENV=production` before shipping this phase.
- `DATABASE_URL` scheme normalization: some managed Postgres hosts hand
  out `postgres://` (not `postgresql://`), and SQLAlchemy/psycopg2 need
  `postgresql+psycopg2://` specifically — `config.py` rewrites whatever
  scheme it's given rather than assuming the platform matches what
  SQLAlchemy wants.

**Migrations and RLS setup run as a separate release step**
(`scripts/release.sh`: `flask db upgrade` then
`psql "$DATABASE_URL" -f scripts/setup_rls.sql`), not inside the
container's own startup command. This matters for two reasons: a failed
migration stops the deploy instead of starting an app against a
half-migrated schema, and it runs exactly once per deploy regardless of
how many app instances/workers get started (each instance's own `CMD`
would otherwise try to run it once per instance). `setup_rls.sql`
needed **no changes at all** to work against a managed Postgres
connection string — it was already written to apply
`FORCE ROW LEVEL SECURITY` (so even the table owner is subject to RLS),
which is exactly what's needed since a managed Postgres role typically
owns its own tables directly rather than connecting as a separate
non-owner role the way local dev's `acquiral_app` does. Either shape
works with this same script, because every managed Postgres offering
gives you a role that is never a superuser and never has `BYPASSRLS` —
confirmed by design review, since that's a security property those
platforms have to guarantee for connection-pooled multi-tenant hosting
in general, not something specific to this app.

No cloud credentials, payment method, or DNS access are things an
assistant can act on directly — `DEPLOYMENT.md` has the exact
click-through steps (or `render.yaml` for a one-shot Blueprint deploy)
for the account owner to run themselves.

## Phase 7: Public Demo Tenant

A single, shared, **read-only** tenant that a marketing link (from
admiralsentinel.com or anywhere else) can send anonymous visitors
straight into, without a signup or credentials.

**`Organization.is_demo`** (migration `c3041694e8d3`) flags the one
tenant this applies to. `app/__init__.py`'s `before_request` guard
blocks **every non-GET request** for a logged-in user whose organization
has this flag set, before it reaches any route handler — the same
deny-by-default philosophy as the RLS setup: rather than trusting every
current and future route to remember "don't let the demo write," one
central check makes it structurally impossible, whichever route the
write would have gone through. GET requests (including PDF downloads)
are unaffected, so a visitor can click through the entire app and
download a sample invoice/payslip, but every `POST`/`PUT`/`PATCH`/
`DELETE` — creating a funder, posting a journal entry, sending an
invoice, importing a statement — is blocked with a flash message and a
redirect to the dashboard. This also means the demo tenant never needs a
scheduled reset job: it cannot be mutated in the first place.

The redirect target is a **fixed internal route** (`accounting.dashboard`),
not `request.referrer` — that header is attacker-controlled (any page,
on or off the site, can set an arbitrary `Referer`), so using it as a
redirect target would itself be an open-redirect vulnerability. Worth
being deliberate about, working for a cybersecurity company.

**`GET /demo`** (`app/__init__.py: try_demo()`) is the one-click entry
point: it looks up the flagged organization's admin user and logs the
visitor straight in with `login_user()`, no credentials typed. A manual
username/password login also works for the same account (see
`scripts/seed_demo.py`'s output), in case a marketing page would rather
link to `/login` with instructions instead.

A demo-mode banner (`is_demo_org` template flag, injected via a
`context_processor`) renders across every page for a logged-in demo
visitor, with a "Sign up for your own organization" call to action.

**`scripts/seed_demo.py`** creates the demo tenant and its data by
driving the actual application routes through Flask's test client —
the same technique `scripts/smoke_test.py` uses — rather than
constructing model rows directly. This means the seeded data is produced
by exactly the same business logic (journal postings, PAYE/pension
calculations, invoice VAT, depreciation) a real user's actions would
produce, instead of a second, potentially-drifting copy of that logic
living in a seed script. It seeds a **fictional** NGO ("Hopewell
Development Initiative") with fictional funders, employees, and
customers spanning every feature built so far — this is a hard rule, not
a style choice: this tenant is reachable by anyone on the internet, so
real Social Action or Admiral Sentinel customer data must never be the
seed data.

One sequencing detail worth calling out because it broke the first draft
of this script: `is_demo` must be set **only after** all seeding
requests complete. Setting it first (immediately after signup) means
every subsequent `POST` the script itself makes gets blocked by its own
read-only guard — caught by actually running the script and noticing the
payroll run and half the seeded data silently didn't exist, not by
reading the code.

Run `PYTHONPATH=. python3 scripts/seed_demo.py` once after a fresh
deploy to create the demo tenant; `--reset` wipes and recreates it if
the seed data ever needs to change.

## What's NOT in this codebase yet

A custom domain is not wired up yet (deployment currently targets a
platform-provided URL, per the agreed scope for that phase) — see
`DEPLOYMENT.md`'s "Custom domain" section for the follow-up steps
whenever that's ready. All items from the original 8-point roadmap are
otherwise complete.
