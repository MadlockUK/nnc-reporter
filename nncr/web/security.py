"""Passwords, one-time links, two-factor codes and encrypted secrets.

Choices, in brief:
  * Passwords are hashed with Argon2id (memory-hard), never stored or logged.
  * A password must be long AND complex: at least 12 characters, with upper and
    lower case letters, a digit and a symbol; it must not contain your name or
    email, must not be a known common or guessable password (zxcvbn score 3+),
    and - unless NNC_HIBP_CHECK=0 - must not appear in a public data breach
    (checked with Have I Been Pwned's k-anonymity range API: only the first five
    characters of the password's SHA-1 hash ever leave the server).
  * Session ids, invitation and reset links are 256-bit random values; only their
    SHA-256 hashes are stored, so a copy of the database cannot be used to sign in.
  * Council passwords people save are encrypted with a key derived from
    NNC_SECRET_KEY, and are never sent back to the browser.
"""
import base64
import hashlib
import hmac
import io
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from . import settings

_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)
# Verified against when the email is unknown, so a wrong email costs the same
# time as a wrong password and response times do not reveal who has an account.
_DUMMY = _hasher.hash(secrets.token_urlsafe(24))


# ------------------------------------------------------------------ passwords

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    try:
        return _hasher.verify(stored or _DUMMY, password or "") and bool(stored)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:
        return True


SYMBOLS = r"""!"#$%&'()*+,-./:;<=>?@[\]^_`{|}~£€ """


def policy_text() -> list:
    n = settings.get().password_min_length
    rules = [f"At least {n} characters (a short phrase is easiest to remember)",
             "Upper and lower case letters, at least one number and one symbol",
             "Not your name or email address, and nothing commonly used or easily "
             "guessed",
             "No runs like 'aaaa' or '1234'"]
    if settings.get().hibp_check:
        rules.append("Not a password that has appeared in a known data breach")
    return rules


def _sequences(pw: str) -> bool:
    low = pw.lower()
    if re.search(r"(.)\1{3,}", low):
        return True
    runs = ("abcdefghijklmnopqrstuvwxyz", "0123456789", "qwertyuiop", "asdfghjkl",
            "zxcvbnm")
    for run in runs:
        for i in range(len(run) - 3):
            if run[i:i + 4] in low or run[i:i + 4][::-1] in low:
                return True
    return False


def password_problems(pw: str, email: str = "", name: str = "",
                      check_breaches: bool = True) -> list:
    """Everything wrong with a proposed password; empty means it is acceptable."""
    s = settings.get()
    pw = pw or ""
    out = []
    if len(pw) < s.password_min_length:
        out.append(f"use at least {s.password_min_length} characters")
    if len(pw) > 256:
        out.append("use no more than 256 characters")
    if not re.search(r"[a-z]", pw):
        out.append("add a lower-case letter")
    if not re.search(r"[A-Z]", pw):
        out.append("add a capital letter")
    if not re.search(r"\d", pw):
        out.append("add a number")
    if not re.search(r"[^A-Za-z0-9]", pw):
        out.append("add a symbol, such as ! ? # or a space")
    low = pw.lower()
    personal = set()
    local = (email or "").split("@")[0].lower()
    for part in re.split(r"[^a-z0-9]+", local) + [local]:
        if len(part) >= 3:
            personal.add(part)
    for part in re.split(r"\s+", (name or "").lower()):
        if len(part) >= 3:
            personal.add(part)
    if any(p in low for p in personal):
        out.append("leave your name and email address out of it")
    if _sequences(pw):
        out.append("avoid runs like 'aaaa', '1234' or 'qwer'")
    if out:
        return out

    try:
        from zxcvbn import zxcvbn
        res = zxcvbn(pw[:100], user_inputs=sorted(personal) + ["nnc", "reporter",
                                                              "northants", "council"])
        if res.get("score", 0) < 3:
            tip = (res.get("feedback") or {}).get("warning") or ""
            out.append("this is too easy to guess" + (f" - {tip.rstrip('.')}" if tip else "")
                       + "; try a longer phrase of unrelated words")
    except ImportError:                       # pragma: no cover - listed in requirements
        pass
    if out:
        return out

    if check_breaches and s.hibp_check:
        seen = breach_count(pw)
        if seen:
            out.append(f"this password has appeared in {seen:,} known data breaches - "
                       f"choose a different one")
    return out


def breach_count(pw: str) -> int | None:
    """How often a password appears in Have I Been Pwned's breach corpus.
    k-anonymity: only the first 5 hex characters of its SHA-1 are sent. None if
    the service could not be reached (the other checks still apply)."""
    import requests
    digest = hashlib.sha1(pw.encode("utf-8")).hexdigest().upper()
    prefix, rest = digest[:5], digest[5:]
    try:
        r = requests.get(f"https://api.pwnedpasswords.com/range/{prefix}",
                         headers={"Add-Padding": "true",
                                  "User-Agent": "nnc-reporter-password-check"},
                         timeout=4)
        r.raise_for_status()
    except Exception:
        return None
    for line in r.text.splitlines():
        h, _, n = line.partition(":")
        if h.strip() == rest:
            try:
                return int(n.strip())
            except ValueError:
                return 1
    return 0


# --------------------------------------------------------- random values

def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").encode(), (b or "").encode())


# ------------------------------------------------------- encrypted secrets

def _fernet():
    from cryptography.fernet import Fernet
    key = hashlib.sha256(("nnc-reporter/credentials/" + settings.get().secret_key)
                         .encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(text: str) -> str:
    return _fernet().encrypt((text or "").encode("utf-8")).decode("ascii") if text else ""


def decrypt(blob: str | None) -> str:
    if not blob:
        return ""
    try:
        return _fernet().decrypt(blob.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


# --------------------------------------------------------------- two-factor

ISSUER = "NNC Reporter"


def new_totp_secret() -> str:
    import pyotp
    return pyotp.random_base32()


def totp_uri(secret: str, email: str) -> str:
    import pyotp
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def totp_step(secret: str, code: str, last_step: int = 0) -> int | None:
    """The time-step a code is valid for (allowing one step of clock drift), or
    None. A step at or before `last_step` is refused, so a code that was seen
    once - over a shoulder, or in a replayed request - cannot be used again."""
    import pyotp
    code = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    totp = pyotp.TOTP(secret)
    import time
    now = int(time.time())
    for drift in (0, -1, 1):
        step = now // 30 + drift
        if hmac.compare_digest(totp.at(step * 30), code) and step > (last_step or 0):
            return step
    return None


def qr_data_uri(text: str) -> str:
    import qrcode
    img = qrcode.make(text, box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def new_recovery_codes(n: int = 10) -> list:
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return ["-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3))
            for _ in range(n)]


def recovery_hash(code: str) -> str:
    return token_hash(re.sub(r"[^a-z0-9]", "", (code or "").lower()))
