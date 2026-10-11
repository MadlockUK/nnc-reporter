"""Your account: details for the council forms, council sign-in, password,
two-factor sign-in, where you are signed in, and a copy of your data."""
import io
import json
import time
import zipfile

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from . import auth, db, mailer, security, settings, usercfg

router = APIRouter()


def _sessions(u: dict, current: str) -> list:
    out = []
    for s in db.sessions_for(u["id"]):
        out.append({"id": s["id"][:12], "current": s["id"] == current,
                    "created_at": s["created_at"], "last_seen": s["last_seen"],
                    "ip": s["ip"], "user_agent": s["user_agent"]})
    return out


@router.get("/api/me")
def me(request: Request):
    u, sess = auth.me(request), request.state.session
    from . import mapbox
    from .. import submit
    return {"user": auth.public_user(u), "csrf": sess["csrf"], "stage": sess["stage"],
            "features": {"mail": settings.get().mail_configured,
                         "mapbox": bool(mapbox.browser_token()["token"]),
                         "submit_mode": submit.mode()}}


@router.get("/api/account")
def account(request: Request):
    u, sess = auth.me(request), request.state.session
    prof = db.profile(u)
    shown = {k: prof.get(k, "" if usercfg.PROFILE_FIELDS[k][0] is not bool else
                         (k == "highways_enabled")) for k in usercfg.PROFILE_FIELDS}
    if not shown.get("full_name"):
        shown["full_name"] = u["name"]
    return {"user": auth.public_user(u), "profile": shown,
            "fields": {k: v[1] for k, v in usercfg.PROFILE_FIELDS.items()},
            "highways_password_set": bool(u.get("highways_password")),
            "sessions": _sessions(u, sess["id"]),
            "rules": security.policy_text(),
            "recovery_codes_left": len(json.loads(u.get("recovery_codes") or "[]")),
            "stage": sess["stage"]}


@router.patch("/api/account/profile")
def edit_profile(body: dict, request: Request):
    u = auth.me(request)
    if request.state.session["stage"] != "full":
        raise HTTPException(403, "finish signing in first")
    changes = usercfg.clean_profile(body)
    if "full_name" in changes and not changes["full_name"]:
        raise HTTPException(400, "the council forms need a name")
    for k in ("council_email", "highways_email"):
        if changes.get(k) and ("@" not in changes[k] or " " in changes[k]):
            raise HTTPException(400, f"that does not look like an email address ({k})")
    prof = {**db.profile(u), **changes}
    fields = {"profile": prof}
    if changes.get("full_name"):
        fields["name"] = changes["full_name"]
    db.update_user(u["id"], **fields)
    db.audit("account.profile", u["id"], ip=auth.client_ip(request),
             detail=",".join(sorted(changes)))
    return account(request)


class HighwaysPassword(BaseModel):
    password: str = ""
    clear: bool = False


@router.put("/api/account/highways-password")
def set_highways_password(body: HighwaysPassword, request: Request):
    """Write-only: the saved password is never sent back to any browser."""
    u = auth.me(request)
    if request.state.session["stage"] != "full":
        raise HTTPException(403, "finish signing in first")
    if body.clear:
        db.update_user(u["id"], highways_password="")
    else:
        if not body.password or len(body.password) > 200:
            raise HTTPException(400, "enter the password for the highways site")
        db.update_user(u["id"], highways_password=security.encrypt(body.password))
    db.audit("account.highways_password", u["id"], ip=auth.client_ip(request),
             detail="cleared" if body.clear else "set")
    return {"ok": True, "highways_password_set": not body.clear}


class PasswordChange(BaseModel):
    current: str
    new: str


@router.post("/api/account/password")
def change_password(body: PasswordChange, request: Request):
    u, sess = auth.me(request), request.state.session
    ip = auth.client_ip(request)
    if db.failures("password_change", 900, ip=ip) >= 10:
        raise HTTPException(429, "too many attempts - wait a few minutes")
    if not security.verify_password(u["password_hash"], body.current):
        db.attempt("password_change", False, ip, u["email"])
        raise HTTPException(400, "your current password is not right")
    if body.current == body.new:
        raise HTTPException(400, "choose a different password from the current one")
    problems = security.password_problems(body.new, u["email"], u["name"])
    if problems:
        raise HTTPException(400, "Choose a stronger password: " + "; ".join(problems) + ".")
    now = time.time()
    db.update_user(u["id"], password_hash=security.hash_password(body.new),
                   password_changed=now)
    # Every other sign-in ends; this one carries on under a fresh id.
    db.delete_sessions(u["id"])
    resp = JSONResponse({})
    csrf = auth.start_session(resp, request, u, sess["stage"])
    db.audit("password.changed", u["id"], ip=ip)
    mailer.notice(u["email"], "Your NNC Reporter password was changed",
                  "The password for your account was just changed, and any other "
                  "devices signed in to it were signed out. If this was not you, "
                  "reset your password and tell your administrator straight away.")
    return auth._with_body(resp, {"ok": True, "csrf": csrf})


class MfaSetup(BaseModel):
    password: str = ""


@router.post("/api/account/mfa/setup")
def mfa_setup(body: MfaSetup, request: Request):
    u, sess = auth.me(request), request.state.session
    if sess["stage"] == "full" and not security.verify_password(u["password_hash"],
                                                                body.password):
        raise HTTPException(400, "enter your current password to set up two-factor sign-in")
    secret = security.new_totp_secret()
    db.update_user(u["id"], mfa_pending=security.encrypt(secret))
    uri = security.totp_uri(secret, u["email"])
    return {"secret": secret, "uri": uri, "qr": security.qr_data_uri(uri)}


class MfaEnable(BaseModel):
    code: str


@router.post("/api/account/mfa/enable")
def mfa_enable(body: MfaEnable, request: Request):
    u, sess = auth.me(request), request.state.session
    secret = security.decrypt(u.get("mfa_pending"))
    if not secret:
        raise HTTPException(400, "start the set-up again")
    step = security.totp_step(secret, body.code, 0)
    if step is None:
        tries = db.bump_session_tries(sess["id"])
        if tries >= 10:
            db.delete_session(sess["id"])
            raise HTTPException(401, "too many wrong codes - sign in again")
        raise HTTPException(400, "that code is not right - check the time on your phone "
                                 "and enter the current code")
    codes = security.new_recovery_codes()
    db.update_user(u["id"], mfa_secret=security.encrypt(secret), mfa_pending="",
                   mfa_enabled=1, mfa_last_step=step,
                   recovery_codes=json.dumps([security.recovery_hash(c) for c in codes]))
    db.audit("mfa.enabled", u["id"], ip=auth.client_ip(request))
    mailer.notice(u["email"], "Two-factor sign-in is on",
                  "Two-factor sign-in has been switched on for your NNC Reporter "
                  "account. If this was not you, tell your administrator.")
    # Enrolling completes the sign-in for a session that was waiting on it.
    resp = JSONResponse({})
    db.delete_sessions(u["id"], keep=sess["id"])
    db.delete_session(sess["id"])
    csrf = auth.start_session(resp, request, db.user(u["id"]), "full")
    if sess["stage"] == "enrol":
        db.update_user(u["id"], last_login=time.time())
    return auth._with_body(resp, {"ok": True, "recovery_codes": codes, "csrf": csrf})


class MfaOff(BaseModel):
    password: str
    code: str = ""


@router.post("/api/account/mfa/disable")
def mfa_disable(body: MfaOff, request: Request):
    u = auth.me(request)
    if auth.mfa_required_for(u):
        raise HTTPException(400, "two-factor sign-in is required for your account")
    if not security.verify_password(u["password_hash"], body.password):
        raise HTTPException(400, "your password is not right")
    if security.totp_step(security.decrypt(u["mfa_secret"]), body.code,
                          u.get("mfa_last_step") or 0) is None:
        raise HTTPException(400, "enter a current code from your authenticator app")
    db.update_user(u["id"], mfa_enabled=0, mfa_secret="", recovery_codes="[]")
    db.audit("mfa.disabled", u["id"], ip=auth.client_ip(request))
    mailer.notice(u["email"], "Two-factor sign-in is off",
                  "Two-factor sign-in was switched off for your NNC Reporter account. "
                  "If this was not you, tell your administrator straight away.")
    return {"ok": True}


@router.post("/api/account/mfa/recovery-codes")
def new_recovery_codes(body: MfaSetup, request: Request):
    u = auth.me(request)
    if not u["mfa_enabled"]:
        raise HTTPException(400, "two-factor sign-in is not on")
    if not security.verify_password(u["password_hash"], body.password):
        raise HTTPException(400, "your password is not right")
    codes = security.new_recovery_codes()
    db.update_user(u["id"], recovery_codes=json.dumps([security.recovery_hash(c)
                                                        for c in codes]))
    db.audit("mfa.recovery_codes", u["id"], ip=auth.client_ip(request))
    return {"recovery_codes": codes}


@router.post("/api/account/sessions/end-others")
def end_other_sessions(request: Request):
    u, sess = auth.me(request), request.state.session
    n = db.delete_sessions(u["id"], keep=sess["id"])
    db.audit("sessions.ended", u["id"], ip=auth.client_ip(request), detail=str(n))
    return {"ended": n}


@router.get("/api/account/export")
def export_my_data(request: Request, _=Depends(auth.need("viewer"))):
    """Everything held for you: your account details, your reports database and
    your photos, as one zip."""
    u = auth.me(request)
    folder = auth.user_dir(u["id"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        info = auth.public_user(u)
        info["profile"] = {k: v for k, v in db.profile(u).items()}
        info["audit"] = db.audit_log(500, u["id"])
        z.writestr("account.json", json.dumps(info, indent=2, default=str))
        if folder.exists():
            for p in sorted(folder.rglob("*")):
                rel = p.relative_to(folder)
                if not p.is_file() or rel.parts[0] in ("browser-profile", "inbox"):
                    continue
                z.write(p, str(rel))
    buf.seek(0)
    db.audit("account.export", u["id"], ip=auth.client_ip(request))
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="nnc-reporter-my-data.zip"'})
