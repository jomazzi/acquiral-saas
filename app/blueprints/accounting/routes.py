from collections import defaultdict
from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.accounting import (
    Account, Funder, Project, JournalEntry, JournalLine, ACCOUNT_TYPES, CURRENCIES,
)
from app.models.assets import FixedAsset
from app.models.payroll import Employee, PayrollRun

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

def account_balance(account):
    """Signed native-currency balance (not converted) — per-account display."""
    lines = JournalLine.query.filter_by(account_id=account.id).all()
    debit = sum(l.debit for l in lines)
    credit = sum(l.credit for l in lines)
    return (debit - credit) if account.normal_side == "debit" else (credit - debit)


def account_balance_base(account):
    """Signed balance converted to the base currency (NGN) — use for
    anything that rolls up across accounts."""
    lines = JournalLine.query.filter_by(account_id=account.id).all()
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
        if Account.query.filter_by(code=code).first():
            flash(f"Account code {code} already exists.", "error")
        else:
            db.session.add(Account(
                organization_id=current_user.organization_id,
                code=code, name=name, type=type_, contra=contra,
                currency=currency, is_cash_or_bank=is_cash_or_bank,
            ))
            db.session.commit()
            flash("Account created.", "success")
            return redirect(url_for("accounting.accounts_list"))
    return render_template("accounting/account_form.html", types=ACCOUNT_TYPES, currencies=CURRENCIES)


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
    db.session.delete(entry)
    db.session.commit()
    flash("Entry deleted.", "success")
    return redirect(url_for("accounting.journal_list"))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

@accounting_bp.route("/reports/trial-balance")
@login_required
def report_trial_balance():
    accounts = Account.query.order_by(Account.code).all()
    rows = []
    total_debit = total_credit = 0.0
    for a in accounts:
        lines = JournalLine.query.filter_by(account_id=a.id).all()
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
    return render_template("accounting/report_trial_balance.html", rows=rows,
                            total_debit=total_debit, total_credit=total_credit)


@accounting_bp.route("/reports/income-statement")
@login_required
def report_income_statement():
    project_id = request.args.get("project_id")
    all_projects = Project.query.order_by(Project.name).all()

    q = JournalLine.query
    if project_id:
        q = q.filter_by(project_id=project_id)
    lines = q.all()

    income_rows = defaultdict(float)
    expense_rows = defaultdict(float)
    for l in lines:
        if l.account.type == "Income":
            income_rows[l.account] += (l.credit_base - l.debit_base)
        elif l.account.type == "Expense":
            expense_rows[l.account] += (l.debit_base - l.credit_base)

    total_income = sum(income_rows.values())
    total_expense = sum(expense_rows.values())

    selected_project = Project.query.filter_by(id=project_id).first() if project_id else None

    return render_template(
        "accounting/report_income_statement.html",
        income_rows=sorted(income_rows.items(), key=lambda x: x[0].code),
        expense_rows=sorted(expense_rows.items(), key=lambda x: x[0].code),
        total_income=total_income, total_expense=total_expense,
        net=total_income - total_expense,
        projects=all_projects, selected_project=selected_project,
    )


@accounting_bp.route("/reports/by-project")
@login_required
def report_by_project():
    all_projects = Project.query.order_by(Project.name).all()
    summaries = []
    for p in all_projects:
        lines = JournalLine.query.filter_by(project_id=p.id).all()
        income = sum(l.credit_base - l.debit_base for l in lines if l.account.type == "Income")
        expense = sum(l.debit_base - l.credit_base for l in lines if l.account.type == "Expense")
        summaries.append({
            "project": p, "funder": p.funder, "income": income, "expense": expense,
            "net": income - expense, "budget": p.budget or 0,
            "remaining": (p.budget or 0) - expense,
        })
    return render_template("accounting/report_by_project.html", summaries=summaries)


@accounting_bp.route("/reports/balance-sheet")
@login_required
def report_balance_sheet():
    accounts = Account.query.order_by(Account.code).all()
    balances_native = {a: account_balance(a) for a in accounts}
    balances_base = {a: account_balance_base(a) for a in accounts}

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

    return render_template(
        "accounting/report_balance_sheet.html", assets=assets, liabilities=liabilities, equity=equity,
        total_assets=total_assets, total_liabilities=total_liabilities,
        total_equity=total_equity, net_surplus=net_surplus,
    )
