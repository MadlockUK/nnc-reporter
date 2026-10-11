"""Sign-in cannot be bypassed: every route is closed without a session, changes
need a same-site origin and the CSRF token, passwords must be strong, brute
force locks the account, and one-time links work once."""
import re
import time

import pyotp
import pytest

from conftest import ORIGIN, STRONG, login, make_user


def all_routes():
    from fastapi.routing import APIRoute
    from nncr.web import server
    for r in server.app.routes:
        if isinstance(r, APIRoute):
            path = re.sub(r"\{[^}]+\}", "1", r.path)
            for m in r.methods:
                yield m, path


def test_every_route_is_closed_without_a_session(client):
    from nncr.web import auth
    opened = []
    for method, path in all_routes():
        if (method, path) in auth.PUBLIC:
            continue
        r = client.request(method, path, json={})
        if r.status_code not in (401, 303, 403):
            opened.append((method, path, r.status_code))
        if r.status_code == 303:
            assert r.headers["location"].startswith("/login")
    assert not opened, f"reachable without signing in: {opened}"


def test_static_assets_hold_nothing_personal(client):
    assert client.get("/assets/common.js").status_code == 200
    for sneaky in ("/assets/../nncr/web/db.py", "/assets/%2e%2e/web/db.py",
                   "/assets/..%2fdb.py"):
        r = client.get(sneaky)
        assert r.status_code in (303, 401, 404) and "argon2" not in r.text
    assert client.get("/docs").status_code in (401, 303, 404)
    assert client.get("/openapi.json").status_code in (401, 303, 404)


def test_password_policy():
    from nncr.web import security
    bad = {
        "short1!A": "12 characters",
        "alllowercase-words-here": "capital",
        "ALLUPPER-WORDS-HERE-1": "lower",
        "NoNumbers-Here-At-All": "number",
        "NoSymbols1234567Abc": None,       # symbol, but also a run - either complaint
        "Password12345!": None,
        "Aaaaaaaaaaaa1!": "aaaa",
        "Jane-Smith-Garden-77": "name",
    }
    for pw, needle in bad.items():
        problems = security.password_problems(pw, "jane.smith@example.com", "Jane Smith",
                                              check_breaches=False)
        assert problems, f"{pw} should be refused"
        if needle:
            assert any(needle in p for p in problems), (pw, problems)
    assert security.password_problems(STRONG, "jane@example.com", "Jane",
                                      check_breaches=False) == []
    assert security.password_problems("Password1234!x", check_breaches=False)


def test_hashes_not_passwords_are_stored(env):
    from nncr.web import db
    make_user("a@example.com")
    u = db.user_by_email("a@example.com")
    assert u["password_hash"].startswith("$argon2id$")
    assert STRONG not in str(u)


def test_login_logout_and_generic_failures(client):
    make_user("a@example.com")
    r = client.post("/api/auth/login", json={"email": "a@example.com", "password": "Wrong-Pass-999!"})
    r2 = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": "x"})
    assert r.status_code == r2.status_code == 401
    assert r.json()["detail"] == r2.json()["detail"]          # no account enumeration
    login(client, "a@example.com")
    assert client.get("/api/me").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/me").status_code == 401


def test_cookie_flags(env, monkeypatch):
    from fastapi.testclient import TestClient
    from nncr.web import server, settings
    monkeypatch.setenv("NNC_BASE_URL", "https://reports.example.org")
    settings.reset()
    make_user("a@example.com")
    with TestClient(server.app, base_url="https://reports.example.org",
                    headers={"Origin": "https://reports.example.org"}) as c:
        r = c.post("/api/auth/login", json={"email": "a@example.com", "password": STRONG})
        cookie = r.headers["set-cookie"]
        assert cookie.startswith("__Host-nnc_session=")
        for flag in ("HttpOnly", "Secure", "Path=/", "SameSite=lax"):
            assert flag.lower() in cookie.lower()
        assert "strict-transport-security" in r.headers
        # Another site's page cannot make changes, even with the cookie.
        r = c.post("/api/account/sessions/end-others", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403


def test_csrf_token_required(client):
    make_user("a@example.com")
    login(client, "a@example.com")
    token = client.headers.pop("X-CSRF-Token")
    assert client.patch("/api/account/profile", json={"phone": "1"}).status_code == 403
    assert client.patch("/api/account/profile", json={"phone": "1"},
                        headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert client.patch("/api/account/profile", json={"phone": "1"},
                        headers={"X-CSRF-Token": token}).status_code == 200
    r = client.patch("/api/account/profile", json={"phone": "1"},
                     headers={"X-CSRF-Token": token, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_brute_force_locks_the_account(client):
    from nncr.web import db
    make_user("a@example.com")
    for _ in range(5):
        client.post("/api/auth/login", json={"email": "a@example.com", "password": "Wrong-Pass-999!"})
    u = db.user_by_email("a@example.com")
    assert u["locked_until"] > time.time()
    # Even the right password is refused while locked - with the same message.
    r = client.post("/api/auth/login", json={"email": "a@example.com", "password": STRONG})
    assert r.status_code == 401
    db.update_user(u["id"], locked_until=0)
    login(client, "a@example.com")


def test_ip_throttle(client):
    for i in range(20):
        client.post("/api/auth/login", json={"email": f"x{i}@example.com", "password": "nope"})
    r = client.post("/api/auth/login", json={"email": "y@example.com", "password": "nope"})
    assert r.status_code == 429


def test_suspended_account_is_signed_out(client):
    from nncr.web import db
    uid = make_user("a@example.com")
    login(client, "a@example.com")
    db.update_user(uid, status="suspended")
    assert client.get("/api/me").status_code == 401
    r = client.post("/api/auth/login", json={"email": "a@example.com", "password": STRONG})
    assert r.status_code == 401


def test_idle_and_absolute_expiry(client):
    from nncr.web import db
    make_user("a@example.com")
    login(client, "a@example.com")
    con = db.connect()
    con.execute("UPDATE sessions SET last_seen=last_seen-100000")
    con.close()
    assert client.get("/api/me").status_code == 401


def test_password_reset_works_once_and_signs_everyone_out(client, monkeypatch):
    from nncr.web import mailer
    sent = {}
    monkeypatch.setattr(mailer, "reset", lambda to, url, minutes: sent.update(url=url) or True)
    make_user("a@example.com")
    login(client, "a@example.com")
    r = client.post("/api/auth/forgot", json={"email": "a@example.com"})
    r2 = client.post("/api/auth/forgot", json={"email": "nobody@example.com"})
    assert r.json() == r2.json()                              # same answer either way
    token = sent["url"].split("#token=")[1]
    new = "Saffron-Kettle-Orbit-81"
    assert client.post("/api/auth/reset", json={"token": token, "password": "weakpass"}).status_code == 400
    assert client.post("/api/auth/reset", json={"token": token, "password": new}).status_code == 200
    assert client.post("/api/auth/reset", json={"token": token, "password": new + "x"}).status_code == 400
    assert client.get("/api/me").status_code == 401           # the old session ended
    assert client.post("/api/auth/login", json={"email": "a@example.com", "password": STRONG}).status_code == 401
    login(client, "a@example.com", new)


def test_invitation_flow(client):
    from nncr.web import auth, db
    admin = make_user("boss@example.com", "admin")
    login(client, "boss@example.com")
    r = client.post("/api/admin/users", json={"email": "New@Example.com", "name": "Newbie", "role": "viewer"})
    assert r.status_code == 200, r.text
    token = r.json()["invite_url"].split("#token=")[1]
    assert client.post("/api/admin/users", json={"email": "new@example.com"}).status_code == 400
    client.post("/api/auth/logout")
    client.headers.pop("X-CSRF-Token", None)
    info = client.post("/api/auth/invite/info", json={"token": token}).json()
    assert info["email"] == "new@example.com"
    assert client.post("/api/auth/invite/accept", json={"token": token, "password": STRONG,
                                                        "name": "Newbie"}).status_code == 200
    assert client.post("/api/auth/invite/accept", json={"token": token, "password": STRONG,
                                                        "name": "Newbie"}).status_code == 400
    login(client, "new@example.com")
    assert client.get("/api/me").json()["user"]["role"] == "viewer"
    assert db.user_by_email("new@example.com")["status"] == "active"
    assert admin


def test_no_self_registration(client):
    # There is no sign-up route at all; an unknown email can never sign in.
    from nncr.web import server
    paths = {getattr(r, "path", "") for r in server.app.routes}
    assert not any("register" in p or "signup" in p for p in paths)


def test_two_factor_enrolment_required_for_admins(env, monkeypatch):
    from fastapi.testclient import TestClient
    from nncr.web import server, settings
    monkeypatch.setenv("NNC_MFA_REQUIRED", "admins")
    settings.reset()
    make_user("boss@example.com", "admin")
    with TestClient(server.app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                    follow_redirects=False) as c:
        r = login(c, "boss@example.com")
        assert r["next"] == "enrol"
        # Until enrolled, nothing but the enrolment steps is reachable.
        assert c.get("/api/state").status_code == 403
        assert c.get("/api/admin/users").status_code == 403
        assert c.get("/").status_code == 303
        setup = c.post("/api/account/mfa/setup", json={}).json()
        code = pyotp.TOTP(setup["secret"]).now()
        r = c.post("/api/account/mfa/enable", json={"code": code})
        assert r.status_code == 200, r.text
        assert len(r.json()["recovery_codes"]) == 10
        c.headers["X-CSRF-Token"] = r.json()["csrf"]
        assert c.get("/api/admin/users").status_code == 200


def test_two_factor_sign_in_and_replay(client):
    from nncr.web import db
    secret = pyotp.random_base32()
    make_user("a@example.com", mfa_secret=secret)
    r = client.post("/api/auth/login", json={"email": "a@example.com", "password": STRONG})
    assert r.json()["next"] == "mfa"
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    assert client.get("/api/me").status_code == 403           # password alone is not enough
    assert client.get("/api/state").status_code == 403
    assert client.post("/api/auth/mfa", json={"code": "000000"}).status_code == 401
    code = pyotp.TOTP(secret).now()
    r = client.post("/api/auth/mfa", json={"code": code})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    assert client.get("/api/me").status_code == 200
    # The same code cannot be used again.
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": "a@example.com", "password": STRONG})
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    assert client.post("/api/auth/mfa", json={"code": code}).status_code == 401
    # A recovery code works, once.
    r = client.post("/api/auth/mfa", json={"code": "AAAA-BBBB-CCCC"})
    assert r.status_code == 200 and r.json()["recovery_codes_left"] == 0
    assert db.user_by_email("a@example.com")["recovery_codes"] == "[]"


def test_mfa_tries_are_limited(client):
    secret = pyotp.random_base32()
    make_user("a@example.com", mfa_secret=secret)
    r = client.post("/api/auth/login", json={"email": "a@example.com", "password": STRONG})
    client.headers["X-CSRF-Token"] = r.json()["csrf"]
    for _ in range(5):
        client.post("/api/auth/mfa", json={"code": "123456"})
    r = client.post("/api/auth/mfa", json={"code": pyotp.TOTP(secret).now()})
    assert r.status_code in (400, 401, 403)                  # that sign-in attempt is over


def test_change_password_needs_current_and_ends_other_sessions(client, env):
    from fastapi.testclient import TestClient
    from nncr.web import server
    make_user("a@example.com")
    with TestClient(server.app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as other:
        login(other, "a@example.com")
        login(client, "a@example.com")
        new = "Velvet-Quarry-Signal-63"
        assert client.post("/api/account/password", json={"current": "nope", "new": new}).status_code == 400
        r = client.post("/api/account/password", json={"current": STRONG, "new": new})
        assert r.status_code == 200, r.text
        client.headers["X-CSRF-Token"] = r.json()["csrf"]
        assert client.get("/api/me").status_code == 200       # this one carries on
        assert other.get("/api/me").status_code == 401        # the other is signed out


def test_security_headers(client):
    r = client.get("/login")
    for h in ("content-security-policy", "x-frame-options", "x-content-type-options",
              "referrer-policy"):
        assert h in r.headers
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"


def test_audit_trail(client):
    from nncr.web import db
    make_user("a@example.com")
    client.post("/api/auth/login", json={"email": "a@example.com", "password": "Wrong-Pass-1!"})
    login(client, "a@example.com")
    events = [e["event"] for e in db.audit_log(10)]
    assert "login.failed" in events and "login.ok" in events


def test_council_password_is_encrypted_and_never_returned(client):
    from nncr.web import db
    make_user("a@example.com")
    login(client, "a@example.com")
    assert client.put("/api/account/highways-password", json={"password": "Highways-Secret-1"}).status_code == 200
    stored = db.user_by_email("a@example.com")["highways_password"]
    assert stored and "Highways-Secret-1" not in stored
    for path in ("/api/account", "/api/state", "/api/me"):
        assert "Highways-Secret-1" not in client.get(path).text
