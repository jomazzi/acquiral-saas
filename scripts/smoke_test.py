"""
Functional smoke test for Phase 1+2: signs up two separate organizations,
logs into each, creates data (funders/projects/journal entries), and
checks the ordinary app flow works end-to-end via the Flask test client
(exercises before_request -> set_tenant -> RLS on every request, exactly
as production traffic would).
"""
import io
import re
from app import create_app

app = create_app()
app.config["WTF_CSRF_ENABLED"] = False


def get_csrf_free_client():
    return app.test_client()


def signup(client, org_name, username, full_name, password):
    resp = client.post("/signup", data={
        "org_name": org_name, "username": username,
        "full_name": full_name, "password": password,
    }, follow_redirects=True)
    assert resp.status_code == 200, resp.status_code
    return resp


def login(client, username, password):
    resp = client.post("/login", data={"username": username, "password": password}, follow_redirects=True)
    assert resp.status_code == 200
    assert b"Invalid username or password" not in resp.data
    return resp


from app.extensions import db
from app.models.tenant import Organization, User

if True:
    c1 = get_csrf_free_client()
    r = signup(c1, "Social Action NGO", "peter", "Peter Mazzi", "password123")
    assert b"Dashboard" in r.data or b"dashboard" in r.data.lower(), "signup did not land on dashboard"
    print("[OK] Org1 signup ->", re.search(rb"<title>(.*?)</title>", r.data).group(1))

    c2 = get_csrf_free_client()
    r = signup(c2, "Acme Consulting", "peter", "Peter Other", "password456")
    print("[OK] Org2 signup with SAME username 'peter' in a different org succeeded (usernames unique per-org, not globally)")

    # Org1: create a funder + project + journal entry
    r = c1.post("/funders", data={"name": "UNICEF", "notes": "Health grant"}, follow_redirects=True)
    assert b"Funder added" in r.data
    r = c1.get("/funders")
    assert b"UNICEF" in r.data
    print("[OK] Org1 created Funder UNICEF")

    r = c2.get("/funders")
    assert b"UNICEF" not in r.data, "TENANT LEAK: Org2 can see Org1's funder via the ORM-scoped route!"
    print("[OK] Org2 cannot see Org1's UNICEF funder (RLS holding on ordinary route traffic)")

    r = c1.get("/accounts")
    assert b"Cash on Hand" in r.data
    r = c2.get("/accounts")
    assert b"Cash on Hand" in r.data  # seeded independently per org
    print("[OK] Both orgs got their own independently-seeded chart of accounts")

    # cross-tenant direct-object-reference check: try to edit org1's funder
    # using org2's session. This inspection runs in its own short-lived
    # app context, separate from the request-handling ones above/below,
    # exactly as a one-off script or shell would do it.
    from app.models.accounting import Funder
    from app import set_tenant
    with app.app_context():
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        # set_tenant uses SET LOCAL, which only lasts for the current
        # transaction — committing in between would wipe it before the
        # next query runs. So: set tenant, then query, all in one
        # transaction, THEN commit/close.
        set_tenant(org1.id)
        unicef = Funder.query.filter_by(name="UNICEF").first()
        unicef_id = unicef.id
        db.session.commit()

    r = c2.get(f"/funders/{unicef_id}/edit")
    assert r.status_code == 404, f"TENANT LEAK: Org2 admin could open Org1's funder edit page (status {r.status_code})"
    print("[OK] Org2 gets 404 trying to open Org1's funder-edit page by guessed/known UUID")

    # -----------------------------------------------------------------
    # Phase 3: Fixed Assets, Employees, Payroll -- flagged as the
    # highest-risk area for a tenant-context bug because of the
    # multi-step/multi-commit pattern (add asset -> flush -> add journal
    # entry -> flush -> commit; generate payroll -> per-employee payslip
    # rows -> commit; post payroll -> build journal lines -> flush ->
    # commit). Any dropped tenant context mid-flow would show up here.
    # -----------------------------------------------------------------

    # Fixed asset, bought "on payable" (no cash movement) so it posts a
    # journal entry as a side effect -- exercises the asset+journal
    # multi-commit path.
    r = c1.post("/assets/new", data={
        "name": "Toyota Hilux", "category": "Vehicles", "cost": "25000000",
        "salvage_value": "2000000", "useful_life_years": "5",
        "acquisition_date": "2026-01-15", "paid_from": "payable",
    }, follow_redirects=True)
    assert b"Fixed asset added" in r.data
    r = c1.get("/assets")
    assert b"Toyota Hilux" in r.data
    print("[OK] Org1 created a Fixed Asset (payable purchase, posts a journal entry)")

    r = c2.get("/assets")
    assert b"Toyota Hilux" not in r.data, "TENANT LEAK: Org2 can see Org1's fixed asset!"
    print("[OK] Org2 cannot see Org1's Toyota Hilux asset")

    # Depreciation run -- iterates ALL of the CURRENT tenant's assets and
    # posts one combined journal entry.
    r = c1.post("/assets/run-depreciation", data={"as_of": "2026-06-30"}, follow_redirects=True)
    assert b"Posted depreciation" in r.data, r.data[:500]
    print("[OK] Org1 depreciation run posted")

    # Employee + payroll run + post -- the multi-commit path flagged above.
    r = c1.post("/employees/new", data={
        "staff_id": "S001", "full_name": "Amaka Obi", "role_title": "Program Officer",
        "basic_salary": "300000", "housing_allowance": "100000", "transport_allowance": "50000",
        "pension_opt_in": "on", "start_date": "2026-01-01",
    }, follow_redirects=True)
    assert b"Employee added" in r.data
    r = c1.get("/employees")
    assert b"Amaka Obi" in r.data
    print("[OK] Org1 created Employee Amaka Obi")

    r = c2.get("/employees")
    assert b"Amaka Obi" not in r.data, "TENANT LEAK: Org2 can see Org1's employee!"
    print("[OK] Org2 cannot see Org1's employee")

    r = c1.post("/payroll/new", data={"month": "6", "year": "2026"}, follow_redirects=True)
    assert b"Amaka Obi" in r.data, "payroll run did not generate a payslip for the employee"
    m = re.search(rb"/payroll/([0-9a-f-]{36})", r.data)
    assert m, "could not find the payroll run id in the response"
    run_id = m.group(1).decode()
    print(f"[OK] Org1 generated draft payroll run {run_id}")

    r = c2.get(f"/payroll/{run_id}")
    assert r.status_code == 404, f"TENANT LEAK: Org2 could open Org1's payroll run (status {r.status_code})"
    print("[OK] Org2 gets 404 trying to open Org1's payroll run by known UUID")

    # Find org1's own NGN bank account id (seeded independently per org,
    # so org1's and org2's "Bank Account (NGN)" have DIFFERENT ids) to
    # post payroll against.
    with app.app_context():
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        set_tenant(org1.id)
        from app.models.accounting import Account
        bank = Account.query.filter_by(code="1010").first()
        bank_id = str(bank.id)
        db.session.commit()

    r = c1.post(f"/payroll/{run_id}/post", data={"bank_account_id": bank_id}, follow_redirects=True)
    assert b"posted to the journal" in r.data, r.data[:800]
    print("[OK] Org1 posted payroll to the journal (multi-commit path completed under correct tenant throughout)")

    r = c1.get("/reports/payroll-summary")
    assert b"Amaka Obi" not in r.data  # summary is by run/period totals, not per-employee names -- just confirm it loads
    assert r.status_code == 200
    r = c2.get("/reports/payroll-summary")
    assert r.status_code == 200  # org2 has no posted runs; should just render empty, not error
    print("[OK] Payroll summary report renders for both orgs, org2's is unaffected by org1's posted run")

    # Payslip PDF download -- exercises the reportlab generation path and
    # confirms it actually returns a PDF, not an error page.
    r = c1.get(f"/payroll/{run_id}")
    m = re.search(rb"/payroll/[0-9a-f-]{36}/payslip/([0-9a-f-]{36})/pdf", r.data)
    assert m, "could not find a payslip PDF link on the payroll detail page"
    payslip_id = m.group(1).decode()
    r = c1.get(f"/payroll/{run_id}/payslip/{payslip_id}/pdf")
    assert r.status_code == 200 and r.data[:4] == b"%PDF", "payslip PDF did not come back as a real PDF"
    print(f"[OK] Org1 downloaded a real PDF payslip ({len(r.data)} bytes)")

    r = c2.get(f"/payroll/{run_id}/payslip/{payslip_id}/pdf")
    assert r.status_code == 404, f"TENANT LEAK: Org2 could download Org1's payslip PDF (status {r.status_code})"
    print("[OK] Org2 gets 404 trying to download Org1's payslip PDF by known UUID")

    # -----------------------------------------------------------------
    # Invoicing: new Customer entity (separate from Funder), draft ->
    # send (posts AR/Income/VAT) -> record payment (posts Bank/AR).
    # -----------------------------------------------------------------
    r = c1.post("/customers", data={
        "name": "Zenith Bank Plc", "email": "billing@zenithbank.example",
        "phone": "0800-000-0000", "address": "Zenith Heights, Lagos",
    }, follow_redirects=True)
    assert b"Customer added" in r.data
    print("[OK] Org1 created Customer Zenith Bank Plc")

    r = c2.get("/customers")
    assert b"Zenith Bank Plc" not in r.data, "TENANT LEAK: Org2 can see Org1's customer!"
    print("[OK] Org2 cannot see Org1's customer")

    r = c1.get("/customers")
    m = re.search(rb"/customers/([0-9a-f-]{36})/edit", r.data)
    customer_id = m.group(1).decode() if m else None

    r = c1.post("/invoices/new", data={
        "customer_id": customer_id, "issue_date": "2026-07-01", "due_date": "2026-07-31",
        "vat_rate": "7.5", "notes": "Net 30.",
        "description[]": ["Penetration testing engagement"],
        "quantity[]": ["1"], "unit_price[]": ["1500000"],
    }, follow_redirects=True)
    assert b"created as a draft" in r.data, r.data[:800]
    m = re.search(rb"/invoices/([0-9a-f-]{36})", r.data)
    assert m, "could not find the invoice id in the response"
    invoice_id = m.group(1).decode()
    print(f"[OK] Org1 created draft invoice {invoice_id}")

    r = c2.get(f"/invoices/{invoice_id}")
    assert r.status_code == 404, f"TENANT LEAK: Org2 could open Org1's invoice (status {r.status_code})"
    print("[OK] Org2 gets 404 trying to open Org1's invoice by known UUID")

    r = c1.post(f"/invoices/{invoice_id}/send", follow_redirects=True)
    assert b"posted to the journal" in r.data, r.data[:800]
    print("[OK] Org1 sent the invoice (posted Dr Accounts Receivable / Cr Income + VAT)")

    r = c1.get(f"/invoices/{invoice_id}/pdf")
    assert r.status_code == 200 and r.data[:4] == b"%PDF", "invoice PDF did not come back as a real PDF"
    print(f"[OK] Org1 downloaded a real invoice PDF ({len(r.data)} bytes)")

    r = c2.get(f"/invoices/{invoice_id}/pdf")
    assert r.status_code == 404, f"TENANT LEAK: Org2 could download Org1's invoice PDF (status {r.status_code})"
    print("[OK] Org2 gets 404 trying to download Org1's invoice PDF by known UUID")

    with app.app_context():
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        set_tenant(org1.id)
        from app.models.accounting import Account as AccountModel
        bank = AccountModel.query.filter_by(code="1010").first()
        bank_id = str(bank.id)
        db.session.commit()

    r = c1.post(f"/invoices/{invoice_id}/record-payment", data={"bank_account_id": bank_id}, follow_redirects=True)
    assert b"marked as paid" in r.data, r.data[:800]
    print("[OK] Org1 recorded payment for the invoice (posted Dr Bank / Cr Accounts Receivable)")

    # -----------------------------------------------------------------
    # Bank Transactions + Reconciliation: import a CSV statement, match
    # one row to an existing (manually posted) journal line, post a new
    # entry directly from an unmatched row, and ignore a third.
    # -----------------------------------------------------------------
    # A known journal line against the bank account, posted manually, for
    # the "match to existing" test -- Dr Bank 1010 / Cr Other Income 4900,
    # a receipt of exactly 42,000.00 on 2026-08-12.
    with app.app_context():
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        set_tenant(org1.id)
        from app.models.accounting import Account as AccountModel2
        bank = AccountModel2.query.filter_by(code="1010").first()
        misc_expense = AccountModel2.query.filter_by(code="5900").first()
        other_income = AccountModel2.query.filter_by(code="4900").first()
        bank_id2, misc_expense_id, other_income_id = str(bank.id), str(misc_expense.id), str(other_income.id)
        db.session.commit()

    r = c1.post("/journal/new", data={
        "entry_date": "2026-08-12", "memo": "Refund received", "reference": "",
        "account_id[]": [bank_id2, other_income_id],
        "project_id[]": ["", ""],
        "debit[]": ["42000", ""],
        "credit[]": ["", "42000"],
        "exchange_rate[]": ["1", "1"],
        "description[]": ["Refund received", "Refund received"],
    }, follow_redirects=True)
    assert r.status_code == 200

    csv_content = (
        "Date,Description,Debit,Credit\n"
        "2026-08-01,Opening balance carried forward,,\n"
        "2026-08-05,POS Purchase - Office Supplies,8500.00,\n"
        "2026-08-12,Refund received,,42000.00\n"
        "2026-08-20,Duplicate statement line (ignore me),1.00,\n"
    ).encode("utf-8")

    r = c1.post(f"/banking/{bank_id2}/import", data={
        "statement_file": (io.BytesIO(csv_content), "aug-statement.csv"),
    }, content_type="multipart/form-data", follow_redirects=True)
    assert b"Imported 3 transaction" in r.data, r.data[:1000]
    print("[OK] Org1 imported a 3-row CSV bank statement (opening-balance row correctly skipped)")

    r = c2.get(f"/banking/{bank_id2}/reconcile")
    assert r.status_code == 404, "TENANT LEAK: Org2 could open Org1's bank account reconciliation page"
    print("[OK] Org2 gets 404 trying to reconcile Org1's bank account by known UUID")

    r = c1.get(f"/banking/{bank_id2}/reconcile")
    assert b"POS Purchase - Office Supplies" in r.data
    assert b"Refund received" in r.data
    assert b"Duplicate statement line" in r.data
    # find the ids of the three imported bank transactions from their ignore-form actions
    txn_ids = re.findall(rb"/banking/transactions/([0-9a-f-]{36})/ignore", r.data)
    assert len(txn_ids) == 3, f"expected 3 unmatched transactions, found {len(txn_ids)}"
    print("[OK] All 3 imported transactions show up as unmatched, correctly isolated to this account")

    # Identify which txn is which by looking at the row order rendered
    # (template renders unmatched in txn_date order: 08-05, 08-12, 08-20).
    office_supplies_txn, refund_txn, duplicate_txn = [t.decode() for t in txn_ids]

    # Match the refund row to the existing manually-posted journal line.
    r = c1.get(f"/banking/{bank_id2}/reconcile")
    line_match = re.search(
        rb'value="([0-9a-f-]{36})">2026-08-12.*?Refund received', r.data, re.S)
    assert line_match, "could not find the candidate journal line option for the refund"
    journal_line_id = line_match.group(1).decode()
    r = c1.post(f"/banking/transactions/{refund_txn}/match-existing",
                data={"journal_line_id": journal_line_id}, follow_redirects=True)
    assert b"Matched to the existing journal entry" in r.data, r.data[:800]
    print("[OK] Org1 matched a bank transaction to an existing (already-posted) journal line")

    # Post a brand-new journal entry directly from the office-supplies row.
    r = c1.post(f"/banking/transactions/{office_supplies_txn}/match-new",
                data={"contra_account_id": misc_expense_id, "memo": "Office supplies"}, follow_redirects=True)
    assert b"Posted a new journal entry and matched it" in r.data, r.data[:800]
    print("[OK] Org1 posted a new journal entry directly from an unmatched bank transaction")

    # Ignore the duplicate row.
    r = c1.post(f"/banking/transactions/{duplicate_txn}/ignore", follow_redirects=True)
    assert b"Transaction ignored" in r.data, r.data[:800]
    print("[OK] Org1 ignored the duplicate statement line")

    with app.app_context():
        from app.models.banking import BankTransaction
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        set_tenant(org1.id)
        statuses = {str(t.id): t.status for t in BankTransaction.query.filter_by(bank_account_id=bank.id).all()}
        db.session.commit()
    assert statuses[office_supplies_txn] == "matched"
    assert statuses[refund_txn] == "matched"
    assert statuses[duplicate_txn] == "ignored"
    print("[OK] All three imported transactions ended in the correct status (matched, matched, ignored)")

    r = c1.get(f"/banking/{bank_id2}/reconcile")
    assert b"Nothing unmatched" in r.data
    print("[OK] Reconciliation screen now shows zero unmatched rows for this account")

    r = c2.get(f"/banking/{bank_id2}/import")
    assert r.status_code == 404, "TENANT LEAK: Org2 could open Org1's bank statement import page"
    print("[OK] Org2 gets 404 trying to import a statement against Org1's bank account by known UUID")

    # -----------------------------------------------------------------
    # Phase 8: report exports (PDF/Excel), prior-year comparatives, and
    # journal-entry document attachments.
    # -----------------------------------------------------------------
    with app.app_context():
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        set_tenant(org1.id)
        from app.models.accounting import Account as AccountModel3
        cash1 = AccountModel3.query.filter_by(code="1000").first()
        income1 = AccountModel3.query.filter_by(type="Income").first()
        cash1_id, income1_id = str(cash1.id), str(income1.id)
        db.session.commit()

    # A prior-year entry, so the comparative column has something to show.
    r = c1.post("/journal/new", data={
        "entry_date": "2025-09-15", "memo": "2025 grant", "reference": "",
        "account_id[]": [cash1_id, income1_id], "project_id[]": ["", ""],
        "debit[]": ["300000", "0"], "credit[]": ["0", "300000"],
        "exchange_rate[]": ["1", "1"], "description[]": ["a", "b"],
    }, follow_redirects=True)
    assert r.status_code == 200
    print("[OK] Org1 posted a prior-year (2025) journal entry for comparative testing")

    r = c1.get("/reports/trial-balance?as_of=2026-09-27")
    assert r.status_code == 200 and b"Prior Yr" in r.data
    print("[OK] Trial balance renders with a prior-year comparative column")

    for report, fmt, magic in [
        ("trial-balance", "pdf", b"%PDF"), ("trial-balance", "xlsx", b"PK"),
        ("income-statement", "pdf", b"%PDF"), ("income-statement", "xlsx", b"PK"),
        ("by-project", "pdf", b"%PDF"), ("by-project", "xlsx", b"PK"),
        ("balance-sheet", "pdf", b"%PDF"), ("balance-sheet", "xlsx", b"PK"),
    ]:
        r = c1.get(f"/reports/{report}/export/{fmt}?as_of=2026-09-27&start=2026-01-01&end=2026-12-31")
        assert r.status_code == 200 and r.data[:len(magic)] == magic, f"{report} {fmt} export failed"
    print("[OK] All 4 reports export cleanly as both PDF and Excel")

    # Supporting-document attachments on a journal entry.
    with app.app_context():
        from app.models.accounting import JournalEntry as JE
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        set_tenant(org1.id)
        an_entry = JE.query.filter_by(organization_id=org1.id).order_by(JE.entry_date.desc()).first()
        an_entry_id = str(an_entry.id)
        db.session.commit()

    r = c1.post(f"/journal/{an_entry_id}/attachments",
                data={"attachment_file": (io.BytesIO(b"%PDF-fake grant letter"), "grant_letter.pdf")},
                content_type="multipart/form-data", follow_redirects=True)
    assert b"Attached grant_letter.pdf" in r.data, r.data[:500]
    print("[OK] Org1 attached a supporting document to a journal entry")

    r = c1.get("/journal")
    assert b"grant_letter.pdf" in r.data
    print("[OK] Attached document appears on the journal listing page")

    r = c2.get("/journal")
    assert b"grant_letter.pdf" not in r.data, "TENANT LEAK: Org2 can see Org1's attachment filename!"
    print("[OK] Org2 cannot see Org1's attachment on its own (empty) journal listing")

    with app.app_context():
        from app.models.accounting import JournalAttachment
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        set_tenant(org1.id)
        att = JournalAttachment.query.filter_by(organization_id=org1.id).first()
        att_id = str(att.id)
        db.session.commit()

    r = c1.get(f"/journal/{an_entry_id}/attachments/{att_id}")
    assert r.status_code == 200 and r.data == b"%PDF-fake grant letter"
    print("[OK] Org1 downloaded the attachment and got back the exact bytes uploaded")

    r = c2.get(f"/journal/{an_entry_id}/attachments/{att_id}")
    assert r.status_code == 404, "TENANT LEAK: Org2 could download Org1's attachment by known UUID"
    print("[OK] Org2 gets 404 trying to download Org1's attachment by known UUID")

    r = c2.post(f"/journal/{an_entry_id}/attachments/{att_id}/delete", follow_redirects=True)
    assert r.status_code == 404, "TENANT LEAK: Org2 could delete Org1's attachment by known UUID"
    print("[OK] Org2 gets 404 trying to delete Org1's attachment by known UUID")

    r = c1.post(f"/journal/{an_entry_id}/attachments",
                data={"attachment_file": (io.BytesIO(b"MZ fake exe"), "virus.exe")},
                content_type="multipart/form-data", follow_redirects=True)
    assert b"aren&#39;t accepted" in r.data or b"aren't accepted" in r.data, r.data[:500]
    print("[OK] Executable upload rejected by extension allowlist")

    # A second attachment, which gets removed -- leaving the FIRST one
    # (grant_letter.pdf) in place so the RLS adversarial test that runs
    # after this script still has a journal_attachments row to attack.
    r = c1.post(f"/journal/{an_entry_id}/attachments",
                data={"attachment_file": (io.BytesIO(b"%PDF-superseded budget"), "old_budget.pdf")},
                content_type="multipart/form-data", follow_redirects=True)
    assert b"Attached old_budget.pdf" in r.data, r.data[:500]
    with app.app_context():
        org1 = Organization.query.filter_by(name="Social Action NGO").first()
        set_tenant(org1.id)
        from app.models.accounting import JournalAttachment as JA2
        old_att = JA2.query.filter_by(organization_id=org1.id, original_filename="old_budget.pdf").first()
        old_att_id = str(old_att.id)
        db.session.commit()
    r = c1.post(f"/journal/{an_entry_id}/attachments/{old_att_id}/delete", follow_redirects=True)
    assert b"Removed old_budget.pdf" in r.data, r.data[:500]
    print("[OK] Org1 removed one attachment successfully, leaving the other in place")

print("\nALL SMOKE TESTS PASSED")
