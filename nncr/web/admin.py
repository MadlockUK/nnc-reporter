"""Administration: inviting people, roles, suspending, resetting, the audit
trail, and the shared settings (boundaries, Mapbox, council categories)."""
import re
import shutil
import time

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from .. import categories, submit
from . import auth, boundaries, db, mailer, mapbox, security, settings

router = APIRouter(dependencies=[Depends(auth.need("admin"))])

EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,}$")


def _row(u: dict) -> dict:
    out = auth.public_user(u)
    out["locked"] = auth.lock_note(u)
    folder = auth.user_dir(u["id"])
    out["has_data"] = (folder / "reports.db").exists()
    return out


@router.get("/api/admin/users")
def list_users():
    return {"users": [_row(u) for u in db.users()], "roles": list(db.ROLES),
            "mail": settings.get().mail_configured}


class Invite(BaseModel):
    email: str
    name: str = ""
    role: str = "editor"


@router.post("/api/admin/users")
def invite(body: Invite, request: Request):
    actor = auth.me(request)
    email = (body.email or "").strip().lower()
    if not EMAIL.match(email):
        raise HTTPException(400, "that does not look like an email address")
    if body.role not in db.ROLES:
        raise HTTPException(400, "pick a role: admin, editor or viewer")
    if db.user_by_email(email):
        raise HTTPException(400, "there is already an account for that email")
    uid = db.create_user(email, body.name, body.role, created_by=actor["id"])
    u = db.user(uid)
    url = auth.issue_invite(u, actor)
    db.audit("user.invited", uid, actor["id"], auth.client_ip(request), body.role)
    return {"user": _row(u), "invite_url": url, "emailed": settings.get().mail_configured}


def _target(uid: int, actor: dict) -> dict:
    u = db.user(uid)
    if not u:
        raise HTTPException(404, "no such account")
    return u


@router.post("/api/admin/users/{uid}/invite")
def resend_invite(uid: int, request: Request):
    actor = auth.me(request)
    u = _target(uid, actor)
    if u["status"] != "invited":
        raise HTTPException(400, "this person has already accepted their invitation")
    url = auth.issue_invite(u, actor)
    db.audit("user.reinvited", uid, actor["id"], auth.client_ip(request))
    return {"invite_url": url, "emailed": settings.get().mail_configured}


class Change(BaseModel):
    role: str | None = None
    status: str | None = None
    name: str | None = None


@router.patch("/api/admin/users/{uid}")
def change(uid: int, body: Change, request: Request):
    actor = auth.me(request)
    u = _target(uid, actor)
    fields = {}
    if body.name is not None:
        fields["name"] = body.name.strip()[:120]
    if body.role is not None and body.role != u["role"]:
        if body.role not in db.ROLES:
            raise HTTPException(400, "unknown role")
        if uid == actor["id"]:
            raise HTTPException(400, "you cannot change your own role")
        fields["role"] = body.role
    if body.status is not None and body.status != u["status"]:
        if body.status not in ("active", "suspended"):
            raise HTTPException(400, "status can be active or suspended")
        if uid == actor["id"]:
            raise HTTPException(400, "you cannot suspend yourself")
        if u["status"] == "invited":
            raise HTTPException(400, "this person has not accepted their invitation yet")
        fields["status"] = body.status
    if u["role"] == "admin" and u["status"] == "active" and (
            fields.get("role", "admin") != "admin" or fields.get("status") == "suspended") \
            and db.active_admins() <= 1:
        raise HTTPException(400, "this is the last active admin")
    if fields:
        db.update_user(uid, **fields)
        if "role" in fields or fields.get("status") == "suspended":
            db.delete_sessions(uid)          # new role or no access: sign in again
        db.audit("user.changed", uid, actor["id"], auth.client_ip(request),
                 ", ".join(f"{k}={v}" for k, v in fields.items()))
    return {"user": _row(db.user(uid))}


@router.post("/api/admin/users/{uid}/reset-password")
def send_reset(uid: int, request: Request):
    actor = auth.me(request)
    u = _target(uid, actor)
    if u["status"] != "active":
        raise HTTPException(400, "only active accounts can reset a password")
    raw = security.new_token()
    db.void_tokens(uid, "reset")
    db.create_token(security.token_hash(raw), uid, "reset", auth.RESET_MINUTES * 60)
    sent = mailer.reset(u["email"], mailer.link(f"/reset#token={raw}"), auth.RESET_MINUTES)
    db.audit("password.reset_sent", uid, actor["id"], auth.client_ip(request))
    # The link goes to the account holder only - never shown to the admin.
    return {"emailed": sent, "note": "" if sent else
            "email is not configured on the server, so the link was written to the "
            "server log instead"}


@router.post("/api/admin/users/{uid}/reset-mfa")
def reset_mfa(uid: int, request: Request):
    actor = auth.me(request)
    u = _target(uid, actor)
    db.update_user(uid, mfa_enabled=0, mfa_secret="", mfa_pending="", recovery_codes="[]")
    db.delete_sessions(uid)
    db.audit("mfa.reset", uid, actor["id"], auth.client_ip(request))
    mailer.notice(u["email"], "Your two-factor sign-in was reset",
                  "An administrator reset two-factor sign-in on your NNC Reporter "
                  "account. You will be asked to set it up again when you next sign in.")
    return {"ok": True}


@router.post("/api/admin/users/{uid}/unlock")
def unlock(uid: int, request: Request):
    actor = auth.me(request)
    _target(uid, actor)
    db.update_user(uid, failed_logins=0, locked_until=0)
    db.audit("user.unlocked", uid, actor["id"], auth.client_ip(request))
    return {"ok": True}


@router.post("/api/admin/users/{uid}/sign-out")
def sign_out_everywhere(uid: int, request: Request):
    actor = auth.me(request)
    _target(uid, actor)
    n = db.delete_sessions(uid)
    db.audit("sessions.ended", uid, actor["id"], auth.client_ip(request), str(n))
    return {"ended": n}


@router.delete("/api/admin/users/{uid}")
def remove(uid: int, request: Request, purge: bool = False):
    """Remove an account. Their reports are kept (and stay on the shared map's
    history) unless `purge` is set, which deletes their data folder too."""
    actor = auth.me(request)
    u = _target(uid, actor)
    if uid == actor["id"]:
        raise HTTPException(400, "you cannot remove your own account")
    if u["role"] == "admin" and u["status"] == "active" and db.active_admins() <= 1:
        raise HTTPException(400, "this is the last active admin")
    folder = auth.user_dir(uid)
    if folder.exists():
        if purge:
            shutil.rmtree(folder, ignore_errors=True)
        else:
            keep = settings.get().users_dir() / "_removed"
            keep.mkdir(exist_ok=True)
            folder.rename(keep / f"u{uid}-{int(time.time())}")
    db.delete_user(uid)
    db.audit("user.removed", uid, actor["id"], auth.client_ip(request),
             f"{u['email']} purge={purge}")
    return {"ok": True}


@router.get("/api/admin/audit")
def audit(limit: int = 300):
    names = {u["id"]: u["email"] for u in db.users()}
    rows = db.audit_log(min(max(limit, 1), 2000))
    for r in rows:
        r["user"] = names.get(r["user_id"], f"#{r['user_id']}" if r["user_id"] else "")
        r["actor"] = names.get(r["actor_id"], f"#{r['actor_id']}" if r["actor_id"] else "")
    return {"events": rows}


@router.get("/api/admin/status")
def status():
    s = settings.get()
    return {
        "base_url": s.base_url or "(not set - links in emails use the server address)",
        "mail": s.mail_configured, "mail_from": s.smtp_from,
        "mfa_required": s.mfa_required, "hibp": s.hibp_check,
        "password_min_length": s.password_min_length,
        "session": {"idle_minutes": s.session_idle_minutes,
                    "max_hours": s.session_max_hours},
        "mapbox": mapbox.status(),
        "anthropic": bool(s.anthropic_key), "what3words": bool(s.what3words_key),
        "submit_mode": submit.mode(),
        "secret_key_from_env": bool((__import__("os").environ.get("NNC_SECRET_KEY") or "")),
        "boundaries": boundaries.status()["layers"],
    }


@router.post("/api/admin/mapbox/test")
def mapbox_test():
    got = mapbox.browser_token(force=True)
    return {"working": bool(got["token"]), "kind": got["kind"], "problem": got["problem"]}


@router.get("/api/admin/boundaries")
def boundary_status():
    return boundaries.status()


@router.post("/api/admin/boundaries/fetch")
def boundary_fetch(request: Request):
    db.audit("boundaries.fetch", actor_id=auth.me(request)["id"], ip=auth.client_ip(request))
    return boundaries.start_fetch()


@router.post("/api/admin/boundaries/upload")
async def boundary_upload(request: Request, layer: str = Form(...),
                          file: UploadFile = File(...)):
    data = await file.read()
    try:
        res = boundaries.upload(layer, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("boundaries.upload", actor_id=auth.me(request)["id"],
             ip=auth.client_ip(request), detail=f"{layer}: {res.get('count')}")
    return res


class Towns(BaseModel):
    names: list[str]


@router.put("/api/admin/boundaries/town-councils")
def town_councils(body: Towns, request: Request):
    names = boundaries.set_town_councils(body.names)
    db.audit("boundaries.town_councils", actor_id=auth.me(request)["id"],
             ip=auth.client_ip(request), detail=", ".join(names)[:400])
    return boundaries.status()


@router.post("/api/admin/categories/refresh")
def refresh_categories():
    try:
        res = categories.refresh_from_site()
        return {"ok": True, "count": res["count"]}
    except Exception as e:                                       # noqa: BLE001
        raise HTTPException(502, f"could not refresh: {e}")
