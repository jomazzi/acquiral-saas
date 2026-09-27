"""Generic tabular report Excel (.xlsx) generator, sharing the same
navy/gold brand colors as the PDF exports and the app's UI. Numeric
cells get a real Excel number format (not text), so an accountant can
drop the file straight into a pivot table or formula without having to
re-parse strings -- this is the one export where that matters, since a
PDF is print/read-only but an .xlsx is meant to be worked with further.
"""
import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

NAVY_FILL = PatternFill(start_color="081B34", end_color="081B34", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TOTALS_FONT = Font(bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14, color="081B34")
SUBTITLE_FONT = Font(size=9, color="5B6B85", italic=True)

NUMBER_FORMAT = "#,##0.00"


def generate_report_excel(title, subtitle, headers, rows, totals_row=None,
                           numeric_columns=None):
    """headers: list of column header strings. rows: list of lists --
    each cell either a string (written as-is) or an int/float (written
    as a real number with NUMBER_FORMAT applied). numeric_columns: set
    of 0-based column indices to right-align and currency-format even
    when a row happens to pass that cell as a string (kept optional --
    callers that already pass floats for amount columns don't need this)."""
    numeric_columns = numeric_columns or set()

    wb = Workbook()
    ws = wb.active
    ws.title = title[:31] if title else "Report"

    ws.cell(row=1, column=1, value=title).font = TITLE_FONT
    if subtitle:
        ws.cell(row=2, column=1, value=subtitle).font = SUBTITLE_FONT

    header_row_idx = 4
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=header_row_idx, column=col_idx, value=h)
        cell.font = HEADER_FONT
        cell.fill = NAVY_FILL
        cell.alignment = Alignment(horizontal="right" if col_idx - 1 in numeric_columns or col_idx > 1 else "left")

    row_idx = header_row_idx + 1
    for row in rows:
        for col_idx, val in enumerate(row, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            if isinstance(val, (int, float)):
                cell.number_format = NUMBER_FORMAT
                cell.alignment = Alignment(horizontal="right")
        row_idx += 1

    if totals_row:
        for col_idx, val in enumerate(totals_row, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=val)
            cell.font = TOTALS_FONT
            if isinstance(val, (int, float)):
                cell.number_format = NUMBER_FORMAT
                cell.alignment = Alignment(horizontal="right")

    # Reasonable auto-width: widest of header/content, capped so one
    # long description doesn't blow out the whole sheet.
    for col_idx, h in enumerate(headers, start=1):
        col_letter = get_column_letter(col_idx)
        max_len = len(str(h))
        for row in rows:
            if col_idx - 1 < len(row) and row[col_idx - 1] is not None:
                max_len = max(max_len, len(str(row[col_idx - 1])))
        ws.column_dimensions[col_letter].width = min(max(max_len + 2, 10), 40)

    ws.freeze_panes = ws.cell(row=header_row_idx + 1, column=1)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
