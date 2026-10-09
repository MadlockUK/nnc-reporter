"""The real North Northamptonshire reporting categories.

Scraped from https://highways.northnorthants.gov.uk (a FixMyStreet instance).
Categories can change, so `refresh_from_site()` re-reads the live list from the
site's own AJAX endpoint and caches it in data/categories.json. The bundled list
below is the fallback used when offline.
"""
import json
from pathlib import Path

from . import config

CACHE = config.BASE_DIR / "data" / "categories.json"

# (group, category). group == "" means a top-level category with no parent group.
BUNDLED = [
    ("", "Abandoned vehicles"),
    ("", "Car parks"),
    ("", "Dog fouling"),
    ("", "Flyposting"),
    ("", "Flytipping"),
    ("", "Graffiti"),
    ("", "Nuisance Parking (No Restrictions)"),
    ("", "Nuisance Parking (Restrictions Present)"),
    ("", "Other"),
    ("", "Parks/landscapes"),
    ("", "Public toilets"),
    ("", "Rubbish (refuse and recycling)"),
    ("", "Street Cleansing"),
    ("", "Street Lighting"),
    ("", "Street Nameplates"),
    ("Bus Stops", "Electronic Timetable Display Faulty"),
    ("Bus Stops", "Shelter Damaged"),
    ("Bus Stops", "Sign/Pole Damaged"),
    ("Crash Barriers", "Crash Barriers - Damaged / Missing"),
    ("Drain Covers", "Broken / Missing"),
    ("Drain Covers", "Loose / Raised/Sunken"),
    ("Drains", "Blocked/Damaged"),
    ("Drains", "Blocked Ditch"),
    ("Drains", "Blocked Ditch Causing Flooding"),
    ("Drains", "Blocked - flooding private property"),
    ("Drains", "Blocked - flooding road/path"),
    ("Footway/Footpath", "Obstruction (Not Vegetation)"),
    ("Footway/Footpath", "Pothole / Failed Reinstatement"),
    ("Footway/Footpath", "Slabs - Missing"),
    ("Footway/Footpath", "Slabs - Uneven / Damaged / Cracked"),
    ("Highway Bridges", "Highway Bridges - Damaged/Unsafe"),
    ("Kerbs", "Damaged/Loose"),
    ("Kerbs", "Missing"),
    ("Pedestrian Barriers", "Pedestrian Barriers - Damaged / Missing"),
    ("Rights of Way", "Bridge-Damaged/ Missing"),
    ("Rights of Way", "Gate - Damaged/ Missing"),
    ("Rights of Way", "Livestock"),
    ("Rights of Way", "Passage-Obstructed/Overgrown"),
    ("Rights of Way", "Sign/Waymarking - Damaged/Missing"),
    ("Rights of Way", "Stile-Damaged/Missing"),
    ("Road Markings", "Road Markings - Worn/Faded"),
    ("Roads", "Damaged Speed Humps"),
    ("Roads", "Flooding"),
    ("Roads", "Mud on Road"),
    ("Roads", "Potholes / Highway Condition"),
    ("Roads", "Spill - Oil/Diesel"),
    ("Safety Bollard", "Safety Bollard - Damaged/Missing"),
    ("Sign", "Abandoned road signs or cones after works"),
    ("Sign", "Damaged / Missing / Facing Wrong Way"),
    ("Sign", "Obscured by Graffiti"),
    ("Sign", "Obscured by vegetation or Dirty"),
    ("Traffic Signals / Signalled Controlled Crossings", "All Traffic Signals OUT (Not Working)"),
    ("Traffic Signals / Signalled Controlled Crossings", "Damaged/Exposed Wiring / Vandalised"),
    ("Traffic Signals / Signalled Controlled Crossings", "Lamp Failure"),
    ("Traffic Signals / Signalled Controlled Crossings", "Pushbutton Not Working"),
    ("Traffic Signals / Signalled Controlled Crossings", "Request Timing Review"),
    ("Traffic Signals / Signalled Controlled Crossings", "Signal Stuck / Not Changing"),
    ("Traffic Signals / Signalled Controlled Crossings", "Temporary Traffic Lights"),
    ("Vegetation", "Fallen Tree"),
    ("Vegetation", "Overgrown Private Vegetation"),
    ("Vegetation", "Restricted Visibility"),
    ("Vegetation", "Restricted Visibility / Overgrown / Overhanging"),
    ("Vegetation", "Weeds"),
    ("Verges", "Verges - Damaged by Vehicles"),
    ("Winter Maintenance", "Grit Bin - damaged/replacement"),
    ("Winter Maintenance", "Grit Bin - empty/refill"),
    ("Winter Maintenance", "Icy Footpath"),
    ("Winter Maintenance", "Icy Road"),
    ("Winter Maintenance", "Missed published Gritted Route"),
]

# Not highways categories: the council has separate online forms for these, and
# these entries are how a report gets routed there (see nncr/routing.py). They are
# merged into the list wherever it comes from - bundled or refreshed from the live
# site - so a category refresh can never lose them.
SYNTHETIC = [
    ("", "Litter or dog waste bin"),
    ("", "Dead animal"),
    ("", "Accumulation or overgrown garden"),
    ("", "Open or derelict property"),
    ("", "Grass, trees or hedges (grounds)"),
    ("Nuisance", "Noise nuisance"),
    ("Nuisance", "Odour or drain nuisance"),
    ("Nuisance", "Smoke or bonfire"),
    ("Nuisance", "Light nuisance"),
    ("Nuisance", "Dust or vibration"),
]

# Categories that should never be auto-picked from a photo: they need human
# judgement, involve accusing people, are emergencies handled by phone, or
# describe something a photograph cannot show (noise, smells, light at night).
NEVER_AUTO = {
    "Other",
    "Request Timing Review",
    "Missed published Gritted Route",
    "Livestock",
    "Nuisance Parking (No Restrictions)",
    "Nuisance Parking (Restrictions Present)",
    "Abandoned vehicles",
    "Noise nuisance",
    "Odour or drain nuisance",
    "Light nuisance",
    "Dust or vibration",
}

# Emergencies NNC asks you to phone in rather than log online.
EMERGENCY_HINTS = {
    "Fallen Tree",
    "Blocked - flooding road/path",
    "Blocked - flooding private property",
    "Flooding",
    "Highway Bridges - Damaged/Unsafe",
    "All Traffic Signals OUT (Not Working)",
}


def _flatten(pairs):
    return [{"group": g, "name": n, "label": f"{g} > {n}" if g else n} for g, n in pairs]


def _with_synthetic(cats: list) -> list:
    """The synthetic form-routing categories, appended once, whatever the source."""
    have = {(_norm(c.get("group")), _norm(c.get("name"))) for c in cats}
    extra = [c for c in _flatten(SYNTHETIC)
             if (_norm(c["group"]), _norm(c["name"])) not in have]
    return cats + extra


def load() -> list:
    if CACHE.exists():
        try:
            data = json.loads(CACHE.read_text(encoding="utf-8"))
            if data.get("categories"):
                return _with_synthetic(data["categories"])
        except (json.JSONDecodeError, OSError):
            pass
    return _with_synthetic(_flatten(BUNDLED))


def names() -> list:
    return [c["name"] for c in load()]


def _norm(s: str) -> str:
    import re
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def find(name: str):
    """Look up a category, tolerating the ways a name gets written back to us:
    'Broken / Missing', 'Drain Covers > Broken / Missing', and
    'Broken / Missing (group: Drain Covers)'."""
    import re
    if not name:
        return None
    cats = load()
    want = _norm(name)

    # 'Name (group: Group)' / 'Name (in Group)'
    group_hint = None
    m = re.match(r"^(.*?)\s*\((?:group|in)\s*:?\s*(.+?)\)$", want)
    if m:
        want, group_hint = _norm(m.group(1)), _norm(m.group(2))

    for c in cats:
        if _norm(c["name"]) == want and (group_hint is None
                                         or _norm(c["group"]) == group_hint):
            return c
    for c in cats:                                  # full 'Group > Name' label
        if _norm(c["label"]) == want:
            return c
    if ">" in want:                                 # 'Group > Name' by parts
        a, b = (_norm(x) for x in want.split(">", 1))
        for c in cats:
            if _norm(c["group"]) == a and _norm(c["name"]) == b:
                return c
        for c in cats:                              # group dropped or wrong
            if _norm(c["name"]) == b:
                return c
    if group_hint is not None:                      # name wrong, group usable
        for c in cats:
            if _norm(c["name"]) == want:
                return c
    hits = [c for c in cats if want and want in _norm(c["label"])]
    return hits[0] if len(hits) == 1 else None      # only if unambiguous


def refresh_from_site(lat: float = config.DEFAULT_CENTRE[0],
                      lon: float = config.DEFAULT_CENTRE[1]) -> dict:
    """Re-read the live category list from the highways site.

    FixMyStreet exposes the per-location new-report form as JSON, which contains
    the category radio buttons/optgroups. We parse the labels out of that HTML.
    """
    import re
    import requests

    url = "https://highways.northnorthants.gov.uk/report/new/ajax"
    r = requests.get(url, params={"latitude": lat, "longitude": lon},
                     timeout=30, headers={"User-Agent": "nnc-reporter/1.0"})
    r.raise_for_status()
    payload = r.json()
    html = payload.get("category") or payload.get("councils_text") or ""
    if isinstance(html, dict):
        html = json.dumps(html)

    pairs, group = [], ""
    for m in re.finditer(
        r'<optgroup[^>]*label="([^"]+)"|</optgroup>|'
        r'<option[^>]*value="([^"]*)"|'
        r'<input[^>]*name="category"[^>]*value="([^"]*)"',
        html, re.I,
    ):
        if m.group(1):
            group = _unescape(m.group(1))
        elif m.group(0).lower().startswith("</optgroup"):
            group = ""
        else:
            val = _unescape(m.group(2) or m.group(3) or "").strip()
            if val and val.lower() not in ("", "-- pick a category --"):
                pairs.append((group, val))

    seen, uniq = set(), []
    for g, n in pairs:
        if (g, n) not in seen:
            seen.add((g, n))
            uniq.append((g, n))
    if not uniq:
        raise RuntimeError("Could not parse any categories from the live site")

    cats = _flatten(uniq)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"categories": cats}, indent=2), encoding="utf-8")
    return {"count": len(cats), "categories": cats}


def _unescape(s: str) -> str:
    import html as _h
    return _h.unescape(s)
