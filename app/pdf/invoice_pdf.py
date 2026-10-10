import io

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from app.pdf.common import (
    NAVY_900, GOLD_400, BORDER, MUTED,
    STYLE_TITLE, STYLE_BRAND, STYLE_MUTED, STYLE_MUTED_RIGHT, STYLE_BODY, STYLE_LABEL, STYLE_H2,
    new_document, money, footer,
)


def generate_invoice_pdf(invoice, app_name="Acquiral", company_name="Admiral Sentinel", pay_url=None):
    """Returns a BytesIO of a single-page invoice PDF for the given
    Invoice ORM object (with its Customer and InvoiceLine relationships
    already loaded/loadable)."""
    buf = io.BytesIO()
    doc = new_document(buf, title=f"{invoice.invoice_number} — {app_name}")
    symbol = "₦" if invoice.currency == "NGN" else invoice.currency + " "

    story = []

    # Header: brand mark + name on the left, "INVOICE" + number on the right.
    header_table = Table(
        [[Paragraph(f'<font color="#D4A017">●</font> {app_name}', STYLE_BRAND),
          Paragraph("INVOICE", STYLE_TITLE)]],
        colWidths=[None, None],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4))

    meta_table = Table(
        [["", Paragraph(f"<b>{invoice.invoice_number}</b>", STYLE_MUTED_RIGHT)],
         ["", Paragraph(f"Issued: {invoice.issue_date.strftime('%d %b %Y')}", STYLE_MUTED_RIGHT)],
         ["", Paragraph(f"Due: {invoice.due_date.strftime('%d %b %Y')}" if invoice.due_date else "Due on receipt", STYLE_MUTED_RIGHT)],
         ["", Paragraph(f"Status: {invoice.status.upper()}", STYLE_MUTED_RIGHT)]],
        colWidths=[None, None],
    )
    story.append(meta_table)
    story.append(Spacer(1, 14))

    # Bill To block
    customer = invoice.customer
    bill_to_lines = [f"<b>{customer.name}</b>"]
    if customer.address:
        bill_to_lines.append(customer.address.replace("\n", "<br/>"))
    if customer.email:
        bill_to_lines.append(customer.email)
    if customer.phone:
        bill_to_lines.append(customer.phone)

    story.append(Paragraph("BILL TO", STYLE_LABEL))
    story.append(Paragraph("<br/>".join(bill_to_lines), STYLE_BODY))
    story.append(Spacer(1, 16))

    # Line items table
    rows = [["Description", "Qty", "Unit Price", "Amount"]]
    for line in invoice.lines:
        rows.append([
            line.description,
            f"{line.quantity:g}",
            money(line.unit_price, symbol),
            money(line.amount, symbol),
        ])

    items_table = Table(rows, colWidths=[None, 22 * mm, 32 * mm, 32 * mm], repeatRows=1)
    items_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), NAVY_900),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, NAVY_900),
        ("LINEBELOW", (0, 1), (-1, -2), 0.5, BORDER),
        ("LINEBELOW", (0, -1), (-1, -1), 0.5, BORDER),
    ]))
    story.append(items_table)
    story.append(Spacer(1, 10))

    # Totals block, right-aligned
    totals_rows = [["Subtotal", money(invoice.subtotal, symbol)]]
    if invoice.vat_rate:
        totals_rows.append([f"VAT ({invoice.vat_rate:g}%)", money(invoice.vat_amount, symbol)])
    totals_rows.append(["Total Due", money(invoice.total, symbol)])

    totals_table = Table(totals_rows, colWidths=[40 * mm, 32 * mm], hAlign="RIGHT")
    totals_style = [
        ("FONTNAME", (0, 0), (-1, -2), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
        ("TEXTCOLOR", (0, 0), (-1, -2), MUTED),
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("FONTSIZE", (0, -1), (-1, -1), 12),
        ("TEXTCOLOR", (0, -1), (-1, -1), NAVY_900),
        ("LINEABOVE", (0, -1), (-1, -1), 1, GOLD_400),
        ("TOPPADDING", (0, -1), (-1, -1), 6),
    ]
    totals_table.setStyle(TableStyle(totals_style))
    story.append(totals_table)

    if invoice.notes:
        story.append(Spacer(1, 18))
        story.append(Paragraph("NOTES", STYLE_LABEL))
        story.append(Paragraph(invoice.notes.replace("\n", "<br/>"), STYLE_BODY))

    if pay_url and invoice.status == "sent":
        story.append(Spacer(1, 18))
        story.append(Paragraph("PAY ONLINE", STYLE_LABEL))
        story.append(Paragraph(f'<link href="{pay_url}" color="#1F4E8C">{pay_url}</link>', STYLE_BODY))

    doc.build(story, onFirstPage=footer(app_name, company_name), onLaterPages=footer(app_name, company_name))
    buf.seek(0)
    return buf
