"""
HTTP Web Routes Integration Test for Purchase Orders, Users Management, and Workflow.
Exercises the Flask Test Client with session cookies, forms, and redirects.
"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

for mod in [
    "flask_migrate",
    "reportlab",
    "reportlab.lib",
    "reportlab.lib.colors",
    "reportlab.lib.pagesizes",
    "reportlab.lib.units",
    "reportlab.platypus",
    "reportlab.lib.styles",
    "openpyxl",
    "openpyxl.styles",
    "openpyxl.utils",
    "pdfplumber",
    "cryptography",
    "cryptography.fernet",
    "psycopg2",
    "psycopg2.extras",
]:
    if mod not in sys.modules:
        sys.modules[mod] = mock.MagicMock()

from app import create_app
from app.extensions import db
from app.models.tenant import Organization, User
from app.models.accounting import Project, Account, Funder
from app.models.purchasing import Vendor, PurchaseOrder, next_po_number

app = create_app()
app.config["TESTING"] = True
app.config["WTF_CSRF_ENABLED"] = False

print("\n" + "=" * 70)
print("TESTING ACQUIRAL WEB ROUTES VIA FLASK TEST CLIENT")
print("=" * 70)

client = app.test_client()

with app.app_context():
    db.create_all()

    # Create Org & Users
    org = Organization(name="Test NGO", slug="test-ngo")
    db.session.add(org)
    db.session.commit()

    admin_user = User(organization_id=org.id, username="admin1", full_name="Admin One", role="admin")
    admin_user.set_password("pass123")
    fm_user = User(organization_id=org.id, username="fm1", full_name="Finance One", role="finance_manager", approval_limit=1_000_000.0)
    fm_user.set_password("pass123")
    po_user = User(organization_id=org.id, username="po1", full_name="Officer One", role="program_officer")
    po_user.set_password("pass123")
    dir_user = User(organization_id=org.id, username="dir1", full_name="Director One", role="director")
    dir_user.set_password("pass123")

    funder = Funder(organization_id=org.id, name="Donor Fund")
    db.session.add_all([admin_user, fm_user, po_user, dir_user, funder])
    db.session.commit()

    project = Project(organization_id=org.id, name="WASH Project", funder_id=funder.id, budget=3_000_000.0)
    vendor = Vendor(organization_id=org.id, name="Logistics Ltd")
    expense_acct = Account(organization_id=org.id, code="5010", name="Project Ops", type="Expense")
    ap_acct = Account(organization_id=org.id, code="2000", name="Accounts Payable", type="Liability")
    db.session.add_all([project, vendor, expense_acct, ap_acct])
    db.session.commit()

# Helper login function
def login(username, password="pass123"):
    return client.post("/login", data={"username": username, "password": password}, follow_redirects=True)

# 1. Admin logs in and visits /users
print("[ROUTE 1] Testing Admin User Management (/users)...")
r = login("admin1")
assert r.status_code == 200
r = client.get("/users")
assert r.status_code == 200
assert b"User Management &amp; Approval Authority" in r.data
assert b"Officer One" in r.data
assert b"Finance Manager" in r.data
print("  ✓ Admin /users page rendered with roles and approval limit table.")

# 2. Admin updates Finance Manager limit via POST /users/<id>/update-role
print("[ROUTE 2] Testing Updating User Limit (/users/<id>/update-role)...")
r = client.post(f"/users/{fm_user.id}/update-role", data={
    "role": "finance_manager",
    "approval_limit": "1500000.0"
}, follow_redirects=True)
assert r.status_code == 200
with app.app_context():
    fm_refreshed = db.session.get(User, fm_user.id)
    assert fm_refreshed.approval_limit == 1_500_000.0
print("  ✓ Admin updated Finance Manager approval limit to ₦1,500,000.00.")

# 3. Program Officer logs in and creates a PO via /purchase-orders/new
print("[ROUTE 3] Testing PO Creation (/purchase-orders/new)...")
client.get("/logout")
login("po1")

r = client.get("/purchase-orders/new")
assert r.status_code == 200
assert b"New Purchase Order" in r.data
assert b"Project Budget Insight" in r.data

# Submit PO for ₦450,000
r = client.post("/purchase-orders/new", data={
    "vendor_id": str(vendor.id),
    "project_id": str(project.id),
    "po_date": "2026-11-27",
    "reference": "REQ-101",
    "notes": "Community water pumps and boreholes",
    "account_id[]": [str(expense_acct.id)],
    "description[]": ["Borehole parts"],
    "amount[]": ["450000.00"],
    "action": "submit"
}, follow_redirects=True)
assert r.status_code == 200
assert b"submitted for Finance Review" in r.data or b"PO-0001" in r.data
print("  ✓ PO-0001 submitted for Finance Review via web interface.")

# 4. View PO Detail page
print("[ROUTE 4] Testing PO Detail & Cryptographic Badges (/purchase-orders/<id>)...")
with app.app_context():
    po = PurchaseOrder.query.filter_by(po_number="PO-0001").first()
    po_id = po.id

r = client.get(f"/purchase-orders/{po_id}")
assert r.status_code == 200
assert b"PO-0001" in r.data
assert b"Audit Signatures Verified" in r.data
assert b"Project Budget Gauge" in r.data
# Maker-checker notification: po1 prepared it, so po1 sees segregation notice
assert b"Segregation of Duties Enforced" in r.data
print("  ✓ PO Detail page verified: Stepper, Budget Gauge, SHA-256 Audit Badge, and Segregation Notice present.")

# 5. Finance Manager logs in and approves PO
print("[ROUTE 5] Testing Finance Manager Review & Encumbrance Approval...")
client.get("/logout")
login("fm1")

r = client.post(f"/purchase-orders/{po_id}/finance-review", follow_redirects=True)
assert r.status_code == 200
assert b"Funds Encumbered" in r.data or b"Approved &amp; Committed" in r.data

with app.app_context():
    po_check = db.session.get(PurchaseOrder, po_id)
    assert po_check.status == "approved_committed"
    proj_check = db.session.get(Project, project.id)
    assert proj_check.reserved_funds == 450_000.0
    assert proj_check.available_budget == 2_550_000.0
print("  ✓ Finance Manager approved PO. ₦450,000.00 encumbered on project budget.")

# 6. Convert Approved PO to Vendor Bill
print("[ROUTE 6] Testing PO Conversion to Vendor Bill (/purchase-orders/<id>/convert-to-bill)...")
r = client.post(f"/purchase-orders/{po_id}/convert-to-bill", follow_redirects=True)
assert r.status_code == 200
assert b"Bill BILL-0001" in r.data or b"converted to Bill" in r.data

with app.app_context():
    po_check = db.session.get(PurchaseOrder, po_id)
    assert po_check.status == "fulfilled"
    assert po_check.bill_id is not None
    proj_check = db.session.get(Project, project.id)
    # Fulfilled PO is liquidated from active commitments
    assert proj_check.reserved_funds == 0.0
print("  ✓ Converted to Bill! Commitment liquidated into Accounts Payable flow.")

print("\n" + "=" * 70)
print("ALL WEB ROUTE INTEGRATION TESTS PASSED! (6/6 HTTP FLOWS VERIFIED)")
print("=" * 70)
