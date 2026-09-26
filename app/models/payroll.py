import uuid
from datetime import date

from sqlalchemy.dialects.postgresql import UUID
from app.extensions import db
from app.models.tenant import TenantScopedMixin

# ---------------------------------------------------------------------------
# Payroll (Nigeria Tax Act 2025 / NTA, effective 1 Jan 2026)
# ---------------------------------------------------------------------------

# Annual chargeable-income bands: (band width in Naira, rate).
# 0% on first 800,000; then progressive 15/18/21/23/25%.
PAYE_BANDS = [
    (800_000, 0.00),
    (2_200_000, 0.15),   # up to 3,000,000
    (9_000_000, 0.18),   # up to 12,000,000
    (13_000_000, 0.21),  # up to 25,000,000
    (25_000_000, 0.23),  # up to 50,000,000
    (float("inf"), 0.25),  # above 50,000,000
]

PENSION_EMPLOYEE_RATE = 0.08
PENSION_EMPLOYER_RATE = 0.10
NHF_RATE = 0.025
RENT_RELIEF_RATE = 0.20
RENT_RELIEF_CAP = 500_000


def compute_annual_paye(chargeable_income):
    tax = 0.0
    remaining = max(chargeable_income, 0)
    for width, rate in PAYE_BANDS:
        if remaining <= 0:
            break
        taxed = min(remaining, width)
        tax += taxed * rate
        remaining -= taxed
    return round(tax, 2)


class Employee(TenantScopedMixin, db.Model):
    __tablename__ = "employees"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    staff_id = db.Column(db.String(30))
    full_name = db.Column(db.String(150), nullable=False)
    role_title = db.Column(db.String(150))
    project_id = db.Column(UUID(as_uuid=True), db.ForeignKey("projects.id"), nullable=True)
    basic_salary = db.Column(db.Float, default=0.0)
    housing_allowance = db.Column(db.Float, default=0.0)
    transport_allowance = db.Column(db.Float, default=0.0)
    other_allowance = db.Column(db.Float, default=0.0)
    pension_opt_in = db.Column(db.Boolean, default=True)
    nhf_opt_in = db.Column(db.Boolean, default=False)
    annual_rent = db.Column(db.Float, default=0.0)
    start_date = db.Column(db.Date)
    active = db.Column(db.Boolean, default=True)

    project = db.relationship("Project")

    # staff_id was globally unique in the single-tenant app; here it's
    # unique only within the org (two different NGOs can both use "S001").
    __table_args__ = (db.UniqueConstraint("organization_id", "staff_id", name="uq_employee_org_staffid"),)

    def calc_payslip(self, bonus=0.0):
        basic = self.basic_salary or 0.0
        housing = self.housing_allowance or 0.0
        transport = self.transport_allowance or 0.0
        other = self.other_allowance or 0.0
        bonus = bonus or 0.0

        gross_monthly = basic + housing + transport + other + bonus
        pensionable_monthly = basic + housing + transport

        pension_employee_m = pensionable_monthly * PENSION_EMPLOYEE_RATE if self.pension_opt_in else 0.0
        pension_employer_m = pensionable_monthly * PENSION_EMPLOYER_RATE if self.pension_opt_in else 0.0
        nhf_m = basic * NHF_RATE if self.nhf_opt_in else 0.0

        annual_gross = gross_monthly * 12
        annual_pension_employee = pension_employee_m * 12
        annual_nhf = nhf_m * 12
        rent_relief = min((self.annual_rent or 0) * RENT_RELIEF_RATE, RENT_RELIEF_CAP)

        chargeable_income = max(annual_gross - annual_pension_employee - annual_nhf - rent_relief, 0)
        annual_paye = compute_annual_paye(chargeable_income)
        monthly_paye = round(annual_paye / 12, 2)

        net_monthly = round(gross_monthly - pension_employee_m - nhf_m - monthly_paye, 2)

        return {
            "gross": round(gross_monthly, 2),
            "pension_employee": round(pension_employee_m, 2),
            "pension_employer": round(pension_employer_m, 2),
            "nhf": round(nhf_m, 2),
            "paye": monthly_paye,
            "net_pay": net_monthly,
            "chargeable_income_annual": round(chargeable_income, 2),
        }


class PayrollRun(TenantScopedMixin, db.Model):
    __tablename__ = "payroll_runs"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    period_month = db.Column(db.Integer, nullable=False)
    period_year = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default="draft")  # draft / posted
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    journal_entry_id = db.Column(UUID(as_uuid=True), db.ForeignKey("journal_entries.id"), nullable=True)
    bank_account_id = db.Column(UUID(as_uuid=True), db.ForeignKey("accounts.id"), nullable=True)
    exchange_rate = db.Column(db.Float, default=1.0)  # only used if bank_account is non-NGN
    payslips = db.relationship("Payslip", backref="run", cascade="all, delete-orphan", lazy=True)
    bank_account = db.relationship("Account")

    @property
    def label(self):
        return date(self.period_year, self.period_month, 1).strftime("%B %Y")

    @property
    def totals(self):
        t = _zero_totals()
        for p in self.payslips:
            t["gross"] += p.gross
            t["pension_employee"] += p.pension_employee
            t["pension_employer"] += p.pension_employer
            t["nhf"] += p.nhf
            t["paye"] += p.paye
            t["net_pay"] += p.net_pay
        return t


def _zero_totals():
    return {"gross": 0.0, "pension_employee": 0.0, "pension_employer": 0.0, "nhf": 0.0, "paye": 0.0, "net_pay": 0.0}


class Payslip(TenantScopedMixin, db.Model):
    __tablename__ = "payslips"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = db.Column(UUID(as_uuid=True), db.ForeignKey("payroll_runs.id"), nullable=False)
    employee_id = db.Column(UUID(as_uuid=True), db.ForeignKey("employees.id"), nullable=False)
    project_id = db.Column(UUID(as_uuid=True), db.ForeignKey("projects.id"), nullable=True)
    bonus = db.Column(db.Float, default=0.0)
    gross = db.Column(db.Float, default=0.0)
    pension_employee = db.Column(db.Float, default=0.0)
    pension_employer = db.Column(db.Float, default=0.0)
    nhf = db.Column(db.Float, default=0.0)
    paye = db.Column(db.Float, default=0.0)
    net_pay = db.Column(db.Float, default=0.0)

    employee = db.relationship("Employee")
    project = db.relationship("Project")
