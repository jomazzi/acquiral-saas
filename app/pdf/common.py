"""Shared PDF styling so invoices and payslips look like the same
product -- the navy/gold palette and Sora-ish heading weight from
app/static/style.css, translated into reportlab's own primitives (no
CSS/HTML available here, since these are generated with reportlab's
native flowables, not a headless-browser HTML-to-PDF pipeline -- kept
pure-Python/no system dependencies on purpose, since this app has to
run unmodified on both the Windows and Linux single-tenant deployments
documented in DEPLOYMENT.md, and a wkhtmltopdf/WeasyPrint-style
renderer would add a native binary dependency that varies by OS)."""
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate

NAVY_900 = colors.HexColor("#081B34")
NAVY_800 = colors.HexColor("#0F2D52")
GOLD_400 = colors.HexColor("#D4A017")
MUTED = colors.HexColor("#5B6B85")
BORDER = colors.HexColor("#DCE2EC")
BG = colors.HexColor("#F3F5F9")

PAGE_SIZE = A4
MARGIN = 18 * mm

_styles = getSampleStyleSheet()

STYLE_TITLE = ParagraphStyle(
    "AcquiralTitle", parent=_styles["Title"], fontName="Helvetica-Bold",
    fontSize=20, textColor=NAVY_900, spaceAfter=2, alignment=2,  # right
)
STYLE_BRAND = ParagraphStyle(
    "AcquiralBrand", parent=_styles["Normal"], fontName="Helvetica-Bold",
    fontSize=16, textColor=NAVY_900,
)
STYLE_MUTED = ParagraphStyle(
    "AcquiralMuted", parent=_styles["Normal"], fontName="Helvetica",
    fontSize=9, textColor=MUTED, leading=13,
)
STYLE_MUTED_RIGHT = ParagraphStyle(
    "AcquiralMutedRight", parent=STYLE_MUTED, alignment=2,
)
STYLE_BODY = ParagraphStyle(
    "AcquiralBody", parent=_styles["Normal"], fontName="Helvetica",
    fontSize=10, textColor=colors.HexColor("#16233B"), leading=14,
)
STYLE_LABEL = ParagraphStyle(
    "AcquiralLabel", parent=_styles["Normal"], fontName="Helvetica-Bold",
    fontSize=8, textColor=MUTED, leading=11,
)
STYLE_H2 = ParagraphStyle(
    "AcquiralH2", parent=_styles["Normal"], fontName="Helvetica-Bold",
    fontSize=12, textColor=NAVY_900, spaceAfter=4,
)


def new_document(buffer, title):
    return SimpleDocTemplate(
        buffer, pagesize=PAGE_SIZE,
        leftMargin=MARGIN, rightMargin=MARGIN, topMargin=MARGIN, bottomMargin=MARGIN,
        title=title,
    )


def money(amount, symbol="₦"):
    try:
        return f"{symbol}{amount:,.2f}"
    except (TypeError, ValueError):
        return f"{symbol}0.00"


def footer(app_name, company_name):
    def _draw(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(MUTED)
        canvas.drawCentredString(
            doc.pagesize[0] / 2, 12 * mm,
            f"{app_name} — a product of {company_name}",
        )
        canvas.restoreState()
    return _draw
