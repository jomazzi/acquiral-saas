# Acquiral SaaS (Phases 1–7)

Multi-tenant rebuild of Acquiral — fund accounting, fixed assets,
Nigerian payroll, invoicing, and bank reconciliation, a product of
Admiral Sentinel. This package covers:

- **Phase 1**: multi-tenant Postgres architecture with Row-Level Security
  (organization_id + RLS), Alembic migrations, auth (signup/login/users).
- **Phase 2**: the accounting engine on top of it — chart of accounts,
  funders, projects, journal entries (multi-currency), and 4 reports
  (trial balance, income statement, by-project, balance sheet).
- **Phase 3**: Fixed Assets (register, disposal, depreciation runs) and
  Employees/Payroll (Nigeria Tax Act 2025 PAYE/pension/NHF, draft payroll
  runs, bonus adjustment, posting to the journal), plus the Asset
  Register and Payroll Summary reports.
- **Phase 4**: Invoicing (a separate Customer entity from Funders, draft
  → sent → paid lifecycle with VAT and journal postings on send/payment)
  and downloadable/printable PDFs for both invoices and payslips. Also
  restructured the UI from a top navbar to a left sidebar with a
  responsive hamburger menu on mobile.
- **Phase 5**: Bank Transactions & Reconciliation — import a CSV, Excel,
  or PDF bank statement per account, then match each transaction to an
  existing journal line, post a brand-new entry directly from an
  unmatched row, or ignore it. No live bank connect, no OCR — by design.
- **Phase 6**: Cloud deployment — a portable Docker image plus a
  production config (secure cookies, proxy-aware HTTPS handling, a
  refuse-to-boot check on the session secret) and a `scripts/release.sh`
  step that runs migrations + RLS setup once per deploy. See
  `DEPLOYMENT.md` for the actual deploy steps (Render, recommended, plus
  notes for Railway/Fly.io/DigitalOcean).
- **Phase 7**: Public demo tenant — a one-click `/demo` login into a
  shared, read-only sample organization (fictional data only), safe to
  link from admiralsentinel.com. Every write is blocked centrally for
  that tenant, so it never needs a reset job. See `scripts/seed_demo.py`.

See `ARCHITECTURE.md` for how tenant isolation actually works and why —
read it before touching auth or adding a new tenant-scoped table. See
`DEPLOYMENT.md` for how to actually put this on the internet.

## Setup

```bash
pip install -r requirements.txt

# Postgres: create a non-superuser app role (RLS has no effect on a
# superuser or BYPASSRLS role) and a database it owns.
sudo -u postgres psql -c "CREATE ROLE acquiral_app WITH LOGIN PASSWORD 'acquiral_app_pw';"
sudo -u postgres psql -c "CREATE DATABASE acquiral_dev OWNER acquiral_app;"

export DATABASE_URL="postgresql+psycopg2://acquiral_app:acquiral_app_pw@localhost/acquiral_dev"
export FLASK_APP=run.py

flask db upgrade                                   # creates all tables
sudo -u postgres psql -d acquiral_dev -f scripts/setup_rls.sql   # applies RLS policies

flask run   # or: python run.py
```

Then visit `/` — "Start free" creates a new organization and its first
admin user; "Log in" for an existing one.

## Setting up the public demo tenant

```bash
export PYTHONPATH=.
python3 scripts/seed_demo.py            # create it (skips if one already exists)
python3 scripts/seed_demo.py --reset    # wipe and recreate it
```

Then link `/demo` (or the deployed URL + `/demo`) from anywhere — it logs
the visitor straight into the shared, read-only demo organization, no
credentials needed. See `ARCHITECTURE.md`'s Phase 7 section for how the
read-only enforcement works.

## Testing

```bash
export DATABASE_URL="postgresql+psycopg2://acquiral_app:acquiral_app_pw@localhost/acquiral_dev"
export FLASK_APP=run.py
export PYTHONPATH=.

python3 scripts/smoke_test.py            # functional: signup, data, cross-tenant app-layer checks
python3 scripts/rls_adversarial_test.py  # adversarial: raw SQL, proves DB-level isolation
```

Both must pass after any change touching models, routes, or
`scripts/setup_rls.sql`.

## Adding a new migration

```bash
flask db migrate -m "description"
flask db upgrade
sudo -u postgres psql -d acquiral_dev -f scripts/setup_rls.sql   # re-apply (idempotent)
```

## Roadmap

Every item from the original 8-point roadmap is now complete except a
custom domain for the deployed app (currently targets a
platform-provided URL — see `DEPLOYMENT.md`'s "Custom domain" section
for the follow-up steps whenever that's ready). Legal documents (Terms
of Service, Privacy Policy, Data Processing Agreement, and NDPA
compliance notes) were drafted separately as living documents — ask for
their links if you need them again.
