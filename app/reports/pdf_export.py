"""Generic tabular report PDF generator, reusing the same navy/gold
styling as invoices and payslips (app/pdf/common.py) so an accountant
downloading a Trial Balance or Balance Sheet gets a document that looks
like it belongs to the same product, not a bare data dump.

Deliberately generic (headers/rows/totals as plain strings the caller
has already formatted) rather than one bespoke module per report --
the four accounting reports share the same "title + optional subtitle
+ one table + optional totals row" shape, so a single function covers
all of them."""
import io

from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

from app.pdf.common import (
    NAVY_900, GOLD_400, BORDER, MUTED,
    STYLE_TITLE, STYLE_BRAND, STYLE_MUTED, STYLE_BODY,
    new_document, footer,
)


def generate_report_pdf(title, subtitle, headers, rows, totals_row=None,
                         app_name="Acquiral", company_name="Admiral Sentinel"):
    """title: e.g. 'Trial Balance'. subtitle: e.g. 'As of 30 Sep 2026'.
    headers: list of column header strings. rows: list of lists of
    strings (already formatted -- this module does no number formatting
    of its own, so amounts/dates arrive exactly as shown on screen).
    totals_row: optional list of strings, rendered bold with a rule
    above it, same column count as headers."""
    buf = io.BytesIO()
    doc = new_document(buf, title=f"{title} — {app_name}")

    story = []

    header_table = Table(
        [[Paragraph(f'<font color="#D4A017">●</font> {app_name}', STYLE_BRAND),
          Paragraph(title.upper(), STYLE_TITLE)]],
        colWidths=[None, None],
    )
    header_table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (1, 0), (1, 0), "RIGHT"),
    ]))
    story.append(header_table)
    if subtitle:
        story.append(Paragraph(subtitle, STYLE_MUTED))
    story.append(Spacer(1, 14))

    body_rows = [headers] + rows
    if totals_row:
        body_rows.append(totals_row)

    table = Table(body_rows, repeatRows=1, hAlign="LEFT")
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY_900),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 0), (-1, 0), 0.5, NAVY_900),
        ("LINEBELOW", (0, 1), (-1, -2 if totals_row else -1), 0.4, BORDER),
    ]
    if totals_row:
        style += [
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("LINEABOVE", (0, -1), (-1, -1), 1, GOLD_400),
            ("TOPPADDING", (0, -1), (-1, -1), 7),
        ]
    table.setStyle(TableStyle(style))
    story.append(table)

    doc.build(story, onFirstPage=footer(app_name, company_name), onLaterPages=footer(app_name, company_name))
    buf.seek(0)
    return buf
