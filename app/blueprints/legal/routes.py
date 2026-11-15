"""Public legal pages: terms, privacy policy, DPA and service levels.
Edit LEGAL below before deploying; bump TERMS_VERSION whenever the text
changes materially (it is stored against each signup)."""
from flask import Blueprint, render_template

legal_bp = Blueprint("legal", __name__)

TERMS_VERSION = "2026-11-14"

# FILL IN every value. Any text still in [square brackets] makes
# scripts/legal_pages_test.py fail, so nothing goes live half-filled.
LEGAL = {
    "company": "Admiral Sentinel",
    "reg_no": "9510445",
    "address": "356 Chapman Road STE, Newark, Delaware",
    "support_email": "support@admiralsentinel.com",
    "privacy_email": "mail@admiralsentinel.com",
    "hosting_location": "United Kingdom",
    "mail_host": "Admiral Sentinel",
    "court": "High courts, Nigeria",
    "version": TERMS_VERSION,
}

def _page(template):
    return render_template(template, L=LEGAL)


@legal_bp.route("/terms")
def terms():
    return _page("legal/terms.html")


@legal_bp.route("/privacy")
def privacy():
    return _page("legal/privacy.html")


@legal_bp.route("/dpa")
def dpa():
    return _page("legal/dpa.html")


@legal_bp.route("/sla")
def sla():
    return _page("legal/sla.html")