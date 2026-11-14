"""Forgot-password flow, mail mocked.   PYTHONPATH=. python scripts/forgot_password_test.py"""
import os
import re
import uuid

os.environ.pop("PAYSTACK_SECRET_KEY", None)
os.environ.update({"MAIL_SERVER": "smtp.test", "MAIL_USERNAME": "x", "MAIL_PASSWORD": "y"})

from app import create_app, mailer
from app.extensions import db
from app.models.tenant import User

app = create_app()
app.app_context().push()
RUN = uuid.uuid4().hex[:6]
UNAME = f"fp{RUN}"
sent = []
mailer.send_email = lambda to, subject, text, html=None: sent.append({"to": to, "text": text}) or True


def check(cond, msg):
    assert cond, "FAIL: " + msg
    print("[OK]", msg)


def get_user(email):
    db.session.expire_all()
    return User.query.filter_by(email=email).first()


email = f"fp-{RUN}@example.test"
c = app.test_client()
c.post("/signup", data={"org_name": f"FP Org {RUN}", "username": UNAME, "full_name": "T",
                        "email": email, "password": "oldpassword"})
vtok = re.search(r"/verify-email/([^\s\"<]+)", sent[-1]["text"]).group(1)
c.get(f"/verify-email/{vtok}")
check(get_user(email).email_verified is True, "account verified")
c.get("/logout")
sent.clear()

print("\n--- Requesting a reset ---")
c2 = app.test_client()
check(c2.get("/forgot-password").status_code == 200, "forgot page renders")
r = c2.post("/forgot-password", data={"email": f"nobody-{RUN}@example.test"})
check(r.status_code == 302 and not sent, "unknown address: same response, no email sent")
r = c2.post("/forgot-password", data={"email": email})
check(r.status_code == 302 and len(sent) == 1 and sent[0]["to"] == email, "known address: one email sent")
tok = re.search(r"/reset-password/([^\s\"<]+)", sent[0]["text"]).group(1)

print("\n--- Using the link ---")
check(c2.get("/reset-password/garbage").status_code == 302, "garbage token is refused")
check(c2.get(f"/reset-password/{tok}").status_code == 200, "valid token shows the form")
r = c2.post(f"/reset-password/{tok}", data={"password": "123"})
check(r.status_code == 200 and not get_user(email).check_password("123"), "too-short password refused")
r = c2.post(f"/reset-password/{tok}", data={"password": "newpassword9"})
check(r.status_code == 302 and get_user(email).check_password("newpassword9"), "password changed")
check(c2.get(f"/reset-password/{tok}").status_code == 302, "the same link can't be used twice")
r = app.test_client().post("/login", data={"username": UNAME, "password": "oldpassword"})
check(r.status_code == 200, "old password no longer works")
r = app.test_client().post("/login", data={"username": UNAME, "password": "newpassword9"})
check(r.status_code == 302, "new password works")

print("\n--- Unverified addresses get no reset email ---")
u = get_user(email)
u.email_verified = False
db.session.commit()
sent.clear()
app.test_client().post("/forgot-password", data={"email": email})
check(not sent, "unverified email address receives nothing")

print("\nAll forgot-password checks passed.")