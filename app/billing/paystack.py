"""Minimal Paystack client -- stdlib only, so the image stays
dependency-free (same reasoning as reportlab being pure Python).

Every call raises PaystackError on any failure (network, non-2xx, or
Paystack's own status:false), so routes have one thing to catch."""
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

API_BASE = "https://api.paystack.co"


class PaystackError(Exception):
    pass


def secret_key():
    return os.environ.get("PAYSTACK_SECRET_KEY") or None


def is_configured():
    return bool(secret_key())


def _request(method, path, payload=None, timeout=20):
    key = secret_key()
    if not key:
        raise PaystackError("Paystack is not configured (PAYSTACK_SECRET_KEY is unset).")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode()).get("message", "")
        except Exception:
            msg = ""
        raise PaystackError(f"Paystack returned HTTP {e.code}: {msg}".strip())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise PaystackError(f"Could not reach Paystack: {e}")
    if not body.get("status"):
        raise PaystackError(body.get("message") or "Paystack reported a failure.")
    return body["data"]


def initialize_transaction(email, amount_minor, currency, plan_code, reference, callback_url, metadata):
    """Starts a hosted checkout. Passing `plan` makes Paystack create a
    recurring subscription from the successful first charge (and use the
    plan's amount, which is why plan amounts must match plans.py)."""
    return _request("POST", "/transaction/initialize", {
        "email": email, "amount": amount_minor, "currency": currency,
        "plan": plan_code, "reference": reference,
        "callback_url": callback_url, "metadata": metadata,
    })


def verify_transaction(reference):
    from urllib.parse import quote
    return _request("GET", f"/transaction/verify/{quote(reference, safe='')}")


def disable_subscription(subscription_code, email_token):
    return _request("POST", "/subscription/disable",
                    {"code": subscription_code, "token": email_token})


def create_plan(name, amount_minor, interval, currency):
    return _request("POST", "/plan", {
        "name": name, "amount": amount_minor, "interval": interval, "currency": currency,
    })


def valid_webhook_signature(raw_body, signature_header):
    """Paystack signs the raw request body with HMAC-SHA512 using the
    secret key and sends the hex digest in X-Paystack-Signature. Must be
    computed over the exact bytes received (not re-serialised JSON), and
    compared in constant time."""
    key = secret_key()
    if not key or not signature_header:
        return False
    expected = hmac.new(key.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip().lower())
