"""
Adversarial RLS test — bypasses the ORM's query-building entirely and
issues raw SQL as the acquiral_app role, to prove isolation is enforced
by Postgres itself (not just by the ORM adding a WHERE clause we could
forget to write). Three attacks, matching the ones proven in the
original (lost) build:

 1. Unfiltered raw SQL SELECT with the WRONG tenant set -> must return 0
    rows for another tenant's data, even though no WHERE organization_id
    clause is written at all.
 2. Raw SQL SELECT by a KNOWN row id, with the wrong tenant context set
    -> must return 0 rows (proves it's not just filtering lists, it
    blocks direct-object access too).
 3. Raw SQL SELECT with NO tenant context set at all -> must return 0
    rows (proves the NULLIF(...) fix: an empty-string GUC must deny all,
    not error out or default-allow).
"""
import subprocess
import psycopg2

DSN = "postgresql://acquiral_app:acquiral_app_pw@localhost/acquiral_dev"


def psql_super(sql):
    """Run SQL as the postgres superuser via peer auth (through sudo),
    for test setup/inspection only -- never used for the actual
    isolation assertions, which all go through the acquiral_app role."""
    out = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", "acquiral_dev", "-t", "-A", "-F", ",", "-c", sql],
        capture_output=True, text=True, check=True,
    )
    lines = [l for l in out.stdout.strip().splitlines() if l]
    return [tuple(l.split(",")) for l in lines]


def run(sql, params=None, tenant=None):
    """tenant=None (or any other falsy value) means: never call SET LOCAL
    at all for this connection/transaction -- used by attack 3 to
    simulate a forgotten before_request hook."""
    conn = psycopg2.connect(DSN)
    conn.autocommit = False
    cur = conn.cursor()
    if tenant:
        cur.execute("SET LOCAL app.current_tenant = %s", (str(tenant),))
    cur.execute(sql, params or ())
    rows = cur.fetchall()
    conn.rollback()
    conn.close()
    return rows


# Fetch a funder and its owning org, plus a second, DIFFERENT org, using
# the postgres superuser (bypasses RLS, just for setup/inspection).
funder_rows = psql_super("SELECT id, organization_id FROM funders LIMIT 1")
assert funder_rows, "expected at least one funder in the db from the smoke test -- run scripts/smoke_test.py first"
known_funder_id, org1_id = funder_rows[0]
org1_name_rows = psql_super(f"SELECT name FROM organizations WHERE id = '{org1_id}'")
org1_name = org1_name_rows[0][0]

other_org_rows = psql_super(f"SELECT id, name FROM organizations WHERE id != '{org1_id}' LIMIT 1")
assert other_org_rows, "expected at least 2 orgs in the db from the smoke test -- run scripts/smoke_test.py first"
org2_id, org2_name = other_org_rows[0]

print(f"org1={org1_name} ({org1_id})\norg2={org2_name} ({org2_id})\nknown funder id from org1: {known_funder_id}\n")

# Attack 1: unfiltered SELECT * FROM funders, tenant set to org2 -> should
# see ONLY org2's rows (i.e. never org1's), even with zero WHERE clause.
rows = run("SELECT organization_id, name FROM funders", tenant=org2_id)
leaked = [r for r in rows if r[0] == org1_id]
assert not leaked, f"ATTACK 1 SUCCEEDED (BAD): unfiltered SELECT under org2's tenant leaked org1 rows: {leaked}"
print("[OK] Attack 1 blocked: unfiltered SELECT under org2 tenant context returns zero org1 rows"
      f" ({len(rows)} row(s), all org2's)")

# Attack 2: direct-object-reference — ask for org1's known funder id by
# PK, while tenant context is set to org2.
rows = run("SELECT id, name FROM funders WHERE id = %s", (known_funder_id,), tenant=org2_id)
assert not rows, f"ATTACK 2 SUCCEEDED (BAD): fetched org1's funder by known id under org2 tenant: {rows}"
print("[OK] Attack 2 blocked: SELECT by known funder id under WRONG tenant context returns zero rows")

# Attack 2b: sanity check the SAME query succeeds under the CORRECT tenant,
# proving attack 2's zero-rows result is RLS denial, not a typo/bad id.
rows = run("SELECT id, name FROM funders WHERE id = %s", (known_funder_id,), tenant=org1_id)
assert rows, "sanity check failed: correct tenant should be able to see its own funder"
print(f"[OK] Sanity check: same funder IS visible under its OWN tenant context ({rows[0][1]})")

# Attack 3: no tenant context set at all (simulates a forgotten
# before_request / a raw psql session that never calls SET LOCAL). The
# NULLIF fix must turn the empty-string GUC into NULL so the policy's
# `organization_id = NULL` compares false for every row, denying all
# access -- rather than erroring out on a bad ::uuid cast, or (much
# worse) silently defaulting to "allow everything".
rows = run("SELECT organization_id, name FROM funders", tenant=None)
assert not rows, f"ATTACK 3 SUCCEEDED (BAD): SELECT with NO tenant context set returned rows: {rows}"
print("[OK] Attack 3 blocked: SELECT with NO tenant context set at all returns zero rows (NULLIF fix holding)")

# ---------------------------------------------------------------------
# Phase 3 tables: fixed_assets, employees, payroll_runs -- the same
# three attacks, since these were added after the original RLS setup
# and are the highest-risk area (multi-commit routes) for a forgotten
# organization_id somewhere.
# ---------------------------------------------------------------------
for table, name_col in [("fixed_assets", "name"), ("employees", "full_name")]:
    row = psql_super(f"SELECT id, organization_id FROM {table} WHERE organization_id = '{org1_id}' LIMIT 1")
    if not row:
        print(f"[SKIP] no {table} row for org1 to test against -- run scripts/smoke_test.py first")
        continue
    known_id, _ = row[0]

    rows = run(f"SELECT organization_id, {name_col} FROM {table}", tenant=org2_id)
    leaked = [r for r in rows if r[0] == org1_id]
    assert not leaked, f"ATTACK 1 SUCCEEDED (BAD) on {table}: unfiltered SELECT under org2 tenant leaked org1 rows: {leaked}"
    print(f"[OK] Attack 1 blocked on {table}: unfiltered SELECT under org2 tenant returns zero org1 rows")

    rows = run(f"SELECT id FROM {table} WHERE id = %s", (known_id,), tenant=org2_id)
    assert not rows, f"ATTACK 2 SUCCEEDED (BAD) on {table}: fetched org1's row by known id under org2 tenant: {rows}"
    rows = run(f"SELECT id FROM {table} WHERE id = %s", (known_id,), tenant=org1_id)
    assert rows, f"sanity check failed on {table}: correct tenant should see its own row"
    print(f"[OK] Attack 2 blocked on {table} (known-id lookup denied under wrong tenant, allowed under correct one)")

    rows = run(f"SELECT id FROM {table}", tenant=None)
    assert not rows, f"ATTACK 3 SUCCEEDED (BAD) on {table}: SELECT with no tenant context returned rows: {rows}"
    print(f"[OK] Attack 3 blocked on {table}: no tenant context set returns zero rows")

# payroll_runs: same 3 attacks, keyed by (period_month, period_year)
# instead of a name column.
row = psql_super(f"SELECT id, organization_id FROM payroll_runs WHERE organization_id = '{org1_id}' LIMIT 1")
if row:
    known_id, _ = row[0]
    rows = run("SELECT organization_id, period_month, period_year FROM payroll_runs", tenant=org2_id)
    leaked = [r for r in rows if r[0] == org1_id]
    assert not leaked, f"ATTACK 1 SUCCEEDED (BAD) on payroll_runs: leaked org1 rows under org2 tenant: {leaked}"
    print("[OK] Attack 1 blocked on payroll_runs")

    rows = run("SELECT id FROM payroll_runs WHERE id = %s", (known_id,), tenant=org2_id)
    assert not rows, f"ATTACK 2 SUCCEEDED (BAD) on payroll_runs: {rows}"
    rows = run("SELECT id FROM payroll_runs WHERE id = %s", (known_id,), tenant=org1_id)
    assert rows, "sanity check failed on payroll_runs"
    print("[OK] Attack 2 blocked on payroll_runs")

    rows = run("SELECT id FROM payroll_runs", tenant=None)
    assert not rows, f"ATTACK 3 SUCCEEDED (BAD) on payroll_runs: {rows}"
    print("[OK] Attack 3 blocked on payroll_runs")
else:
    print("[SKIP] no payroll_runs row for org1 to test against -- run scripts/smoke_test.py first")

# ---------------------------------------------------------------------
# Invoicing tables: customers, invoices, invoice_lines -- same three
# attacks, added with the Invoicing/Payslip-PDF phase.
# ---------------------------------------------------------------------
for table, name_col in [("customers", "name"), ("invoices", "invoice_number"), ("invoice_lines", "description")]:
    row = psql_super(f"SELECT id, organization_id FROM {table} WHERE organization_id = '{org1_id}' LIMIT 1")
    if not row:
        print(f"[SKIP] no {table} row for org1 to test against -- run scripts/smoke_test.py first")
        continue
    known_id, _ = row[0]

    rows = run(f"SELECT organization_id, {name_col} FROM {table}", tenant=org2_id)
    leaked = [r for r in rows if r[0] == org1_id]
    assert not leaked, f"ATTACK 1 SUCCEEDED (BAD) on {table}: unfiltered SELECT under org2 tenant leaked org1 rows: {leaked}"
    print(f"[OK] Attack 1 blocked on {table}: unfiltered SELECT under org2 tenant returns zero org1 rows")

    rows = run(f"SELECT id FROM {table} WHERE id = %s", (known_id,), tenant=org2_id)
    assert not rows, f"ATTACK 2 SUCCEEDED (BAD) on {table}: fetched org1's row by known id under org2 tenant: {rows}"
    rows = run(f"SELECT id FROM {table} WHERE id = %s", (known_id,), tenant=org1_id)
    assert rows, f"sanity check failed on {table}: correct tenant should see its own row"
    print(f"[OK] Attack 2 blocked on {table} (known-id lookup denied under wrong tenant, allowed under correct one)")

    rows = run(f"SELECT id FROM {table}", tenant=None)
    assert not rows, f"ATTACK 3 SUCCEEDED (BAD) on {table}: SELECT with no tenant context returned rows: {rows}"
    print(f"[OK] Attack 3 blocked on {table}: no tenant context set returns zero rows")

# ---------------------------------------------------------------------
# Bank Transactions + Reconciliation tables: bank_statement_imports,
# bank_transactions -- same three attacks, added with this phase.
# ---------------------------------------------------------------------
for table, name_col in [("bank_statement_imports", "original_filename"), ("bank_transactions", "description")]:
    row = psql_super(f"SELECT id, organization_id FROM {table} WHERE organization_id = '{org1_id}' LIMIT 1")
    if not row:
        print(f"[SKIP] no {table} row for org1 to test against -- run scripts/smoke_test.py first")
        continue
    known_id, _ = row[0]

    rows = run(f"SELECT organization_id, {name_col} FROM {table}", tenant=org2_id)
    leaked = [r for r in rows if r[0] == org1_id]
    assert not leaked, f"ATTACK 1 SUCCEEDED (BAD) on {table}: unfiltered SELECT under org2 tenant leaked org1 rows: {leaked}"
    print(f"[OK] Attack 1 blocked on {table}: unfiltered SELECT under org2 tenant returns zero org1 rows")

    rows = run(f"SELECT id FROM {table} WHERE id = %s", (known_id,), tenant=org2_id)
    assert not rows, f"ATTACK 2 SUCCEEDED (BAD) on {table}: fetched org1's row by known id under org2 tenant: {rows}"
    rows = run(f"SELECT id FROM {table} WHERE id = %s", (known_id,), tenant=org1_id)
    assert rows, f"sanity check failed on {table}: correct tenant should see its own row"
    print(f"[OK] Attack 2 blocked on {table} (known-id lookup denied under wrong tenant, allowed under correct one)")

    rows = run(f"SELECT id FROM {table}", tenant=None)
    assert not rows, f"ATTACK 3 SUCCEEDED (BAD) on {table}: SELECT with no tenant context returned rows: {rows}"
    print(f"[OK] Attack 3 blocked on {table}: no tenant context set returns zero rows")

print("\nALL ADVERSARIAL RLS TESTS PASSED")
