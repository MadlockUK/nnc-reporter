"""Sign-in: sessions, the gatekeeper middleware, and the public auth endpoints.

The gatekeeper is deny-by-default. Every request - page, API call, photo - must
carry a valid, unexpired session for an active account, except the handful of
addresses listed in PUBLIC (the sign-in pages themselves, their static assets
and the health check). A new route is therefore protected the moment it is
written, and forgetting a check on it fails closed, not open.

On top of that:
  * a session that has passed the password but not the two-factor step can only
    reach the two-factor endpoints; one that must still enrol in two-factor can
    only reach the enrolment endpoints;
  * every state-changing request must come from this site (Origin checked) and,
    once signed in, carry the session's CSRF token in X-CSRF-Token;
  * admin pages and endpoints need the admin role, and viewers cannot change
    anything outside their own account;
  * the person's data folder is bound to the request here (config.use_user), so
    the report code below cannot reach anyone else's data.
"""
import json
import logging
import math
import time
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import config
from . import db, mailer, security, settings

log = logging.getLogger("nnc.auth")

COOKIE_SECURE_NAME = "__Host-nnc_session"
COOKIE_PLAIN_NAME = "nnc_session"
INVITE_DAYS = 7
RESET_MINUTES = 60
MFA_STAGE_SECONDS = 600
ENROL_STAGE_SECONDS = 1800

# Reachable with no session at all.
PUBLIC = {
    ("GET", "/login"), ("GET", "/forgot"), ("GET", "/reset"), ("GET", "/invite"),
    ("GET", "/healthz"), ("GET", "/favicon.ico"), ("GET", "/privacy"),
    ("GET", "/api/auth/state"), ("GET", "/api/auth/policy"),
    ("POST", "/api/auth/login"), ("POST", "/api/auth/forgot"),
    ("POST", "/api/auth/reset"), ("POST", "/api/auth/invite/info"),
    ("POST", "/api/auth/invite/accept"), ("POST", "/api/auth/logout"),
}
PUBLIC_PREFIXES = ("/assets/",)          # CSS, JS and icons - nothing personal

# What a half-signed-in session may reach, on top of PUBLIC.
STAGE_ALLOWED = {
    "mfa": {("POST", "/api/auth/mfa")},
    "enrol": {("GET", "/account"), ("GET", "/api/me"), ("GET", "/api/account"),
              ("POST", "/api/account/mfa/setup"), ("POST", "/api/account/mfa/enable")},
}

# Viewers may change these things, and nothing else.
VIEWER_WRITES = ("/api/account", "/api/auth/")

UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


# --------------------------------------------------------------- helpers

def cookie_name() -> str:
    return COOKIE_SECURE_NAME if settings.get().cookie_secure else COOKIE_PLAIN_NAME


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "") or "?"


def user_dir(uid: int):
    return settings.get().users_dir() / f"u{int(uid)}"


def mfa_required_for(u: dict) -> bool:
    need = settings.get().mfa_required
    return need == "all" or (need == "admins" and u["role"] == "admin")


def public_user(u: dict) -> dict:
    """What the browser may know about an account - never a hash or secret."""
    prof = db.profile(u)
    return {"id": u["id"], "email": u["email"], "name": u["name"], "role": u["role"],
            "status": u["status"], "mfa_enabled": bool(u["mfa_enabled"]),
            "mfa_required": mfa_required_for(u),
            "last_login": u.get("last_login"), "created_at": u.get("created_at"),
            "share_name": bool(prof.get("share_name", False))}


def _set_cookie(resp, value: str, max_age: int) -> None:
    resp.set_cookie(cookie_name(), value, max_age=max_age, path="/", httponly=True,
                    secure=settings.get().cookie_secure, samesite="lax")


def _clear_cookie(resp) -> None:
    resp.delete_cookie(cookie_name(), path="/", secure=settings.get().cookie_secure,
                       httponly=True, samesite="lax")


def start_session(resp, request: Request, u: dict, stage: str) -> str:
    """A brand-new session id every time (never re-use one across sign-in steps).
    Returns the session's CSRF token."""
    s = settings.get()
    raw, csrf = security.new_token(), security.new_token()
    ttl = {"mfa": MFA_STAGE_SECONDS, "enrol": ENROL_STAGE_SECONDS}.get(
        stage, s.session_max_hours * 3600)
    db.create_session(security.token_hash(raw), u["id"], stage, csrf,
                      ttl, client_ip(request), request.headers.get("user-agent", ""))
    _set_cookie(resp, raw, int(ttl))
    return csrf


def end_session(resp, request: Request, clear: bool = True) -> None:
    raw = request.cookies.get(cookie_name())
    if raw:
        db.delete_session(security.token_hash(raw))
    if clear:
        _clear_cookie(resp)


def resolve(raw: str | None) -> tuple:
    """(session, user) for a cookie value, or (None, None). Expired, idle or
    orphaned sessions are deleted on sight."""
    if not raw or len(raw) > 200:
        return None, None
    sid = security.token_hash(raw)
    sess = db.session(sid)
    if not sess:
        return None, None
    now = time.time()
    idle = settings.get().session_idle_minutes * 60
    if sess["expires_at"] < now or (sess["stage"] == "full" and now - sess["last_seen"] > idle):
        db.delete_session(sid)
        return None, None
    u = db.user(sess["user_id"])
    if not u or u["status"] != "active":
        db.delete_session(sid)
        return None, None
    if u.get("password_changed") and sess["created_at"] < u["password_changed"] - 1:
        db.delete_session(sid)           # password changed since this sign-in
        return None, None
    if now - sess["last_seen"] > 60:
        db.touch_session(sid)
    return sess, u


def _origin_ok(request: Request) -> bool:
    """Is this state-changing request from our own pages?"""
    origin = request.headers.get("origin")
    if not origin:
        ref = request.headers.get("referer") or ""
        if ref:
            u = urlparse(ref)
            origin = f"{u.scheme}://{u.netloc}"
    if not origin or origin == "null":
        return False
    want = settings.get().origin
    if want:
        return origin == want
    host = request.headers.get("host", "")
    return bool(host) and urlparse(origin).netloc == host


def _deny(request: Request, status: int, message: str):
    if request.url.path.startswith("/api/") or request.method != "GET":
        return JSONResponse({"detail": message}, status_code=status)
    if status == 401:
        nxt = request.url.path + (("?" + request.url.query) if request.url.query else "")
        return RedirectResponse("/login" + (f"?next={quote(nxt)}" if nxt != "/" else ""),
                                status_code=303)
    return JSONResponse({"detail": message}, status_code=status)


# ------------------------------------------------------------ the gatekeeper

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), payment=(), usb=(), "
                          "geolocation=(self)",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://api.mapbox.com; "
        "style-src 'self' 'unsafe-inline' https://api.mapbox.com; "
        "img-src 'self' data: blob: https:; "
        "font-src 'self' data:; "
        "connect-src 'self' https://api.mapbox.com https://events.mapbox.com "
        "https://*.tiles.mapbox.com; "
        "worker-src 'self' blob:; child-src blob:; frame-src 'none'; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'; object-src 'none'"),
}


class Gatekeeper:
    """Pure ASGI middleware, so the user's data folder bound here is the one the
    endpoint (and any thread it starts) sees."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        secure = settings.get().cookie_secure

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                have = {k.lower() for k, _ in headers}
                for k, v in SECURITY_HEADERS.items():
                    if k.lower().encode() not in have:
                        headers.append((k.encode(), v.encode()))
                if secure:
                    headers.append((b"Strict-Transport-Security",
                                    b"max-age=31536000; includeSubDomains"))
                if not scope["path"].startswith("/assets/") and b"cache-control" not in have:
                    headers.append((b"Cache-Control", b"no-store"))
                message["headers"] = headers
            await send(message)

        request = Request(scope)
        method, path = scope["method"], scope["path"]
        key = (method if method != "HEAD" else "GET", path)

        # Request size: refuse oversized bodies before reading them.
        limit = settings.get().max_upload_mb * 1024 * 1024 * 8   # a batch of photos
        try:
            length = int(request.headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > limit:
            return await JSONResponse({"detail": "that upload is too large - send fewer "
                                       "photos at a time"}, status_code=413)(
                scope, receive, send_with_headers)

        sess, user = await run_in_threadpool(resolve, request.cookies.get(cookie_name()))
        scope.setdefault("state", {})
        scope["state"]["session"] = sess
        scope["state"]["user"] = user

        is_public = key in PUBLIC or path.startswith(PUBLIC_PREFIXES)

        # Cross-site request forgery: state-changing requests must come from our
        # own pages, and signed-in ones must carry the session's token too.
        if method in UNSAFE:
            if not _origin_ok(request):
                return await _deny(request, 403, "request refused: it did not come "
                                   "from this site")(scope, receive, send_with_headers)
            if sess and not is_public:
                if not security.same(request.headers.get("x-csrf-token", ""), sess["csrf"]):
                    return await _deny(request, 403, "request refused: missing or "
                                       "stale security token - reload the page")(
                        scope, receive, send_with_headers)

        if not is_public:
            if not sess:
                return await _deny(request, 401, "sign in first")(
                    scope, receive, send_with_headers)
            if sess["stage"] != "full" and key not in STAGE_ALLOWED.get(sess["stage"], ()):
                msg = ("finish signing in with your two-factor code" if sess["stage"] == "mfa"
                       else "set up two-factor sign-in on your account page first")
                if method == "GET" and not path.startswith("/api/"):
                    target = "/login" if sess["stage"] == "mfa" else "/account"
                    return await RedirectResponse(target, status_code=303)(
                        scope, receive, send_with_headers)
                return await _deny(request, 403, msg)(scope, receive, send_with_headers)
            role = user["role"]
            if (path.startswith("/admin") or path.startswith("/api/admin")) and role != "admin":
                return await _deny(request, 403, "admins only")(scope, receive,
                                                                 send_with_headers)
            if role == "viewer" and method in UNSAFE and not path.startswith(VIEWER_WRITES):
                return await _deny(request, 403, "your account can view, not change, "
                                   "reports")(scope, receive, send_with_headers)

        tokens = None
        if sess and user and sess["stage"] == "full":
            tokens = config.use_user(f"u{user['id']}", user_dir(user["id"]))
        try:
            await self.app(scope, receive, send_with_headers)
        finally:
            if tokens:
                config.release_user(tokens)


# ------------------------------------------------------------ dependencies

def me(request: Request) -> dict:
    u = getattr(request.state, "user", None)
    sess = getattr(request.state, "session", None)
    if not u or not sess:
        raise HTTPException(401, "sign in first")
    return u


def need(role: str):
    """Route dependency: the role (or better) this route needs. The gatekeeper
    has already checked; this is the second lock on the same door."""
    order = {"viewer": 0, "editor": 1, "admin": 2}

    def check(request: Request) -> dict:
        u = me(request)
        sess = request.state.session
        if sess["stage"] != "full":
            raise HTTPException(403, "finish signing in first")
        if order[u["role"]] < order[role]:
            raise HTTPException(403, f"needs the {role} role")
        return u
    return check


# ------------------------------------------------------------ the endpoints

router = APIRouter()


class Login(BaseModel):
    email: str
    password: str


class Code(BaseModel):
    code: str


class Forgot(BaseModel):
    email: str


class Reset(BaseModel):
    token: str
    password: str


class InviteInfo(BaseModel):
    token: str


class Accept(BaseModel):
    token: str
    password: str
    name: str = ""


GENERIC_LOGIN_FAIL = ("Email or password not recognised, or the account is locked "
                      "for a while after too many attempts. Try again later, or "
                      "reset your password.")


def _too_many(kind: str, ip: str, per_ip: int, seconds: int) -> None:
    if db.failures(kind, seconds, ip=ip) >= per_ip:
        raise HTTPException(429, "too many attempts from this connection - wait a few "
                                 "minutes and try again")


def _register_failure(u: dict, ip: str) -> None:
    s = settings.get()
    n = (u.get("failed_logins") or 0) + 1
    fields = {"failed_logins": n}
    if n % s.login_max_failures == 0:
        rounds = n // s.login_max_failures
        minutes = min(24 * 60, s.lockout_minutes * (2 ** (rounds - 1)))
        fields["locked_until"] = time.time() + minutes * 60
        db.audit("account.locked", u["id"], ip=ip, detail=f"{n} failures, {minutes} min")
        if rounds == 1:
            mailer.notice(u["email"], "Your NNC Reporter account was locked",
                          f"There were {n} failed attempts to sign in to your account, "
                          f"so it is locked for {minutes} minutes. If this was not you, "
                          f"reset your password once the lock ends, and tell your "
                          f"administrator.")
    db.update_user(u["id"], **fields)


@router.get("/api/auth/policy")
def password_policy():
    return {"rules": security.policy_text(),
            "min_length": settings.get().password_min_length}


@router.get("/api/auth/state")
def auth_state(request: Request):
    sess, u = request.state.session, request.state.user
    if not sess:
        return {"signed_in": False}
    return {"signed_in": sess["stage"] == "full", "stage": sess["stage"],
            "email": u["email"], "csrf": sess["csrf"]}


@router.post("/api/auth/login")
def login(body: Login, request: Request):
    ip = client_ip(request)
    _too_many("login", ip, 20, 900)
    email = (body.email or "").strip().lower()[:254]
    u = db.user_by_email(email)
    now = time.time()
    # Verify even when the account is unknown or locked, so every failure takes
    # the same time and says the same thing.
    ok = security.verify_password(u.get("password_hash") if u else None, body.password or "")
    usable = bool(u and u["status"] == "active" and (u.get("locked_until") or 0) <= now)
    if not (ok and usable):
        db.attempt("login", False, ip, email)
        if u and u["status"] == "active" and not ok:
            _register_failure(u, ip)
        db.audit("login.failed", u["id"] if u else None, ip=ip,
                 detail="locked" if (u and ok and not usable) else "")
        raise HTTPException(401, GENERIC_LOGIN_FAIL)

    db.attempt("login", True, ip, email)
    fields = {"failed_logins": 0, "locked_until": 0}
    if security.needs_rehash(u["password_hash"]):
        fields["password_hash"] = security.hash_password(body.password)
    db.update_user(u["id"], **fields)

    if u["mfa_enabled"]:
        nxt, stage = "mfa", "mfa"
    elif mfa_required_for(u):
        nxt, stage = "enrol", "enrol"
    else:
        nxt, stage = "app", "full"
    resp = JSONResponse({})
    end_session(resp, request, clear=False)    # any earlier session on this browser
    csrf = start_session(resp, request, u, stage)
    if stage == "full":
        db.update_user(u["id"], last_login=now)
    db.audit("login.ok" if stage != "mfa" else "login.password_ok", u["id"], ip=ip,
             detail="must enrol in two-factor" if stage == "enrol" else "")
    return _with_body(resp, {"next": nxt, "csrf": csrf})


def _with_body(resp: JSONResponse, content: dict) -> JSONResponse:
    """Put the body on a response whose cookies are already set."""
    body = resp.render(content)
    resp.body = body
    resp.headers["content-length"] = str(len(body))
    return resp


@router.post("/api/auth/mfa")
def login_mfa(body: Code, request: Request):
    sess, u = request.state.session, request.state.user
    if not sess or sess["stage"] != "mfa":
        raise HTTPException(400, "start again from the sign-in page")
    ip = client_ip(request)
    step = security.totp_step(security.decrypt(u["mfa_secret"]), body.code,
                              u.get("mfa_last_step") or 0)
    used_recovery = False
    if step is None:
        codes = json.loads(u.get("recovery_codes") or "[]")
        h = security.recovery_hash(body.code)
        if h in codes:
            codes.remove(h)
            db.update_user(u["id"], recovery_codes=json.dumps(codes))
            used_recovery = True
    if step is None and not used_recovery:
        db.attempt("mfa", False, ip, u["email"])
        tries = db.bump_session_tries(sess["id"])
        if tries >= 5:
            db.delete_session(sess["id"])
            _register_failure(db.user(u["id"]), ip)
            db.audit("mfa.failed", u["id"], ip=ip, detail="session ended after 5 tries")
            raise HTTPException(401, "too many wrong codes - sign in again")
        raise HTTPException(401, "that code is not right - check your authenticator "
                                 "app's clock and try the current code")
    if step is not None:
        db.update_user(u["id"], mfa_last_step=step)
    db.attempt("mfa", True, ip, u["email"])
    left = len(json.loads(db.user(u["id"]).get("recovery_codes") or "[]"))
    resp = JSONResponse({})
    db.delete_session(sess["id"])
    csrf = start_session(resp, request, u, "full")
    db.update_user(u["id"], last_login=time.time())
    db.audit("login.ok", u["id"], ip=ip, detail="recovery code" if used_recovery else "2fa")
    return _with_body(resp, {"next": "app", "csrf": csrf,
                             "recovery_codes_left": left if used_recovery else None})


@router.post("/api/auth/logout")
def logout(request: Request):
    resp = JSONResponse({"ok": True})
    u = request.state.user
    end_session(resp, request)
    if u:
        db.audit("logout", u["id"], ip=client_ip(request))
    return resp


@router.post("/api/auth/forgot")
def forgot(body: Forgot, request: Request):
    """Always the same answer, whether or not the email has an account."""
    ip = client_ip(request)
    email = (body.email or "").strip().lower()[:254]
    answer = {"ok": True, "message": "If that email has an account, a link to reset "
              "the password is on its way. It works once, for an hour."}
    recent_ip = db.failures("forgot", 3600, ip=ip, include_ok=True)
    recent_email = db.failures("forgot", 3600, email=email, include_ok=True)
    db.attempt("forgot", True, ip, email)
    if recent_ip >= 10 or recent_email >= 3:
        return answer
    u = db.user_by_email(email)
    if u and u["status"] == "active":
        raw = security.new_token()
        db.void_tokens(u["id"], "reset")
        db.create_token(security.token_hash(raw), u["id"], "reset", RESET_MINUTES * 60)
        mailer.reset(u["email"], mailer.link(f"/reset#token={raw}"), RESET_MINUTES)
        db.audit("password.reset_requested", u["id"], ip=ip)
    elif u and u["status"] == "invited":
        db.audit("password.reset_requested", u["id"], ip=ip, detail="still invited")
    return answer


def _check_link(kind: str, raw: str, ip: str) -> dict:
    _too_many(kind, ip, 20, 3600)
    row = db.token(security.token_hash(raw or ""), kind)
    u = db.user(row["user_id"]) if row else None
    if not u or (kind == "reset" and u["status"] != "active") \
            or (kind == "invite" and u["status"] != "invited"):
        db.attempt(kind, False, ip)
        raise HTTPException(400, "this link has expired or has already been used - "
                                 + ("ask for a new one" if kind == "reset"
                                    else "ask your administrator to resend the invitation"))
    return u


@router.post("/api/auth/reset")
def reset(body: Reset, request: Request):
    ip = client_ip(request)
    u = _check_link("reset", body.token, ip)
    problems = security.password_problems(body.password, u["email"], u["name"])
    if problems:
        raise HTTPException(400, "Choose a stronger password: " + "; ".join(problems) + ".")
    if security.verify_password(u["password_hash"], body.password):
        raise HTTPException(400, "Choose a password you have not used for this account.")
    if db.use_token(security.token_hash(body.token), "reset") != u["id"]:
        raise HTTPException(400, "this link has expired or has already been used")
    db.void_tokens(u["id"], "reset")
    db.update_user(u["id"], password_hash=security.hash_password(body.password),
                   password_changed=time.time(), failed_logins=0, locked_until=0)
    db.delete_sessions(u["id"])
    db.audit("password.reset", u["id"], ip=ip)
    mailer.notice(u["email"], "Your NNC Reporter password was changed",
                  "The password for your NNC Reporter account has just been reset, and "
                  "every device signed in to it has been signed out. If this was not "
                  "you, tell your administrator straight away.")
    return {"ok": True}


@router.post("/api/auth/invite/info")
def invite_info(body: InviteInfo, request: Request):
    u = _check_link("invite", body.token, client_ip(request))
    return {"email": u["email"], "name": u["name"], "rules": security.policy_text()}


@router.post("/api/auth/invite/accept")
def invite_accept(body: Accept, request: Request):
    ip = client_ip(request)
    u = _check_link("invite", body.token, ip)
    name = (body.name or u["name"] or "").strip()[:120]
    if not name:
        raise HTTPException(400, "tell us your name - it is what the council forms use")
    problems = security.password_problems(body.password, u["email"], name)
    if problems:
        raise HTTPException(400, "Choose a stronger password: " + "; ".join(problems) + ".")
    if db.use_token(security.token_hash(body.token), "invite") != u["id"]:
        raise HTTPException(400, "this link has expired or has already been used")
    db.void_tokens(u["id"], "invite")
    prof = db.profile(u)
    prof.setdefault("full_name", name)
    db.update_user(u["id"], name=name, status="active", profile=prof,
                   password_hash=security.hash_password(body.password),
                   password_changed=time.time())
    db.audit("invite.accepted", u["id"], ip=ip)
    return {"ok": True}


def issue_invite(u: dict, inviter: dict | None) -> str:
    """A fresh invitation link (earlier ones stop working), emailed if possible.
    Returns the link so an admin can pass it on another way if email fails."""
    raw = security.new_token()
    db.void_tokens(u["id"], "invite")
    db.create_token(security.token_hash(raw), u["id"], "invite", INVITE_DAYS * 86400)
    url = mailer.link(f"/invite#token={raw}")
    mailer.invite(u["email"], u["name"], (inviter or {}).get("name") or "Your administrator",
                  url, INVITE_DAYS)
    return url


def lock_note(u: dict) -> str:
    left = (u.get("locked_until") or 0) - time.time()
    return f"locked for {math.ceil(left / 60)} more min" if left > 0 else ""
