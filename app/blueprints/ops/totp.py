"""Time-based one-time passwords (RFC 6238) with no extra dependency."""
import base64
import hashlib
import hmac
import os
import struct
import time
from urllib.parse import quote


def new_secret():
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def _code(secret, step):
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    n = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{n:06d}"


def verify(secret, code, last_step=None, now=None, window=1):
    """Returns the matching 30-second step, or None. A step at or before
    last_step is refused, so a code can't be replayed."""
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if len(code) != 6:
        return None
    current = int((now if now is not None else time.time()) // 30)
    for step in range(current - window, current + window + 1):
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def uri(secret, username, issuer="Acquiral Ops"):
    return (f"otpauth://totp/{quote(issuer)}:{quote(username)}?secret={secret}"
            f"&issuer={quote(issuer)}&period=30&digits=6&algorithm=SHA1")