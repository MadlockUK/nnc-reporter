"""Config loading. Everything lives in config.json next to app.py - never in code.

API keys can be kept out of this folder entirely, which matters if the folder is
synced or shared. Precedence, highest first:
  1. environment variables ANTHROPIC_API_KEY / WHAT3WORDS_API_KEY
  2. %USERPROFILE%\\.nnc-reporter\\keys.json   ->  {"anthropic": "...", "what3words": "..."}
  3. the "keys" block in config.json
"""
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config.json"
EXAMPLE_PATH = BASE_DIR / "config.example.json"

DEFAULTS = {
    "reporter": {"full_name": "", "first_name": "", "last_name": "", "email": "",
                 "phone": "", "show_name_publicly": False,
                 "address_postcode": "", "address_line": ""},
    "keys": {"what3words": "", "anthropic": "", "mapbox": ""},
    "highways_login": {"email": "", "password": "", "enabled": True},
    "classify": {"enabled": True, "model": "claude-sonnet-5"},
    # Which form each category goes to is fixed in nncr/routing.py, not here:
    # the highways site refuses fly-tipping outright.
    "routing": {},
    "submission": {"mode": "confirm", "wait_for_submit_seconds": 900},
    "export": {"map_folder": "..", "kml_filename": "NNC_Reports.kml",
               "csv_filename": "NNC_Reports.csv"},
    "geocode": {"nominatim_email": ""},
    # How close a park has to be before the bins form's "is it in a park?"
    # question is answered yes.
    "bins": {"park_radius_metres": 5},
    "map": {"centre_lat": 52.4914, "centre_lon": -0.6923, "zoom": 13},
    "server": {"port": 8712, "open_browser": True},
    # The Google My Map whose pins are merged into the interactive map.
    "mymap": {"mid": ""},
}

# Where to look when nothing else is known - Corby town centre.
DEFAULT_CENTRE = (52.4914, -0.6923)


def centre(cfg: dict | None = None) -> tuple[float, float]:
    m = (cfg or {}).get("map") or {}
    return (float(m.get("centre_lat", DEFAULT_CENTRE[0])),
            float(m.get("centre_lon", DEFAULT_CENTRE[1])))

_PLACEHOLDERS = ("PUT_YOUR", "REPLACE", "XXX")


def _copy(d: dict) -> dict:
    """A private copy, so per-profile changes cannot leak back into DEFAULTS."""
    return {k: _copy(v) if isinstance(v, dict) else v for k, v in d.items()}


def _merge(base: dict, over: dict) -> dict:
    out = _copy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------- profiles
# A profile is one person reporting: their own name and logins, their own list of
# reports, their own map file. The main profile is the top level of config.json
# and keeps its data where it has always been; extra profiles live in a
# "profiles" block and get a folder of their own under data/profiles/.

MAIN_PROFILE = "main"


def slug(name: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in (name or "").strip().lower()]
    return "".join(keep).strip("-") or "profile"


def _raw() -> dict:
    if not CONFIG_PATH.exists() and EXAMPLE_PATH.exists():
        CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"config.json is not valid JSON: {e}")


def profile_names() -> list:
    extra = [n for n in (_raw().get("profiles") or {}) if n != MAIN_PROFILE]
    return [MAIN_PROFILE] + sorted(extra)


def _active_file() -> Path:
    d = BASE_DIR / "data"
    d.mkdir(exist_ok=True)
    return d / "active_profile.txt"      # outside any one profile's folder


def active_profile() -> str:
    try:
        name = _active_file().read_text(encoding="utf-8").strip()
    except OSError:
        name = ""
    return name if name in profile_names() else MAIN_PROFILE


def use_profile(name: str) -> str:
    """Switch profile, and remember it for next time."""
    if name not in profile_names():
        raise ValueError(f"there is no profile called {name!r}")
    _active_file().write_text(name, encoding="utf-8")
    return name


def profile_data_dir(name: str | None = None) -> Path:
    """The main profile keeps data/ as it is; the others get a folder each."""
    name = name or active_profile()
    d = BASE_DIR / "data" if name == MAIN_PROFILE \
        else BASE_DIR / "data" / "profiles" / slug(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "photos").mkdir(exist_ok=True)
    (d / "thumbs").mkdir(exist_ok=True)
    return d


def save_profile(name: str, settings: dict) -> dict:
    """Create or update one profile in config.json, leaving the rest alone."""
    raw = _raw()
    if name == MAIN_PROFILE:
        for key, block in (settings or {}).items():
            raw[key] = _merge(raw.get(key, {}), block) if isinstance(block, dict) else block
    else:
        profiles = raw.setdefault("profiles", {})
        profiles[name] = _merge(profiles.get(name, {}), settings or {})
    CONFIG_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    return raw


def delete_profile(name: str) -> None:
    if name == MAIN_PROFILE:
        raise ValueError("the main profile cannot be removed")
    raw = _raw()
    (raw.get("profiles") or {}).pop(name, None)
    CONFIG_PATH.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    if active_profile() == name:
        _active_file().write_text(MAIN_PROFILE, encoding="utf-8")


def load(profile: str | None = None) -> dict:
    raw = _raw()
    name = profile or active_profile()
    root = {k: v for k, v in raw.items() if k != "profiles"}
    cfg = _merge(DEFAULTS, root)
    if name != MAIN_PROFILE:
        cfg = _merge(cfg, (raw.get("profiles") or {}).get(name, {}))
        # Its own map layer, unless the profile names one itself.
        given = ((raw.get("profiles") or {}).get(name, {}).get("export") or {})
        for key, stem in (("kml_filename", ".kml"), ("csv_filename", ".csv")):
            if not given.get(key):
                cfg["export"][key] = f"NNC_Reports_{slug(name)}{stem}"
        if not given.get("photo_folder"):
            cfg["export"]["photo_folder"] = f"NNC_Reports_{slug(name)}_photos"
    cfg["profile"] = name
    cfg["profiles"] = profile_names()

    # Keys held outside this folder win, so the folder can be synced or shared
    # without the keys going with it.
    external = Path.home() / ".nnc-reporter" / "keys.json"
    if external.exists():
        try:
            for k, v in (json.loads(external.read_text(encoding="utf-8")) or {}).items():
                if not v:
                    continue
                if k == "highways_password":
                    cfg["highways_login"]["password"] = v
                elif k == "highways_email":
                    cfg["highways_login"]["email"] = v
                else:
                    cfg["keys"][k] = v
            cfg["keys"]["_source"] = str(external)
        except json.JSONDecodeError:
            pass
    for var, k in (("ANTHROPIC_API_KEY", "anthropic"),
                   ("WHAT3WORDS_API_KEY", "what3words"),
                   ("MAPBOX_ACCESS_TOKEN", "mapbox")):
        if os.environ.get(var):
            cfg["keys"][k] = os.environ[var]
    if os.environ.get("NNC_HIGHWAYS_PASSWORD"):
        cfg["highways_login"]["password"] = os.environ["NNC_HIGHWAYS_PASSWORD"]

    # Strip unreplaced placeholder keys so features degrade instead of erroring.
    for k, v in list(cfg["keys"].items()):
        if isinstance(v, str) and any(p in v.upper() for p in _PLACEHOLDERS):
            cfg["keys"][k] = ""
    return cfg


def save(cfg: dict) -> None:
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    CONFIG_PATH.write_text(json.dumps(clean, indent=2), encoding="utf-8")


def names(reporter: dict) -> tuple[str, str]:
    """First and last name, split from full_name unless given explicitly."""
    first = (reporter.get("first_name") or "").strip()
    last = (reporter.get("last_name") or "").strip()
    if first and last:
        return first, last
    parts = (reporter.get("full_name") or "").split()
    if not parts:
        return first, last
    return first or parts[0], last or (" ".join(parts[1:]) if len(parts) > 1 else "")


def data_dir() -> Path:
    """This profile's data: its database, its photos. Follows the active profile,
    so the reports of one person are never mixed with another's."""
    return profile_data_dir()


def map_export_dir(cfg: dict) -> Path:
    p = Path(cfg["export"].get("map_folder") or "..")
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p
