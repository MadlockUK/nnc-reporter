"""Fixtures for the web app tests: a fresh data folder and app per test, no
network (the breached-password check is off and outside calls are stubbed)."""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["NNC_ENV_FILE"] = str(ROOT / "tests" / "no-such.env")   # ignore any local .env

STRONG = "Cobalt-Heron-Lantern-42"
ORIGIN = "http://testserver"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("NNC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NNC_HIBP_CHECK", "0")
    monkeypatch.setenv("NNC_MFA_REQUIRED", "none")
    monkeypatch.setenv("NNC_SECRET_KEY", "test-secret-key-that-is-long-enough-1234567890")
    for var in ("NNC_BASE_URL", "SMTP_HOST", "MAPBOX_SECRET_TOKEN", "MAPBOX_PUBLIC_TOKEN",
                "ANTHROPIC_API_KEY", "WHAT3WORDS_API_KEY", "NNC_BOOTSTRAP_ADMIN"):
        monkeypatch.delenv(var, raising=False)
    from nncr.web import settings, boundaries, mapbox, aggregate
    settings.reset()
    boundaries._cache.clear()
    aggregate._cache.clear()
    mapbox._cached.update(token="", expires=0.0, problem="", tried=0.0)
    # No real network in tests: geocoding and What3Words come back empty.
    from nncr import geo
    monkeypatch.setattr(geo, "describe", lambda lat, lon, cfg: {
        "street": "Test Street", "locality": "Corby", "postcode": "NN17 1AA",
        "address": "", "w3w": None, "where": "Test Street", "street_is_nearest": "",
        "geo_error": None, "w3w_error": None})
    monkeypatch.setattr(geo, "nearby_park", lambda lat, lon, cfg: {"in_park": False,
                                                                  "radius": 5, "error": None})
    yield tmp_path
    settings.reset()


@pytest.fixture
def client(env):
    from fastapi.testclient import TestClient
    from nncr.web import server
    with TestClient(server.app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                    follow_redirects=False) as c:
        yield c


def make_user(email, role="editor", password=STRONG, name="Test Person", mfa_secret=None):
    import json
    import time
    from nncr.web import db, security
    uid = db.create_user(email, name, role, status="active")
    fields = {"password_hash": security.hash_password(password),
              "password_changed": time.time() - 5, "profile": {"full_name": name}}
    if mfa_secret:
        fields.update(mfa_secret=security.encrypt(mfa_secret), mfa_enabled=1,
                      recovery_codes=json.dumps([security.recovery_hash("aaaa-bbbb-cccc")]))
    db.update_user(uid, **fields)
    return uid


def login(c, email, password=STRONG):
    r = c.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf"]
    return r.json()


def photo(path, lat=52.4914, lon=-0.6923, colour=(130, 130, 130)):
    """A JPEG with GPS tags, like a phone photo."""
    import piexif
    from fractions import Fraction
    from PIL import Image

    def dms(v):
        v = abs(v)
        d = int(v)
        m = int((v - d) * 60)
        f = Fraction(round((v - d - m / 60) * 3600, 4)).limit_denominator(10000)
        return ((d, 1), (m, 1), (f.numerator, f.denominator))
    exif = {"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:07:20 14:05:11"},
            "GPS": {piexif.GPSIFD.GPSLatitudeRef: b"N", piexif.GPSIFD.GPSLatitude: dms(lat),
                    piexif.GPSIFD.GPSLongitudeRef: b"W" if lon < 0 else b"E",
                    piexif.GPSIFD.GPSLongitude: dms(lon)}, "1st": {}, "thumbnail": None}
    Image.new("RGB", (600, 450), colour).save(path, "JPEG", exif=piexif.dump(exif))
    return path
