import uuid
from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin

BANK_TXN_STATUSES = ["unmatched", "matched", "ignored"]


class BankStatementImport(TenantScopedMixin, db.Model):
    """One uploaded statement file. Kept mainly as an audit trail -- who
    imported what file, when, against which account -- and as the parent
    for the BankTransaction rows it produced."""
    __tablename__ = "bank_statement_imports"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    bank_account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("accounts.id"), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    imported_by = db.Column(UUID(as_uuid=True), db.ForeignKey("users.id"), nullable=True)
    imported_at = db.Column(db.DateTime, server_default=db.func.now())
    row_count = db.Column(db.Integer, default=0)

    bank_account = db.relationship("Account")
    transactions = db.relationship("BankTransaction", backref="import_batch", cascade="all, delete-orphan", lazy=True)


class BankTransaction(TenantScopedMixin, db.Model):
    """A single line from an imported bank statement. `amount` is SIGNED:
    positive = money in (deposit/credit), negative = money out
    (withdrawal/debit) -- matching the sign convention the parser and the
    matching UI both use.

    Reconciliation never edits the journal transaction it matches to --
    it only records the link (`matched_journal_line_id`), or, for a bank
    line with no existing journal entry yet (bank charges, an unrecorded
    deposit), creates a brand-new journal entry and links to the new
    line it produces. Either way the journal stays the single source of
    truth for the books; this table is a reconciliation index over it."""
    __tablename__ = "bank_transactions"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    import_id = db.Column(UUID(as_uuid=True), db.ForeignKey("bank_statement_imports.id"), nullable=False)
    bank_account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("accounts.id"), nullable=False)
    txn_date = db.Column(db.Date, nullable=False)
    description = db.Column(db.String(500))
    amount = db.Column(db.Float, nullable=False)
    status = db.Column(db.String(20), default="unmatched")  # unmatched | matched | ignored
    matched_journal_line_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_lines.id"), nullable=True, unique=True)

    bank_account = db.relationship("Account")
    matched_journal_line = db.relationship("JournalLine")

    @property
    def is_deposit(self):
        return self.amount > 0
