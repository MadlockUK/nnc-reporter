"""Mapbox tokens for the browser, without ever exposing the secret token.

The secret (sk.) token lives only in the server's environment
(MAPBOX_SECRET_TOKEN) - never in the repository, never in a page. Signed-in
users' browsers are given a *temporary* token (tk.) minted from it through
Mapbox's Tokens API: read-only map scopes, valid for under an hour, cached and
shared until a few minutes before it expires. Anyone who lifts one from a
browser gets a map-viewing token that dies within the hour.

Minting needs the secret token to carry the `tokens:write` scope. If it does
not (or Mapbox cannot be reached), a public pk. token in MAPBOX_PUBLIC_TOKEN -
URL-restricted to the app's own address - is used instead, if one is set.
"""
import base64
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from . import settings

log = logging.getLogger("nnc.mapbox")

DEFAULT_SCOPES = ["styles:read", "styles:tiles", "fonts:read", "datasets:read",
                  "vision:read"]
MINIMAL_SCOPES = ["styles:read", "styles:tiles", "fonts:read"]
LIFETIME = 3600 - 60          # Mapbox allows temporary tokens up to an hour
REFRESH_BEFORE = 600

_lock = threading.Lock()
_cached = {"token": "", "expires": 0.0, "problem": "", "tried": 0.0}


def username_from(token: str) -> str:
    """The account name is in the token's own payload: sk.<base64 json>.<sig>."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part)).get("u") or ""
    except Exception:
        return ""


def _scopes() -> list:
    raw = (os.environ.get("MAPBOX_TOKEN_SCOPES") or "").strip()
    return [x.strip() for x in raw.split(",") if x.strip()] if raw else DEFAULT_SCOPES


def _mint(secret: str, user: str) -> tuple:
    import requests
    expires = datetime.fromtimestamp(time.time() + LIFETIME, tz=timezone.utc)
    last = ""
    for scopes in (_scopes(), MINIMAL_SCOPES):
        try:
            r = requests.post(f"https://api.mapbox.com/tokens/v2/{user}",
                              params={"access_token": secret},
                              json={"expires": expires.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                    "scopes": scopes},
                              timeout=10)
        except Exception as e:                                    # noqa: BLE001
            return "", f"could not reach Mapbox: {type(e).__name__}"
        if r.ok:
            tok = (r.json() or {}).get("token") or ""
            if tok and not tok.startswith("sk."):
                return tok, ""
            last = "Mapbox returned no temporary token"
        else:
            try:
                msg = (r.json() or {}).get("message") or r.text[:200]
            except ValueError:
                msg = r.text[:200]
            last = f"Mapbox refused to mint a temporary token ({r.status_code}: {msg})"
            if r.status_code in (401, 403) and "scope" not in msg.lower():
                break
    return "", last + (". The secret token needs the tokens:write scope." if last else "")


def browser_token(force: bool = False) -> dict:
    """{token, expires, kind, problem} - for a signed-in user's browser only."""
    s = settings.get()
    secret = s.mapbox_secret_token
    now = time.time()
    with _lock:
        if not force and _cached["token"] and _cached["expires"] - now > REFRESH_BEFORE:
            return {"token": _cached["token"], "expires": int(_cached["expires"]),
                    "kind": "temporary", "problem": ""}
        problem = ""
        if secret.startswith("sk."):
            # Do not hammer Mapbox when it is refusing: retry at most once a minute.
            if force or now - _cached["tried"] > 60 or not _cached["problem"]:
                _cached["tried"] = now
                user = s.mapbox_username or username_from(secret)
                tok, problem = _mint(secret, user) if user else (
                    "", "set MAPBOX_USERNAME - it could not be read from the token")
                _cached.update(token=tok, expires=now + LIFETIME if tok else 0,
                               problem=problem)
                if tok:
                    return {"token": tok, "expires": int(now + LIFETIME),
                            "kind": "temporary", "problem": ""}
                log.warning("Mapbox temporary token: %s", problem)
            problem = _cached["problem"]
        elif secret:
            problem = ("MAPBOX_SECRET_TOKEN does not look like a secret (sk.) token")

    pub = s.mapbox_public_token
    if pub.startswith("pk."):
        return {"token": pub, "expires": 0, "kind": "public", "problem": ""}
    if pub.startswith("sk."):
        problem = "MAPBOX_PUBLIC_TOKEN holds a secret token - it is never sent to browsers"
    if not secret and not pub:
        problem = ("No Mapbox token configured. Set MAPBOX_SECRET_TOKEN (with the "
                   "tokens:write scope) in the server's environment.")
    return {"token": "", "expires": 0, "kind": "", "problem": problem}


def status() -> dict:
    """For the admin page: which token is in use, never the token itself."""
    s = settings.get()
    got = browser_token()
    return {"secret_set": bool(s.mapbox_secret_token),
            "public_set": bool(s.mapbox_public_token),
            "username": s.mapbox_username or username_from(s.mapbox_secret_token),
            "kind": got["kind"], "working": bool(got["token"]), "problem": got["problem"]}
