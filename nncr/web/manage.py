"""Command-line administration for the web app.

  python -m nncr.web.manage create-admin --email you@example.com [--name "You"]
  python -m nncr.web.manage invite --email them@example.com --role editor
  python -m nncr.web.manage users
  python -m nncr.web.manage unlock --email them@example.com
  python -m nncr.web.manage reset-mfa --email them@example.com
  python -m nncr.web.manage fetch-boundaries
  python -m nncr.web.manage import-local --email you@example.com --from data [--profile main]
  python -m nncr.web.manage check

Invitation links are printed here as well as emailed, so the first admin can be
set up before email is configured. They work once, for 7 days.
"""
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from .. import config
from . import auth, boundaries, db, mapbox, settings


def _user(email: str) -> dict:
    u = db.user_by_email(email)
    if not u:
        sys.exit(f"no account for {email}")
    return u


def create(email: str, name: str, role: str) -> None:
    email = email.strip().lower()
    u = db.user_by_email(email)
    if u and u["status"] != "invited":
        sys.exit(f"{email} already has an active account - use the admin page, or "
                 f"'reset-mfa' / 'unlock' here")
    if not u:
        uid = db.create_user(email, name, role)
        u = db.user(uid)
        db.audit("user.invited", uid, detail=f"{role}, from the command line")
    elif u["role"] != role:
        db.update_user(u["id"], role=role)
    url = auth.issue_invite(db.user(u["id"]), None)
    print(f"\nInvitation for {email} ({role}). Open this link to set the password - "
          f"it works once, for {auth.INVITE_DAYS} days:\n\n  {url}\n")


def import_local(email: str, source: str, profile: str | None) -> None:
    """Bring a desktop install's reports and photos into a web account."""
    u = _user(email)
    src = Path(source).resolve()
    if profile and profile != "main":
        src = src / "profiles" / config.slug(profile)
    db_file = src / "reports.db"
    if not db_file.exists():
        sys.exit(f"no reports.db in {src}")
    dest = auth.user_dir(u["id"])
    dest.mkdir(parents=True, exist_ok=True)
    if (dest / "reports.db").exists():
        sys.exit(f"{email} already has reports - refusing to overwrite {dest / 'reports.db'}")
    for sub in ("photos", "thumbs", "pins"):
        if (src / sub).exists():
            shutil.copytree(src / sub, dest / sub, dirs_exist_ok=True)
    shutil.copy2(db_file, dest / "reports.db")

    # Point the stored file paths at the copies (they were absolute Windows paths).
    con = sqlite3.connect(dest / "reports.db")
    moved = 0
    for table, cols in (("reports", ("photo_path", "thumb_path")),
                        ("map_pins", ("photo_path", "thumb_path"))):
        try:
            rows = con.execute(f"SELECT id, {', '.join(cols)} FROM {table}").fetchall()
        except sqlite3.Error:
            continue
        for row in rows:
            fixes = {}
            for col, val in zip(cols, row[1:]):
                if not val:
                    continue
                name = val.replace("\\", "/").rsplit("/", 1)[-1]
                parent = val.replace("\\", "/").rsplit("/", 2)[-2] if "/" in val.replace(
                    "\\", "/") else ""
                folder = parent if parent in ("photos", "thumbs", "pins") else \
                    ("thumbs" if col == "thumb_path" and table == "reports" else
                     "pins" if table == "map_pins" else "photos")
                fixes[col] = str(dest / folder / name)
            if fixes:
                con.execute(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in fixes)} "
                            f"WHERE id=?", (*fixes.values(), row[0]))
                moved += 1
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    con.close()
    db.audit("user.imported", u["id"], detail=f"{n} reports from {src}")
    print(f"Imported {n} reports into {email}'s account ({moved} file paths updated).")


def check() -> None:
    s = settings.get()
    rows = [
        ("Data folder", str(s.data_dir)),
        ("Base URL", s.base_url or "NOT SET"),
        ("Secure cookies", s.cookie_secure),
        ("Secret key", "from NNC_SECRET_KEY" if __import__("os").environ.get("NNC_SECRET_KEY")
         else "generated, in the data folder"),
        ("Email (SMTP)", f"{s.smtp_host}:{s.smtp_port} as {s.smtp_from}"
         if s.mail_configured else "NOT CONFIGURED"),
        ("Two-factor required for", s.mfa_required),
        ("Breached-password check", s.hibp_check),
        ("Mapbox", mapbox.status()),
        ("Anthropic key", bool(s.anthropic_key)),
        ("What3Words key", bool(s.what3words_key)),
        ("Submission mode", s.submit_mode),
        ("Accounts", len(db.users())),
        ("Boundaries", {k: v["count"] for k, v in boundaries.status()["layers"].items()}),
    ]
    for k, v in rows:
        print(f"  {k:26} {v}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m nncr.web.manage")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("create-admin")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="")
    p = sub.add_parser("invite")
    p.add_argument("--email", required=True)
    p.add_argument("--name", default="")
    p.add_argument("--role", default="editor", choices=db.ROLES)
    sub.add_parser("users")
    for name in ("unlock", "reset-mfa", "sign-out"):
        p = sub.add_parser(name)
        p.add_argument("--email", required=True)
    sub.add_parser("fetch-boundaries")
    p = sub.add_parser("import-local")
    p.add_argument("--email", required=True)
    p.add_argument("--from", dest="source", required=True)
    p.add_argument("--profile", default=None)
    sub.add_parser("check")
    a = ap.parse_args(argv)

    if a.cmd == "create-admin":
        create(a.email, a.name, "admin")
    elif a.cmd == "invite":
        create(a.email, a.name, a.role)
    elif a.cmd == "users":
        for u in db.users():
            print(f"  {u['id']:>4}  {u['email']:40} {u['role']:7} {u['status']:9} "
                  f"2FA={'on' if u['mfa_enabled'] else 'off'}  {auth.lock_note(u)}")
    elif a.cmd == "unlock":
        u = _user(a.email)
        db.update_user(u["id"], failed_logins=0, locked_until=0)
        db.audit("user.unlocked", u["id"], detail="command line")
        print("unlocked")
    elif a.cmd == "reset-mfa":
        u = _user(a.email)
        db.update_user(u["id"], mfa_enabled=0, mfa_secret="", mfa_pending="",
                       recovery_codes="[]")
        db.delete_sessions(u["id"])
        db.audit("mfa.reset", u["id"], detail="command line")
        print("two-factor reset - they will set it up again at next sign-in")
    elif a.cmd == "sign-out":
        u = _user(a.email)
        print(f"ended {db.delete_sessions(u['id'])} session(s)")
    elif a.cmd == "fetch-boundaries":
        print(boundaries.fetch_from_ons())
    elif a.cmd == "import-local":
        import_local(a.email, a.source, a.profile)
    elif a.cmd == "check":
        check()
    return 0


if __name__ == "__main__":
    sys.exit(main())
