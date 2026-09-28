import os
from collections import defaultdict
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, current_app
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.accounting import (
    Account, Funder, Project, JournalEntry, JournalLine, JournalAttachment, ACCOUNT_TYPES, CURRENCIES,
)
from app.models.assets import FixedAsset
from app.models.payroll import Employee, PayrollRun
from app.reports.pdf_export import generate_report_pdf
from app.reports.excel_export import generate_report_excel
from app.attachments import save_attachment, delete_attachment_file, attachment_path, AttachmentError

accounting_bp = Blueprint("accounting", __name__)

# ---------------------------------------------------------------------------
# Balance helpers
#
# These deliberately do NOT filter by organization_id themselves — RLS
# does that at the database level once the tenant context is set (see
# app/__init__.py: set_tenant, applied in before_request). That's the
# whole point of using RLS for these tables rather than hand-filtering
# everywhere the way users.py has to.
# ---------------------------------------------------------------------------

def _as_of_query(base_query, as_of):
    """Restricts a JournalLine query to lines whose entry is dated on or
    before as_of -- used for point-in-time reports (Trial Balance,
    Balance Sheet). as_of=None means all-time (unchanged prior behavior)."""
    if as_of is None:
        return base_query
    return base_query.join(JournalEntry, JournalLine.entry_id == JournalEntry.id) \
                      .filter(JournalEntry.entry_date <= as_of)


def _range_query(base_query, start, end):
    """Restricts a JournalLine query to lines whose entry falls within
    [start, end] inclusive -- used for flow reports (Income Statement,
    By-Project). Both None means all-time (unchanged prior behavior)."""
    if start is None and end is None:
        return base_query
    q = base_query.join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
    if start is not None:
        q = q.filter(JournalEntry.entry_date >= start)
    if end is not None:
        q = q.filter(JournalEntry.entry_date <= end)
    return q


def _prior_year_date(d):
    """Same calendar date one year earlier. Feb 29 falls back to Feb 28
    when the prior year isn't a leap year."""
    try:
        return d.replace(year=d.year - 1)
    except ValueError:
        return d.replace(year=d.year - 1, day=28)


def account_balance(account, as_of=None):
    """Signed native-currency balance (not converted) — per-account display.
    as_of=None (default) is the all-time balance; pass a date for the
    balance as it stood at the end of that day."""
    lines = _as_of_query(JournalLine.query.filter_by(account_id=account.id), as_of).all()
    debit = sum(l.debit for l in lines)
    credit = sum(l.credit for l in lines)
    return (debit - credit) if account.normal_side == "debit" else (credit - debit)


def account_balance_base(account, as_of=None):
    """Signed balance converted to the base currency (NGN) — use for
    anything that rolls up across accounts. as_of=None is all-time."""
    lines = _as_of_query(JournalLine.query.filter_by(account_id=account.id), as_of).all()
    debit = sum(l.debit_base for l in lines)
    credit = sum(l.credit_base for l in lines)
    return (debit - credit) if account.normal_side == "debit" else (credit - debit)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@accounting_bp.route("/dashboard")
@login_required
def dashboard():
    accounts = Account.query.all()
    balances_base = {a.id: account_balance_base(a) for a in accounts}

    bank_accounts = [a for a in accounts if a.is_cash_or_bank and a.active]
    cash_balance = sum(balances_base.get(a.id, 0) for a in bank_accounts)
    bank_breakdown = [
        {"account": a, "native_balance": account_balance(a), "base_balance": balances_base.get(a.id, 0)}
        for a in bank_accounts
    ]

    total_income = sum(balances_base.get(a.id, 0) for a in accounts if a.type == "Income")
    total_expense = sum(balances_base.get(a.id, 0) for a in accounts if a.type == "Expense")

    projects = Project.query.filter_by(active=True).all()
    project_summaries = []
    for p in projects:
        p_lines = JournalLine.query.filter_by(project_id=p.id).all()
        inc = sum(l.credit_base - l.debit_base for l in p_lines if l.account.type == "Income")
        exp = sum(l.debit_base - l.credit_base for l in p_lines if l.account.type == "Expense")
        project_summaries.append({
            "project": p, "income": inc, "expense": exp, "net": inc - exp,
            "budget": p.budget or 0,
            "budget_used_pct": round((exp / p.budget) * 100, 1) if p.budget else None,
        })

    recent_entries = (
        JournalEntry.query.order_by(JournalEntry.entry_date.desc(), JournalEntry.created_at.desc())
        .limit(8).all()
    )

    asset_count = FixedAsset.query.filter(FixedAsset.status != "Disposed").count()
    total_nbv = sum(a.net_book_value() for a in FixedAsset.query.filter(FixedAsset.status != "Disposed").all())
    employee_count = Employee.query.filter_by(active=True).count()
    latest_payroll = PayrollRun.query.order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc()).first()

    return render_template(
        "accounting/dashboard.html", cash_balance=cash_balance, total_income=total_income,
        total_expense=total_expense, project_summaries=project_summaries,
        recent_entries=recent_entries, bank_breakdown=bank_breakdown,
        asset_count=asset_count, total_nbv=total_nbv,
        employee_count=employee_count, latest_payroll=latest_payroll,
    )


# ---------------------------------------------------------------------------
# Chart of Accounts
# ---------------------------------------------------------------------------

@accounting_bp.route("/accounts")
@login_required
def accounts_list():
    accounts = Account.query.order_by(Account.code).all()
    grouped = defaultdict(list)
    balances = {}
    for a in accounts:
        grouped[a.type].append(a)
        balances[a.id] = account_balance(a)
    return render_template("accounting/accounts.html", grouped=grouped, types=ACCOUNT_TYPES, balances=balances)


@accounting_bp.route("/accounts/new", methods=["GET", "POST"])
@login_required
@admin_required
def account_new():
    if request.method == "POST":
        code = request.form["code"].strip()
        name = request.form["name"].strip()
        type_ = request.form["type"]
        contra = bool(request.form.get("contra"))
        currency = request.form.get("currency") or "NGN"
        is_cash_or_bank = bool(request.form.get("is_cash_or_bank"))
        account_number = request.form.get("account_number", "").strip() or None
        bank_branch = request.form.get("bank_branch", "").strip() or None
        if Account.query.filter_by(code=code).first():
            flash(f"Account code {code} already exists.", "error")
        else:
            db.session.add(Account(
                organization_id=current_user.organization_id,
                code=code, name=name, type=type_, contra=contra,
                currency=currency, is_cash_or_bank=is_cash_or_bank,
                account_number=account_number, bank_branch=bank_branch,
            ))
            db.session.commit()
            flash("Account created.", "success")
            return redirect(url_for("accounting.accounts_list"))
    return render_template("accounting/account_form.html", types=ACCOUNT_TYPES, currencies=CURRENCIES)


@accounting_bp.route("/accounts/<uuid:account_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def account_edit(account_id):
    account = Account.query.filter_by(id=account_id).first_or_404()
    if request.method == "POST":
        new_code = request.form["code"].strip()
        if new_code != account.code and Account.query.filter_by(code=new_code).first():
            flash(f"Account code {new_code} already exists.", "error")
            return render_template("accounting/account_form.html", types=ACCOUNT_TYPES, currencies=CURRENCIES, account=account)
        account.code = new_code
        account.name = request.form["name"].strip()
        account.type = request.form["type"]
        account.contra = bool(request.form.get("contra"))
        account.currency = request.form.get("currency") or "NGN"
        account.is_cash_or_bank = bool(request.form.get("is_cash_or_bank"))
        account.account_number = request.form.get("account_number", "").strip() or None
        account.bank_branch = request.form.get("bank_branch", "").strip() or None
        db.session.commit()
        flash("Account updated.", "success")
        return redirect(url_for("accounting.accounts_list"))
    return render_template("accounting/account_form.html", types=ACCOUNT_TYPES, currencies=CURRENCIES, account=account)


# ---------------------------------------------------------------------------
# Funders & Projects
# ---------------------------------------------------------------------------

@accounting_bp.route("/funders", methods=["GET", "POST"])
@login_required
def funders():
    if request.method == "POST":
        name = request.form["name"].strip()
        notes = request.form.get("notes", "").strip()
        if name and not Funder.query.filter_by(name=name).first():
            db.session.add(Funder(organization_id=current_user.organization_id, name=name, notes=notes))
            db.session.commit()
            flash("Funder added.", "success")
        return redirect(url_for("accounting.funders"))
    all_funders = Funder.query.order_by(Funder.name).all()
    return render_template("accounting/funders.html", funders=all_funders)


@accounting_bp.route("/funders/<uuid:funder_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def funder_edit(funder_id):
    funder = Funder.query.filter_by(id=funder_id).first_or_404()
    if request.method == "POST":
        funder.name = request.form["name"].strip()
        funder.notes = request.form.get("notes", "").strip()
        db.session.commit()
        flash("Funder updated.", "success")
        return redirect(url_for("accounting.funders"))
    return render_template("accounting/funder_edit.html", funder=funder)


@accounting_bp.route("/projects", methods=["GET", "POST"])
@login_required
def projects():
    if request.method == "POST":
        name = request.form["name"].strip()
        funder_id = request.form.get("funder_id") or None
        budget = float(request.form.get("budget") or 0)
        start_date = request.form.get("start_date") or None
        end_date = request.form.get("end_date") or None
        p = Project(
            organization_id=current_user.organization_id,
            name=name, funder_id=funder_id, budget=budget,
            start_date=datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None,
            end_date=datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None,
        )
        db.session.add(p)
        db.session.commit()
        flash("Project added.", "success")
        return redirect(url_for("accounting.projects"))
    all_projects = Project.query.order_by(Project.name).all()
    all_funders = Funder.query.order_by(Funder.name).all()
    return render_template("accounting/projects.html", projects=all_projects, funders=all_funders)


@accounting_bp.route("/projects/<uuid:project_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def project_edit(project_id):
    project = Project.query.filter_by(id=project_id).first_or_404()
    all_funders = Funder.query.order_by(Funder.name).all()
    if request.method == "POST":
        project.name = request.form["name"].strip()
        project.funder_id = request.form.get("funder_id") or None
        project.budget = float(request.form.get("budget") or 0)
        start_date = request.form.get("start_date") or None
        end_date = request.form.get("end_date") or None
        project.start_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
        project.end_date = datetime.strptime(end_date, "%Y-%m-%d").date() if end_date else None
        project.active = bool(request.form.get("active"))
        db.session.commit()
        flash("Project updated.", "success")
        return redirect(url_for("accounting.projects"))
    return render_template("accounting/project_edit.html", project=project, funders=all_funders)


# ---------------------------------------------------------------------------
# Journal Entries
# ---------------------------------------------------------------------------

@accounting_bp.route("/journal")
@login_required
def journal_list():
    entries = JournalEntry.query.order_by(JournalEntry.entry_date.desc(), JournalEntry.created_at.desc()).all()
    return render_template("accounting/journal.html", entries=entries)


@accounting_bp.route("/journal/new", methods=["GET", "POST"])
@login_required
def journal_new():
    accounts = Account.query.filter_by(active=True).order_by(Account.code).all()
    all_projects = Project.query.filter_by(active=True).order_by(Project.name).all()
    accounts_by_id = {str(a.id): a for a in accounts}

    if request.method == "POST":
        entry_date = datetime.strptime(request.form["entry_date"], "%Y-%m-%d").date()
        memo = request.form.get("memo", "").strip()
        reference = request.form.get("reference", "").strip()

        account_ids = request.form.getlist("account_id[]")
        project_ids = request.form.getlist("project_id[]")
        debits = request.form.getlist("debit[]")
        credits = request.form.getlist("credit[]")
        rates = request.form.getlist("exchange_rate[]")
        descriptions = request.form.getlist("description[]")

        lines = []
        total_debit_base = 0.0
        total_credit_base = 0.0
        error = None
        for acc_id, proj_id, deb, cred, rate, desc in zip(account_ids, project_ids, debits, credits, rates, descriptions):
            if not acc_id:
                continue
            deb_val = float(deb or 0)
            cred_val = float(cred or 0)
            if deb_val == 0 and cred_val == 0:
                continue
            account = accounts_by_id.get(acc_id)
            # NGN accounts always convert at 1:1 regardless of what was
            # submitted; only a genuinely foreign-currency account's rate
            # is ever used. Full precision kept here (no intermediate
            # rounding of native_amount) — rounding a native amount before
            # converting back to base introduced up to ~N1.20 mismatches
            # on odd exchange rates in the single-tenant app; only the
            # final displayed/summed totals are rounded.
            rate_val = 1.0 if (not account or account.currency == "NGN") else float(rate or 0)
            if account and account.currency != "NGN" and rate_val <= 0:
                error = f"Enter an exchange rate for {account.name} ({account.currency})."
                break
            total_debit_base += round(deb_val * rate_val, 2)
            total_credit_base += round(cred_val * rate_val, 2)
            lines.append(JournalLine(
                organization_id=current_user.organization_id,
                account_id=acc_id,
                project_id=proj_id if proj_id else None,
                debit=deb_val, credit=cred_val, exchange_rate=rate_val, description=desc,
            ))

        if error:
            flash(error, "error")
        elif len(lines) < 2:
            flash("A journal entry needs at least two lines.", "error")
        elif round(total_debit_base - total_credit_base, 2) != 0:
            flash(f"Entry is not balanced in Naira terms: total debit N{total_debit_base:,.2f} vs total credit N{total_credit_base:,.2f}. "
                  f"If this involves a currency conversion, add a line to Realized Exchange Gain/Loss for the difference.", "error")
        else:
            entry = JournalEntry(
                organization_id=current_user.organization_id,
                entry_date=entry_date, memo=memo, reference=reference,
                lines=lines, created_by=current_user.id, source="manual",
            )
            db.session.add(entry)
            db.session.commit()
            flash("Journal entry posted.", "success")
            return redirect(url_for("accounting.journal_list"))

    return render_template("accounting/journal_form.html", accounts=accounts, projects=all_projects,
                            today=date.today().isoformat())


@accounting_bp.route("/journal/<uuid:entry_id>/delete", methods=["POST"])
@login_required
@admin_required
def journal_delete(entry_id):
    entry = JournalEntry.query.filter_by(id=entry_id).first_or_404()
    if entry.source != "manual":
        flash("This entry was generated by another module — reverse it from there instead.", "error")
        return redirect(url_for("accounting.journal_list"))
    # The attachments row cascade (delete-orphan) removes the DB records
    # automatically, but the files on disk are outside SQLAlchemy's
    # reach -- collect their names before the delete so we can clean
    # them up on disk too, once the transaction actually commits.
    stored_filenames = [a.stored_filename for a in entry.attachments]
    org_id = entry.organization_id
    db.session.delete(entry)
    db.session.commit()
    for fn in stored_filenames:
        delete_attachment_file(current_app, org_id, fn)
    flash("Entry deleted.", "success")
    return redirect(url_for("accounting.journal_list"))


@accounting_bp.route("/journal/<uuid:entry_id>/attachments", methods=["POST"])
@login_required
def journal_attachment_upload(entry_id):
    entry = JournalEntry.query.filter_by(id=entry_id).first_or_404()
    f = request.files.get("attachment_file")
    try:
        stored_filename, original_filename, content_type, size = save_attachment(
            current_app, current_user.organization_id, f
        )
    except AttachmentError as e:
        flash(str(e), "error")
        return redirect(url_for("accounting.journal_list"))

    db.session.add(JournalAttachment(
        organization_id=current_user.organization_id,
        entry_id=entry.id, stored_filename=stored_filename, original_filename=original_filename,
        content_type=content_type, file_size=size, uploaded_by=current_user.id,
    ))
    db.session.commit()
    flash(f"Attached {original_filename}.", "success")
    return redirect(url_for("accounting.journal_list"))


@accounting_bp.route("/journal/<uuid:entry_id>/attachments/<uuid:attachment_id>")
@login_required
def journal_attachment_download(entry_id, attachment_id):
    # RLS already restricts this query to the current tenant; the
    # entry_id match additionally guards against an attachment id from
    # one entry being requested under a different entry's URL (a
    # belt-and-braces check, not a tenant-isolation one -- RLS already
    # makes the cross-tenant case impossible at the database level).
    att = JournalAttachment.query.filter_by(id=attachment_id, entry_id=entry_id).first_or_404()
    path = attachment_path(current_app, att.organization_id, att.stored_filename)
    if not os.path.exists(path):
        flash("That file is no longer available on the server.", "error")
        return redirect(url_for("accounting.journal_list"))
    return send_file(path, mimetype=att.content_type or "application/octet-stream",
                      as_attachment=True, download_name=att.original_filename)


@accounting_bp.route("/journal/<uuid:entry_id>/attachments/<uuid:attachment_id>/delete", methods=["POST"])
@login_required
def journal_attachment_delete(entry_id, attachment_id):
    att = JournalAttachment.query.filter_by(id=attachment_id, entry_id=entry_id).first_or_404()
    org_id, stored_filename, name = att.organization_id, att.stored_filename, att.original_filename
    db.session.delete(att)
    db.session.commit()
    delete_attachment_file(current_app, org_id, stored_filename)
    flash(f"Removed {name}.", "success")
    return redirect(url_for("accounting.journal_list"))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _trial_balance_rows(as_of):
    """Returns (rows, total_debit, total_credit) as of the given date.
    Each row is (account, bal_debit, bal_credit, bal_debit_base, bal_credit_base)."""
    accounts = Account.query.order_by(Account.code).all()
    rows = []
    total_debit = total_credit = 0.0
    for a in accounts:
        lines = _as_of_query(JournalLine.query.filter_by(account_id=a.id), as_of).all()
        debit = sum(l.debit for l in lines)
        credit = sum(l.credit for l in lines)
        debit_base = sum(l.debit_base for l in lines)
        credit_base = sum(l.credit_base for l in lines)
        if debit == 0 and credit == 0:
            continue
        if a.normal_side == "debit":
            bal_debit, bal_credit = max(debit - credit, 0), max(credit - debit, 0)
            bal_debit_base, bal_credit_base = max(debit_base - credit_base, 0), max(credit_base - debit_base, 0)
        else:
            bal_credit, bal_debit = max(credit - debit, 0), max(debit - credit, 0)
            bal_credit_base, bal_debit_base = max(credit_base - debit_base, 0), max(debit_base - credit_base, 0)
        total_debit += bal_debit_base
        total_credit += bal_credit_base
        rows.append((a, bal_debit, bal_credit, bal_debit_base, bal_credit_base))
    return rows, total_debit, total_credit


def _parse_as_of():
    raw = request.args.get("as_of")
    if raw:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            pass
    return date.today()


@accounting_bp.route("/reports/trial-balance")
@login_required
def report_trial_balance():
    as_of = _parse_as_of()
    prior_as_of = _prior_year_date(as_of)
    rows, total_debit, total_credit = _trial_balance_rows(as_of)
    prior_rows, prior_total_debit, prior_total_credit = _trial_balance_rows(prior_as_of)
    prior_by_account = {a.id: (pbd, pbc) for a, _, _, pbd, pbc in prior_rows}
    rows_with_prior = [
        (a, bd_n, bc_n, bd, bc) + prior_by_account.get(a.id, (0.0, 0.0))
        for a, bd_n, bc_n, bd, bc in rows
    ]
    return render_template(
        "accounting/report_trial_balance.html", rows=rows_with_prior,
        total_debit=total_debit, total_credit=total_credit,
        as_of=as_of, prior_as_of=prior_as_of,
        prior_total_debit=prior_total_debit, prior_total_credit=prior_total_credit,
    )


def _parse_range():
    """Default range is Jan 1 of the current year through today -- the
    natural 'this fiscal year to date' window for an NGO whose fiscal
    year follows the calendar year."""
    today = date.today()
    start_raw = request.args.get("start")
    end_raw = request.args.get("end")
    try:
        start = datetime.strptime(start_raw, "%Y-%m-%d").date() if start_raw else date(today.year, 1, 1)
    except ValueError:
        start = date(today.year, 1, 1)
    try:
        end = datetime.strptime(end_raw, "%Y-%m-%d").date() if end_raw else today
    except ValueError:
        end = today
    return start, end


def _income_statement_totals(start, end, project_id=None):
    q = _range_query(JournalLine.query, start, end)
    if project_id:
        q = q.filter(JournalLine.project_id == project_id)
    lines = q.all()

    income_rows = defaultdict(float)
    expense_rows = defaultdict(float)
    for l in lines:
        if l.account.type == "Income":
            income_rows[l.account] += (l.credit_base - l.debit_base)
        elif l.account.type == "Expense":
            expense_rows[l.account] += (l.debit_base - l.credit_base)
    return income_rows, expense_rows


@accounting_bp.route("/reports/income-statement")
@login_required
def report_income_statement():
    project_id = request.args.get("project_id")
    all_projects = Project.query.order_by(Project.name).all()
    start, end = _parse_range()
    prior_start, prior_end = _prior_year_date(start), _prior_year_date(end)

    income_rows, expense_rows = _income_statement_totals(start, end, project_id)
    prior_income_rows, prior_expense_rows = _income_statement_totals(prior_start, prior_end, project_id)

    total_income = sum(income_rows.values())
    total_expense = sum(expense_rows.values())
    prior_total_income = sum(prior_income_rows.values())
    prior_total_expense = sum(prior_expense_rows.values())

    selected_project = Project.query.filter_by(id=project_id).first() if project_id else None

    return render_template(
        "accounting/report_income_statement.html",
        income_rows=sorted(income_rows.items(), key=lambda x: x[0].code),
        expense_rows=sorted(expense_rows.items(), key=lambda x: x[0].code),
        prior_income_by_account={a.id: v for a, v in prior_income_rows.items()},
        prior_expense_by_account={a.id: v for a, v in prior_expense_rows.items()},
        total_income=total_income, total_expense=total_expense,
        net=total_income - total_expense,
        prior_total_income=prior_total_income, prior_total_expense=prior_total_expense,
        prior_net=prior_total_income - prior_total_expense,
        projects=all_projects, selected_project=selected_project,
        start=start, end=end, prior_start=prior_start, prior_end=prior_end,
    )


def _by_project_summaries(start, end, prior_start, prior_end):
    all_projects = Project.query.order_by(Project.name).all()
    summaries = []
    for p in all_projects:
        lines = _range_query(JournalLine.query.filter_by(project_id=p.id), start, end).all()
        prior_lines = _range_query(JournalLine.query.filter_by(project_id=p.id), prior_start, prior_end).all()
        income = sum(l.credit_base - l.debit_base for l in lines if l.account.type == "Income")
        expense = sum(l.debit_base - l.credit_base for l in lines if l.account.type == "Expense")
        prior_income = sum(l.credit_base - l.debit_base for l in prior_lines if l.account.type == "Income")
        prior_expense = sum(l.debit_base - l.credit_base for l in prior_lines if l.account.type == "Expense")
        summaries.append({
            "project": p, "funder": p.funder, "income": income, "expense": expense,
            "net": income - expense, "budget": p.budget or 0,
            "remaining": (p.budget or 0) - expense,
            "prior_income": prior_income, "prior_expense": prior_expense,
            "prior_net": prior_income - prior_expense,
        })
    return summaries


@accounting_bp.route("/reports/by-project")
@login_required
def report_by_project():
    start, end = _parse_range()
    prior_start, prior_end = _prior_year_date(start), _prior_year_date(end)
    summaries = _by_project_summaries(start, end, prior_start, prior_end)
    return render_template("accounting/report_by_project.html", summaries=summaries,
                            start=start, end=end, prior_start=prior_start, prior_end=prior_end)


def _balance_sheet_data(as_of):
    accounts = Account.query.order_by(Account.code).all()
    balances_native = {a: account_balance(a, as_of) for a in accounts}
    balances_base = {a: account_balance_base(a, as_of) for a in accounts}

    assets = [(a, balances_native[a], balances_base[a]) for a in accounts if a.type == "Asset" and balances_base[a] != 0]
    liabilities = [(a, balances_native[a], balances_base[a]) for a in accounts if a.type == "Liability" and balances_base[a] != 0]
    equity = [(a, balances_native[a], balances_base[a]) for a in accounts if a.type == "Equity" and balances_base[a] != 0]

    total_assets = sum((-b if a.contra else b) for a, _, b in assets)
    total_liabilities = sum(b for _, _, b in liabilities)
    total_equity_stated = sum(b for _, _, b in equity)

    total_income = sum(balances_base[a] for a in accounts if a.type == "Income")
    total_expense = sum(balances_base[a] for a in accounts if a.type == "Expense")
    net_surplus = total_income - total_expense
    total_equity = total_equity_stated + net_surplus

    return {
        "assets": assets, "liabilities": liabilities, "equity": equity,
        "total_assets": total_assets, "total_liabilities": total_liabilities,
        "total_equity": total_equity, "net_surplus": net_surplus,
        "balances_base_by_account": {a.id: b for a, b in balances_base.items()},
    }


@accounting_bp.route("/reports/balance-sheet")
@login_required
def report_balance_sheet():
    as_of = _parse_as_of()
    prior_as_of = _prior_year_date(as_of)
    data = _balance_sheet_data(as_of)
    prior_data = _balance_sheet_data(prior_as_of)

    return render_template(
        "accounting/report_balance_sheet.html",
        assets=data["assets"], liabilities=data["liabilities"], equity=data["equity"],
        total_assets=data["total_assets"], total_liabilities=data["total_liabilities"],
        total_equity=data["total_equity"], net_surplus=data["net_surplus"],
        prior_balances=prior_data["balances_base_by_account"],
        prior_total_assets=prior_data["total_assets"], prior_total_liabilities=prior_data["total_liabilities"],
        prior_total_equity=prior_data["total_equity"], prior_net_surplus=prior_data["net_surplus"],
        as_of=as_of, prior_as_of=prior_as_of,
    )


# ---------------------------------------------------------------------------
# Report exports (PDF / Excel) -- one download route per report, sharing
# the same period-parsing and data helpers as the on-screen version so
# the export always matches exactly what's on screen for that period.
# ---------------------------------------------------------------------------

def _export_response(fmt, filename_base, title, subtitle, headers, rows, totals_row=None, numeric_columns=None):
    if fmt == "pdf":
        buf = generate_report_pdf(title, subtitle, headers, rows, totals_row)
        return send_file(buf, mimetype="application/pdf", as_attachment=True,
                          download_name=f"{filename_base}.pdf")
    if fmt == "xlsx":
        buf = generate_report_excel(title, subtitle, headers, rows, totals_row, numeric_columns)
        return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                          as_attachment=True, download_name=f"{filename_base}.xlsx")
    flash("Unknown export format.", "error")
    return redirect(url_for("accounting.dashboard"))


@accounting_bp.route("/reports/trial-balance/export/<fmt>")
@login_required
def report_trial_balance_export(fmt):
    as_of = _parse_as_of()
    prior_as_of = _prior_year_date(as_of)
    rows, total_debit, total_credit = _trial_balance_rows(as_of)
    prior_rows, prior_total_debit, prior_total_credit = _trial_balance_rows(prior_as_of)
    prior_by_account = {a.id: (pbd, pbc) for a, _, _, pbd, pbc in prior_rows}

    headers = ["Code", "Account", "Debit (NGN)", "Credit (NGN)",
               f"Prior Yr Debit ({prior_as_of.isoformat()})", f"Prior Yr Credit ({prior_as_of.isoformat()})"]
    out_rows = []
    for a, d, c, d_base, c_base in rows:
        pbd, pbc = prior_by_account.get(a.id, (0.0, 0.0))
        out_rows.append([a.code, a.name, round(d_base, 2), round(c_base, 2), round(pbd, 2), round(pbc, 2)])
    totals_row = ["", "Total", round(total_debit, 2), round(total_credit, 2),
                  round(prior_total_debit, 2), round(prior_total_credit, 2)]

    return _export_response(
        fmt, f"trial-balance-{as_of.isoformat()}", "Trial Balance",
        f"As of {as_of.strftime('%d %b %Y')} (comparative: {prior_as_of.strftime('%d %b %Y')})",
        headers, out_rows, totals_row, numeric_columns={2, 3, 4, 5},
    )


@accounting_bp.route("/reports/income-statement/export/<fmt>")
@login_required
def report_income_statement_export(fmt):
    project_id = request.args.get("project_id")
    start, end = _parse_range()
    prior_start, prior_end = _prior_year_date(start), _prior_year_date(end)
    income_rows, expense_rows = _income_statement_totals(start, end, project_id)
    prior_income_rows, prior_expense_rows = _income_statement_totals(prior_start, prior_end, project_id)
    prior_income_by_account = {a.id: v for a, v in prior_income_rows.items()}
    prior_expense_by_account = {a.id: v for a, v in prior_expense_rows.items()}
    selected_project = Project.query.filter_by(id=project_id).first() if project_id else None

    headers = ["Code", "Account", "Amount (NGN)", "Prior Yr (NGN)"]
    out_rows = [["", "INCOME", "", ""]]
    for a, amt in sorted(income_rows.items(), key=lambda x: x[0].code):
        out_rows.append([a.code, a.name, round(amt, 2), round(prior_income_by_account.get(a.id, 0), 2)])
    out_rows.append(["", "Total Income", round(sum(income_rows.values()), 2), round(sum(prior_income_rows.values()), 2)])
    out_rows.append(["", "", "", ""])
    out_rows.append(["", "EXPENSES", "", ""])
    for a, amt in sorted(expense_rows.items(), key=lambda x: x[0].code):
        out_rows.append([a.code, a.name, round(amt, 2), round(prior_expense_by_account.get(a.id, 0), 2)])
    out_rows.append(["", "Total Expenses", round(sum(expense_rows.values()), 2), round(sum(prior_expense_rows.values()), 2)])

    net = sum(income_rows.values()) - sum(expense_rows.values())
    prior_net = sum(prior_income_rows.values()) - sum(prior_expense_rows.values())
    totals_row = ["", "Net Surplus / (Deficit)", round(net, 2), round(prior_net, 2)]

    subtitle = f"{start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')}"
    if selected_project:
        subtitle += f" — {selected_project.name}"
    subtitle += f" (comparative: {prior_start.strftime('%d %b %Y')} to {prior_end.strftime('%d %b %Y')})"

    return _export_response(
        fmt, f"income-statement-{start.isoformat()}-to-{end.isoformat()}", "Income Statement",
        subtitle, headers, out_rows, totals_row, numeric_columns={2, 3},
    )


@accounting_bp.route("/reports/by-project/export/<fmt>")
@login_required
def report_by_project_export(fmt):
    start, end = _parse_range()
    prior_start, prior_end = _prior_year_date(start), _prior_year_date(end)
    summaries = _by_project_summaries(start, end, prior_start, prior_end)

    headers = ["Project", "Funder", "Income (NGN)", "Expense (NGN)", "Net (NGN)",
               "Budget (NGN)", "Remaining (NGN)", "Prior Yr Income", "Prior Yr Expense", "Prior Yr Net"]
    out_rows = []
    for s in summaries:
        out_rows.append([
            s["project"].name, s["funder"].name if s["funder"] else "",
            round(s["income"], 2), round(s["expense"], 2), round(s["net"], 2),
            round(s["budget"], 2) if s["budget"] else "", round(s["remaining"], 2) if s["budget"] else "",
            round(s["prior_income"], 2), round(s["prior_expense"], 2), round(s["prior_net"], 2),
        ])

    subtitle = f"{start.strftime('%d %b %Y')} to {end.strftime('%d %b %Y')} (comparative: {prior_start.strftime('%d %b %Y')} to {prior_end.strftime('%d %b %Y')})"
    return _export_response(
        fmt, f"by-project-{start.isoformat()}-to-{end.isoformat()}", "Income & Expense by Project",
        subtitle, headers, out_rows, numeric_columns={2, 3, 4, 5, 6, 7, 8, 9},
    )


@accounting_bp.route("/reports/balance-sheet/export/<fmt>")
@login_required
def report_balance_sheet_export(fmt):
    as_of = _parse_as_of()
    prior_as_of = _prior_year_date(as_of)
    data = _balance_sheet_data(as_of)
    prior_data = _balance_sheet_data(prior_as_of)
    prior_balances = prior_data["balances_base_by_account"]

    headers = ["Code", "Account", "Amount (NGN)", f"Prior Yr ({prior_as_of.isoformat()})"]
    out_rows = [["", "ASSETS", "", ""]]
    for a, native, base in data["assets"]:
        val = -base if a.contra else base
        prior_val = (-1 if a.contra else 1) * prior_balances.get(a.id, 0)
        out_rows.append([a.code, a.name, round(val, 2), round(prior_val, 2)])
    out_rows.append(["", "Total Assets", round(data["total_assets"], 2), round(prior_data["total_assets"], 2)])
    out_rows.append(["", "", "", ""])
    out_rows.append(["", "LIABILITIES", "", ""])
    for a, native, base in data["liabilities"]:
        out_rows.append([a.code, a.name, round(base, 2), round(prior_balances.get(a.id, 0), 2)])
    out_rows.append(["", "Total Liabilities", round(data["total_liabilities"], 2), round(prior_data["total_liabilities"], 2)])
    out_rows.append(["", "", "", ""])
    out_rows.append(["", "EQUITY / NET ASSETS", "", ""])
    for a, native, base in data["equity"]:
        out_rows.append([a.code, a.name, round(base, 2), round(prior_balances.get(a.id, 0), 2)])
    out_rows.append(["", "Current Year Surplus/(Deficit)", round(data["net_surplus"], 2), round(prior_data["net_surplus"], 2)])
    totals_row = ["", "Total Liabilities + Equity",
                  round(data["total_liabilities"] + data["total_equity"], 2),
                  round(prior_data["total_liabilities"] + prior_data["total_equity"], 2)]

    subtitle = f"As of {as_of.strftime('%d %b %Y')} (comparative: {prior_as_of.strftime('%d %b %Y')})"
    return _export_response(
        fmt, f"balance-sheet-{as_of.isoformat()}", "Balance Sheet",
        subtitle, headers, out_rows, totals_row, numeric_columns={2, 3},
    )
