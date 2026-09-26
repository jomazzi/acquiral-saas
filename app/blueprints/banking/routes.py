from datetime import date

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.models.accounting import Account, JournalEntry, JournalLine
from app.models.banking import BankStatementImport, BankTransaction
from app.banking.statement_parser import parse_statement, StatementParseError

banking_bp = Blueprint("banking", __name__, url_prefix="/banking")


@banking_bp.route("/")
@login_required
def bank_accounts():
    """Landing page: one row per cash/bank account with its unmatched
    transaction count, so the user picks which account's statement to
    import or reconcile."""
    accounts = Account.query.filter_by(is_cash_or_bank=True, active=True).order_by(Account.code).all()
    counts = {}
    for a in accounts:
        counts[a.id] = BankTransaction.query.filter_by(bank_account_id=a.id, status="unmatched").count()
    return render_template("banking/accounts.html", accounts=accounts, counts=counts)


@banking_bp.route("/<uuid:account_id>/import", methods=["GET", "POST"])
@login_required
def import_statement(account_id):
    account = Account.query.filter_by(id=account_id, is_cash_or_bank=True).first_or_404()

    if request.method == "POST":
        f = request.files.get("statement_file")
        if not f or not f.filename:
            flash("Choose a CSV, Excel, or PDF statement file to upload.", "error")
            return redirect(url_for("banking.import_statement", account_id=account_id))

        try:
            rows = parse_statement(f.filename, f.read())
        except StatementParseError as e:
            flash(str(e), "error")
            return redirect(url_for("banking.import_statement", account_id=account_id))

        batch = BankStatementImport(
            organization_id=current_user.organization_id,
            bank_account_id=account.id, original_filename=f.filename,
            imported_by=current_user.id, row_count=len(rows),
        )
        db.session.add(batch)
        db.session.flush()

        for r in rows:
            db.session.add(BankTransaction(
                organization_id=current_user.organization_id,
                import_id=batch.id, bank_account_id=account.id,
                txn_date=r["date"], description=r["description"], amount=r["amount"],
                status="unmatched",
            ))
        db.session.commit()
        flash(f"Imported {len(rows)} transaction(s) from {f.filename}.", "success")
        return redirect(url_for("banking.reconcile", account_id=account_id))

    return render_template("banking/import_form.html", account=account)


@banking_bp.route("/<uuid:account_id>/reconcile")
@login_required
def reconcile(account_id):
    account = Account.query.filter_by(id=account_id, is_cash_or_bank=True).first_or_404()
    unmatched = (BankTransaction.query.filter_by(bank_account_id=account_id, status="unmatched")
                 .order_by(BankTransaction.txn_date).all())
    matched = (BankTransaction.query.filter_by(bank_account_id=account_id, status="matched")
               .order_by(BankTransaction.txn_date.desc()).limit(25).all())
    ignored_count = BankTransaction.query.filter_by(bank_account_id=account_id, status="ignored").count()

    # Journal lines against this account not yet linked to any bank
    # transaction -- candidates the user can match an unmatched bank row
    # against, instead of creating a brand-new journal entry for it.
    already_matched_ids = [
        bt.matched_journal_line_id for bt in
        BankTransaction.query.filter(BankTransaction.matched_journal_line_id.isnot(None)).all()
    ]
    candidates_q = JournalLine.query.filter_by(account_id=account_id)
    if already_matched_ids:
        candidates_q = candidates_q.filter(~JournalLine.id.in_(already_matched_ids))
    candidate_lines = candidates_q.order_by(JournalLine.id.desc()).all()

    # Non-bank accounts, for the "create a new journal entry" side of a
    # match (e.g. bank charges, an unrecorded deposit).
    other_accounts = Account.query.filter(Account.id != account_id, Account.active.is_(True)) \
        .order_by(Account.code).all()

    return render_template(
        "banking/reconcile.html", account=account, unmatched=unmatched, matched=matched,
        ignored_count=ignored_count, candidate_lines=candidate_lines, other_accounts=other_accounts,
    )


@banking_bp.route("/transactions/<uuid:txn_id>/match-existing", methods=["POST"])
@login_required
def match_existing(txn_id):
    """Links an unmatched bank transaction to an EXISTING, already-posted
    journal line for the same account (e.g. a payment already recorded
    manually before the statement arrived) -- no new journal entry, just
    the reconciliation link."""
    txn = BankTransaction.query.filter_by(id=txn_id).first_or_404()
    if txn.status != "unmatched":
        flash("This transaction is no longer unmatched.", "error")
        return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))

    line_id = request.form.get("journal_line_id")
    line = JournalLine.query.filter_by(id=line_id, account_id=txn.bank_account_id).first()
    if not line:
        flash("Choose a journal line to match against.", "error")
        return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))
    if BankTransaction.query.filter_by(matched_journal_line_id=line.id).first():
        flash("That journal line is already matched to another transaction.", "error")
        return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))

    txn.matched_journal_line_id = line.id
    txn.status = "matched"
    db.session.commit()
    flash("Matched to the existing journal entry.", "success")
    return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))


@banking_bp.route("/transactions/<uuid:txn_id>/match-new", methods=["POST"])
@login_required
def match_new(txn_id):
    """Creates a brand-new journal entry from a bank transaction that has
    no existing counterpart in the books yet (bank charges, interest,
    an unrecorded deposit) and matches the bank line to the bank-side
    JournalLine it just created. A deposit (amount > 0) debits the bank
    account and credits the chosen contra account; a withdrawal (amount
    < 0) credits the bank account and debits the contra account."""
    txn = BankTransaction.query.filter_by(id=txn_id).first_or_404()
    if txn.status != "unmatched":
        flash("This transaction is no longer unmatched.", "error")
        return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))

    contra_account = Account.query.filter_by(id=request.form.get("contra_account_id")).first()
    if not contra_account:
        flash("Choose which account this transaction affects.", "error")
        return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))

    memo = request.form.get("memo", "").strip() or txn.description
    org_id = current_user.organization_id
    amount = abs(txn.amount)

    if txn.amount > 0:
        bank_line = JournalLine(organization_id=org_id, account_id=txn.bank_account_id, debit=amount, credit=0, description=memo)
        contra_line = JournalLine(organization_id=org_id, account_id=contra_account.id, debit=0, credit=amount, description=memo)
    else:
        bank_line = JournalLine(organization_id=org_id, account_id=txn.bank_account_id, debit=0, credit=amount, description=memo)
        contra_line = JournalLine(organization_id=org_id, account_id=contra_account.id, debit=amount, credit=0, description=memo)

    entry = JournalEntry(
        organization_id=org_id, entry_date=txn.txn_date, memo=memo,
        reference=None, source="bank_reconciliation", created_by=current_user.id,
        lines=[bank_line, contra_line],
    )
    db.session.add(entry)
    db.session.flush()

    txn.matched_journal_line_id = bank_line.id
    txn.status = "matched"
    db.session.commit()
    flash("Posted a new journal entry and matched it to this transaction.", "success")
    return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))


@banking_bp.route("/transactions/<uuid:txn_id>/ignore", methods=["POST"])
@login_required
def ignore_txn(txn_id):
    """For duplicate rows, or statement lines that don't belong in the
    books at all (e.g. an informational balance line the parser
    mistakenly picked up)."""
    txn = BankTransaction.query.filter_by(id=txn_id).first_or_404()
    if txn.status == "unmatched":
        txn.status = "ignored"
        db.session.commit()
        flash("Transaction ignored.", "success")
    return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))


@banking_bp.route("/transactions/<uuid:txn_id>/unmatch", methods=["POST"])
@login_required
def unmatch_txn(txn_id):
    """Undo a match or an ignore, WITHOUT touching any journal entry that
    may have been created -- reconciliation links can be freely undone;
    real accounting entries are reversed through the Journal, not by
    deleting them here."""
    txn = BankTransaction.query.filter_by(id=txn_id).first_or_404()
    txn.matched_journal_line_id = None
    txn.status = "unmatched"
    db.session.commit()
    flash("Transaction returned to unmatched.", "success")
    return redirect(url_for("banking.reconcile", account_id=txn.bank_account_id))
