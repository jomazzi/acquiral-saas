"""
Bank statement import parsing: CSV, Excel (.xlsx), and PDF only.

Deliberately NOT in scope (per the agreed roadmap): live bank-API
connections and image/OCR extraction. A PDF statement is parsed via
pdfplumber's table extraction, which reads the PDF's actual text/table
structure -- it does not rasterize the page and run OCR, so a scanned
image-only PDF will not parse (this is by design, not a bug: OCR
introduces silent transcription errors that are dangerous in financial
data, so it was intentionally left out of scope).

Every format normalizes to the same shape: a list of
{date: datetime.date, description: str, amount: float} dicts, where
amount is SIGNED -- positive for money coming into the account (a
deposit/credit), negative for money going out (a withdrawal/debit).
This matches the sign convention used throughout the reconciliation
matching logic in app/blueprints/banking/routes.py.
"""
import csv
import io
import re
from datetime import datetime

import openpyxl
import pdfplumber


class StatementParseError(Exception):
    pass


# Column header aliases seen across common Nigerian bank statement
# exports (GTBank, Zenith, UBA, Access, First Bank, etc.) and generic
# CSV/Excel exports. Matching is case-insensitive and ignores
# punctuation/whitespace differences.
DATE_ALIASES = ["date", "transactiondate", "valuedate", "transdate", "trandate", "postingdate"]
DESC_ALIASES = ["description", "narration", "remarks", "details", "particulars", "transactiondetails"]
DEBIT_ALIASES = ["debit", "withdrawal", "dr", "debitamount", "moneyout", "withdrawals"]
CREDIT_ALIASES = ["credit", "deposit", "cr", "creditamount", "moneyin", "deposits"]
AMOUNT_ALIASES = ["amount", "value", "transactionamount"]

DATE_FORMATS = [
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y", "%d %b %Y", "%d-%b-%Y",
    "%d %B %Y", "%d.%m.%Y", "%Y/%m/%d", "%b %d, %Y", "%d/%m/%y", "%d-%m-%y",
]


def _norm_header(h):
    return re.sub(r"[^a-z0-9]", "", str(h or "").lower())


def _match_column(headers_norm, aliases):
    for i, h in enumerate(headers_norm):
        if h in aliases:
            return i
    return None


def _parse_date(raw):
    raw = str(raw).strip()
    if not raw:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_number(raw):
    """Strips currency symbols, thousands separators, parentheses (used
    by some exports for negative numbers), and returns a float, or None
    if there's nothing numeric there."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in ("-", "."):
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return -abs(val) if negative else val


def _rows_from_table(header_row, data_rows):
    headers_norm = [_norm_header(h) for h in header_row]
    date_col = _match_column(headers_norm, DATE_ALIASES)
    desc_col = _match_column(headers_norm, DESC_ALIASES)
    debit_col = _match_column(headers_norm, DEBIT_ALIASES)
    credit_col = _match_column(headers_norm, CREDIT_ALIASES)
    amount_col = _match_column(headers_norm, AMOUNT_ALIASES)

    if date_col is None:
        raise StatementParseError(
            "Could not find a Date column in the statement. Expected a header "
            "like 'Date', 'Transaction Date', or 'Value Date'."
        )
    if debit_col is None and credit_col is None and amount_col is None:
        raise StatementParseError(
            "Could not find Debit/Credit or Amount columns in the statement."
        )

    results = []
    for row in data_rows:
        if date_col >= len(row):
            continue
        txn_date = _parse_date(row[date_col])
        if not txn_date:
            continue  # skip subtotal/footer rows etc. that don't parse as a date

        description = str(row[desc_col]).strip() if desc_col is not None and desc_col < len(row) else ""

        if amount_col is not None and amount_col < len(row):
            amount = _parse_number(row[amount_col])
        else:
            debit = _parse_number(row[debit_col]) if debit_col is not None and debit_col < len(row) else None
            credit = _parse_number(row[credit_col]) if credit_col is not None and credit_col < len(row) else None
            debit = abs(debit) if debit else 0.0
            credit = abs(credit) if credit else 0.0
            amount = credit - debit

        if amount is None or amount == 0:
            continue

        results.append({"date": txn_date, "description": description or "(no description)", "amount": amount})

    if not results:
        raise StatementParseError("No transaction rows could be read from this file.")
    return results


def parse_csv(file_bytes):
    text = file_bytes.decode("utf-8-sig", errors="replace")
    reader = list(csv.reader(io.StringIO(text)))
    reader = [r for r in reader if any(c.strip() for c in r)]  # drop blank lines
    if len(reader) < 2:
        raise StatementParseError("The CSV file has no data rows.")
    return _rows_from_table(reader[0], reader[1:])


def parse_excel(file_bytes):
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), data_only=True)
    sheet = wb.worksheets[0]
    rows = [[c.value if c.value is not None else "" for c in row] for row in sheet.iter_rows()]
    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if len(rows) < 2:
        raise StatementParseError("The Excel file has no data rows.")
    return _rows_from_table(rows[0], rows[1:])


_PDF_TABLE_STRATEGIES = [
    None,  # pdfplumber's default: detect ruled lines (works for statements with a drawn grid/borders)
    {"vertical_strategy": "text", "horizontal_strategy": "text"},  # fallback: infer columns from text alignment
]


def _extract_tables(pdf, settings):
    header_row = None
    all_rows = []
    for page in pdf.pages:
        tables = page.extract_tables(table_settings=settings) if settings else page.extract_tables()
        for table in tables or []:
            if not table:
                continue
            if header_row is None:
                header_row = table[0]
                all_rows.extend(table[1:])
            else:
                # Subsequent pages often repeat the header row -- skip it
                # if it looks the same as the first page's header.
                same_header = _norm_header(table[0][0] if table[0] else "") == _norm_header(header_row[0] if header_row else "")
                all_rows.extend(table[1:] if same_header else table)
    return header_row, all_rows


def parse_pdf(file_bytes):
    """Extracts the first table-like structure found across all pages,
    trying a couple of pdfplumber table-detection strategies since bank
    statement exports vary: some draw a visible grid (the default,
    line-based strategy finds these reliably), others just align text
    into columns with no ruling (the text-based fallback handles those,
    but it's a heuristic -- tested and found to occasionally misjudge a
    column boundary on a sparse row, e.g. one where the Debit column is
    blank, and silently drop that row rather than parse it wrong). This
    is not a correctness risk for the books themselves: a parsed row
    only ever becomes an "unmatched" BankTransaction sitting in the
    reconciliation queue for a human to review and match by hand --
    nothing is ever posted to the journal automatically from here. A
    dropped row just means the user's reconciliation won't balance
    against their real bank balance, which is exactly the kind of thing
    reconciliation is meant to surface, and they can re-import from a
    CSV/Excel export (deterministic, no guessing) if that happens.
    Either way this only works on a text-based/table PDF -- a scanned
    image-only PDF will not parse, by design (see module docstring)."""
    header_row, all_rows = None, []
    try:
        pdf_ctx = pdfplumber.open(io.BytesIO(file_bytes))
    except Exception as exc:
        # pdfminer raises its own exception types (not ours) for a
        # password-protected PDF or one with a malformed/corrupted
        # structure -- both are common for bank-issued statement PDFs
        # (many Nigerian banks encrypt the download with the account
        # number or the customer's date of birth as the password).
        # Without this, either case previously reached the user as a
        # raw 500 Internal Server Error instead of an actionable
        # message, since pdfplumber.open() fails before any of our own
        # error handling below ever runs.
        raise StatementParseError(
            "This PDF could not be opened -- it may be password-protected "
            "(common for bank-issued statement downloads) or corrupted. "
            "If it's password-protected, remove the password first (open it "
            "in a PDF reader, enter the password, then 'Print to PDF' or "
            "'Save As' to produce an unprotected copy) and re-import that "
            "file. CSV or Excel exports from your bank, if available, avoid "
            "this issue entirely."
        ) from exc

    with pdf_ctx as pdf:
        for settings in _PDF_TABLE_STRATEGIES:
            header_row, all_rows = _extract_tables(pdf, settings)
            if header_row is not None:
                break
    if header_row is None:
        raise StatementParseError(
            "No table could be found in this PDF. Only text-based statements with "
            "an actual table structure are supported -- a scanned/image-only PDF "
            "cannot be read (OCR is intentionally not supported for financial data)."
        )
    return _rows_from_table(header_row, all_rows)


def parse_statement(filename, file_bytes):
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext == "csv":
        return parse_csv(file_bytes)
    if ext in ("xlsx", "xlsm"):
        return parse_excel(file_bytes)
    if ext == "pdf":
        return parse_pdf(file_bytes)
    raise StatementParseError(f"Unsupported file type '.{ext}'. Upload a .csv, .xlsx, or .pdf bank statement.")
