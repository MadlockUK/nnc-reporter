"""Boundaries for North Northamptonshire: the council, its wards, the town
councils and the town councils' own wards.

Four layers, kept as GeoJSON in <data>/boundaries/:

  lad           North Northamptonshire Council itself (E06000061)
  wards         the unitary council's 31 wards (in force since May 2025)
  parishes      every parish in the area, flagged where it is a town council
  parish_wards  the town councils' wards

The first three come from the ONS Open Geography Portal (OGL v3) - an admin
presses "Fetch boundaries" (or runs `python -m nncr.web.manage fetch-boundaries`)
on a server with internet access. Until then the ward file bundled with the app
is used and the council boundary is worked out from it.

Town council wards are not published nationally. They are derived here by
cutting each town council along the unitary ward lines - which is how most of
them were drawn by the North Northamptonshire (Electoral Changes) Order 2024 -
and an admin can upload the official boundaries (GeoJSON, WGS84) for any town
whose wards are drawn differently. Those that differ from the unitary wards are
what the map shows; all of them are used to say which town council ward a report
falls in.
"""
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

from .. import config
from . import db

log = logging.getLogger("nnc.boundaries")

LAD_CODE = "E06000061"
LAD_NAME = "North Northamptonshire"
ENVELOPE = (-1.10, 52.15, -0.25, 52.75)            # lon/lat box around the council
BUNDLED_WARDS = Path(__file__).resolve().parent.parent / "static" / "wards_nnc.geojson"
ATTRIBUTION = ("Boundaries: Office for National Statistics, Open Government Licence "
               "v3.0. Contains OS data © Crown copyright and database right 2026.")

_ONS = "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
SOURCES = {
    "lad": os.environ.get("NNC_ONS_LAD_URL")
    or _ONS + "LAD_MAY_2025_UK_BFC_V2/FeatureServer/0/query",
    "wards": os.environ.get("NNC_ONS_WARDS_URL")
    or _ONS + "WD_MAY_2025_UK_BFC_V2/FeatureServer/0/query",
    "parishes": os.environ.get("NNC_ONS_PARISHES_URL")
    or _ONS + "PARNCP_DEC_2025_EW_BFC/FeatureServer/0/query",
}

# Which parishes are town councils. Editable by an admin (Boundaries panel),
# because a community governance review can change it.
DEFAULT_TOWN_COUNCILS = [
    "Burton Latimer", "Corby", "Desborough", "Finedon", "Higham Ferrers",
    "Irthlingborough", "Kettering", "Oundle", "Raunds", "Rothwell", "Rushden",
    "Thrapston", "Wellingborough",
]

# Where a derived town council ward is better known by another name than the
# unitary ward it follows: (parish, unitary ward) -> parish ward.
WARD_NAMES = {
    ("corby", "lloyds & corby village"): "Lloyds",
}

LAYERS = ("lad", "wards", "parishes", "parish_wards")
_lock = threading.RLock()
_cache: dict = {}
_status = {"running": False, "message": "", "started": 0, "finished": 0}


def folder() -> Path:
    d = config.shared_dir() / "boundaries"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _norm(s: str) -> str:
    s = (s or "").lower().replace("&", "and")
    s = re.sub(r"\b(town|ccp|cp|parish)\b", " ", s)
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", s).split())


def town_councils() -> list:
    return db.get_setting("town_councils", DEFAULT_TOWN_COUNCILS)


def is_town_council(name: str) -> bool:
    want = {_norm(n) for n in town_councils()}
    return _norm(name) in want


def _empty() -> dict:
    return {"type": "FeatureCollection", "features": []}


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()


def _write(name: str, fc: dict) -> None:
    path = folder() / f"{name}.geojson"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(fc, separators=(",", ":")), encoding="utf-8")
    tmp.replace(path)


def meta() -> dict:
    return _read(folder() / "meta.json") if (folder() / "meta.json").exists() else {}


def _set_meta(layer: str, **info) -> None:
    m = meta()
    m[layer] = {**m.get(layer, {}), **info, "updated": time.time()}
    (folder() / "meta.json").write_text(json.dumps(m, indent=1), encoding="utf-8")


def _version() -> tuple:
    out = []
    for n in LAYERS + ("meta",):
        p = folder() / (f"{n}.geojson" if n != "meta" else "meta.json")
        try:
            out.append(p.stat().st_mtime)
        except OSError:
            out.append(0)
    return tuple(out) + (tuple(town_councils()),)


# ------------------------------------------------------------------ layers

def _bundled_wards() -> dict:
    fc = _read(BUNDLED_WARDS)
    for f in fc.get("features", []):
        f["properties"] = {"code": f["properties"].get("code"),
                           "name": f["properties"].get("name"), "kind": "ward",
                           "detail": f["properties"].get("detail", "")}
    return fc


def _load_raw() -> dict:
    """Every layer as stored, with fallbacks for what has not been fetched yet."""
    out = {n: _read(folder() / f"{n}.geojson") for n in LAYERS}
    if not out["wards"].get("features"):
        out["wards"] = _bundled_wards()
        out["_wards_bundled"] = True
    if not out["lad"].get("features"):
        out["lad"] = _lad_from_wards(out["wards"])
        out["_lad_derived"] = True
    # Town council status is a setting, so apply it on every load.
    for f in out["parishes"].get("features", []):
        f["properties"]["town_council"] = is_town_council(f["properties"].get("name"))
    return out


def layers() -> dict:
    """All layers plus prepared shapes for point lookups. Cached until a file or
    the town council list changes."""
    with _lock:
        v = _version()
        if _cache.get("version") == v:
            return _cache["data"]
        raw = _load_raw()
        raw["_index"] = _build_index(raw)
        _cache.update(version=v, data=raw)
        return raw


def version():
    """Changes whenever a boundary file or the town council list changes."""
    layers()
    return _cache.get("version")


def _lad_from_wards(wards: dict) -> dict:
    try:
        from shapely.geometry import mapping, shape
        from shapely.ops import unary_union
        geoms = [shape(f["geometry"]).buffer(0) for f in wards.get("features", [])]
        if not geoms:
            return _empty()
        u = unary_union(geoms)
        return {"type": "FeatureCollection", "features": [{
            "type": "Feature", "geometry": mapping(u),
            "properties": {"code": LAD_CODE, "name": LAD_NAME, "kind": "lad",
                           "derived": True}}]}
    except Exception as e:                                       # noqa: BLE001
        log.warning("could not work out the council boundary from the wards: %s", e)
        return _empty()


def _build_index(raw: dict) -> dict:
    from shapely.geometry import shape
    from shapely.prepared import prep
    idx = {}
    for name in ("wards", "parishes", "parish_wards"):
        items = []
        for f in raw[name].get("features", []):
            try:
                g = shape(f["geometry"])
                if not g.is_valid:
                    g = g.buffer(0)
                items.append((g.bounds, prep(g), f["properties"]))
            except Exception:                                    # noqa: BLE001
                continue
        idx[name] = items
    return idx


def _hit(items: list, lon: float, lat: float) -> dict | None:
    from shapely.geometry import Point
    pt = None
    for (x0, y0, x1, y1), g, props in items:
        if x0 <= lon <= x1 and y0 <= lat <= y1:
            pt = pt or Point(lon, lat)
            if g.contains(pt) or g.intersects(pt):
                return props
    return None


def locate(lat: float, lon: float) -> dict:
    """Which ward, parish / town council and town council ward a point is in."""
    if lat is None or lon is None:
        return {}
    idx = layers()["_index"]
    w = _hit(idx["wards"], lon, lat) or {}
    p = _hit(idx["parishes"], lon, lat) or {}
    pw = _hit(idx["parish_wards"], lon, lat) or {}
    return {"ward": w.get("name") or "", "ward_code": w.get("code") or "",
            "parish": p.get("name") or "", "parish_code": p.get("code") or "",
            "town_council": bool(p.get("town_council")),
            "parish_ward": pw.get("name") or ""}


# ------------------------------------------------------------- for the map

def _simplified(fc: dict, prefix: str, tolerance: float = 0.00004) -> dict:
    """Lighter copies for drawing - about 4m of tolerance, invisible on a map -
    each with an id the map can use to find the full shape again."""
    from shapely.geometry import mapping, shape
    feats = []
    for i, f in enumerate(fc.get("features", [])):
        props = {**f["properties"], "fid": f"{prefix}:{i}"}
        try:
            g = shape(f["geometry"]).simplify(tolerance, preserve_topology=True)
            geom = json.loads(json.dumps(mapping(g)), parse_float=lambda x: round(float(x), 6))
        except Exception:                                        # noqa: BLE001
            geom = f["geometry"]
        feats.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": feats}


def labels(name: str) -> dict:
    """One label point per shape, for the map's name labels."""
    from .. import webmap
    return webmap.ward_labels(display(name))


def display(name: str) -> dict:
    """One layer, simplified, ready for the browser. Cached per version."""
    with _lock:
        raw = layers()
        key = ("display", name)
        if _cache.get(key, (None,))[0] == _cache.get("version"):
            return _cache[key][1]
        if name == "town_councils":
            fc = {"type": "FeatureCollection", "features": [
                f for f in raw["parishes"].get("features", [])
                if f["properties"].get("town_council")]}
        elif name == "other_parishes":
            fc = {"type": "FeatureCollection", "features": [
                f for f in raw["parishes"].get("features", [])
                if not f["properties"].get("town_council")]}
        elif name == "parish_wards_differ":
            fc = {"type": "FeatureCollection", "features": [
                f for f in raw["parish_wards"].get("features", [])
                if f["properties"].get("differs")]}
        elif name in LAYERS:
            fc = raw[name]
        else:
            return _empty()
        out = _simplified(fc, name)
        _cache[key] = (_cache.get("version"), out)
        return out


def map_layers() -> list:
    """The boundary layers the maps draw, in drawing order, without geometry
    (the page fetches each from /api/boundaries/<id>)."""
    raw = layers()
    n = {k: len(raw[k].get("features", [])) for k in LAYERS}
    towns = sum(1 for f in raw["parishes"].get("features", [])
                if f["properties"].get("town_council"))
    differ = sum(1 for f in raw["parish_wards"].get("features", [])
                 if f["properties"].get("differs"))
    return [
        {"id": "lad", "label": "North Northamptonshire Council", "colour": "#1b1f24",
         "width": 3, "dash": None, "labels": False, "visible": True, "count": n["lad"],
         "note": "worked out from the wards" if raw.get("_lad_derived") else ""},
        {"id": "town_councils", "label": "Town councils", "colour": "#C2410C",
         "width": 2.2, "dash": None, "labels": True, "visible": True, "count": towns,
         "note": "" if n["parishes"] else "not loaded yet - an admin can fetch them"},
        {"id": "parish_wards_differ", "label": "Town council wards (where they differ)",
         "colour": "#7C3AED", "width": 1.6, "dash": [1, 1.5], "labels": True,
         "visible": True, "count": differ,
         "note": "" if differ else "none differ from the unitary wards yet - an admin "
                                    "can upload official town council wards"},
        {"id": "parish_wards", "label": "All town council wards", "colour": "#7C3AED",
         "width": 1.2, "dash": [2, 2], "labels": True, "visible": False,
         "count": n["parish_wards"], "note": ""},
        {"id": "other_parishes", "label": "Parish councils", "colour": "#15803D",
         "width": 1.2, "dash": [3, 2], "labels": True, "visible": False,
         "count": n["parishes"] - towns, "note": ""},
    ]


def wards_for_map() -> dict:
    return display("wards")


def status() -> dict:
    raw = layers()
    m = meta()
    return {
        "layers": {k: {"count": len(raw[k].get("features", [])), **m.get(k, {})}
                   for k in LAYERS},
        "wards_bundled": bool(raw.get("_wards_bundled")),
        "lad_derived": bool(raw.get("_lad_derived")),
        "town_councils": town_councils(),
        "parishes": sorted(f["properties"].get("name") or ""
                           for f in raw["parishes"].get("features", [])),
        "fetch": dict(_status),
        "sources": SOURCES,
    }


# ------------------------------------------------------------- fetching

def _props(p: dict, kind: str) -> dict:
    code = name = ""
    for k, v in p.items():
        ku = k.upper()
        if not code and re.fullmatch(r"(WD|LAD|PAR|PARNCP)\d{2}CD", ku):
            code = v
        if not name and re.fullmatch(r"(WD|LAD|PAR|PARNCP)\d{2}NM", ku):
            name = v
    lad = next((v for k, v in p.items() if re.fullmatch(r"LAD\d{2}CD", k.upper())), "")
    return {"code": code or "", "name": name or "", "kind": kind, "lad": lad or ""}


def _arcgis(url: str, timeout: int = 90) -> list:
    """Every feature intersecting the council's box, paged."""
    import requests
    feats, offset = [], 0
    envelope = {"xmin": ENVELOPE[0], "ymin": ENVELOPE[1], "xmax": ENVELOPE[2],
                "ymax": ENVELOPE[3], "spatialReference": {"wkid": 4326}}
    while True:
        r = requests.get(url, timeout=timeout, params={
            "where": "1=1", "outFields": "*", "outSR": 4326, "f": "geojson",
            "geometry": json.dumps(envelope), "geometryType": "esriGeometryEnvelope",
            "inSR": 4326, "spatialRel": "esriSpatialRelIntersects",
            "geometryPrecision": 6, "resultOffset": offset, "resultRecordCount": 500})
        r.raise_for_status()
        d = r.json()
        if d.get("error"):
            raise RuntimeError(d["error"].get("message") or str(d["error"]))
        page = d.get("features") or []
        feats += page
        more = d.get("exceededTransferLimit") or (d.get("properties") or {}).get(
            "exceededTransferLimit")
        if not page or not more:
            return feats
        offset += len(page)


def _inside(feature: dict, lad_shape) -> bool:
    """Is most of this feature inside the council? (Used when the source has no
    council code to filter on.)"""
    from shapely.geometry import shape
    try:
        g = shape(feature["geometry"])
        if g.is_empty:
            return False
        return lad_shape.contains(g.representative_point())
    except Exception:                                            # noqa: BLE001
        return False


def fetch_from_ons() -> dict:
    """Download the council, ward and parish boundaries. Returns a summary."""
    from shapely.geometry import shape
    from shapely.ops import unary_union
    summary = {}

    wards = []
    for f in _arcgis(SOURCES["wards"]):
        p = _props(f.get("properties") or {}, "ward")
        if p["lad"] == LAD_CODE:
            wards.append({"type": "Feature", "geometry": f["geometry"], "properties": p})
    if not wards:
        raise RuntimeError("the ward service returned no North Northamptonshire wards")
    _write("wards", {"type": "FeatureCollection", "features": wards})
    _set_meta("wards", source="ONS " + SOURCES["wards"].split("/services/")[1].split("/")[0],
              count=len(wards))
    summary["wards"] = len(wards)

    lad = []
    try:
        for f in _arcgis(SOURCES["lad"]):
            p = _props(f.get("properties") or {}, "lad")
            if p["code"] == LAD_CODE:
                lad.append({"type": "Feature", "geometry": f["geometry"], "properties": p})
    except Exception as e:                                       # noqa: BLE001
        log.warning("council boundary service failed (%s) - using the wards", e)
    if not lad:
        lad = _lad_from_wards({"features": wards})["features"]
    _write("lad", {"type": "FeatureCollection", "features": lad})
    _set_meta("lad", source="ONS" if not lad[0]["properties"].get("derived")
              else "worked out from the wards", count=len(lad))
    summary["lad"] = len(lad)

    lad_shape = unary_union([shape(f["geometry"]).buffer(0) for f in lad])
    parishes = []
    for f in _arcgis(SOURCES["parishes"]):
        p = _props(f.get("properties") or {}, "parish")
        if (p["lad"] == LAD_CODE) if p["lad"] else _inside(f, lad_shape):
            parishes.append({"type": "Feature", "geometry": f["geometry"], "properties": p})
    _write("parishes", {"type": "FeatureCollection", "features": parishes})
    _set_meta("parishes", source="ONS " + SOURCES["parishes"].split("/services/")[1]
              .split("/")[0], count=len(parishes))
    summary["parishes"] = len(parishes)

    summary["parish_wards"] = derive_parish_wards()
    return summary


def derive_parish_wards() -> int:
    """Cut each town council along the unitary ward lines, keeping any official
    town council wards that were uploaded (those win for their town)."""
    from shapely.geometry import mapping, shape
    raw = _load_raw()
    current = raw["parish_wards"].get("features", [])
    uploaded = [f for f in current if f["properties"].get("source") == "upload"]
    covered = {_norm(f["properties"].get("parish")) for f in uploaded}

    wards = [(shape(f["geometry"]).buffer(0), f["properties"])
             for f in raw["wards"].get("features", [])]
    out = list(uploaded)
    for pf in raw["parishes"].get("features", []):
        pp = pf["properties"]
        if not is_town_council(pp.get("name")) or _norm(pp.get("name")) in covered:
            continue
        pg = shape(pf["geometry"]).buffer(0)
        pieces = []
        for wg, wp in wards:
            if not pg.intersects(wg):
                continue
            piece = pg.intersection(wg)
            if piece.is_empty or piece.area < 0.01 * pg.area:
                continue
            pieces.append((piece, wp))
        if len(pieces) < 2:
            continue                          # one ward - the town is not divided
        for piece, wp in pieces:
            name = WARD_NAMES.get((_norm(pp["name"]), (wp.get("name") or "").lower()),
                                  wp.get("name"))
            out.append({"type": "Feature", "geometry": mapping(piece),
                        "properties": {"name": name, "parish": pp["name"],
                                       "parish_code": pp.get("code"),
                                       "ward_code": wp.get("code"), "kind": "parish_ward",
                                       "source": "derived", "differs": False}})
    _write("parish_wards", {"type": "FeatureCollection", "features": out})
    _set_meta("parish_wards", source="derived from the unitary wards"
              + (" and uploaded official boundaries" if uploaded else ""),
              count=len(out))
    return len(out)


def start_fetch() -> dict:
    with _lock:
        if _status["running"]:
            return dict(_status)
        _status.update(running=True, message="fetching from ONS…", started=time.time())

    def work():
        try:
            got = fetch_from_ons()
            _status["message"] = ("fetched " + ", ".join(f"{v} {k.replace('_', ' ')}"
                                                         for k, v in got.items()))
        except Exception as e:                                   # noqa: BLE001
            log.exception("boundary fetch failed")
            _status["message"] = f"failed: {e}"
        finally:
            _status.update(running=False, finished=time.time())

    threading.Thread(target=work, daemon=True).start()
    return dict(_status)


# ------------------------------------------------------------- uploads

def _first(p: dict, *patterns) -> str:
    for pat in patterns:
        for k, v in p.items():
            if re.fullmatch(pat, k, re.I) and v not in (None, ""):
                return str(v)
    return ""


def upload(layer: str, data: bytes) -> dict:
    """Replace a layer (or, for town council wards, the wards of the towns in the
    file) with an uploaded GeoJSON FeatureCollection in WGS84 lon/lat."""
    from shapely.geometry import mapping, shape
    if layer not in ("lad", "wards", "parishes", "parish_wards"):
        raise ValueError("unknown layer")
    if len(data) > 40 * 1024 * 1024:
        raise ValueError("that file is over 40MB")
    try:
        fc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f"not a GeoJSON file: {e}") from e
    feats = fc.get("features") if isinstance(fc, dict) else None
    if not feats:
        raise ValueError("the file has no features - it must be a GeoJSON FeatureCollection")

    clean = []
    for f in feats:
        g = (f or {}).get("geometry") or {}
        if g.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        try:
            shp = shape(g)
        except Exception:                                        # noqa: BLE001
            continue
        x0, y0, x1, y1 = shp.bounds
        if not (ENVELOPE[0] - 1 <= x0 <= ENVELOPE[2] + 1 and ENVELOPE[1] - 1 <= y1 <= ENVELOPE[3] + 1):
            raise ValueError("the coordinates are not longitude/latitude around North "
                             "Northamptonshire - export the file as GeoJSON in WGS84 "
                             "(EPSG:4326)")
        p = f.get("properties") or {}
        name = _first(p, r"name", r".*NM", r"ward.*", r"label", r"title")
        if not name:
            raise ValueError("every shape needs a name property")
        props = {"name": name, "code": _first(p, r"code", r".*CD", r"id"),
                 "kind": layer.rstrip("s").replace("parishe", "parish")}
        if layer == "parish_wards":
            props.update(parish=_first(p, r"parish", r"town", r"council", r"PAR.*NM"),
                         source="upload", kind="parish_ward")
        clean.append({"type": "Feature", "geometry": mapping(shp.buffer(0)
                                                             if not shp.is_valid else shp),
                      "properties": props})
    if not clean:
        raise ValueError("no polygons found in the file")

    if layer != "parish_wards":
        _write(layer, {"type": "FeatureCollection", "features": clean})
        _set_meta(layer, source="uploaded", count=len(clean))
        if layer in ("wards", "parishes"):
            derive_parish_wards()
        return {"layer": layer, "count": len(clean)}

    # Town council wards: fill in the town from the parish layer where the file
    # does not say, then work out which of them differ from the unitary wards.
    raw = _load_raw()
    parishes = [(shape(f["geometry"]).buffer(0), f["properties"])
                for f in raw["parishes"].get("features", [])]
    wards = [shape(f["geometry"]).buffer(0) for f in raw["wards"].get("features", [])]
    for f in clean:
        g = shape(f["geometry"])
        town = None
        if not f["properties"]["parish"]:
            pt = g.representative_point()
            for pg, pp in parishes:
                if pg.contains(pt):
                    f["properties"]["parish"] = pp.get("name")
                    f["properties"]["parish_code"] = pp.get("code")
                    town = pg
                    break
        if not f["properties"]["parish"]:
            raise ValueError(f"cannot tell which town '{f['properties']['name']}' belongs "
                             f"to - add a 'parish' property, or fetch the parish "
                             f"boundaries first")
        if town is None:
            town = next((pg for pg, pp in parishes
                         if _norm(pp.get("name")) == _norm(f["properties"]["parish"])), None)
        # It is the same as a unitary ward when it matches that ward's part of the
        # town almost exactly; otherwise it differs, and the map draws it.
        same = False
        for wg in wards:
            if not g.intersects(wg):
                continue
            ref = wg.intersection(town) if town is not None else wg
            inter = g.intersection(ref).area
            if ref.area and inter >= 0.97 * g.area and inter >= 0.97 * ref.area:
                same = True
                break
        f["properties"]["differs"] = not same
    towns = {_norm(f["properties"]["parish"]) for f in clean}
    keep = [f for f in raw["parish_wards"].get("features", [])
            if _norm(f["properties"].get("parish")) not in towns]
    _write("parish_wards", {"type": "FeatureCollection", "features": keep + clean})
    _set_meta("parish_wards", source="derived and uploaded", count=len(keep) + len(clean))
    return {"layer": layer, "count": len(clean),
            "towns": sorted({f["properties"]["parish"] for f in clean}),
            "differ": sum(1 for f in clean if f["properties"]["differs"])}


def set_town_councils(names: list) -> list:
    names = sorted({(n or "").strip() for n in names if (n or "").strip()})
    db.set_setting("town_councils", names)
    try:
        derive_parish_wards()
    except Exception as e:                                       # noqa: BLE001
        log.warning("could not re-derive town council wards: %s", e)
    return names
