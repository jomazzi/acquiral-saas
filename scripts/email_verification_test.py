"""Email verification flow, against the real dev Postgres with email
sending mocked.   PYTHONPATH=. python scripts/email_verification_test.py"""
import os
import re
import uuid
from datetime import timedelta

os.environ.pop("PAYSTACK_SECRET_KEY", None)
os.environ.pop("MAIL_SERVER", None)

from app import create_app, mailer
from app.extensions import db
from app.models.tenant import User
from app.billing.access import utcnow

app = create_app()
app.app_context().push()
RUN = uuid.uuid4().hex[:6]
sent = []


def fake_send(to, subject, text, html=None):
    sent.append({"to": to, "text": text})
    return True


mailer.send_email = fake_send


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


def user_by_email(email):
    db.session.expire_all()
    return User.query.filter_by(email=email).first()


def form(org, email):
    return {"org_name": org, "username": "admin", "full_name": "T", "email": email, "password": "password123", "accept_terms": "on"}


def token_from(msg):
    return re.search(r"/verify-email/([^\s\"<]+)", msg["text"]).group(1)


print("--- Mail off: no verification needed ---")
e0 = f"off-{RUN}@example.test"
r = app.test_client().post("/signup", data=form(f"Mail Off {RUN}", e0))
check(r.status_code == 302 and "verify" not in r.headers["Location"], "signup goes straight into the app")
check(user_by_email(e0).email_verified is True and not sent, "user is verified and no email was sent")

print("\n--- Mail on ---")
os.environ.update({"MAIL_SERVER": "smtp.test", "MAIL_USERNAME": "x", "MAIL_PASSWORD": "y"})
c = app.test_client()
r = c.post("/signup", data=form(f"Mail On {RUN}", ""))
check(r.status_code == 200 and not sent, "signup without a valid email is rejected")
e = f"on-{RUN}@example.test"
r = c.post("/signup", data=form(f"Mail On {RUN}", e))
check(r.status_code == 302 and "verify-email-pending" in r.headers["Location"], "signup lands on the pending page")
check(user_by_email(e).email_verified is False, "new admin starts unverified")
check(len(sent) == 1 and sent[0]["to"] == e and "/verify-email/" in sent[0]["text"], "one email sent with a link")
r = c.get("/accounts")
check(r.status_code == 302 and "verify-email-pending" in r.headers["Location"], "unverified user is kept out of the app")
check(c.get("/verify-email-pending").status_code == 200, "pending page renders")

print("\n--- Resend limits ---")
c.post("/resend-verification")
check(len(sent) == 1, "resend within a minute is refused")
u = user_by_email(e)
u.verification_sent_at = utcnow() - timedelta(minutes=5)
db.session.commit()
c.post("/resend-verification")
check(len(sent) == 2, "resend works after the cooldown")

print("\n--- Links ---")
c.get("/verify-email/not-a-real-token")
check(user_by_email(e).email_verified is False, "a garbage token verifies nothing")
tok = token_from(sent[-1])
c.get(f"/verify-email/{tok}")
check(user_by_email(e).email_verified is True, "the emailed link verifies the user")
check(c.get("/accounts").status_code == 200, "verified user reaches the app")

print("\n--- Changing the address ---")
e2 = f"new-{RUN}@example.test"
c.post("/change-email", data={"email": e2})
u2 = user_by_email(e2)
check(u2 is not None and u2.email_verified is False, "new address starts unverified")
check(len(sent) == 3 and sent[-1]["to"] == e2, "link sent to the new address")
c.get(f"/verify-email/{tok}")
check(user_by_email(e2).email_verified is False, "the old link doesn't verify the new address")

print("\nAll email verification checks passed.")