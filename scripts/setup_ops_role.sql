-- Grants for the ops console's database role. Run as postgres, once per
-- database, after migrations (safe to re-run):
--   sudo -u postgres psql -d acquiraldb < scripts/setup_ops_role.sql
-- Create the role once per server first (see deployment steps).
-- Release 1 is read-only. The role gets NO access to journals, accounts,
-- payroll, invoices, bills, bank data, attachments, or password hashes.
\set ON_ERROR_STOP on

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acquiral_ops') THEN
        RAISE EXCEPTION 'Role acquiral_ops does not exist: create it first.';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acquiral_ops' AND (rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'acquiral_ops must not be a superuser or have BYPASSRLS.';
    END IF;
END $$;

REVOKE ALL ON ALL TABLES IN SCHEMA public FROM acquiral_ops;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM acquiral_ops;
GRANT USAGE ON SCHEMA public TO acquiral_ops;

GRANT SELECT (id, name, slug, created_at, active, is_demo, billing_status, billing_email,
              plan_key, plan_interval, plan_currency, trial_ends_at, current_period_end,
              paystack_customer_code, paystack_subscription_code)
    ON organizations TO acquiral_ops;

GRANT SELECT (id, organization_id, username, full_name, role, active, email, email_verified,
              terms_accepted_at, terms_version)
    ON users TO acquiral_ops;

GRANT SELECT ON subscription_payments TO acquiral_ops;
GRANT SELECT ON alembic_version TO acquiral_ops;
GRANT SELECT, UPDATE ON ops_admins TO acquiral_ops;
GRANT SELECT, INSERT ON ops_audit_log TO acquiral_ops;
GRANT USAGE ON SEQUENCE ops_audit_log_id_seq TO acquiral_ops;

-- subscription_payments has forced row-level security (one tenant at a
-- time). This policy lets ONLY the ops role read it across tenants.
DROP POLICY IF EXISTS ops_read ON subscription_payments;
CREATE POLICY ops_read ON subscription_payments FOR SELECT TO acquiral_ops USING (true);

-- Pricing: read the settings; change only specific columns on them.
GRANT SELECT ON price_list, tax_settings, campaigns TO acquiral_ops;
GRANT UPDATE (amount_minor, discount_bp, updated_at, updated_by) ON price_list TO acquiral_ops;
GRANT UPDATE (vat_bp, enabled, updated_at, updated_by) ON tax_settings TO acquiral_ops;
GRANT INSERT ON campaigns TO acquiral_ops;
GRANT UPDATE (active) ON campaigns TO acquiral_ops;
GRANT UPDATE (sellable) ON tax_settings TO acquiral_ops;