from datetime import date, datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file
from flask_login import login_required, current_user

from app.extensions import db
from app.decorators import admin_required
from app.models.accounting import Account, Project, JournalEntry, JournalLine
from app.models.payroll import Employee, PayrollRun, Payslip
from app.pdf.payslip_pdf import generate_payslip_pdf

payroll_bp = Blueprint("payroll", __name__)


# ---------------------------------------------------------------------------
# Employees
# ---------------------------------------------------------------------------

@payroll_bp.route("/employees")
@login_required
def employees_list():
    employees = Employee.query.order_by(Employee.full_name).all()
    return render_template("payroll/employees.html", employees=employees)


@payroll_bp.route("/employees/new", methods=["GET", "POST"])
@login_required
def employee_new():
    all_projects = Project.query.filter_by(active=True).order_by(Project.name).all()
    if request.method == "POST":
        e = Employee(
            organization_id=current_user.organization_id,
            staff_id=request.form.get("staff_id", "").strip(),
            full_name=request.form["full_name"].strip(),
            role_title=request.form.get("role_title", "").strip(),
            project_id=request.form.get("project_id") or None,
            basic_salary=float(request.form.get("basic_salary") or 0),
            housing_allowance=float(request.form.get("housing_allowance") or 0),
            transport_allowance=float(request.form.get("transport_allowance") or 0),
            other_allowance=float(request.form.get("other_allowance") or 0),
            pension_opt_in=bool(request.form.get("pension_opt_in")),
            nhf_opt_in=bool(request.form.get("nhf_opt_in")),
            annual_rent=float(request.form.get("annual_rent") or 0),
            start_date=datetime.strptime(request.form["start_date"], "%Y-%m-%d").date() if request.form.get("start_date") else None,
        )
        db.session.add(e)
        db.session.commit()
        flash("Employee added.", "success")
        return redirect(url_for("payroll.employees_list"))
    return render_template("payroll/employee_form.html", projects=all_projects, today=date.today().isoformat())


@payroll_bp.route("/employees/<uuid:employee_id>/toggle", methods=["POST"])
@login_required
@admin_required
def employee_toggle(employee_id):
    e = Employee.query.filter_by(id=employee_id).first_or_404()
    e.active = not e.active
    db.session.commit()
    flash(f"{e.full_name} {'activated' if e.active else 'deactivated'}.", "success")
    return redirect(url_for("payroll.employees_list"))


@payroll_bp.route("/employees/<uuid:employee_id>/edit", methods=["GET", "POST"])
@login_required
@admin_required
def employee_edit(employee_id):
    e = Employee.query.filter_by(id=employee_id).first_or_404()
    all_projects = Project.query.filter_by(active=True).order_by(Project.name).all()
    if request.method == "POST":
        e.staff_id = request.form.get("staff_id", "").strip()
        e.full_name = request.form["full_name"].strip()
        e.role_title = request.form.get("role_title", "").strip()
        e.project_id = request.form.get("project_id") or None
        e.basic_salary = float(request.form.get("basic_salary") or 0)
        e.housing_allowance = float(request.form.get("housing_allowance") or 0)
        e.transport_allowance = float(request.form.get("transport_allowance") or 0)
        e.other_allowance = float(request.form.get("other_allowance") or 0)
        e.pension_opt_in = bool(request.form.get("pension_opt_in"))
        e.nhf_opt_in = bool(request.form.get("nhf_opt_in"))
        e.annual_rent = float(request.form.get("annual_rent") or 0)
        start_date = request.form.get("start_date") or None
        e.start_date = datetime.strptime(start_date, "%Y-%m-%d").date() if start_date else None
        db.session.commit()
        flash(f"{e.full_name} updated. Note: this does not change payslips already generated for past runs.", "success")
        return redirect(url_for("payroll.employees_list"))
    return render_template("payroll/employee_form.html", employee=e, projects=all_projects,
                            today=e.start_date.isoformat() if e.start_date else date.today().isoformat())


# ---------------------------------------------------------------------------
# Payroll runs
# ---------------------------------------------------------------------------

@payroll_bp.route("/payroll")
@login_required
def payroll_runs():
    runs = PayrollRun.query.order_by(PayrollRun.period_year.desc(), PayrollRun.period_month.desc()).all()
    return render_template("payroll/payroll_runs.html", runs=runs)


@payroll_bp.route("/payroll/new", methods=["GET", "POST"])
@login_required
def payroll_new():
    if request.method == "POST":
        month = int(request.form["month"])
        year = int(request.form["year"])
        existing = PayrollRun.query.filter_by(period_month=month, period_year=year).first()
        if existing:
            flash("A payroll run for that period already exists.", "error")
            return redirect(url_for("payroll.payroll_detail", run_id=existing.id))

        run = PayrollRun(organization_id=current_user.organization_id,
                          period_month=month, period_year=year, status="draft")
        db.session.add(run)
        db.session.flush()

        for emp in Employee.query.filter_by(active=True).all():
            calc = emp.calc_payslip()
            db.session.add(Payslip(
                organization_id=current_user.organization_id,
                run_id=run.id, employee_id=emp.id, project_id=emp.project_id,
                gross=calc["gross"], pension_employee=calc["pension_employee"],
                pension_employer=calc["pension_employer"], nhf=calc["nhf"],
                paye=calc["paye"], net_pay=calc["net_pay"],
            ))
        db.session.commit()
        flash("Draft payroll run generated. Review and post it below.", "success")
        return redirect(url_for("payroll.payroll_detail", run_id=run.id))

    today = date.today()
    return render_template("payroll/payroll_new.html", month=today.month, year=today.year)


@payroll_bp.route("/payroll/<uuid:run_id>")
@login_required
def payroll_detail(run_id):
    run = PayrollRun.query.filter_by(id=run_id).first_or_404()
    bank_accounts = Account.query.filter_by(is_cash_or_bank=True, active=True).order_by(Account.code).all()
    return render_template("payroll/payroll_detail.html", run=run, totals=run.totals, bank_accounts=bank_accounts)


@payroll_bp.route("/payroll/<uuid:run_id>/payslip/<uuid:payslip_id>/bonus", methods=["POST"])
@login_required
def payroll_edit_bonus(run_id, payslip_id):
    run = PayrollRun.query.filter_by(id=run_id).first_or_404()
    if run.status != "draft":
        flash("This run is already posted and can no longer be edited.", "error")
        return redirect(url_for("payroll.payroll_detail", run_id=run_id))
    payslip = Payslip.query.filter_by(id=payslip_id).first_or_404()
    bonus = float(request.form.get("bonus") or 0)
    calc = payslip.employee.calc_payslip(bonus=bonus)
    payslip.bonus = bonus
    payslip.gross = calc["gross"]
    payslip.pension_employee = calc["pension_employee"]
    payslip.pension_employer = calc["pension_employer"]
    payslip.nhf = calc["nhf"]
    payslip.paye = calc["paye"]
    payslip.net_pay = calc["net_pay"]
    db.session.commit()
    return redirect(url_for("payroll.payroll_detail", run_id=run_id))


@payroll_bp.route("/payroll/<uuid:run_id>/payslip/<uuid:payslip_id>/pdf")
@login_required
def payslip_pdf(run_id, payslip_id):
    run = PayrollRun.query.filter_by(id=run_id).first_or_404()
    payslip = Payslip.query.filter_by(id=payslip_id, run_id=run_id).first_or_404()
    pdf_buf = generate_payslip_pdf(payslip, run)
    filename = f"Payslip-{payslip.employee.full_name.replace(' ', '-')}-{run.period_year}-{run.period_month:02d}.pdf"
    return send_file(pdf_buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


@payroll_bp.route("/payroll/<uuid:run_id>/post", methods=["POST"])
@login_required
def payroll_post(run_id):
    run = PayrollRun.query.filter_by(id=run_id).first_or_404()
    if run.status == "posted":
        flash("This run has already been posted.", "error")
        return redirect(url_for("payroll.payroll_detail", run_id=run_id))
    if not run.payslips:
        flash("Nothing to post — this run has no payslips.", "error")
        return redirect(url_for("payroll.payroll_detail", run_id=run_id))

    bank_acc = Account.query.filter_by(id=request.form.get("bank_account_id")).first()
    if not bank_acc or not bank_acc.is_cash_or_bank:
        flash("Choose which bank account salaries are paid from.", "error")
        return redirect(url_for("payroll.payroll_detail", run_id=run_id))

    rate = 1.0
    if bank_acc.currency != "NGN":
        rate = request.form.get("exchange_rate", type=float) or 0
        if rate <= 0:
            flash(f"Enter the Naira exchange rate for {bank_acc.name} ({bank_acc.currency}) to post this run.", "error")
            return redirect(url_for("payroll.payroll_detail", run_id=run_id))

    salaries_acc = Account.query.filter_by(code="5000").first()
    employer_pension_exp = Account.query.filter_by(code="5020").first()
    paye_payable = Account.query.filter_by(code="2200").first()
    pension_payable = Account.query.filter_by(code="2210").first()
    nhf_payable = Account.query.filter_by(code="2220").first()

    org_id = current_user.organization_id
    lines = []
    t = run.totals
    for p in run.payslips:
        lines.append(JournalLine(organization_id=org_id, account_id=salaries_acc.id, project_id=p.project_id,
                                  debit=p.gross, credit=0, description=f"Gross pay: {p.employee.full_name}"))
        if p.pension_employer:
            lines.append(JournalLine(organization_id=org_id, account_id=employer_pension_exp.id, project_id=p.project_id,
                                      debit=p.pension_employer, credit=0,
                                      description=f"Employer pension: {p.employee.full_name}"))
    # Net pay is always computed in Naira, so the bank line's native
    # amount is net_pay / rate -- same unrounded-intermediate rule as
    # elsewhere in the app.
    bank_native_amount = (t["net_pay"] / rate) if rate else t["net_pay"]
    lines.append(JournalLine(organization_id=org_id, account_id=bank_acc.id, debit=0, credit=bank_native_amount,
                              exchange_rate=rate, description="Net salaries paid"))
    if t["paye"]:
        lines.append(JournalLine(organization_id=org_id, account_id=paye_payable.id, debit=0, credit=t["paye"],
                                  description=f"PAYE — {run.label}"))
    if t["pension_employee"] + t["pension_employer"]:
        lines.append(JournalLine(organization_id=org_id, account_id=pension_payable.id, debit=0,
                                  credit=t["pension_employee"] + t["pension_employer"],
                                  description=f"Pension — {run.label}"))
    if t["nhf"]:
        lines.append(JournalLine(organization_id=org_id, account_id=nhf_payable.id, debit=0, credit=t["nhf"],
                                  description=f"NHF — {run.label}"))

    entry = JournalEntry(organization_id=org_id, entry_date=date(run.period_year, run.period_month, 28),
                          memo=f"Payroll — {run.label}",
                          source="payroll", created_by=current_user.id, lines=lines)
    db.session.add(entry)
    db.session.flush()
    run.journal_entry_id = entry.id
    run.bank_account_id = bank_acc.id
    run.exchange_rate = rate
    run.status = "posted"
    db.session.commit()
    flash(f"Payroll for {run.label} posted to the journal.", "success")
    return redirect(url_for("payroll.payroll_detail", run_id=run_id))


@payroll_bp.route("/reports/payroll-summary")
@login_required
def report_payroll_summary():
    runs = PayrollRun.query.filter_by(status="posted").order_by(PayrollRun.period_year, PayrollRun.period_month).all()
    return render_template("payroll/report_payroll_summary.html", runs=runs)
