import io

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from app.pdf.common import (
    NAVY_900, GOLD_400, BORDER, MUTED,
    STYLE_TITLE, STYLE_BRAND, STYLE_MUTED_RIGHT, STYLE_BODY, STYLE_LABEL,
    new_document, money, footer,
)


def generate_payslip_pdf(payslip, run, app_name="Acquiral", company_name="Admiral Sentinel"):
    """Returns a BytesIO of a single-page payslip PDF for one Payslip
    within a PayrollRun. Amounts are always Naira (Nigerian payroll is
    computed in NGN regardless of which account eventually pays it --
    see Employee.calc_payslip / PayrollRun.bank_account)."""
    buf = io.BytesIO()
    employee = payslip.employee
    doc = new_document(buf, title=f"Payslip — {employee.full_name} — {run.label}")

    story = []

    header_table = Table(
        [[Paragraph(f'<font color="#D4A017">●</font> {app_name}', STYLE_BRAND),
          Paragraph("PAYSLIP", STYLE_TITLE)]],
        colWidths=[None, None],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4))
    story.append(Paragraph(f"Pay period: {run.label}", STYLE_MUTED_RIGHT))
    story.append(Spacer(1, 16))

    # Employee details
    emp_rows = [
        [Paragraph("EMPLOYEE", STYLE_LABEL), Paragraph("STAFF ID", STYLE_LABEL), Paragraph("ROLE", STYLE_LABEL)],
        [Paragraph(f"<b>{employee.full_name}</b>", STYLE_BODY),
         Paragraph(employee.staff_id or "—", STYLE_BODY),
         Paragraph(employee.role_title or "—", STYLE_BODY)],
    ]
    emp_table = Table(emp_rows, colWidths=[None, 45 * mm, 55 * mm])
    emp_table.setStyle(TableStyle([
        ("BOTTOMPADDING", (0, 0), (-1, 0), 2),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 10),
        ("LINEBELOW", (0, 1), (-1, 1), 0.5, BORDER),
    ]))
    story.append(emp_table)
    story.append(Spacer(1, 14))

    # Earnings / Deductions side by side
    def kv_table(title, rows, total_label, total_value):
        data = [[Paragraph(title, STYLE_LABEL), ""]]
        for label, value in rows:
            data.append([label, money(value)])
        data.append([Paragraph(f"<b>{total_label}</b>", STYLE_BODY),
                     Paragraph(f"<b>{money(total_value)}</b>", STYLE_BODY)])
        t = Table(data, colWidths=[45 * mm, 35 * mm])
        t.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 9.5),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("SPAN", (0, 0), (1, 0)),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, NAVY_900),
            ("LINEABOVE", (0, -1), (-1, -1), 0.5, BORDER),
        ]))
        return t

    basic = employee.basic_salary or 0
    housing = employee.housing_allowance or 0
    transport = employee.transport_allowance or 0
    other = employee.other_allowance or 0
    bonus = payslip.bonus or 0

    earnings = kv_table(
        "EARNINGS",
        [("Basic Salary", basic), ("Housing Allowance", housing),
         ("Transport Allowance", transport), ("Other Allowance", other),
         ("Bonus", bonus)],
        "Gross Pay", payslip.gross,
    )
    deductions = kv_table(
        "DEDUCTIONS",
        [("Pension (Employee, 8%)", payslip.pension_employee),
         ("NHF", payslip.nhf), ("PAYE Tax", payslip.paye)],
        "Total Deductions", payslip.pension_employee + payslip.nhf + payslip.paye,
    )

    side_by_side = Table([[earnings, deductions]], colWidths=[85 * mm, 85 * mm])
    side_by_side.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story.append(side_by_side)
    story.append(Spacer(1, 14))

    net_table = Table([["NET PAY", money(payslip.net_pay)]], colWidths=[45 * mm, 35 * mm], hAlign="LEFT")
    net_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), NAVY_900),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.white),
        ("FONTNAME", (0, 0), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (0, 0), 10),
        ("RIGHTPADDING", (1, 0), (1, 0), 10),
        ("LINEBELOW", (0, 0), (-1, 0), 2, GOLD_400),
    ]))
    story.append(net_table)

    story.append(Spacer(1, 18))
    story.append(Paragraph(
        f"Employer pension contribution (10%, not deducted from pay): {money(payslip.pension_employer)}",
        STYLE_BODY,
    ))

    doc.build(story, onFirstPage=footer(app_name, company_name), onLaterPages=footer(app_name, company_name))
    buf.seek(0)
    return buf
