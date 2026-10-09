"""Turn coordinates into human location: street/postcode (OSM) and What3Words."""
import json
import math
import time
from pathlib import Path

import requests

from . import config

_CACHE_FILE = config.BASE_DIR / "data" / "geo_cache.json"
_cache: dict | None = None
_last_nominatim = 0.0


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(json.dumps(_load_cache(), indent=0), encoding="utf-8")
    except OSError:
        pass


def _key(prefix: str, lat: float, lon: float) -> str:
    return f"{prefix}:{lat:.6f},{lon:.6f}"


def reverse_geocode(lat: float, lon: float, cfg: dict) -> dict:
    """Nearest street, locality and postcode via OpenStreetMap Nominatim."""
    ck = _key("nom", lat, lon)
    cache = _load_cache()
    if ck in cache:
        return cache[ck]

    global _last_nominatim
    wait = 1.1 - (time.time() - _last_nominatim)  # Nominatim: max 1 req/sec
    if wait > 0:
        time.sleep(wait)

    out = {"street": None, "locality": None, "postcode": None, "address": None,
           "error": None}
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 18,
                    "addressdetails": 1,
                    "email": cfg.get("geocode", {}).get("nominatim_email") or ""},
            headers={"User-Agent": "nnc-reporter/1.0 (council issue reporting)"},
            timeout=20,
        )
        _last_nominatim = time.time()
        r.raise_for_status()
        d = r.json()
        a = d.get("address", {})
        out["street"] = a.get("road") or a.get("pedestrian") or a.get("footway") \
            or a.get("path") or a.get("cycleway")
        out["locality"] = (a.get("village") or a.get("town") or a.get("suburb")
                           or a.get("hamlet") or a.get("city"))
        out["postcode"] = a.get("postcode")
        out["address"] = d.get("display_name")
    except Exception as e:
        out["error"] = str(e)
        return out  # don't cache failures

    cache[ck] = out
    _save_cache()
    return out


def what3words(lat: float, lon: float, cfg: dict) -> dict:
    key = (cfg.get("keys") or {}).get("what3words") or ""
    if not key:
        return {"words": None, "error": "no What3Words API key in config.json"}

    ck = _key("w3w", lat, lon)
    cache = _load_cache()
    if ck in cache:
        return cache[ck]

    out = {"words": None, "nearest_place": None, "error": None}
    try:
        r = requests.get(
            "https://api.what3words.com/v3/convert-to-3wa",
            params={"coordinates": f"{lat},{lon}", "key": key, "language": "en"},
            timeout=20,
        )
        d = r.json()
        if r.status_code != 200 or "error" in d:
            err = d.get("error", {})
            out["error"] = err.get("message") or f"HTTP {r.status_code}"
            return out
        out["words"] = d.get("words")
        out["nearest_place"] = d.get("nearestPlace")
    except Exception as e:
        out["error"] = str(e)
        return out

    cache[ck] = out
    _save_cache()
    return out


def forward_geocode(query: str, cfg: dict, limit: int = 5) -> dict:
    """Turn a typed address or postcode into candidate coordinates.

    Used by the manual "new report without a photo" path - a noise or smell has
    no EXIF. Results are biased to North Northamptonshire with a viewbox, and
    cached like everything else Nominatim gives us.
    """
    q = " ".join((query or "").split())
    if not q:
        return {"results": [], "error": "nothing to search for"}
    ck = f"fwd:{q.lower()}"
    cache = _load_cache()
    if ck in cache:
        return cache[ck]

    global _last_nominatim
    wait = 1.1 - (time.time() - _last_nominatim)  # Nominatim: max 1 req/sec
    if wait > 0:
        time.sleep(wait)

    out = {"results": [], "error": None}
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": q, "format": "jsonv2", "limit": limit,
                    "countrycodes": "gb", "addressdetails": 1,
                    # Roughly North Northamptonshire: lon lat lon lat
                    "viewbox": "-0.95,52.68,-0.35,52.25", "bounded": 0,
                    "email": cfg.get("geocode", {}).get("nominatim_email") or ""},
            headers={"User-Agent": "nnc-reporter/1.0 (council issue reporting)"},
            timeout=20,
        )
        _last_nominatim = time.time()
        r.raise_for_status()
        for d in r.json() or []:
            a = d.get("address", {})
            out["results"].append({
                "lat": float(d["lat"]), "lon": float(d["lon"]),
                "label": d.get("display_name"),
                "street": a.get("road") or a.get("pedestrian"),
                "locality": (a.get("village") or a.get("town") or a.get("suburb")
                             or a.get("hamlet") or a.get("city")),
                "postcode": a.get("postcode"),
            })
    except Exception as e:
        out["error"] = str(e)
        return out  # don't cache failures

    cache[ck] = out
    _save_cache()
    return out


PARK_KINDS = "park|garden|recreation_ground|playground|nature_reserve|common|pitch"


def nearby_park(lat: float, lon: float, cfg: dict) -> dict:
    """Is this spot in (or right beside) a park? The bins form asks.

    Uses OpenStreetMap's Overpass API. The radius is deliberately tight - a bin on
    the pavement outside a park is not "in a park" - and is set by
    `bins.park_radius_metres` in config.json.
    """
    radius = int((cfg.get("bins") or {}).get("park_radius_metres", 5))
    ck = _key(f"park{radius}", lat, lon)
    cache = _load_cache()
    if ck in cache:
        return cache[ck]

    out = {"in_park": False, "name": None, "radius": radius, "error": None}
    query = (f'[out:json][timeout:20];'
             f'nwr(around:{radius},{lat:.6f},{lon:.6f})'
             f'["leisure"~"{PARK_KINDS}"];out tags 1;')
    try:
        r = requests.post("https://overpass-api.de/api/interpreter",
                          data={"data": query}, timeout=25,
                          headers={"User-Agent": "nnc-reporter/1.0"})
        r.raise_for_status()
        els = (r.json() or {}).get("elements") or []
        if els:
            tags = els[0].get("tags") or {}
            out["in_park"] = True
            out["name"] = tags.get("name") or tags.get("leisure")
    except Exception as e:
        out["error"] = str(e)
        return out                      # don't cache a failure

    cache[ck] = out
    _save_cache()
    return out


def nearest_street(lat: float, lon: float, cfg: dict, radius: int = 80) -> dict:
    """The nearest named road, for spots that have no street of their own.

    A bin on a footpath, in a park or on a green gets no road name back from the
    address lookup, and the council's forms all ask for the nearest street.
    """
    ck = _key(f"road{radius}", lat, lon)
    cache = _load_cache()
    if ck in cache:
        return cache[ck]

    out = {"street": None, "radius": radius, "error": None}
    query = (f'[out:json][timeout:20];'
             f'way(around:{radius},{lat:.6f},{lon:.6f})'
             f'["highway"]["name"]'
             f'["highway"!~"footway|path|cycleway|steps|track|service"];'
             f'out tags 1;')
    try:
        r = requests.post("https://overpass-api.de/api/interpreter",
                          data={"data": query}, timeout=25,
                          headers={"User-Agent": "nnc-reporter/1.0"})
        r.raise_for_status()
        els = (r.json() or {}).get("elements") or []
        if els:
            out["street"] = (els[0].get("tags") or {}).get("name")
    except Exception as e:
        out["error"] = str(e)
        return out                       # don't cache a failure

    cache[ck] = out
    _save_cache()
    return out


def describe(lat: float, lon: float, cfg: dict) -> dict:
    """Everything we know about a point, for display and for the report text."""
    rg = reverse_geocode(lat, lon, cfg)
    w3w = what3words(lat, lon, cfg)
    near = ""
    if not rg.get("street"):
        found = nearest_street(lat, lon, cfg)
        if found.get("street"):
            rg["street"] = found["street"]
            near = found["street"]
    bits = [b for b in (rg.get("street"), rg.get("locality"), rg.get("postcode")) if b]
    return {
        "street": rg.get("street"),
        "locality": rg.get("locality"),
        "postcode": rg.get("postcode"),
        "address": rg.get("address"),
        "w3w": w3w.get("words"),
        "where": ", ".join(bits) if bits else f"{lat:.5f}, {lon:.5f}",
        "street_is_nearest": near,          # set when the point had no street itself
        "geo_error": rg.get("error"),
        "w3w_error": w3w.get("error"),
    }


def metres_between(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))
