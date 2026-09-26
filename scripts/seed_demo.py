"""
Seeds the single, shared, public demo tenant (Organization.is_demo=True)
that /demo logs visitors into, and that app/__init__.py's before_request
guard makes read-only for every non-GET request.

IMPORTANT: uses entirely FICTIONAL data -- a fictional NGO, fictional
funders, fictional employees, fictional customers. This must never be
seeded from a real customer's export or real Social Action data: this
tenant is reachable by anyone on the internet who clicks "Try the Demo"
on admiralsentinel.com, so nothing sensitive or real belongs in it.

Goes through the actual application routes via Flask's test client
(the same technique scripts/smoke_test.py uses), not direct model
manipulation -- that way the seeded data is produced by exactly the
same business logic (journal postings, PAYE/pension calculations,
invoice VAT, etc.) a real user's actions would produce, instead of a
second, potentially-drifting copy of that logic living in this script.

Usage:
    PYTHONPATH=. python3 scripts/seed_demo.py            # create if absent
    PYTHONPATH=. python3 scripts/seed_demo.py --reset     # wipe and reseed
"""
import re
import sys

from app import create_app, set_tenant
from app.extensions import db
from app.models.tenant import Organization

DEMO_ORG_NAME = "Hopewell Development Initiative"
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "AcquiralDemo2026!"

app = create_app()
app.config["WTF_CSRF_ENABLED"] = False


def find_id(pattern, data):
    m = re.search(pattern, data)
    return m.group(1).decode() if m else None


def reset_existing():
    with app.app_context():
        existing = Organization.query.filter_by(is_demo=True).first()
        if existing:
            print(f"Deleting existing demo org '{existing.name}' ({existing.id})...")
            set_tenant(existing.id)
            # Tables carry ON DELETE behavior via FKs in some cases, but
            # not all -- delete children before parents to be safe rather
            # than rely on cascade being configured everywhere.
            from app.models.accounting import JournalLine, JournalEntry, Account, Project, Funder
            from app.models.assets import FixedAsset
            from app.models.payroll import Payslip, PayrollRun, Employee
            from app.models.invoicing import InvoiceLine, Invoice, Customer
            from app.models.banking import BankTransaction, BankStatementImport
            from app.models.tenant import User
            for model in [BankTransaction, BankStatementImport, InvoiceLine, Invoice, Customer,
                          Payslip, PayrollRun, Employee, FixedAsset, JournalLine, JournalEntry,
                          Account, Project, Funder, User]:
                model.query.filter_by(organization_id=existing.id).delete()
            db.session.commit()
            db.session.delete(existing)
            db.session.commit()
            print("Deleted.")


if "--reset" in sys.argv:
    reset_existing()

with app.app_context():
    if Organization.query.filter_by(is_demo=True).first():
        print("A demo organization already exists. Re-run with --reset to wipe and reseed.")
        sys.exit(0)

c = app.test_client()

print(f"Signing up demo organization '{DEMO_ORG_NAME}'...")
r = c.post("/signup", data={
    "org_name": DEMO_ORG_NAME, "username": DEMO_USERNAME,
    "full_name": "Demo Admin", "password": DEMO_PASSWORD,
}, follow_redirects=True)
assert r.status_code == 200, r.status_code

with app.app_context():
    org = Organization.query.filter_by(name=DEMO_ORG_NAME).order_by(Organization.created_at.desc()).first()
    org_id = org.id
    db.session.commit()
print(f"Organization created ({org_id}). Seeding data BEFORE flagging is_demo=True -- "
      f"the read-only guard would otherwise block this very script's own POST requests.")

# --- Funders & Projects -----------------------------------------------
c.post("/funders", data={"name": "Global Impact Foundation", "notes": "Multi-year WASH and livelihoods grants."})
c.post("/funders", data={"name": "Northbridge Trust", "notes": "Girls' education programming."})
r = c.get("/funders")
# Funders are listed alphabetically -- "Global Impact Foundation" sorts
# before "Northbridge Trust", so the first edit-link id is the one we want.
funder_ids = re.findall(rb"/funders/([0-9a-f-]{36})/edit", r.data)
global_impact_id = funder_ids[0].decode() if funder_ids else ""

c.post("/projects", data={
    "name": "Clean Water Access — Rivers State", "funder_id": global_impact_id,
    "budget": "45000000", "start_date": "2026-01-01", "end_date": "2026-12-31",
}, follow_redirects=True)
c.post("/projects", data={
    "name": "Girls' Education Fund", "funder_id": "",
    "budget": "28000000", "start_date": "2026-02-01", "end_date": "2026-11-30",
}, follow_redirects=True)
print("Seeded 2 funders and 2 projects.")

# --- Journal entries: grant receipts + program spend -------------------
with app.app_context():
    set_tenant(org_id)
    from app.models.accounting import Account
    bank = Account.query.filter_by(code="1010").first()
    grant_income = Account.query.filter_by(code="4000").first()
    program = Account.query.filter_by(code="5100").first()
    travel = Account.query.filter_by(code="5200").first()
    comms = Account.query.filter_by(code="5500").first()
    bank_id, grant_income_id, program_id, travel_id, comms_id = (
        str(bank.id), str(grant_income.id), str(program.id), str(travel.id), str(comms.id))
    db.session.commit()

def post_journal(entry_date, memo, lines):
    """lines: list of (account_id, debit, credit)"""
    data = {
        "entry_date": entry_date, "memo": memo, "reference": "",
        "account_id[]": [l[0] for l in lines],
        "project_id[]": ["" for _ in lines],
        "debit[]": [str(l[1]) for l in lines],
        "credit[]": [str(l[2]) for l in lines],
        "exchange_rate[]": ["1" for _ in lines],
        "description[]": [memo for _ in lines],
    }
    return c.post("/journal/new", data=data, follow_redirects=True)

post_journal("2026-02-03", "Grant disbursement — Global Impact Foundation", [(bank_id, 20000000, 0), (grant_income_id, 0, 20000000)])
post_journal("2026-03-10", "Borehole drilling — Phase 1", [(program_id, 6500000, 0), (bank_id, 0, 6500000)])
post_journal("2026-04-05", "Field team transport", [(travel_id, 850000, 0), (bank_id, 0, 850000)])
post_journal("2026-05-12", "Internet & communications — Q2", [(comms_id, 210000, 0), (bank_id, 0, 210000)])
post_journal("2026-06-01", "Grant disbursement — Northbridge Trust", [(bank_id, 14000000, 0), (grant_income_id, 0, 14000000)])
post_journal("2026-07-18", "Community training workshops", [(program_id, 3200000, 0), (bank_id, 0, 3200000)])
print("Posted 6 journal entries (grant receipts + program spend).")

# --- Fixed assets --------------------------------------------------------
c.post("/assets/new", data={
    "name": "Toyota Hilux (Field Vehicle)", "category": "Vehicles", "cost": "28000000",
    "salvage_value": "3000000", "useful_life_years": "6", "acquisition_date": "2026-01-20",
    "paid_from": "payable",
}, follow_redirects=True)
c.post("/assets/new", data={
    "name": "20KVA Generator", "category": "Office Equipment", "cost": "3200000",
    "salvage_value": "200000", "useful_life_years": "5", "acquisition_date": "2026-02-15",
    "paid_from": bank_id,
}, follow_redirects=True)
c.post("/assets/run-depreciation", data={"as_of": "2026-08-31"}, follow_redirects=True)
print("Seeded 2 fixed assets and ran depreciation to 2026-08-31.")

# --- Employees & payroll ---------------------------------------------------
employees = [
    {"staff_id": "HDI-001", "full_name": "Chiamaka Nwosu", "role_title": "Programs Manager",
     "basic_salary": "420000", "housing_allowance": "150000", "transport_allowance": "60000"},
    {"staff_id": "HDI-002", "full_name": "Tunde Bakare", "role_title": "Field Officer",
     "basic_salary": "280000", "housing_allowance": "90000", "transport_allowance": "45000"},
    {"staff_id": "HDI-003", "full_name": "Grace Effiong", "role_title": "Finance Officer",
     "basic_salary": "310000", "housing_allowance": "100000", "transport_allowance": "50000"},
]
for e in employees:
    c.post("/employees/new", data={**e, "pension_opt_in": "on", "start_date": "2026-01-01"}, follow_redirects=True)

r = c.post("/payroll/new", data={"month": "6", "year": "2026"}, follow_redirects=True)
run_id = find_id(rb"/payroll/([0-9a-f-]{36})", r.data)
c.post(f"/payroll/{run_id}/post", data={"bank_account_id": bank_id}, follow_redirects=True)
print(f"Seeded 3 employees and posted payroll run {run_id} for June 2026.")

# --- Customers & invoices (consulting/services income) ---------------------
c.post("/customers", data={"name": "Zenith Consulting Ltd", "email": "accounts@zenithconsulting.example",
                            "phone": "0803-000-1111", "address": "12 Adeola Odeku St, Victoria Island, Lagos"})
c.post("/customers", data={"name": "BlueWave Media", "email": "finance@bluewavemedia.example",
                            "phone": "0805-222-3333", "address": "5 Ademola Adetokunbo Cres, Wuse II, Abuja"})
r = c.get("/customers")
customer_id = find_id(rb"/customers/([0-9a-f-]{36})/edit", r.data)

# Draft invoice (untouched, shows the draft/editable state).
c.post("/invoices/new", data={
    "customer_id": customer_id, "issue_date": "2026-08-01", "due_date": "2026-08-31", "vat_rate": "7.5",
    "notes": "Net 30. Thank you for your business.",
    "description[]": ["Monitoring & evaluation training workshop"], "quantity[]": ["1"], "unit_price[]": ["850000"],
}, follow_redirects=True)

# Sent (unpaid) invoice.
r = c.post("/invoices/new", data={
    "customer_id": customer_id, "issue_date": "2026-07-01", "due_date": "2026-07-31", "vat_rate": "7.5",
    "notes": "Net 30.",
    "description[]": ["Grant compliance advisory — July"], "quantity[]": ["1"], "unit_price[]": ["650000"],
}, follow_redirects=True)
sent_invoice_id = find_id(rb"/invoices/([0-9a-f-]{36})", r.data)
c.post(f"/invoices/{sent_invoice_id}/send", follow_redirects=True)

# Fully paid invoice, showing the complete lifecycle.
r = c.post("/invoices/new", data={
    "customer_id": customer_id, "issue_date": "2026-05-01", "due_date": "2026-05-31", "vat_rate": "7.5",
    "notes": "Net 30.",
    "description[]": ["Annual financial systems review"], "quantity[]": ["1"], "unit_price[]": ["1200000"],
}, follow_redirects=True)
paid_invoice_id = find_id(rb"/invoices/([0-9a-f-]{36})", r.data)
c.post(f"/invoices/{paid_invoice_id}/send", follow_redirects=True)
c.post(f"/invoices/{paid_invoice_id}/record-payment", data={"bank_account_id": bank_id}, follow_redirects=True)
print("Seeded 2 customers and 3 invoices (draft, sent, paid).")

# --- Bank reconciliation: a small statement, partly worked through -------
import io
csv_content = (
    "Date,Description,Debit,Credit\n"
    "2026-08-02,POS - Office Supplies Ltd,45000.00,\n"
    "2026-08-10,Transfer - Zenith Consulting Ltd payment,,1400000.00\n"
    "2026-08-15,Bank Charges - COT,3200.00,\n"
).encode("utf-8")
c.post(f"/banking/{bank_id}/import", data={"statement_file": (io.BytesIO(csv_content), "aug-2026-statement.csv")},
       content_type="multipart/form-data", follow_redirects=True)

r = c.get(f"/banking/{bank_id}/reconcile")
txn_ids = re.findall(rb"/banking/transactions/([0-9a-f-]{36})/ignore", r.data)
if len(txn_ids) >= 3:
    office_supplies_txn, payment_txn, charges_txn = [t.decode() for t in txn_ids]
    with app.app_context():
        set_tenant(org_id)
        from app.models.accounting import Account as AccountModel
        misc_expense = AccountModel.query.filter_by(code="5700").first()  # Bank Charges
        misc_expense_id = str(misc_expense.id)
        db.session.commit()
    # Post the bank charges directly as a new entry -- leaves the other
    # two rows genuinely unmatched, so a demo visitor can see (and try,
    # harmlessly, since writes are blocked) the matching workflow itself.
    c.post(f"/banking/transactions/{charges_txn}/match-new",
           data={"contra_account_id": misc_expense_id, "memo": "Bank charges"}, follow_redirects=True)
    print("Imported a bank statement and resolved 1 of 3 transactions, leaving 2 for visitors to try matching.")
else:
    print(f"WARNING: expected 3 imported bank transactions, found {len(txn_ids)} -- statement import may have failed.")

# Flip the read-only flag LAST, now that every seeding request above has
# gone through while the org was still an ordinary, writable tenant.
with app.app_context():
    set_tenant(org_id)
    org = Organization.query.filter_by(id=org_id).first()
    org.is_demo = True
    db.session.commit()

print(f"\nDemo tenant ready and now flagged is_demo=True (read-only): '{DEMO_ORG_NAME}' ({org_id})")
print(f"One-click entry point: GET /demo")
print(f"Manual login: username='{DEMO_USERNAME}' password='{DEMO_PASSWORD}'")
