import uuid
from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin

ACCOUNT_TYPES = ["Asset", "Liability", "Equity", "Income", "Expense"]
DEBIT_NORMAL = {"Asset", "Expense"}
CREDIT_NORMAL = {"Liability", "Equity", "Income"}

BASE_CURRENCY = "NGN"
CURRENCIES = {
    "NGN": {"symbol": "₦", "name": "Nigerian Naira"},
    "USD": {"symbol": "$", "name": "US Dollar"},
    "EUR": {"symbol": "€", "name": "Euro"},
    "GBP": {"symbol": "£", "name": "British Pound"},
}


def currency_symbol(code):
    return CURRENCIES.get(code, {}).get("symbol", code + " ")


class Funder(TenantScopedMixin, db.Model):
    __tablename__ = "funders"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(150), nullable=False)
    notes = db.Column(db.Text)
    projects = db.relationship("Project", backref="funder", lazy=True)

    __table_args__ = (db.UniqueConstraint("organization_id", "name", name="uq_funder_org_name"),)


class Project(TenantScopedMixin, db.Model):
    __tablename__ = "projects"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = db.Column(db.String(150), nullable=False)
    funder_id = db.Column(UUID(as_uuid=True), db.ForeignKey("funders.id"), nullable=True)
    budget = db.Column(db.Float, default=0.0)
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    active = db.Column(db.Boolean, default=True)


class Account(TenantScopedMixin, db.Model):
    __tablename__ = "accounts"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(150), nullable=False)
    type = db.Column(db.String(20), nullable=False)
    contra = db.Column(db.Boolean, default=False)
    active = db.Column(db.Boolean, default=True)
    currency = db.Column(db.String(3), default=BASE_CURRENCY)
    is_cash_or_bank = db.Column(db.Boolean, default=False)
    # Bank-identifying details, relevant only when is_cash_or_bank is set --
    # shown on the Chart of Accounts and useful for matching a physical
    # bank statement to the right ledger account at a glance. The bank's
    # own name is already part of the account name (e.g. "Zenith Bank -
    # USD Grant Account"), so it isn't duplicated as a separate field.
    account_number = db.Column(db.String(50))
    bank_branch = db.Column(db.String(150))

    # Account codes unique WITHIN an org's chart of accounts only.
    __table_args__ = (db.UniqueConstraint("organization_id", "code", name="uq_account_org_code"),)

    @property
    def normal_side(self):
        base = "debit" if self.type in DEBIT_NORMAL else "credit"
        if self.contra:
            return "credit" if base == "debit" else "debit"
        return base

    @property
    def symbol(self):
        return currency_symbol(self.currency)


class JournalEntry(TenantScopedMixin, db.Model):
    __tablename__ = "journal_entries"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_date = db.Column(db.Date, nullable=False)
    memo = db.Column(db.String(300))
    reference = db.Column(db.String(80))
    source = db.Column(db.String(30), default="manual")
    created_by = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    lines = db.relationship("JournalLine", backref="entry", cascade="all, delete-orphan", lazy=True)
    attachments = db.relationship("JournalAttachment", backref="entry", cascade="all, delete-orphan", lazy=True)

    @property
    def total_debit_base(self):
        return round(sum(l.debit_base for l in self.lines), 2)

    @property
    def total_credit_base(self):
        return round(sum(l.credit_base for l in self.lines), 2)

    @property
    def is_balanced(self):
        return round(self.total_debit_base - self.total_credit_base, 2) == 0


class JournalLine(TenantScopedMixin, db.Model):
    """Also tenant-scoped directly, even though it's reachable via its
    JournalEntry — belt-and-braces so RLS protects it even if a query
    reaches it without going through the parent entry."""
    __tablename__ = "journal_lines"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=False)
    account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("accounts.id"), nullable=False)
    project_id = db.Column(UUID(as_uuid=True), db.ForeignKey("projects.id"), nullable=True)
    debit = db.Column(db.Float, default=0.0)
    credit = db.Column(db.Float, default=0.0)
    exchange_rate = db.Column(db.Float, default=1.0)
    description = db.Column(db.String(300))

    account = db.relationship("Account")
    project = db.relationship("Project")

    @property
    def debit_base(self):
        return round((self.debit or 0) * (self.exchange_rate or 1), 2)

    @property
    def credit_base(self):
        return round((self.credit or 0) * (self.exchange_rate or 1), 2)


class JournalAttachment(TenantScopedMixin, db.Model):
    """Supporting documentation (a receipt, invoice, contract, grant
    letter, etc.) attached to a journal entry -- added so an external
    auditor can find the backing document for a sampled transaction
    inside the app rather than the accountant having to separately dig
    it out of email or a filing cabinet on request.

    The actual file bytes live on disk (see app/attachments.py), named
    by a random UUID rather than the user's original filename -- this
    avoids both filename collisions between organizations sharing a
    disk and path-traversal risk from an attacker-controlled filename.
    original_filename is kept purely for display and for the name the
    browser is offered on download."""
    __tablename__ = "journal_attachments"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=False)
    stored_filename = db.Column(db.String(64), nullable=False)  # <uuid4>.<ext>, on disk under ATTACHMENTS_DIR/<org_id>/
    original_filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(120))
    file_size = db.Column(db.Integer)
    uploaded_by = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    uploaded_at = db.Column(db.DateTime, server_default=db.func.now())


def seed_default_accounts(organization_id):
    defaults = [
        ("1000", "Cash on Hand", "Asset", False, True),
        ("1010", "Bank Account (NGN)", "Asset", False, True),
        ("1200", "Grants Receivable", "Asset", False, False),
        ("1500", "Office Equipment", "Asset", False, False),
        ("1510", "Vehicles", "Asset", False, False),
        ("1520", "Furniture & Fittings", "Asset", False, False),
        ("1530", "IT Equipment", "Asset", False, False),
        ("1540", "Land & Buildings", "Asset", False, False),
        ("1580", "Accumulated Depreciation", "Asset", True, False),
        ("2000", "Accounts Payable", "Liability", False, False),
        ("2100", "Deferred/Unspent Grant Income", "Liability", False, False),
        ("2200", "PAYE Payable", "Liability", False, False),
        ("2210", "Pension Payable", "Liability", False, False),
        ("2220", "NHF Payable", "Liability", False, False),
        ("2230", "VAT Payable", "Liability", False, False),
        ("2300", "Accounts Receivable (Invoices)", "Asset", False, False),
        ("3000", "Net Assets / Fund Balance", "Equity", False, False),
        ("4000", "Grant Income", "Income", False, False),
        ("4100", "Donation Income", "Income", False, False),
        ("4200", "Service/Invoice Income", "Income", False, False),
        ("4900", "Other Income", "Income", False, False),
        ("4950", "Realized Exchange Gain", "Income", False, False),
        ("5000", "Salaries & Staff Costs", "Expense", False, False),
        ("5020", "Employer Pension Contribution", "Expense", False, False),
        ("5100", "Program Activities", "Expense", False, False),
        ("5200", "Travel & Transport", "Expense", False, False),
        ("5300", "Workshops & Training", "Expense", False, False),
        ("5400", "Office Rent & Utilities", "Expense", False, False),
        ("5500", "Communications & Internet", "Expense", False, False),
        ("5600", "Office Supplies", "Expense", False, False),
        ("5700", "Bank Charges", "Expense", False, False),
        ("5850", "Depreciation Expense", "Expense", False, False),
        ("5900", "Other Expenses", "Expense", False, False),
        ("5950", "Realized Exchange Loss", "Expense", False, False),
    ]
    for code, name, type_, contra, is_bank in defaults:
        db.session.add(Account(organization_id=organization_id, code=code, name=name, type=type_,
                                contra=contra, is_cash_or_bank=is_bank, currency=BASE_CURRENCY))
    db.session.commit()
