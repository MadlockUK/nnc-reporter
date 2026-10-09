"""Interactive Mapbox web map - one per profile, from that profile's reports,
merged with the pins brought over from the Google My Map (nncr/mymaps.py).

Two ways in:
  * GET /api/webmap serves the active profile's map live from the app.
  * "Write interactive maps" exports a self-contained HTML file per profile,
    written next to that profile's KML, openable and shareable without the tool.

What the map does beyond showing pins:
  * NNC ward borders (ONS, May 2026 wards - the arrangements in force since the
    May 2025 elections), full detail for the wards in and around Corby.
  * Area statistics: draw an area, pick a ward or use the visible map, set a
    "since" date, and get a count, breakdowns and a reports-over-time chart -
    with a showcase layout and a PNG export for screenshots.

The map needs a Mapbox PUBLIC token (pk...) in config: keys.mapbox, the
%USERPROFILE%\\.nnc-reporter\\keys.json file, or MAPBOX_ACCESS_TOKEN. The token is
embedded in the exported HTML, which is exactly what public tokens are for -
a secret (sk...) token is refused outright rather than leaked into a file.
"""
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config
from .kml import STYLES, _style_for

MAPBOX_GL_VERSION = "3.7.0"
WARDS_FILE = Path(__file__).parent / "static" / "wards_nnc.geojson"
WARDS_ATTRIBUTION = ("Ward boundaries: Office for National Statistics (May 2026), "
                     "Open Government Licence v3.0. Contains OS data "
                     "© Crown copyright and database right 2026.")

# The queue statuses collapse into one bucket, mirroring the app's tabs.
STATUS_BUCKETS = (
    ("queued", ("new", "ready", "awaiting")),
    ("submitted", ("submitted",)),
    ("mapped", ("mapped",)),
    ("completed", ("completed",)),
)

# Pin colours by broad kind: the KML palette, plus the two kinds that only the
# My Map has (non-council issues, and landmarks such as the Autumn Centre).
COLOURS = {k: f"#{v}" for k, v in STYLES.items()}
COLOURS["issue"] = "#AD1457"
COLOURS["place"] = "#00838F"

KIND_LABELS = [
    ("flytipping", "Fly-tipping / waste"),
    ("pothole", "Road / footway"),
    ("drain", "Drainage / flooding"),
    ("vegetation", "Vegetation"),
    ("lighting", "Lighting / signals"),
    ("sign", "Signs / graffiti"),
    ("issue", "Community issue"),
    ("place", "Landmark"),
    ("other", "Other"),
]


def _bucket(status: str) -> str:
    low = (status or "").strip().lower()
    for name, members in STATUS_BUCKETS:
        if low in members:
            return name
    return "queued"


def _pin_bucket(status: str) -> str:
    """My Map pins: open (still an issue), completed (history), landmark."""
    low = (status or "").strip().lower()
    if low in ("completed", "closed", "done", "resolved"):
        return "completed"
    if low in ("landmark", "place", "info"):
        return "landmark"
    if low in ("mapped", "submitted"):
        return low
    return "open"


def _db_rows(db: Path, sql: str) -> list:
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(db, timeout=30)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql).fetchall()]
        con.close()
        return rows
    except sqlite3.Error:
        return []


def reports_for_profile(name: str) -> list:
    """Every report a profile holds, read directly - no switching profiles."""
    return _db_rows(config.profile_data_dir(name) / "reports.db",
                    "SELECT * FROM reports ORDER BY COALESCE(taken_at, created_at)")


def pins_for_profile(name: str) -> list:
    return _db_rows(config.profile_data_dir(name) / "reports.db",
                    "SELECT * FROM map_pins ORDER BY id")


def areas_for_profile(name: str) -> list:
    rows = _db_rows(config.profile_data_dir(name) / "reports.db",
                    "SELECT * FROM map_areas ORDER BY id")
    for r in rows:
        try:
            r["geometry"] = json.loads(r.pop("geojson") or "{}")
        except ValueError:
            r["geometry"] = {}
    return rows


def wards() -> dict:
    if WARDS_FILE.exists():
        try:
            return json.loads(WARDS_FILE.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"type": "FeatureCollection", "features": []}


def _centroid(geometry: dict) -> list | None:
    """Area-weighted centroid of the largest ring - one label point per ward,
    so a name is not repeated in every map tile the ward spans."""
    rings = []
    if geometry.get("type") == "Polygon":
        rings = [geometry["coordinates"][0]]
    elif geometry.get("type") == "MultiPolygon":
        rings = [p[0] for p in geometry["coordinates"]]
    best, best_area = None, 0.0
    for ring in rings:
        a = cx = cy = 0.0
        for i in range(len(ring) - 1):
            x0, y0 = ring[i]
            x1, y1 = ring[i + 1]
            cross = x0 * y1 - x1 * y0
            a += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross
        if abs(a) > best_area and a:
            best_area = abs(a)
            best = [cx / (3 * a), cy / (3 * a)]
    return best


def ward_labels(ward_fc: dict) -> dict:
    feats = []
    for f in ward_fc.get("features") or []:
        c = _centroid(f.get("geometry") or {})
        if c:
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": c},
                          "properties": {"name": f["properties"].get("name", "")}})
    return {"type": "FeatureCollection", "features": feats}


def _sized(url: str, px: int) -> str:
    """Google's image host takes a size hint on the URL."""
    if not url:
        return ""
    if "fife=" in url:
        return re.sub(r"fife=s\d+", f"fife=s{px}", url)
    if "googleusercontent.com" in url and "=" not in url.rsplit("/", 1)[-1]:
        return f"{url}=s{px}"
    return url


def features(rows: list, live: bool = False, embed: bool = False) -> dict:
    """The reports as a GeoJSON FeatureCollection for Mapbox GL.

    `live` means the map is served by the running app, so the app's own photo
    endpoint can illustrate reports the council never published a photo for.
    An exported file only uses the council's public photo URLs - a file:// page
    cannot reach the app.
    """
    feats = []
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        if (r.get("status") or "") == "skipped":
            continue
        when = (r.get("taken_at") or r.get("created_at") or "")[:16].replace("T", " ")
        photo = r.get("photo_url") or ""
        full = photo
        if not photo and live and r.get("photo_path"):
            photo = f"/api/photo/{r['id']}"
            full = f"/api/photo/{r['id']}?full=1"
        elif not photo and embed and r.get("thumb_path"):
            photo = full = _data_uri(r["thumb_path"])
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [round(float(r["lon"]), 7),
                                         round(float(r["lat"]), 7)]},
            "properties": {
                "id": r.get("id"),
                "src": "report",
                "title": (r.get("title") or r.get("category") or "Report")[:120],
                "category": r.get("category") or "",
                "kind": _style_for(r.get("category")),
                "status": _bucket(r.get("status")),
                "reference": r.get("reference") or "",
                "report_url": r.get("report_url") or "",
                "photo": photo,
                "photo_full": full,
                "street": r.get("street") or "",
                "locality": r.get("locality") or "",
                "postcode": r.get("postcode") or "",
                "w3w": r.get("w3w") or "",
                "when": when,
                "when_est": False,
                "folder": "",
                "detail": (r.get("detail") or "").replace("\r", "")[:400],
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def _data_uri(path: str, limit: int = 200_000) -> str:
    """A small JPEG as a data: URI, for exported maps that have no app behind
    them. Anything over the limit is left out rather than bloating the file."""
    try:
        p = Path(path)
        if p.exists() and p.stat().st_size <= limit:
            import base64
            return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()
    except OSError:
        pass
    return ""


def pin_features(pins: list, live: bool = False, embed: bool = False) -> dict:
    """The My Map pins in the same shape as the reports, so one layer shows both.

    `embed` puts the cached thumbnail into the file itself - Google's image host
    refuses to serve the My Map photos to any other site, so an exported map
    cannot simply link to them."""
    feats = []
    for p in pins:
        if p.get("lat") is None or p.get("lon") is None:
            continue
        photo = full = ""
        if live and p.get("thumb_path"):
            photo = f"/api/pins/{p['id']}/photo"
            full = f"/api/pins/{p['id']}/photo?full=1"
        elif embed and p.get("thumb_path"):
            photo = full = _data_uri(p["thumb_path"])
        if not photo and p.get("photo_url"):
            photo = _sized(p["photo_url"], 640)
            full = _sized(p["photo_url"], 2048)
        title = p.get("name") or p.get("category") or "Pin"
        # A bare reference as the name is not much of a title; say what it was.
        if re.match(r"^[A-Za-z]{0,8}\d{6,}$", title.strip()) and p.get("category"):
            title = f"{p['category']} ({title.strip()})"
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point",
                         "coordinates": [round(float(p["lon"]), 7),
                                         round(float(p["lat"]), 7)]},
            "properties": {
                "id": p.get("id"),
                "src": "pin",
                "title": title[:120],
                "category": p.get("category") or "",
                "kind": p.get("kind") or "other",
                "status": _pin_bucket(p.get("status")),
                "reference": p.get("reference") or "",
                "report_url": p.get("report_url") or "",
                "photo": photo,
                "photo_full": full,
                "street": p.get("street") or "",
                "locality": "",
                "postcode": p.get("postcode") or "",
                "w3w": p.get("w3w") or "",
                "when": (p.get("photographed") or "")[:16],
                "when_est": (p.get("date_source") == "estimated"),
                "folder": p.get("folder") or "",
                "detail": (p.get("detail") or "").replace("\r", "")[:400],
            },
        })
    return {"type": "FeatureCollection", "features": feats}


def merge(*collections: dict) -> dict:
    feats = []
    for fc in collections:
        feats.extend((fc or {}).get("features") or [])
    return {"type": "FeatureCollection", "features": feats}


def token_for(cfg: dict) -> tuple[str, str]:
    """(token, problem). A secret token is never used: it would be written into
    a shareable HTML file, which is exactly what secret tokens must not touch."""
    tok = ((cfg.get("keys") or {}).get("mapbox") or "").strip()
    if not tok:
        return "", ("No Mapbox token set. Add a PUBLIC token (starts pk.) as "
                    "keys.mapbox in config.json - or in keys.json / "
                    "MAPBOX_ACCESS_TOKEN - then reload.")
    if tok.startswith("sk."):
        return "", ("The configured Mapbox token is a SECRET (sk.) token, which "
                    "must never go into a web page or exported file. Make a "
                    "public token (pk.) at account.mapbox.com/access-tokens "
                    "with just the styles and fonts read scopes, and use that.")
    return tok, ""


def _h(s) -> str:
    import html
    return html.escape(str(s or ""))


def build_html(label: str, fc: dict, cfg: dict, live: bool = False,
               areas: list | None = None, ward_fc: dict | None = None,
               mymap_url: str = "") -> str:
    """A single, self-contained HTML page: clustered, colour-coded, filterable,
    with ward borders and an area-statistics panel."""
    token, problem = token_for(cfg)
    lat, lon = config.centre(cfg)
    zoom = int((cfg.get("map") or {}).get("zoom", 13))
    stamp = datetime.now().strftime("%d %b %Y %H:%M")
    feats = fc.get("features") or []
    n_reports = sum(1 for f in feats if f["properties"].get("src") == "report")
    n_pins = len(feats) - n_reports

    match_expr = ["match", ["get", "kind"]]
    for kind, col in COLOURS.items():
        if kind != "other":
            match_expr += [kind, col]
    match_expr.append(COLOURS["other"])

    legend_rows = "".join(
        f'<label class="row kind" data-kind="{k}"><input type="checkbox" checked>'
        f'<span class="dot" style="background:{COLOURS[k]}"></span>{lbl}</label>'
        for k, lbl in KIND_LABELS)

    js = _JS.replace("__MATCH__", json.dumps(match_expr))
    return (_HTML.replace("__JS__", js)
            .replace("__TITLE__", _h(label))
            .replace("__VERSION__", MAPBOX_GL_VERSION)
            .replace("__STAMP__", stamp)
            .replace("__N_REPORTS__", str(n_reports))
            .replace("__N_PINS__", str(n_pins))
            .replace("__LEGEND__", legend_rows)
            .replace("__ATTRIB__", _h(WARDS_ATTRIBUTION))
            .replace("__LIVE_ONLY__", "" if live else "hidden")
            .replace("__DATA_JSON__", json.dumps(fc))
            .replace("__AREAS_JSON__", json.dumps(areas or []))
            .replace("__WARDS_JSON__", json.dumps(ward_fc or {"type": "FeatureCollection", "features": []}))
            .replace("__WARD_LABELS_JSON__", json.dumps(ward_labels(ward_fc or {})))
            .replace("__COLOURS_JSON__", json.dumps(COLOURS))
            .replace("__TOKEN_JSON__", json.dumps(token))
            .replace("__PROBLEM_JSON__", json.dumps(problem))
            .replace("__MYMAP_JSON__", json.dumps(mymap_url or ""))
            .replace("__LIVE_JSON__", "true" if live else "false")
            .replace("__CENTRE__", f"[{lon:.6f}, {lat:.6f}]")
            .replace("__ZOOM__", str(zoom)))


def _map_filename(cfg: dict) -> str:
    stem = Path(cfg["export"].get("kml_filename") or "NNC_Reports.kml").stem
    return f"{stem}_map.html"


def export_all() -> dict:
    """Write one self-contained interactive map per profile, next to its KML."""
    written, empty, problem = [], [], ""
    ward_fc = wards()
    for name in config.profile_names():
        pcfg = config.load(name)
        tok, problem = token_for(pcfg)
        label = (pcfg.get("reporter") or {}).get("full_name") or name
        fc = merge(features(reports_for_profile(name), live=False, embed=True),
                   pin_features(pins_for_profile(name), live=False, embed=True))
        out = config.map_export_dir(pcfg) / _map_filename(pcfg)
        out.write_text(build_html(label, fc, pcfg, live=False,
                                  areas=areas_for_profile(name), ward_fc=ward_fc),
                       encoding="utf-8")
        (written if fc["features"] else empty).append(
            {"profile": name, "file": str(out), "pins": len(fc["features"])})
    return {"written": written, "empty": empty, "token_problem": problem}


# ------------------------------------------------------------------ the page

_HTML = r"""<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NNC Reports — __TITLE__</title>
<link href="https://api.mapbox.com/mapbox-gl-js/v__VERSION__/mapbox-gl.css" rel="stylesheet">
<script src="https://api.mapbox.com/mapbox-gl-js/v__VERSION__/mapbox-gl.js"></script>
<style>
  :root{--ink:#1b1f24;--muted:#5c6672;--line:#e4e7eb;--panel:#fff;--accent:#1d5fa6;
        --sel:#1d5fa6;--bar:#2b6ca8}
  *{box-sizing:border-box}
  body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;color:var(--ink)}
  #map{position:absolute;inset:0}
  .panel{position:absolute;z-index:2;background:var(--panel);border-radius:10px;
         box-shadow:0 1px 8px rgba(0,0,0,.22);padding:12px 14px}
  #head{top:10px;left:10px;max-width:360px}
  #head h1{font-size:15px;margin:0 0 2px}
  #head .sub{color:var(--muted);font-size:12.5px}
  #head .tools{margin-top:8px;display:flex;flex-wrap:wrap;gap:6px}
  button{font:inherit;font-size:12.5px;padding:5px 10px;border:1px solid #cfd5dc;border-radius:7px;
         background:#f7f8fa;cursor:pointer;color:var(--ink)}
  button:hover{background:#eef1f5}
  button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
  button.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
  button.small{padding:3px 8px;font-size:12px}
  #legend{bottom:26px;left:10px;font-size:12.5px;max-height:calc(100vh - 80px);overflow:auto;width:250px}
  #legend h4{margin:0 0 6px;font-size:12.5px}
  .row{display:flex;align-items:center;gap:7px;margin:3px 0;cursor:pointer}
  .row input{margin:0}
  .dot{width:12px;height:12px;border-radius:50%;border:1.5px solid #fff;flex:none;
       box-shadow:0 0 0 1px rgba(0,0,0,.25)}
  .dot.ring{border-color:#1b1f24}
  .sect{border-top:1px solid var(--line);margin-top:8px;padding-top:8px}
  .sect h4{margin:0 0 4px;font-size:12.5px}
  .muted{color:var(--muted)}
  .tiny{font-size:11px;color:var(--muted)}
  #stats{top:10px;right:10px;width:340px;max-height:calc(100vh - 20px);overflow:auto}
  #stats h2{font-size:15px;margin:0 0 6px;display:flex;justify-content:space-between;align-items:center}
  #stats .modes{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
  #stats label.inl{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;margin:2px 10px 2px 0}
  #stats input[type=date]{font:inherit;font-size:12.5px;padding:3px 5px;border:1px solid #cfd5dc;border-radius:6px}
  #stats input[type=text]{font:inherit;width:100%;padding:4px 6px;border:1px solid #cfd5dc;border-radius:6px;margin:4px 0 6px}
  .kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:8px 0}
  .kpi{background:#f4f6f9;border-radius:8px;padding:8px 10px}
  .kpi b{display:block;font-size:24px;line-height:1.1;font-variant-numeric:tabular-nums}
  .kpi span{font-size:11.5px;color:var(--muted)}
  .bars{margin:6px 0}
  .bar{display:grid;grid-template-columns:120px 1fr 34px;align-items:center;gap:8px;font-size:12px;margin:3px 0}
  .bar i{display:block;height:8px;border-radius:4px;background:var(--bar);min-width:2px}
  .bar b{font-weight:600;text-align:right;font-variant-numeric:tabular-nums}
  #chart{width:100%;height:auto;display:block;margin-top:4px}
  #chart rect.b{fill:var(--bar)}
  #chart rect.b:hover{fill:#1d4f80}
  #chart text{font-size:10px;fill:var(--muted)}
  #chart line{stroke:var(--line)}
  #tip{position:absolute;z-index:9;background:#1b1f24;color:#fff;font-size:11.5px;padding:4px 7px;
       border-radius:5px;pointer-events:none;display:none;white-space:nowrap}
  #hint{position:absolute;z-index:3;left:50%;top:12px;transform:translateX(-50%);background:#1b1f24;
        color:#fff;padding:7px 12px;border-radius:8px;font-size:12.5px;display:none;box-shadow:0 1px 8px rgba(0,0,0,.3)}
  .mapboxgl-popup{max-width:320px}
  .mapboxgl-popup-content{font:12.5px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;padding:12px 14px}
  .mapboxgl-popup-content h3{margin:0 0 4px;font-size:13.5px;padding-right:14px}
  .mapboxgl-popup-content img{max-width:100%;max-height:220px;border-radius:6px;margin:6px 0;display:block;cursor:zoom-in}
  .mapboxgl-popup-content .tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:9px;background:#eef1f5;color:#3d4753;margin-right:4px}
  #err{position:absolute;inset:0;z-index:5;display:flex;align-items:center;justify-content:center;background:#f6f7f9}
  #err div{max-width:460px;background:#fff;border:1px solid #dfe3e8;border-radius:10px;padding:22px;font-size:14px;line-height:1.6}
  /* showcase: just the map, the selection and one clean card */
  body.showcase #head, body.showcase #legend, body.showcase .modes, body.showcase .cfg,
  body.showcase .mapboxgl-ctrl-top-right, body.showcase .mapboxgl-ctrl-bottom-left{display:none!important}
  body.showcase #stats{width:380px;top:18px;right:18px;padding:18px 20px;border-radius:14px;box-shadow:0 4px 24px rgba(0,0,0,.25)}
  body.showcase #stats h2{font-size:18px}
  body.showcase .kpi b{font-size:30px}
  body.showcase #showbar{display:flex}
  #showbar{display:none;position:absolute;z-index:4;left:50%;bottom:22px;transform:translateX(-50%);gap:8px}
  #showbar button{box-shadow:0 1px 8px rgba(0,0,0,.25)}
  #stats .foot{font-size:11px;color:var(--muted);margin-top:8px;border-top:1px solid var(--line);padding-top:6px}
  #lightbox{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.85);display:none;align-items:center;justify-content:center;cursor:zoom-out}
  #lightbox img{max-width:96vw;max-height:96vh;border-radius:6px}
</style>
</head>
<body>
<div id="map"></div>
<div id="hint"></div>
<div id="tip"></div>
<div id="lightbox" onclick="this.style.display='none'"><img alt=""></div>
<div class="panel" id="head">
  <h1>NNC Reports — __TITLE__</h1>
  <div class="sub"><span id="n-all">__N_REPORTS__</span> report<span id="n-all-s">s</span> from the tool ·
    <span id="n-pins">__N_PINS__</span> pins from the My Map · generated __STAMP__</div>
  <div class="tools">
    <button class="small __LIVE_ONLY__" onclick="importMyMap()" title="Fetch the Google My Map again and merge any new or changed pins">Refresh My Map pins</button>
    <button class="small" onclick="toggleShowcase()" title="Hide the controls and lay the statistics out for a screenshot">Showcase</button>
    <a id="mymaplink" href="#" target="_blank" hidden><button class="small">Open My Map</button></a>
  </div>
</div>
<div class="panel" id="legend">
  <h4>Type <span class="tiny">(click to show/hide)</span></h4>
  __LEGEND__
  <div class="sect" id="filters">
    <h4>Reports from the tool</h4>
    <label class="row"><input type="checkbox" data-status="queued"> To submit (<span id="n-queued">0</span>)</label>
    <label class="row"><input type="checkbox" data-status="submitted" checked> Submitted (<span id="n-submitted">0</span>)</label>
    <label class="row"><input type="checkbox" data-status="mapped" checked> On the map (<span id="n-mapped">0</span>)</label>
    <label class="row"><input type="checkbox" data-status="completed" checked> Completed (<span id="n-completed">0</span>)</label>
    <h4 style="margin-top:6px">Pins from the My Map <span class="dot ring" style="display:inline-block;vertical-align:-2px;background:#fff"></span></h4>
    <label class="row"><input type="checkbox" data-status="open" checked> Outstanding issues (<span id="n-open">0</span>)</label>
    <label class="row"><input type="checkbox" data-status="pin-completed" checked> Completed history (<span id="n-pin-completed">0</span>)</label>
    <label class="row"><input type="checkbox" data-status="landmark" checked> Landmarks (<span id="n-landmark">0</span>)</label>
  </div>
  <div class="sect" id="layers">
    <h4>Boundaries</h4>
    <label class="row"><input type="checkbox" id="l-wards" checked> NNC ward borders</label>
    <label class="row"><input type="checkbox" id="l-wardnames" checked> Ward names</label>
    <label class="row"><input type="checkbox" id="l-areas" checked> Drawn areas (Exeter Estate)</label>
    <label class="row"><input type="checkbox" id="l-sketch"> Old hand-drawn Lloyds ward</label>
    <label class="row"><input type="checkbox" id="l-cluster" checked> Cluster nearby pins</label>
    <div class="tiny" style="margin-top:4px">__ATTRIB__</div>
  </div>
</div>
<div class="panel" id="stats">
  <h2><span>Area statistics</span><button class="small cfg" onclick="clearSel()" title="Clear the selected area">Clear</button></h2>
  <div class="modes cfg">
    <button id="m-draw" onclick="setMode('draw')" title="Click points on the map to outline an area; click the first point again or double-click to finish">Draw area</button>
    <button id="m-ward" onclick="setMode('ward')" title="Click a ward on the map">Pick ward</button>
    <button id="m-view" onclick="setMode('view')" title="Count whatever is in the current view">Visible map</button>
  </div>
  <div class="cfg">
    <input type="text" id="sel-name" placeholder="Area name (shown on the card)">
    <label class="inl">Since <input type="date" id="since"></label>
    <label class="inl">Until <input type="date" id="until"></label>
    <label class="inl"><input type="checkbox" id="inc-pins" checked> include My Map pins</label>
    <label class="inl"><input type="checkbox" id="inc-est" checked> include ≈ dated</label>
    <label class="inl"><input type="checkbox" id="inc-undated" checked> include undated</label>
  </div>
  <div id="stat-body"></div>
</div>
<div id="showbar">
  <button class="primary" onclick="savePng()">Save PNG</button>
  <button onclick="toggleShowcase()">Exit showcase</button>
</div>
<script>
__JS__
</script>
</body>
</html>
"""

_JS = r"""
const DATA = __DATA_JSON__;
const AREAS = __AREAS_JSON__;
const WARDS = __WARDS_JSON__;
const WARD_LABELS = __WARD_LABELS_JSON__;
const COLOURS = __COLOURS_JSON__;
const TOKEN = __TOKEN_JSON__;
const PROBLEM = __PROBLEM_JSON__;
const MYMAP = __MYMAP_JSON__;
const LIVE = __LIVE_JSON__;
const CENTRE = __CENTRE__, ZOOM = __ZOOM__;
const esc = s => (s ?? '').toString().replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]);
const $ = id => document.getElementById(id);
if (MYMAP) { $('mymaplink').href = MYMAP; $('mymaplink').hidden = false; }

// ---------------------------------------------------------------- helpers
// Point-in-polygon (ray casting) for Polygon and MultiPolygon geometries.
function inRing(pt, ring) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
    const hit = ((yi > pt[1]) !== (yj > pt[1])) &&
      (pt[0] < (xj - xi) * (pt[1] - yi) / (yj - yi) + xi);
    if (hit) inside = !inside;
  }
  return inside;
}
function inPoly(pt, geom) {
  if (!geom) return false;
  const polys = geom.type === 'Polygon' ? [geom.coordinates]
              : geom.type === 'MultiPolygon' ? geom.coordinates : [];
  for (const p of polys) {
    if (inRing(pt, p[0]) && !p.slice(1).some(h => inRing(pt, h))) return true;
  }
  return false;
}
function bboxOf(geom) {
  const b = [Infinity, Infinity, -Infinity, -Infinity];
  const walk = c => Array.isArray(c[0]) ? c.forEach(walk)
    : (b[0] = Math.min(b[0], c[0]), b[1] = Math.min(b[1], c[1]),
       b[2] = Math.max(b[2], c[0]), b[3] = Math.max(b[3], c[1]));
  walk(geom.coordinates);
  return b;
}
const dateOf = p => (p.when || '').slice(0, 10);
const fmtD = s => s ? new Date(s + 'T00:00:00').toLocaleDateString('en-GB',
  {day: 'numeric', month: 'short', year: 'numeric'}) : '';
const MONTHS = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const KIND_NAMES = {flytipping:'Fly-tipping / waste', pothole:'Road / footway',
  drain:'Drainage / flooding', vegetation:'Vegetation', lighting:'Lighting / signals',
  sign:'Signs / graffiti', issue:'Community issues', place:'Landmarks', other:'Other'};
const STATUS_NAMES = {queued:'To submit', submitted:'Submitted', mapped:'On the map',
  completed:'Completed', open:'Outstanding', landmark:'Landmark'};

// A pin's filter bucket: My Map "completed" is history, kept apart from the
// tool's own Completed so either can be switched off on its own.
const bucketOf = p => (p.src === 'pin' && p.status === 'completed') ? 'pin-completed' : p.status;

// ------------------------------------------------------------------ state
const counts = {};
DATA.features.forEach(f => { const b = bucketOf(f.properties); counts[b] = (counts[b] || 0) + 1; });
for (const k in counts) { const el = $('n-' + k); if (el) el.textContent = counts[k]; }
if (DATA.features.filter(f => f.properties.src === 'report').length === 1) $('n-all-s').textContent = '';

const wantedStatus = () => new Set([...document.querySelectorAll('#filters input')]
  .filter(b => b.checked).map(b => b.dataset.status));
const wantedKind = () => new Set([...document.querySelectorAll('#legend .kind')]
  .filter(l => l.querySelector('input').checked).map(l => l.dataset.kind));

// Filtering rebuilds the source data rather than using setFilter, because the
// clustering happens in the source - a filtered layer would still show the
// hidden reports inside the cluster counts.
function filtered() {
  const st = wantedStatus(), kd = wantedKind();
  return {type: 'FeatureCollection', features: DATA.features.filter(f =>
    st.has(bucketOf(f.properties)) && kd.has(f.properties.kind))};
}

let map = null, mode = '', sel = null, selName = '', drawPts = [], hoverWard = null;

// Guard BEFORE constructing the map - a missing token is otherwise a silent
// blank canvas with no explanation.
if (!TOKEN) {
  document.body.insertAdjacentHTML('beforeend',
    '<div id="err"><div><b>The map needs a Mapbox token.</b><br><br>' + esc(PROBLEM) + '</div></div>');
} else if (typeof mapboxgl === 'undefined') {
  document.body.insertAdjacentHTML('beforeend',
    '<div id="err"><div><b>Could not load Mapbox GL.</b><br><br>The map library loads from '
    + 'api.mapbox.com - check the internet connection (or whether that host is blocked) and reload.</div></div>');
} else {
  mapboxgl.accessToken = TOKEN;
  map = new mapboxgl.Map({
    container: 'map', style: 'mapbox://styles/mapbox/streets-v12',
    center: CENTRE, zoom: ZOOM, cooperativeGestures: false,
    preserveDrawingBuffer: true, doubleClickZoom: false
  });
  map.addControl(new mapboxgl.NavigationControl(), 'top-right');
  map.addControl(new mapboxgl.FullscreenControl(), 'top-right');
  map.addControl(new mapboxgl.ScaleControl({unit: 'metric'}));
  map.on('error', e => console.error('Map error:', e.error));
  map.on('load', onLoad);
  // A tab opened in the background never gets a frame, so Mapbox's 'load' can
  // sit unfired until the tab is shown - and sometimes not even then. Once the
  // style is in, build the layers anyway.
  map.once('styledata', () => setTimeout(() => { if (map.isStyleLoaded()) onLoad(); }, 1500));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) setTimeout(() => { if (map.isStyleLoaded()) onLoad(); }, 500); });
}

let didLoad = false;
function onLoad() {
  if (didLoad) return;
  didLoad = true;
  // Wards first, so pins draw on top of them.
  map.addSource('wards', {type: 'geojson', data: WARDS, promoteId: 'code'});
  map.addLayer({id: 'ward-fill', type: 'fill', source: 'wards',
    paint: {'fill-color': '#1d5fa6', 'fill-opacity':
      ['case', ['boolean', ['feature-state', 'hover'], false], 0.18, 0.04]}});
  map.addLayer({id: 'ward-line', type: 'line', source: 'wards',
    paint: {'line-color': '#1d3f6e', 'line-width':
      ['interpolate', ['linear'], ['zoom'], 10, 1, 14, 2.2], 'line-dasharray': [3, 2], 'line-opacity': 0.85}});
  map.addSource('ward-labels', {type: 'geojson', data: WARD_LABELS});
  map.addLayer({id: 'ward-label', type: 'symbol', source: 'ward-labels',
    layout: {'text-field': ['get', 'name'], 'text-size': 12,
             'text-font': ['DIN Offc Pro Medium', 'Arial Unicode MS Bold'],
             'text-letter-spacing': 0.05, 'text-transform': 'uppercase',
             'text-max-width': 8, 'text-allow-overlap': false},
    paint: {'text-color': '#1d3f6e', 'text-halo-color': '#fff', 'text-halo-width': 1.6}});

  const areaFc = k => ({type: 'FeatureCollection', features: AREAS.filter(a => a.kind === k)
    .map(a => ({type: 'Feature', geometry: a.geometry, properties: {name: a.name, colour: a.colour || '#000'}}))});
  map.addSource('areas', {type: 'geojson', data: areaFc('area')});
  map.addLayer({id: 'area-line', type: 'line', source: 'areas',
    paint: {'line-color': ['get', 'colour'], 'line-width': 2.5, 'line-opacity': 0.9}});
  map.addLayer({id: 'area-label', type: 'symbol', source: 'areas',
    layout: {'text-field': ['get', 'name'], 'text-size': 11.5, 'text-offset': [0, 1.2],
             'text-font': ['DIN Offc Pro Medium', 'Arial Unicode MS Bold']},
    paint: {'text-color': '#222', 'text-halo-color': '#fff', 'text-halo-width': 1.4}});
  map.addSource('sketch', {type: 'geojson', data: areaFc('ward-sketch')});
  map.addLayer({id: 'sketch-line', type: 'line', source: 'sketch',
    layout: {visibility: 'none'},
    paint: {'line-color': '#E65100', 'line-width': 2, 'line-dasharray': [1, 1.5]}});

  map.addSource('sel', {type: 'geojson', data: {type: 'FeatureCollection', features: []}});
  map.addLayer({id: 'sel-fill', type: 'fill', source: 'sel',
    paint: {'fill-color': '#1d5fa6', 'fill-opacity': 0.12}});
  map.addLayer({id: 'sel-line', type: 'line', source: 'sel',
    paint: {'line-color': '#1d5fa6', 'line-width': 2.5}});
  map.addSource('draw', {type: 'geojson', data: {type: 'FeatureCollection', features: []}});
  map.addLayer({id: 'draw-line', type: 'line', source: 'draw',
    paint: {'line-color': '#1d5fa6', 'line-width': 2, 'line-dasharray': [2, 1.5]}});
  map.addLayer({id: 'draw-pts', type: 'circle', source: 'draw',
    filter: ['==', ['geometry-type'], 'Point'],
    paint: {'circle-radius': 5, 'circle-color': '#fff', 'circle-stroke-color': '#1d5fa6', 'circle-stroke-width': 2}});

  map.addSource('reports', {type: 'geojson', data: filtered(),
    cluster: true, clusterMaxZoom: 15, clusterRadius: 40});
  map.addLayer({id: 'clusters', type: 'circle', source: 'reports', filter: ['has', 'point_count'],
    paint: {'circle-color': ['step', ['get', 'point_count'], '#7fb2d9', 10, '#4c8ec4', 30, '#2b6ca8'],
            'circle-radius': ['step', ['get', 'point_count'], 16, 10, 22, 30, 28],
            'circle-stroke-color': '#fff', 'circle-stroke-width': 2}});
  map.addLayer({id: 'cluster-count', type: 'symbol', source: 'reports', filter: ['has', 'point_count'],
    layout: {'text-field': ['get', 'point_count_abbreviated'],
             'text-font': ['DIN Offc Pro Medium', 'Arial Unicode MS Bold'], 'text-size': 12},
    paint: {'text-color': '#fff'}});
  map.addSource('reports-flat', {type: 'geojson', data: filtered()});
  map.addLayer({id: 'report-points-flat', type: 'circle', source: 'reports-flat',
    layout: {visibility: 'none'},
    paint: {'circle-color': __MATCH__,
            'circle-radius': ['interpolate', ['linear'], ['zoom'], 11, 4, 14, 6, 16, 8],
            'circle-opacity': ['match', ['get', 'status'], 'queued', 0.55, 0.9],
            'circle-stroke-color': ['case', ['==', ['get', 'src'], 'pin'], '#1b1f24', '#fff'],
            'circle-stroke-width': 1.2}});
  map.addLayer({id: 'report-points', type: 'circle', source: 'reports', filter: ['!', ['has', 'point_count']],
    paint: {'circle-color': __MATCH__,
            'circle-radius': ['case', ['==', ['get', 'status'], 'landmark'], 6, 7],
            'circle-opacity': ['match', ['get', 'status'], 'queued', 0.55, 0.9],
            // My Map pins wear a dark ring, the tool's own reports a white one.
            'circle-stroke-color': ['case', ['==', ['get', 'src'], 'pin'], '#1b1f24', '#fff'],
            'circle-stroke-width': 1.5}});

  map.on('click', 'clusters', e => {
    if (mode) return;
    const f = map.queryRenderedFeatures(e.point, {layers: ['clusters']})[0];
    map.getSource('reports').getClusterExpansionZoom(f.properties.cluster_id,
      (err, zoom) => { if (!err) map.easeTo({center: f.geometry.coordinates, zoom}); });
  });
  map.on('click', 'report-points', e => { if (!mode) popup(e.features[0]); });
  map.on('click', 'report-points-flat', e => { if (!mode) popup(e.features[0]); });
  for (const layer of ['clusters', 'report-points', 'report-points-flat']) {
    map.on('mouseenter', layer, () => { if (!mode) map.getCanvas().style.cursor = 'pointer'; });
    map.on('mouseleave', layer, () => { if (!mode) map.getCanvas().style.cursor = ''; });
  }
  map.on('click', onMapClick);
  map.on('dblclick', e => { if (mode === 'draw') { e.preventDefault(); finishDraw(); } });
  map.on('mousemove', onMove);
  map.on('moveend', () => { if (mode === 'view') recalc(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') { if (mode === 'draw') { drawPts = []; drawSrc(); } setMode(''); }
    if (e.key === 'Enter' && mode === 'draw') finishDraw();
  });

  document.querySelectorAll('#filters input, #legend .kind input').forEach(b =>
    b.addEventListener('change', () => { const d = filtered(); map.getSource('reports').setData(d);
      map.getSource('reports-flat').setData(d); recalc(); }));
  $('l-cluster').addEventListener('change', e => setClustering(e.target.checked));
  $('l-wards').addEventListener('change', e => {
    for (const l of ['ward-fill', 'ward-line']) map.setLayoutProperty(l, 'visibility', e.target.checked ? 'visible' : 'none');
    $('l-wardnames').disabled = !e.target.checked;
    map.setLayoutProperty('ward-label', 'visibility', (e.target.checked && $('l-wardnames').checked) ? 'visible' : 'none');
  });
  $('l-wardnames').addEventListener('change', e =>
    map.setLayoutProperty('ward-label', 'visibility', e.target.checked ? 'visible' : 'none'));
  $('l-areas').addEventListener('change', e => { for (const l of ['area-line', 'area-label'])
    map.setLayoutProperty(l, 'visibility', e.target.checked ? 'visible' : 'none'); });
  $('l-sketch').addEventListener('change', e =>
    map.setLayoutProperty('sketch-line', 'visibility', e.target.checked ? 'visible' : 'none'));
  for (const id of ['since', 'until', 'inc-pins', 'inc-est', 'inc-undated']) $(id).addEventListener('change', recalc);
  $('sel-name').addEventListener('input', e => { selName = e.target.value; recalc(); });

  const shown = filtered().features;
  if (shown.length) {
    const bounds = new mapboxgl.LngLatBounds();
    shown.forEach(f => bounds.extend(f.geometry.coordinates));
    map.fitBounds(bounds, {padding: {top: 70, bottom: 70, left: 280, right: 370}, maxZoom: 15});
  }
  // Default "since": the start of the year the earliest dated item falls in.
  const dates = DATA.features.map(f => dateOf(f.properties)).filter(Boolean).sort();
  if (dates.length) $('since').value = dates[0].slice(0, 4) + '-01-01';
  recalc();
}

// ------------------------------------------------------------------ popups
function popup(f) {
  const p = f.properties;
  const bits = [];
  if (p.photo) bits.push('<img src="' + esc(p.photo) + '" alt="" loading="lazy" onclick="lightbox(\'' + esc(p.photo_full || p.photo) + '\')">');
  const tags = ['<span class="tag">' + esc(KIND_NAMES[p.kind] || p.kind) + '</span>',
                '<span class="tag">' + esc(STATUS_NAMES[p.status] || p.status) + '</span>'];
  if (p.src === 'pin') tags.push('<span class="tag">My Map' + (p.folder ? ' · ' + esc(p.folder) : '') + '</span>');
  bits.push(tags.join(''));
  if (p.category) bits.push('<span class="muted">' + esc(p.category) + '</span>');
  if (p.reference) bits.push('<b>Ref:</b> ' + (p.report_url && /^https?:/.test(p.report_url)
    ? '<a href="' + esc(p.report_url) + '" target="_blank">' + esc(p.reference) + '</a>' : esc(p.reference)));
  const where = [p.street, p.locality, p.postcode].filter(Boolean).join(', ');
  if (where) bits.push('<b>Where:</b> ' + esc(where));
  if (p.w3w) bits.push('<b>W3W:</b> ///' + esc(p.w3w));
  if (p.when) bits.push('<b>' + (p.src === 'pin' ? 'Dated' : 'Photographed') + ':</b> '
    + (p.when_est === true || p.when_est === 'true' ? '≈ ' : '') + esc(p.when)
    + (p.when_est === true || p.when_est === 'true' ? ' <span class="muted">(estimated from the reference number)</span>' : ''));
  if (p.detail) bits.push('<span class="muted">' + esc(p.detail) + '</span>');
  const head = p.src === 'report' ? '#' + p.id + ' · ' : '';
  new mapboxgl.Popup().setLngLat(f.geometry.coordinates.slice())
    .setHTML('<h3>' + head + esc(p.title) + '</h3>' + bits.join('<br>')).addTo(map);
}
function lightbox(src) { const lb = $('lightbox'); lb.querySelector('img').src = src; lb.style.display = 'flex'; }

// --------------------------------------------------------------- selection
function setMode(m) {
  mode = (mode === m) ? '' : m;
  for (const id of ['draw', 'ward', 'view']) $('m-' + id).classList.toggle('on', mode === id);
  map.getCanvas().style.cursor = mode ? 'crosshair' : '';
  const hint = $('hint');
  hint.style.display = mode ? 'block' : 'none';
  hint.textContent = mode === 'draw' ? 'Click to add corners · click the first corner, double-click or press Enter to finish · Esc to cancel'
                   : mode === 'ward' ? 'Click a ward' : mode === 'view' ? 'Counting everything in view — pan and zoom to change it' : '';
  if (mode !== 'draw') { drawPts = []; drawSrc(); }
  if (mode === 'view') { sel = null; selName = ''; $('sel-name').value = ''; setSel(null); recalc(); }
}
function drawSrc() {
  const feats = drawPts.map(c => ({type: 'Feature', geometry: {type: 'Point', coordinates: c}, properties: {}}));
  if (drawPts.length > 1) feats.push({type: 'Feature', geometry: {type: 'LineString', coordinates: drawPts}, properties: {}});
  map.getSource('draw').setData({type: 'FeatureCollection', features: feats});
}
function onMapClick(e) {
  if (mode === 'draw') {
    const c = [e.lngLat.lng, e.lngLat.lat];
    if (drawPts.length >= 3) {
      const first = map.project(drawPts[0]);
      if (Math.hypot(first.x - e.point.x, first.y - e.point.y) < 12) return finishDraw();
    }
    drawPts.push(c); drawSrc();
  } else if (mode === 'ward') {
    const f = map.queryRenderedFeatures(e.point, {layers: ['ward-fill']})[0];
    if (f) {
      const full = WARDS.features.find(w => w.properties.code === f.properties.code);
      sel = full.geometry; selName = full.properties.name + ' ward';
      $('sel-name').value = selName; setSel(sel);
      setMode(''); recalc();
    }
  }
}
function finishDraw() {
  if (drawPts.length < 3) return;
  const ring = drawPts.slice(); ring.push(ring[0]);
  sel = {type: 'Polygon', coordinates: [ring]};
  if (!selName) { selName = 'Drawn area'; $('sel-name').value = selName; }
  drawPts = []; drawSrc(); setSel(sel); setMode(''); recalc();
}
function setSel(geom) {
  map.getSource('sel').setData(geom ? {type: 'Feature', geometry: geom, properties: {}}
                                    : {type: 'FeatureCollection', features: []});
}
function clearSel() { sel = null; selName = ''; $('sel-name').value = ''; setSel(null); if (mode !== 'view') setMode(''); recalc(); }
function onMove(e) {
  if (mode !== 'ward') { if (hoverWard !== null) { map.setFeatureState({source: 'wards', id: hoverWard}, {hover: false}); hoverWard = null; } return; }
  const f = map.queryRenderedFeatures(e.point, {layers: ['ward-fill']})[0];
  const id = f ? f.id : null;
  if (hoverWard !== null && hoverWard !== id) map.setFeatureState({source: 'wards', id: hoverWard}, {hover: false});
  if (id !== null && id !== undefined) map.setFeatureState({source: 'wards', id}, {hover: true});
  hoverWard = id;
}

// ------------------------------------------------------------------- stats
let period = 'month';
function selected() {
  const since = $('since').value, until = $('until').value;
  const incPins = $('inc-pins').checked, incEst = $('inc-est').checked, incUndated = $('inc-undated').checked;
  const b = (mode === 'view' && !sel) ? map.getBounds() : null;
  const bb = sel ? bboxOf(sel) : null;
  return filtered().features.filter(f => {
    const p = f.properties, c = f.geometry.coordinates;
    if (p.status === 'landmark') return false;
    if (p.src === 'pin' && !incPins) return false;
    if ((p.when_est === true || p.when_est === 'true') && !incEst) return false;
    const d = dateOf(p);
    if (since && d && d < since) return false;
    if (until && d && d > until) return false;
    if (!d && !incUndated) return false;               // undated can't be placed in a period
    if (b) return b.contains(c);
    if (sel) return c[0] >= bb[0] && c[0] <= bb[2] && c[1] >= bb[1] && c[1] <= bb[3] && inPoly(c, sel);
    return true;
  });
}
function recalc() {
  if (!map || !map.getSource('reports')) return;
  const rows = selected();
  const since = $('since').value, until = $('until').value;
  const scope = sel ? (selName || 'Selected area') : mode === 'view' ? 'Visible map' : (selName || 'Everything on the map');
  const n = rows.length;
  const byKind = {}, byStatus = {}, byStreet = {}, byMonth = {}, byWeek = {};
  let est = 0, undated = 0, pins = 0, first = '', last = '';
  for (const f of rows) {
    const p = f.properties;
    byKind[p.kind] = (byKind[p.kind] || 0) + 1;
    byStatus[p.status] = (byStatus[p.status] || 0) + 1;
    if (p.street) byStreet[p.street] = (byStreet[p.street] || 0) + 1;
    if (p.src === 'pin') pins++;
    if (p.when_est === true || p.when_est === 'true') est++;
    const d = dateOf(p);
    if (!d) { undated++; continue; }
    if (!first || d < first) first = d;
    if (!last || d > last) last = d;
    byMonth[d.slice(0, 7)] = (byMonth[d.slice(0, 7)] || 0) + 1;
    const wk = weekKey(d); byWeek[wk] = (byWeek[wk] || 0) + 1;
  }
  const live = n - (byStatus.completed || 0);
  const kpis = [
    [n, 'in ' + (sel || mode === 'view' ? 'area' : 'total') + (since ? ' since ' + fmtD(since) : '')],
    [byKind.flytipping || 0, 'fly-tipping & waste'],
    [byKind.pothole || 0, 'road & footway'],
    [live, 'still open · ' + (byStatus.completed || 0) + ' completed'],
  ];
  const bars = Object.entries(byKind).sort((a, b) => b[1] - a[1]).map(([k, v]) =>
    '<div class="bar"><span>' + esc(KIND_NAMES[k] || k) + '</span><i style="width:' + Math.round(100 * v / Math.max(n, 1)) + '%;background:' + (COLOURS[k] || '#888') + '"></i><b>' + v + '</b></div>').join('');
  const streets = Object.entries(byStreet).sort((a, b) => b[1] - a[1]).slice(0, 5).map(([k, v]) =>
    '<div class="bar"><span>' + esc(k) + '</span><i style="width:' + Math.round(100 * v / Math.max(n, 1)) + '%"></i><b>' + v + '</b></div>').join('');
  const period_txt = (since ? fmtD(since) : (first ? fmtD(first) : '')) + ' – ' + (until ? fmtD(until) : 'today');
  $('stat-body').innerHTML =
    '<div><b style="font-size:14px">' + esc(scope) + '</b><br><span class="muted">' + esc(period_txt) + '</span></div>'
    + '<div class="kpis">' + kpis.map(k => '<div class="kpi"><b>' + k[0] + '</b><span>' + esc(k[1]) + '</span></div>').join('') + '</div>'
    + '<div class="sect"><h4>Reports over time <span class="tiny" style="float:right"><a href="#" onclick="period=\'month\';recalc();return false" style="color:' + (period === 'month' ? 'var(--ink)' : 'var(--muted)') + '">month</a> · <a href="#" onclick="period=\'week\';recalc();return false" style="color:' + (period === 'week' ? 'var(--ink)' : 'var(--muted)') + '">week</a></span></h4>'
    + chartSvg(period === 'month' ? byMonth : byWeek, since || first, until) + '</div>'
    + (bars ? '<div class="sect"><h4>By type</h4><div class="bars">' + bars + '</div></div>' : '')
    + (streets ? '<div class="sect"><h4>Most-reported streets</h4><div class="bars">' + streets + '</div></div>' : '')
    + '<div class="foot">' + (pins ? pins + ' from the My Map · ' : '') + (est ? est + ' dated ≈ by reference number · ' : '')
    + (undated ? undated + ' undated · ' : '') + 'NNC Reporter · ' + new Date().toLocaleDateString('en-GB', {day: 'numeric', month: 'short', year: 'numeric'}) + '</div>';
  window.__stats = {scope, period_txt, kpis, byKind, byMonth, byWeek, n, est, pins, undated};
  attachTips();
}
function weekKey(d) {
  const dt = new Date(d + 'T00:00:00'); const day = (dt.getDay() + 6) % 7;   // Monday-based
  dt.setDate(dt.getDate() - day);
  return dt.toISOString().slice(0, 10);
}
function bucketsBetween(from, to, weekly) {
  const out = [];
  if (!from) return out;
  const end = to ? new Date(to + 'T00:00:00') : new Date();
  if (weekly) {
    let d = new Date(weekKey(from) + 'T00:00:00');
    while (d <= end && out.length < 120) { out.push(d.toISOString().slice(0, 10)); d.setDate(d.getDate() + 7); }
  } else {
    let y = +from.slice(0, 4), m = +from.slice(5, 7);
    const ey = end.getFullYear(), em = end.getMonth() + 1;
    while ((y < ey || (y === ey && m <= em)) && out.length < 60) {
      out.push(y + '-' + String(m).padStart(2, '0')); m++; if (m > 12) { m = 1; y++; }
    }
  }
  return out;
}
function chartSvg(series, from, to) {
  const weekly = period === 'week';
  const keys = bucketsBetween(from, to, weekly);
  if (!keys.length) return '<div class="tiny">No dated items in this selection.</div>';
  const W = 312, H = 120, padL = 4, padB = 18, padT = 12;
  const max = Math.max(1, ...keys.map(k => series[k] || 0));
  const bw = (W - padL) / keys.length;
  const bars = keys.map((k, i) => {
    const v = series[k] || 0, h = v ? Math.max(2, (H - padB - padT) * v / max) : 0;
    const x = padL + i * bw + 1, y = H - padB - h, w = Math.max(1, bw - 2);
    const lbl = weekly ? ('w/c ' + fmtD(k)) : (MONTHS[+k.slice(5, 7) - 1] + ' ' + k.slice(0, 4));
    return '<rect class="b" x="' + x + '" y="' + y + '" width="' + w + '" height="' + h + '" rx="2" data-tip="' + esc(lbl + ': ' + v) + '"></rect>'
      + (v === max && v > 0 ? '<text x="' + (x + w / 2) + '" y="' + (y - 3) + '" text-anchor="middle">' + v + '</text>' : '');
  }).join('');
  const step = Math.ceil(keys.length / (weekly ? 6 : 8));
  const labels = keys.map((k, i) => (i % step === 0 || i === keys.length - 1) ?
    '<text x="' + (padL + i * bw + bw / 2) + '" y="' + (H - 4) + '" text-anchor="middle">'
    + (weekly ? (+k.slice(8, 10)) + ' ' + MONTHS[+k.slice(5, 7) - 1] : MONTHS[+k.slice(5, 7) - 1] + (k.slice(5, 7) === '01' || i === 0 ? ' ' + k.slice(2, 4) : '')) + '</text>' : '').join('');
  return '<svg id="chart" viewBox="0 0 ' + W + ' ' + H + '"><line x1="0" x2="' + W + '" y1="' + (H - padB) + '" y2="' + (H - padB) + '"></line>' + bars + labels + '</svg>';
}
function attachTips() {
  const tip = $('tip');
  document.querySelectorAll('#chart rect.b').forEach(r => {
    r.addEventListener('mousemove', e => { tip.textContent = r.dataset.tip; tip.style.display = 'block';
      tip.style.left = (e.clientX + 12) + 'px'; tip.style.top = (e.clientY - 24) + 'px'; });
    r.addEventListener('mouseleave', () => tip.style.display = 'none');
  });
}

// ---------------------------------------------------------------- showcase
let clusterBefore = true;
function setClustering(on) {
  for (const l of ['clusters', 'cluster-count', 'report-points']) map.setLayoutProperty(l, 'visibility', on ? 'visible' : 'none');
  map.setLayoutProperty('report-points-flat', 'visibility', on ? 'none' : 'visible');
  $('l-cluster').checked = on;
}
function toggleShowcase() {
  const on = document.body.classList.toggle('showcase');
  // A showcase wants every pin visible, not cluster bubbles; restore afterwards.
  if (on) { clusterBefore = $('l-cluster').checked; setClustering(false); }
  else setClustering(clusterBefore);
  setTimeout(() => map && map.resize(), 50);
}
async function savePng() {
  const s = window.__stats || {};
  const src = map.getCanvas();
  const cw = src.width, ch = src.height;
  const out = document.createElement('canvas');
  out.width = cw; out.height = ch;
  const g = out.getContext('2d');
  g.drawImage(src, 0, 0);
  // The card, drawn by hand so the PNG is self-contained (no html2canvas).
  const dpr = cw / src.clientWidth, pad = 18 * dpr, W = 380 * dpr;
  const x0 = cw - W - pad, y0 = pad;
  const stats = s, kp = stats.kpis || [];
  let y = y0 + 22 * dpr;
  const H = (196 + 150 + 20) * dpr;
  g.fillStyle = 'rgba(255,255,255,0.97)'; roundRect(g, x0, y0, W, H, 14 * dpr); g.fill();
  g.shadowColor = 'rgba(0,0,0,0)';
  g.fillStyle = '#1b1f24'; g.font = 'bold ' + (18 * dpr) + 'px Segoe UI, Roboto, sans-serif';
  g.fillText(fit(g, stats.scope || 'Area statistics', W - 40 * dpr), x0 + 20 * dpr, y); y += 20 * dpr;
  g.fillStyle = '#5c6672'; g.font = (12.5 * dpr) + 'px Segoe UI, Roboto, sans-serif';
  g.fillText(stats.period_txt || '', x0 + 20 * dpr, y); y += 14 * dpr;
  const tw = (W - 40 * dpr - 8 * dpr) / 2, th = 58 * dpr;
  kp.forEach((k, i) => {
    const tx = x0 + 20 * dpr + (i % 2) * (tw + 8 * dpr), ty = y + Math.floor(i / 2) * (th + 8 * dpr);
    g.fillStyle = '#f4f6f9'; roundRect(g, tx, ty, tw, th, 8 * dpr); g.fill();
    g.fillStyle = '#1b1f24'; g.font = 'bold ' + (28 * dpr) + 'px Segoe UI, Roboto, sans-serif';
    g.fillText(String(k[0]), tx + 10 * dpr, ty + 32 * dpr);
    g.fillStyle = '#5c6672'; g.font = (11 * dpr) + 'px Segoe UI, Roboto, sans-serif';
    g.fillText(fit(g, k[1], tw - 20 * dpr), tx + 10 * dpr, ty + 49 * dpr);
  });
  y += 2 * (th + 8 * dpr) + 6 * dpr;
  g.fillStyle = '#1b1f24'; g.font = 'bold ' + (12 * dpr) + 'px Segoe UI, Roboto, sans-serif';
  g.fillText('Reports over time', x0 + 20 * dpr, y + 10 * dpr); y += 18 * dpr;
  const series = period === 'month' ? (stats.byMonth || {}) : (stats.byWeek || {});
  const keys = bucketsBetween($('since').value || firstDate(), $('until').value, period === 'week');
  const cW = W - 40 * dpr, cH = 100 * dpr, max = Math.max(1, ...keys.map(k => series[k] || 0));
  const bw = keys.length ? cW / keys.length : cW;
  keys.forEach((k, i) => {
    const v = series[k] || 0, h = v ? Math.max(2 * dpr, (cH - 16 * dpr) * v / max) : 0;
    g.fillStyle = '#2b6ca8'; roundRect(g, x0 + 20 * dpr + i * bw + 1, y + cH - 16 * dpr - h, Math.max(1, bw - 2), h, 2 * dpr); g.fill();
    if (v === max && v) { g.fillStyle = '#5c6672'; g.font = (10 * dpr) + 'px Segoe UI, Roboto, sans-serif'; g.textAlign = 'center';
      g.fillText(String(v), x0 + 20 * dpr + i * bw + bw / 2, y + cH - 16 * dpr - h - 3 * dpr); g.textAlign = 'left'; }
  });
  g.strokeStyle = '#e4e7eb'; g.lineWidth = dpr; g.beginPath();
  g.moveTo(x0 + 20 * dpr, y + cH - 16 * dpr); g.lineTo(x0 + 20 * dpr + cW, y + cH - 16 * dpr); g.stroke();
  const step = Math.ceil(keys.length / 8);
  g.fillStyle = '#5c6672'; g.font = (10 * dpr) + 'px Segoe UI, Roboto, sans-serif'; g.textAlign = 'center';
  keys.forEach((k, i) => { if (i % step === 0 || i === keys.length - 1)
    g.fillText(period === 'week' ? (+k.slice(8, 10)) + ' ' + MONTHS[+k.slice(5, 7) - 1] : MONTHS[+k.slice(5, 7) - 1] + ' ' + k.slice(2, 4),
      x0 + 20 * dpr + i * bw + bw / 2, y + cH - 3 * dpr); });
  g.textAlign = 'left'; y += cH + 6 * dpr;
  g.fillStyle = '#5c6672'; g.font = (10.5 * dpr) + 'px Segoe UI, Roboto, sans-serif';
  const foot = (stats.pins ? stats.pins + ' from the My Map · ' : '') + (stats.est ? stats.est + ' dated ≈ · ' : '')
    + 'NNC Reporter · ' + new Date().toLocaleDateString('en-GB', {day: 'numeric', month: 'short', year: 'numeric'});
  g.fillText(fit(g, foot, W - 40 * dpr), x0 + 20 * dpr, y + 8 * dpr);
  const a = document.createElement('a');
  const slug = (stats.scope || 'map').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
  a.download = 'nnc-reports-' + slug + '-' + new Date().toISOString().slice(0, 10) + '.png';
  a.href = out.toDataURL('image/png'); a.click();
}
function firstDate() { const d = DATA.features.map(f => dateOf(f.properties)).filter(Boolean).sort(); return d[0] || ''; }
function roundRect(g, x, y, w, h, r) {
  g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath();
}
function fit(g, text, maxW) {
  if (g.measureText(text).width <= maxW) return text;
  while (text.length > 1 && g.measureText(text + '…').width > maxW) text = text.slice(0, -1);
  return text + '…';
}

// ------------------------------------------------------------- live import
async function importMyMap() {
  if (!LIVE) return;
  const btn = window.event && window.event.target; if (btn) { btn.disabled = true; btn.textContent = 'Fetching…'; }
  try {
    const res = await fetch('/api/pins/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'});
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || res.status);
    alert('My Map merged: ' + d.placemarks + ' placemarks → ' + d.matched + ' already in the tool, '
      + d.pins_added + ' new pin' + (d.pins_added === 1 ? '' : 's') + ', ' + d.pins_updated + ' updated, '
      + d.areas + ' drawn area' + (d.areas === 1 ? '' : 's') + '.'
      + (d.photos_to_cache ? '\n' + d.photos_to_cache + ' photo(s) are being cached in the background.' : '')
      + '\n\nThe map will reload.');
    location.reload();
  } catch (e) {
    alert('Could not import the My Map: ' + e.message);
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh My Map pins'; }
  }
}
"""
