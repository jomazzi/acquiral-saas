-- Row-Level Security policies for Acquiral's multi-tenant schema.
--
-- IMPORTANT PREREQUISITE: the app must connect as a non-superuser role
-- (acquiral_app) with no BYPASSRLS attribute. Superusers and BYPASSRLS
-- roles skip RLS entirely, silently — confirmed by testing.
--
-- Tenant context is set per-request/per-transaction by the app via:
--   SET LOCAL app.current_tenant = '<organization_id>';
-- (see app/__init__.py: set_tenant()). SET LOCAL only lasts for the
-- current transaction — after COMMIT/ROLLBACK it reverts, and Postgres
-- resets a `SET LOCAL`-only custom GUC to '' (empty string), NOT NULL.
-- A bare ::uuid cast of '' raises an error, so every policy below uses
-- NULLIF(current_setting(...), '')::uuid to turn that '' into a real
-- NULL first — a NULL tenant compares false against every row, so a
-- forgotten/expired tenant setting safely denies all access instead of
-- crashing or (worse) leaking rows.
--
-- users IS DELIBERATELY EXCLUDED. Logging in requires looking up a User
-- to find out which organization they belong to; if User were
-- row-secured, that lookup itself would need app.current_tenant already
-- set, which requires knowing the user first. Circular. So `users` has
-- RLS disabled here and isolation is enforced by hand in every route
-- (always filter User queries by organization_id explicitly) — see the
-- docstring on User in app/models/tenant.py.
--
-- Whenever a new tenant-scoped table is added (TenantScopedMixin), add
-- a matching ENABLE/FORCE/CREATE POLICY block for it here.

DO $$
DECLARE
    tbl text;
BEGIN
    FOREACH tbl IN ARRAY ARRAY[
        'funders',
        'projects',
        'accounts',
        'journal_entries',
        'journal_lines',
        'fixed_assets',
        'employees',
        'payroll_runs',
        'payslips',
        'customers',
        'invoices',
        'invoice_lines',
        'bank_statement_imports',
        'bank_transactions',
        'journal_attachments',
        'vendors',
        'bills',
        'bill_lines'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', tbl);
        -- FORCE so even the table owner is subject to RLS (by default
        -- the owning role bypasses RLS, which would be a silent hole
        -- since acquiral_app owns these tables).
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', tbl);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', tbl);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I
                USING (organization_id = NULLIF(current_setting(''app.current_tenant'', true), '''')::uuid)
                WITH CHECK (organization_id = NULLIF(current_setting(''app.current_tenant'', true), '''')::uuid)',
            tbl
        );
    END LOOP;
END $$;

-- users: explicitly NOT row-secured (see rationale above). This line is
-- a no-op if RLS was never enabled, but documents the decision in the
-- same file as everything else, and is safe to re-run.
ALTER TABLE users DISABLE ROW LEVEL SECURITY;
