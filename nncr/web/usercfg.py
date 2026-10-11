"""One person's settings, in the shape the reporting modules already expect.

The modules under nncr/ take a `cfg` dict (reporter details, keys, council
login...). In the web app that dict is built per request from the person's
account - never from config.json - with the shared API keys from the server's
environment. The council password is decrypted only here, only when built, and
nothing in the web layer ever sends `cfg` itself to a browser.
"""
from .. import config
from . import auth, db, security, settings

PROFILE_FIELDS = {
    # key: (max length or type, label)
    "full_name": (120, "Full name, as the council should see it"),
    "council_email": (254, "Email for the council's replies"),
    "phone": (40, "Phone number"),
    "address_line": (200, "Your address, first line"),
    "address_postcode": (12, "Your postcode"),
    "show_name_publicly": (bool, "Show my name publicly on the highways site"),
    "share_name": (bool, "Show my name against my reports on the shared map"),
    "highways_email": (254, "Highways site sign-in email"),
    "highways_enabled": (bool, "Sign in to the highways site automatically"),
    "mymap_mid": (300, "Google My Map link or id"),
}


def clean_profile(given: dict) -> dict:
    out = {}
    for k, v in (given or {}).items():
        if k not in PROFILE_FIELDS or v is None:
            continue
        kind = PROFILE_FIELDS[k][0]
        if kind is bool:
            out[k] = bool(v)
        else:
            out[k] = " ".join(str(v).split())[:kind]
    return out


def build(u: dict) -> dict:
    s = settings.get()
    prof = db.profile(u)
    cfg = config._merge(config.DEFAULTS, db.get_setting("org_config", {}) or {})
    cfg["reporter"] = {
        "full_name": prof.get("full_name") or u["name"],
        "first_name": "", "last_name": "",
        "email": prof.get("council_email") or u["email"],
        "phone": prof.get("phone") or "",
        "show_name_publicly": bool(prof.get("show_name_publicly", False)),
        "address_postcode": prof.get("address_postcode") or "",
        "address_line": prof.get("address_line") or "",
    }
    cfg["keys"] = {"what3words": s.what3words_key, "anthropic": s.anthropic_key,
                   "mapbox": ""}
    cfg["highways_login"] = {
        "email": prof.get("highways_email") or "",
        "password": security.decrypt(u.get("highways_password")),
        "enabled": bool(prof.get("highways_enabled", True)),
    }
    if s.classify_model:
        cfg["classify"]["model"] = s.classify_model
    folder = auth.user_dir(u["id"])
    cfg["export"] = {"map_folder": str(folder / "exports"),
                     "kml_filename": "NNC_Reports.kml",
                     "csv_filename": "NNC_Reports.csv",
                     "photo_folder": "NNC_Reports_photos"}
    cfg["geocode"]["nominatim_email"] = s.nominatim_email or s.smtp_from
    cfg["server"] = {"port": s.port, "open_browser": False}
    cfg["mymap"] = {"mid": prof.get("mymap_mid") or ""}
    cfg["profile"] = f"u{u['id']}"
    cfg["profiles"] = [cfg["profile"]]
    return cfg
