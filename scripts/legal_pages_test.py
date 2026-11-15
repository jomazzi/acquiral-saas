"""Legal pages and terms acceptance.   PYTHONPATH=. python scripts/legal_pages_test.py"""
import os
import re
import uuid

os.environ.pop("PAYSTACK_SECRET_KEY", None)
os.environ.pop("MAIL_SERVER", None)

from app import create_app
from app.extensions import db
from app.models.tenant import User
from app.blueprints.legal.routes import TERMS_VERSION

app = create_app()
app.app_context().push()
RUN = uuid.uuid4().hex[:6]


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


c = app.test_client()
print("--- Public pages ---")
for path, phrase in (("/terms", b"Free trial"), ("/privacy", b"Nigeria Data Protection Act"),
                     ("/dpa", b"sub-processors"), ("/sla", b"99.0%")):
    r = c.get(path)
    check(r.status_code == 200 and phrase in r.data, f"{path} renders")
    text = re.sub(rb"<[^>]+>", b" ", r.data)
    check(b"[" not in text, f"{path} has no unfilled [placeholders] (edit LEGAL in app/blueprints/legal/routes.py)")

print("\n--- Terms acceptance at signup ---")
email = f"terms-{RUN}@example.test"
data = {"org_name": f"Terms Org {RUN}", "username": f"t{RUN}", "full_name": "T",
        "email": email, "password": "password123"}
r = c.post("/signup", data=data)
db.session.expire_all()
check(r.status_code == 200 and User.query.filter_by(email=email).first() is None,
      "signup without ticking the box is refused")
r = c.post("/signup", data={**data, "accept_terms": "on"})
db.session.expire_all()
u = User.query.filter_by(email=email).first()
check(r.status_code == 302 and u is not None, "signup with the box ticked succeeds")
check(u.terms_version == TERMS_VERSION and u.terms_accepted_at is not None, "acceptance date and version stored")
print("\nAll legal page checks passed.")