"""The accounts database: people, sign-in sessions, one-time links, audit trail.

Kept apart from the report databases (one per person, in their own folder), so a
bug in report handling can never reach a password hash, and a person's reports
can be exported or deleted as a folder.

Secrets are never stored as given: passwords are Argon2id hashes, session ids and
one-time links are stored as SHA-256 hashes of the random value the browser
holds, and council passwords are encrypted (see security.py).
"""
import json
import sqlite3
import threading
import time

from . import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    email            TEXT UNIQUE NOT NULL COLLATE NOCASE,
    name             TEXT NOT NULL DEFAULT '',
    role             TEXT NOT NULL CHECK (role IN ('admin', 'editor', 'viewer')),
    status           TEXT NOT NULL CHECK (status IN ('invited', 'active', 'suspended')),
    password_hash    TEXT,
    password_changed REAL,
    mfa_secret       TEXT,              -- encrypted
    mfa_pending      TEXT,              -- encrypted, while enrolling
    mfa_enabled      INTEGER NOT NULL DEFAULT 0,
    mfa_last_step    INTEGER NOT NULL DEFAULT 0,
    recovery_codes   TEXT,              -- JSON list of SHA-256 hashes
    failed_logins    INTEGER NOT NULL DEFAULT 0,
    locked_until     REAL NOT NULL DEFAULT 0,
    profile          TEXT NOT NULL DEFAULT '{}',
    highways_password TEXT,             -- encrypted
    created_at       REAL NOT NULL,
    created_by       INTEGER,
    last_login       REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,       -- SHA-256 of the cookie value
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage       TEXT NOT NULL,          -- full | mfa | enrol
    csrf        TEXT NOT NULL,
    created_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    expires_at  REAL NOT NULL,
    ip          TEXT,
    user_agent  TEXT,
    tries       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS tokens (
    id          TEXT PRIMARY KEY,       -- SHA-256 of the emailed value
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose     TEXT NOT NULL,          -- invite | reset
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    used_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id, purpose);
CREATE TABLE IF NOT EXISTS attempts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    at      REAL NOT NULL,
    ip      TEXT,
    email   TEXT,
    kind    TEXT NOT NULL,              -- login | mfa | forgot | reset
    ok      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts ON attempts(kind, at);
CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    at       REAL NOT NULL,
    event    TEXT NOT NULL,
    user_id  INTEGER,
    actor_id INTEGER,
    ip       TEXT,
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit(at);
CREATE TABLE IF NOT EXISTS settings (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""

ROLES = ("admin", "editor", "viewer")
_init_lock = threading.Lock()
_ready_for = None


def path():
    return settings.get().data_dir / "accounts.db"


def connect() -> sqlite3.Connection:
    global _ready_for
    con = sqlite3.connect(path(), timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    if _ready_for != str(path()):
        with _init_lock:
            con.execute("PRAGMA journal_mode=WAL")
            con.executescript(SCHEMA)
            _ready_for = str(path())
    return con


def _one(sql: str, args=()) -> dict | None:
    con = connect()
    try:
        r = con.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def _all(sql: str, args=()) -> list:
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def _run(sql: str, args=()) -> sqlite3.Cursor:
    con = connect()
    try:
        return con.execute(sql, args)
    finally:
        con.close()


# ------------------------------------------------------------------- people

USER_FIELDS = {"email", "name", "role", "status", "password_hash", "password_changed",
               "mfa_secret", "mfa_pending", "mfa_enabled", "mfa_last_step",
               "recovery_codes", "failed_logins", "locked_until", "profile",
               "highways_password", "last_login"}


def user(uid: int) -> dict | None:
    return _one("SELECT * FROM users WHERE id=?", (uid,))


def user_by_email(email: str) -> dict | None:
    return _one("SELECT * FROM users WHERE email=?", ((email or "").strip(),))


def users() -> list:
    return _all("SELECT * FROM users ORDER BY status='suspended', name COLLATE NOCASE, email")


def create_user(email: str, name: str, role: str, created_by: int | None = None,
                status: str = "invited") -> int:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    cur = _run("INSERT INTO users (email, name, role, status, created_at, created_by) "
               "VALUES (?,?,?,?,?,?)",
               ((email or "").strip().lower(), (name or "").strip(), role, status,
                time.time(), created_by))
    return cur.lastrowid


def update_user(uid: int, **fields) -> dict | None:
    bad = set(fields) - USER_FIELDS
    if bad:
        raise ValueError(f"not user fields: {bad}")
    if "profile" in fields and isinstance(fields["profile"], dict):
        fields["profile"] = json.dumps(fields["profile"])
    if fields:
        sets = ",".join(f"{k}=?" for k in fields)
        _run(f"UPDATE users SET {sets} WHERE id=?", (*fields.values(), uid))
    return user(uid)


def delete_user(uid: int) -> None:
    _run("DELETE FROM users WHERE id=?", (uid,))


def active_admins() -> int:
    r = _one("SELECT COUNT(*) n FROM users WHERE role='admin' AND status='active'")
    return r["n"] if r else 0


def profile(u: dict) -> dict:
    try:
        return json.loads(u.get("profile") or "{}") or {}
    except ValueError:
        return {}


# ----------------------------------------------------------------- sessions

def create_session(sid_hash: str, user_id: int, stage: str, csrf: str, ttl: float,
                   ip: str, user_agent: str) -> None:
    now = time.time()
    _run("INSERT INTO sessions (id, user_id, stage, csrf, created_at, last_seen, "
         "expires_at, ip, user_agent) VALUES (?,?,?,?,?,?,?,?,?)",
         (sid_hash, user_id, stage, csrf, now, now, now + ttl, ip, (user_agent or "")[:300]))


def session(sid_hash: str) -> dict | None:
    return _one("SELECT * FROM sessions WHERE id=?", (sid_hash,))


def touch_session(sid_hash: str) -> None:
    _run("UPDATE sessions SET last_seen=? WHERE id=?", (time.time(), sid_hash))


def bump_session_tries(sid_hash: str) -> int:
    _run("UPDATE sessions SET tries=tries+1 WHERE id=?", (sid_hash,))
    r = session(sid_hash)
    return r["tries"] if r else 99


def delete_session(sid_hash: str) -> None:
    _run("DELETE FROM sessions WHERE id=?", (sid_hash,))


def delete_sessions(user_id: int, keep: str | None = None) -> int:
    if keep:
        return _run("DELETE FROM sessions WHERE user_id=? AND id<>?", (user_id, keep)).rowcount
    return _run("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount


def sessions_for(user_id: int) -> list:
    return _all("SELECT * FROM sessions WHERE user_id=? AND stage='full' "
                "ORDER BY last_seen DESC", (user_id,))


def purge_expired() -> None:
    now = time.time()
    _run("DELETE FROM sessions WHERE expires_at<?", (now,))
    _run("DELETE FROM tokens WHERE expires_at<? AND (used_at IS NOT NULL OR expires_at<?)",
         (now, now - 30 * 86400))
    _run("DELETE FROM attempts WHERE at<?", (now - 7 * 86400,))


# ------------------------------------------------------------ one-time links

def create_token(token_hash: str, user_id: int, purpose: str, ttl: float) -> None:
    now = time.time()
    _run("INSERT INTO tokens (id, user_id, purpose, created_at, expires_at) "
         "VALUES (?,?,?,?,?)", (token_hash, user_id, purpose, now, now + ttl))


def token(token_hash: str, purpose: str) -> dict | None:
    """A link that is still good: right purpose, unused, unexpired."""
    return _one("SELECT * FROM tokens WHERE id=? AND purpose=? AND used_at IS NULL "
                "AND expires_at>?", (token_hash, purpose, time.time()))


def use_token(token_hash: str, purpose: str) -> int | None:
    """Spend a link. Atomic: two requests racing with one link get one success."""
    con = connect()
    try:
        cur = con.execute("UPDATE tokens SET used_at=? WHERE id=? AND purpose=? AND "
                          "used_at IS NULL AND expires_at>?",
                          (time.time(), token_hash, purpose, time.time()))
        if cur.rowcount != 1:
            return None
        r = con.execute("SELECT user_id FROM tokens WHERE id=?", (token_hash,)).fetchone()
        return r["user_id"] if r else None
    finally:
        con.close()


def void_tokens(user_id: int, purpose: str) -> None:
    _run("UPDATE tokens SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
         (time.time(), user_id, purpose))


# ----------------------------------------------------------------- throttling

def attempt(kind: str, ok: bool, ip: str = "", email: str = "") -> None:
    _run("INSERT INTO attempts (at, ip, email, kind, ok) VALUES (?,?,?,?,?)",
         (time.time(), ip, (email or "").lower()[:200], kind, 1 if ok else 0))


def failures(kind: str, seconds: float, ip: str | None = None,
             email: str | None = None, include_ok: bool = False) -> int:
    q = "SELECT COUNT(*) n FROM attempts WHERE kind=? AND at>?"
    args = [kind, time.time() - seconds]
    if not include_ok:
        q += " AND ok=0"
    if ip is not None:
        q += " AND ip=?"
        args.append(ip)
    if email is not None:
        q += " AND email=?"
        args.append((email or "").lower())
    r = _one(q, args)
    return r["n"] if r else 0


# ---------------------------------------------------------------------- audit

def audit(event: str, user_id: int | None = None, actor_id: int | None = None,
          ip: str = "", detail: str = "") -> None:
    _run("INSERT INTO audit (at, event, user_id, actor_id, ip, detail) VALUES (?,?,?,?,?,?)",
         (time.time(), event, user_id, actor_id, ip, (detail or "")[:500]))


def audit_log(limit: int = 200, user_id: int | None = None) -> list:
    if user_id:
        return _all("SELECT * FROM audit WHERE user_id=? OR actor_id=? ORDER BY id DESC "
                    "LIMIT ?", (user_id, user_id, limit))
    return _all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))


# ------------------------------------------------------------ org settings

def get_setting(key: str, default=None):
    r = _one("SELECT value FROM settings WHERE key=?", (key,))
    if not r:
        return default
    try:
        return json.loads(r["value"])
    except ValueError:
        return default


def set_setting(key: str, value) -> None:
    _run("INSERT INTO settings (key, value) VALUES (?,?) "
         "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))
