"""Merge the pins from the Google My Map into the tool's own map.

The "Exeter/Lloyds Issue Tracker" My Map holds two kinds of thing:

  * pins the tool put there itself - imported from NNC_Reports.kml. Those are
    already in reports.db, so they are matched (by reference, or by position and
    category when a report has no reference) and left alone; the database stays
    the single source of truth for them.
  * pins added by hand - the fly-tipping history from before the tool existed,
    and things the tool does not report (speeding, parking, HMOs, an empty
    house). Those are copied into a `map_pins` table, with their Google-hosted
    photos cached locally so the map still shows them if the My Map ever goes.

Boundaries drawn on the My Map (the Exeter Estate outline, the hand-drawn Lloyds
ward) are kept in `map_areas`. The official ward borders come from the ONS, not
from here - see nncr/static/wards_nnc.geojson.

Re-importing is safe: every pin has a stable key, so an import updates what is
there rather than doubling it.
"""
import hashlib
import html
import io
import json
import re
import threading
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import xml.etree.ElementTree as ET

from . import config, store
from .geo import metres_between
from .kml import STYLES, _style_for

KML_NS = "{http://www.opengis.net/kml/2.2}"
SOURCE = "mymaps"

# A pin on the My Map that sits within this many metres of a report of the same
# broad type, when neither side has a reference to compare, is the same thing.
MATCH_METRES = 8.0

# Granicus-style references the council hands out. The prefix says which form.
REF_PREFIXES = {
    "FLY": ("Flytipping", "flytipping"),
    "PB": ("Litter or dog waste bin", "flytipping"),
    "GROUNDM": ("Grounds maintenance", "vegetation"),
    "ABNVEH": ("Abandoned vehicle", "other"),
}
REF_RE = re.compile(r"^\s*([A-Za-z]{0,8})\s*(\d{6,})\s*$")

# Hand-drawn pins with no council reference: what they are, from their names.
KEYWORD_KINDS = [
    (("speeding", "parking", "hmo", "empty house", "homes england", "intersection",
      "anti-social", "antisocial", "land grab"), ("Community issue", "issue")),
    (("overgrow", "overgrown", "hedge", "tree", "weeds", "grass"),
     ("Restricted Visibility / Overgrown / Overhanging", "vegetation")),
    (("dead ", "hedgehog", "fox", "badger", "animal"), ("Dead animal", "other")),
    (("clothes bin", "clothing bank", "recycling bank"),
     ("Rubbish (refuse and recycling)", "flytipping")),
    (("fly", "rubbish", "dumped", "mattress", "waste", "litter", "trolley"),
     ("Flytipping", "flytipping")),
    (("pothole", "road surface", "carriageway"), ("Potholes / Highway Condition", "pothole")),
    (("light", "lamp"), ("Street lighting", "lighting")),
]

# My Maps "Completed" folder pins are old fly-tips; "Outstanding Issues" are open.
FOLDER_STATUS = {"completed": "completed", "outstanding issues": "open"}

# The tool's own KML folders are named by broad kind; the council category that
# best stands for each when the pin itself does not say.
FOLDER_CATEGORIES = {
    "pothole": "Potholes / Highway Condition",
    "flytipping": "Flytipping",
    "drain": "Blocked/Damaged",
    "vegetation": "Restricted Visibility / Overgrown / Overhanging",
    "lighting": "Street lighting",
    "sign": "Signs",
}


# ----------------------------------------------------------------- fetching

def kml_url_for(mid: str) -> str:
    return f"https://www.google.com/maps/d/kml?mid={mid}&forcekml=1"


def mid_from(text: str) -> str:
    """The map id out of any My Maps URL (edit, viewer or kml), or the id itself."""
    text = (text or "").strip()
    if "mid=" in text:
        q = parse_qs(urlparse(text).query)
        if q.get("mid"):
            return q["mid"][0]
    return text


def fetch_kml(mid: str, timeout: int = 60) -> bytes:
    """Download the map as KML. Follows the NetworkLink stub that an exported
    KMZ (or an old-style URL) hands back instead of the data."""
    import requests
    data = requests.get(kml_url_for(mid), timeout=timeout,
                        headers={"User-Agent": "NNC-Reporter/1.0"}).content
    data = unwrap_kmz(data)
    for _ in range(2):
        root = ET.fromstring(data)
        href = root.find(f".//{KML_NS}NetworkLink/{KML_NS}Link/{KML_NS}href")
        if href is None or root.find(f".//{KML_NS}Placemark") is not None:
            break
        link = href.text.strip()
        if "forcekml" not in link:
            link += ("&" if "?" in link else "?") + "forcekml=1"
        data = unwrap_kmz(requests.get(link, timeout=timeout).content)
    return data


def unwrap_kmz(data: bytes) -> bytes:
    """A KMZ is a zip with doc.kml inside; a KML is just the bytes."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise ValueError("the KMZ holds no .kml file")
            return z.read(names[0])
    return data


# ------------------------------------------------------------------ parsing

def _text(el, tag: str) -> str:
    x = el.find(f"{KML_NS}{tag}") if el is not None else None
    return (x.text or "").strip() if x is not None and x.text else ""


def _strip_html(s: str) -> str:
    s = re.sub(r"<img[^>]*>", "", s or "", flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def parse_style_colour(style_url: str) -> str:
    """'#icon-1857-0F9D58' -> '#0F9D58'. My Maps encodes the colour in the id."""
    m = re.search(r"-([0-9A-Fa-f]{6})(?:-|$)", style_url or "")
    return f"#{m.group(1).upper()}" if m else ""


def parse_placemarks(kml_bytes: bytes) -> list:
    """Every placemark on the map, flattened, with its folder name."""
    root = ET.fromstring(kml_bytes)
    doc = root.find(f"{KML_NS}Document") or root
    out = []

    def walk(container, folder):
        for child in container:
            if child.tag == f"{KML_NS}Folder":
                walk(child, _text(child, "name") or folder)
            elif child.tag == f"{KML_NS}Placemark":
                out.append(_placemark(child, folder))

    walk(doc, "")
    return out


def _placemark(p, folder: str) -> dict:
    ext = {}
    ed = p.find(f"{KML_NS}ExtendedData")
    if ed is not None:
        for d in ed.findall(f"{KML_NS}Data"):
            ext[d.get("name") or ""] = _text(d, "value")
    desc_html = _text(p, "description")
    photos = re.findall(r'<img[^>]+src="([^"]+)"', desc_html, flags=re.I)
    for u in (ext.get("gx_media_links") or "").split():
        if u not in photos:
            photos.append(u)
    photos = [html.unescape(u) for u in photos]

    geom, coords = "", []
    for tag in ("Point", "LineString", "Polygon"):
        g = p.find(f".//{KML_NS}{tag}")
        if g is not None:
            geom = tag
            raw = g.find(f".//{KML_NS}coordinates")
            for triple in (raw.text or "").split():
                bits = triple.split(",")
                if len(bits) >= 2:
                    coords.append((float(bits[0]), float(bits[1])))
            break

    return {
        "folder": folder,
        "name": _text(p, "name").replace("\xa0", " ").strip(),
        "style": _text(p, "styleUrl"),
        "colour": parse_style_colour(_text(p, "styleUrl")),
        "description_html": desc_html,
        "description": _strip_html(desc_html),
        "ext": ext,
        "photos": photos,
        "geom": geom,
        "coords": coords,
    }


# ------------------------------------------------------------- interpreting

def _reference(pm: dict) -> str:
    ref = (pm["ext"].get("reference") or "").strip()
    if ref:
        return ref
    m = REF_RE.match(pm["name"] or "")
    if m:
        return f"{m.group(1)}{m.group(2)}".strip()
    return ""


def _category_kind(pm: dict, reference: str) -> tuple:
    cat = (pm["ext"].get("category") or "").strip()
    if cat:
        return cat, _style_for(cat)
    m = REF_RE.match(reference or "")
    if m and m.group(1).upper() in REF_PREFIXES:
        return REF_PREFIXES[m.group(1).upper()]
    folder = (pm["folder"] or "").lower()
    if m and not m.group(1):
        # A bare number is a highways-site report id; the layer it sits in on the
        # My Map (the tool's own "Pothole" / "Other" folders) says what it was.
        fk = _style_for(folder)
        if fk != "other":
            return FOLDER_CATEGORIES.get(fk, folder.title()), fk
        return "Highways report", "other"
    hay = f"{pm['name']} {pm['description']}".lower()
    for words, result in KEYWORD_KINDS:
        if any(w in hay for w in words):
            return result
    if m:                                   # FLY-style number, unknown prefix
        return "Flytipping", "flytipping"
    if "boundary" in folder or "landmark" in folder:
        return "Landmark", "place"
    return "", "other"


def _status(pm: dict, reference: str = "", kind: str = "") -> str:
    st = (pm["ext"].get("status") or "").strip().lower()
    if st:
        return st
    folder = (pm["folder"] or "").strip().lower()
    if folder in FOLDER_STATUS:
        return FOLDER_STATUS[folder]
    if kind == "place":
        return "landmark"
    # A pin with a council reference that is not filed under "Outstanding" was
    # reported and has since dropped off the tool's own list: history.
    return "completed" if reference else "open"


_DATE_PATTERNS = [
    (re.compile(r"Reported (\d{1,2} \w+ \d{4})"), "%d %B %Y"),
    (re.compile(r"Photographed:? (\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)"), None),
    (re.compile(r"PXL_(\d{8})_(\d{6})"), "PXL"),
]


def _date(pm: dict) -> tuple:
    """(iso date-time or '', how it was found)."""
    val = (pm["ext"].get("photographed") or "").strip()
    if val:
        return val[:16].replace("T", " "), "exact"
    text = pm["description"]
    for rx, fmt in _DATE_PATTERNS:
        m = rx.search(text)
        if not m:
            continue
        try:
            if fmt == "PXL":
                d = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
                return d.strftime("%Y-%m-%d %H:%M"), "exact"
            if fmt is None:
                return m.group(1), "exact"
            d = datetime.strptime(m.group(1), fmt)
            return d.strftime("%Y-%m-%d"), "exact"
        except ValueError:
            continue
    return "", ""


def _detail(pm: dict) -> str:
    """The free text of the pin, without the tool's own 'Category: ...' preamble
    (that is already in its own columns)."""
    d = (pm["ext"].get("description") or pm["description"] or "").strip()
    if "Detail:" in d:
        m = re.search(r"Detail:\s*(.*?)(?:\n(?:Report|Photo file):|\Z)", d, flags=re.S)
        if m:
            d = m.group(1).strip()
    # The old CSV import left "description: X<br>category: <br>..." behind:
    # keep the description's text, drop every label that has nothing after it.
    d = re.sub(r"^description:\s*", "", d, flags=re.I)
    lines = [ln for ln in d.split("\n")
             if not re.match(r"^\s*(description|category|status|reference|what3words|"
                             r"photographed)\s*:\s*$", ln, flags=re.I)]
    return "\n".join(lines).strip()[:1200]


def _field(pm: dict, label: str) -> str:
    m = re.search(rf"{label}:\s*(.+)", pm["description"])
    return m.group(1).strip() if m else ""


def _key(reference: str, pm: dict) -> str:
    if reference:
        return f"{SOURCE}:{reference.upper()}"
    lon, lat = pm["coords"][0] if pm["coords"] else (0.0, 0.0)
    h = hashlib.sha1(f"{pm['name']}|{lat:.6f}|{lon:.6f}".encode()).hexdigest()[:12]
    return f"{SOURCE}:{h}"


# --------------------------------------------------- dating by reference no.

class RefDater:
    """Granicus hands out FLY numbers in sequence, so a pin whose only clue is
    its reference can be placed in time by the references we *do* have dates
    for. It is an estimate - good to a week or two recently, a month or more a
    year back - and every date it produces is flagged as such."""

    def __init__(self, anchors: list):
        # anchors: [(number, datetime)] from reports with both a FLY ref and a date
        self.anchors = sorted(anchors)
        if len(self.anchors) >= 2:
            (n0, d0), (n1, d1) = self.anchors[0], self.anchors[-1]
            days = max((d1 - d0).total_seconds() / 86400, 1.0)
            self.rate = max((n1 - n0) / days, 50_000.0)      # numbers per day
        else:
            self.rate = 270_000.0

    def estimate(self, reference: str):
        m = re.match(r"^FLY(\d+)$", (reference or "").upper())
        if not m or not self.anchors:
            return None
        n = int(m.group(1))
        prev = nxt = None
        for a in self.anchors:
            if a[0] <= n:
                prev = a
            elif nxt is None:
                nxt = a
        if prev and nxt and nxt[0] != prev[0]:
            frac = (n - prev[0]) / (nxt[0] - prev[0])
            return prev[1] + (nxt[1] - prev[1]) * frac
        if prev:
            return prev[1] + timedelta(days=(n - prev[0]) / self.rate)
        return nxt[1] - timedelta(days=(nxt[0] - n) / self.rate)


def _anchors(reports: list) -> list:
    out = []
    for r in reports:
        m = re.match(r"^FLY(\d+)$", (r.get("reference") or "").strip().upper())
        when = r.get("taken_at") or r.get("submitted_at") or ""
        if m and when:
            try:
                out.append((int(m.group(1)), datetime.fromisoformat(when[:19])))
            except ValueError:
                pass
    return out


# ------------------------------------------------------------------ matching

def _norm_ref(s: str) -> str:
    return re.sub(r"\s+", "", (s or "")).upper()


def match_report(pm: dict, reference: str, kind: str, reports: list,
                 by_ref: dict) -> dict | None:
    """The report in the database this pin is a copy of, if any."""
    if reference and _norm_ref(reference) in by_ref:
        return by_ref[_norm_ref(reference)]
    # Position-matching is only for pins the tool exported itself (they carry
    # its ExtendedData), never for hand-placed pins: a hand pin on the same spot
    # as a later report is a different event, not a copy.
    if not pm["coords"] or not (pm["ext"].get("category") or "").strip():
        return None
    lon, lat = pm["coords"][0]
    best, best_d = None, MATCH_METRES + 1
    for r in reports:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        if reference and r.get("reference"):
            continue                       # both have refs and they differ
        d = metres_between(lat, lon, r["lat"], r["lon"])
        if d <= MATCH_METRES and d < best_d:
            rk = _style_for(r.get("category"))
            if rk == kind or kind == "other":
                best, best_d = r, d
    return best


# ------------------------------------------------------------------- import

def import_kml(kml_bytes: bytes, source_label: str = "") -> dict:
    """Bring a map's pins and areas into the active profile's database."""
    placemarks = parse_placemarks(kml_bytes)
    reports = store.all_reports()
    by_ref = {_norm_ref(r["reference"]): r for r in reports if r.get("reference")}
    dater = RefDater(_anchors(reports))
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    summary = {"placemarks": len(placemarks), "matched_reports": [], "pins_added": 0,
               "pins_updated": 0, "areas": 0, "skipped": [], "estimated_dates": 0,
               "duplicate_references": [],
               "photos_to_cache": 0, "source": source_label}
    seen_keys = []

    with store.connect() as con:
        for pm in placemarks:
            if pm["geom"] in ("Polygon", "LineString") and len(pm["coords"]) >= 2:
                _upsert_area(con, pm, now)
                summary["areas"] += 1
                continue
            if pm["geom"] != "Point" or not pm["coords"]:
                summary["skipped"].append(f"{pm['name']!r}: no position")
                continue

            reference = _reference(pm)
            category, kind = _category_kind(pm, reference)
            hit = match_report(pm, reference, kind, reports, by_ref)
            if hit:
                summary["matched_reports"].append(
                    {"pin": pm["name"], "report": hit["id"],
                     "reference": hit.get("reference") or ""})
                continue

            when, how = _date(pm)
            if not when:
                est = dater.estimate(reference)
                if est:
                    when, how = est.strftime("%Y-%m-%d"), "estimated"
                    summary["estimated_dates"] += 1

            lon, lat = pm["coords"][0]
            key = _key(reference, pm)
            if key in seen_keys:
                # The same reference on two pins (it happens by hand). Keep both,
                # and say so, rather than letting one overwrite the other.
                summary["duplicate_references"].append(reference or pm["name"])
                key = f"{key}:{_key('', pm).split(':')[-1]}"
            seen_keys.append(key)
            row = {
                "key": key, "source": SOURCE, "folder": pm["folder"],
                "name": pm["name"][:200], "category": category, "kind": kind,
                "status": _status(pm, reference, kind), "reference": reference,
                "lat": round(lat, 7), "lon": round(lon, 7),
                "street": _field(pm, "Street"), "postcode": _field(pm, "Postcode"),
                "w3w": (pm["ext"].get("what3words") or "").strip().lstrip("/"),
                "photographed": when, "date_source": how,
                "detail": _detail(pm), "report_url": _field(pm, "Report"),
                "photo_url": pm["photos"][0] if pm["photos"] else "",
                "photo_urls": json.dumps(pm["photos"]),
                "colour": pm["colour"], "icon": pm["style"],
                "raw": json.dumps({"ext": pm["ext"], "html": pm["description_html"][:4000]}),
                "updated_at": now,
            }
            existing = con.execute("SELECT id, photo_url, photo_path, thumb_path "
                                   "FROM map_pins WHERE key=?", (key,)).fetchone()
            if existing:
                if existing["photo_url"] == row["photo_url"] and existing["photo_path"]:
                    row["photo_path"] = existing["photo_path"]
                    row["thumb_path"] = existing["thumb_path"]
                else:
                    row["photo_path"] = row["thumb_path"] = ""
                sets = ",".join(f"{k}=?" for k in row if k != "key")
                con.execute(f"UPDATE map_pins SET {sets} WHERE key=?",
                            [v for k, v in row.items() if k != "key"] + [key])
                summary["pins_updated"] += 1
            else:
                cols = ",".join(row)
                con.execute(f"INSERT INTO map_pins ({cols}) VALUES ({','.join('?' * len(row))})",
                            list(row.values()))
                summary["pins_added"] += 1

        summary["photos_to_cache"] = con.execute(
            "SELECT COUNT(*) FROM map_pins WHERE photo_url<>'' "
            "AND (photo_path IS NULL OR photo_path='')").fetchone()[0]
        summary["pins_total"] = con.execute("SELECT COUNT(*) FROM map_pins").fetchone()[0]

    summary["matched"] = len(summary["matched_reports"])
    return summary


def _upsert_area(con, pm: dict, now: str) -> None:
    ring = [[lon, lat] for lon, lat in pm["coords"]]
    if pm["geom"] == "Polygon":
        if ring[0] != ring[-1]:
            ring.append(ring[0])
        geometry = {"type": "Polygon", "coordinates": [ring]}
    else:
        geometry = {"type": "LineString", "coordinates": ring}
    name = pm["name"] or "Area"
    low = name.lower()
    kind = "ward-sketch" if "ward" in low else "area"
    slug = re.sub(r"[^a-z0-9]+", "-", low).strip("-")
    key = f"{SOURCE}:area:{slug}"
    row = (SOURCE, name, kind, pm["folder"], json.dumps(geometry), pm["colour"],
           pm["description"][:1000], now)
    con.execute(
        "INSERT INTO map_areas (key, source, name, kind, folder, geojson, colour, "
        "detail, updated_at) VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET "
        "name=excluded.name, kind=excluded.kind, folder=excluded.folder, "
        "geojson=excluded.geojson, colour=excluded.colour, detail=excluded.detail, "
        "updated_at=excluded.updated_at", (key, *row))


def import_from_mid(mid: str) -> dict:
    data = fetch_kml(mid_from(mid))
    return import_kml(data, source_label=f"My Map {mid_from(mid)}")


# ------------------------------------------------------------------- photos

_cache_lock = threading.Lock()
_cache_state = {"running": False, "done": 0, "failed": 0, "total": 0, "last": ""}


def photo_cache_state() -> dict:
    return dict(_cache_state)


def _sized(url: str, px: int) -> str:
    """Google's image host takes a size hint; ask for something sensible rather
    than the 16383px original the KML links to."""
    if "fife=" in url:
        return re.sub(r"fife=s\d+", f"fife=s{px}", url)
    if "googleusercontent.com" in url and "=" not in url.rsplit("/", 1)[-1]:
        return f"{url}=s{px}"
    return url


def cache_photos(data_dir: Path | None = None) -> dict:
    """Download every uncached pin photo into data/pins/. Runs in the background
    after an import; safe to call again - it only fetches what is missing."""
    with _cache_lock:
        if _cache_state["running"]:
            return photo_cache_state()
        _cache_state.update(running=True, done=0, failed=0, total=0, last="")

    def work():
        import requests
        try:
            base = data_dir or config.data_dir()
            folder = base / "pins"
            folder.mkdir(parents=True, exist_ok=True)
            with store.connect() as con:
                todo = [dict(r) for r in con.execute(
                    "SELECT id, key, photo_url FROM map_pins WHERE photo_url<>'' "
                    "AND (photo_path IS NULL OR photo_path='')")]
            _cache_state["total"] = len(todo)
            for r in todo:
                stem = re.sub(r"[^A-Za-z0-9]+", "-", r["key"]).strip("-")
                full = folder / f"{stem}.jpg"
                thumb = folder / f"{stem}_thumb.jpg"
                try:
                    if not full.exists():
                        resp = requests.get(_sized(r["photo_url"], 1600), timeout=40,
                                            headers={"User-Agent": "NNC-Reporter/1.0"})
                        resp.raise_for_status()
                        if not resp.content or b"<html" in resp.content[:200].lower():
                            raise ValueError("not an image")
                        full.write_bytes(resp.content)
                    if not thumb.exists():
                        _thumb(full, thumb)
                    with store.connect() as con:
                        con.execute("UPDATE map_pins SET photo_path=?, thumb_path=? WHERE id=?",
                                    (str(full), str(thumb) if thumb.exists() else str(full),
                                     r["id"]))
                    _cache_state["done"] += 1
                except Exception as e:                      # noqa: BLE001
                    _cache_state["failed"] += 1
                    _cache_state["last"] = f"{r['key']}: {e}"[:200]
        finally:
            _cache_state["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return photo_cache_state()


def _thumb(src: Path, dest: Path, size: int = 480) -> None:
    try:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGB")
            im.thumbnail((size, size))
            im.save(dest, "JPEG", quality=82)
    except Exception:                                        # noqa: BLE001
        pass


# -------------------------------------------------------------------- reads

def pins() -> list:
    with store.connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM map_pins ORDER BY COALESCE(photographed, ''), id")]


def areas() -> list:
    with store.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM map_areas ORDER BY id")]
    for r in rows:
        r["geometry"] = json.loads(r.pop("geojson") or "{}")
    return rows


def pin(pid: int) -> dict | None:
    with store.connect() as con:
        r = con.execute("SELECT * FROM map_pins WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def delete_source(source: str = SOURCE) -> int:
    with store.connect() as con:
        n = con.execute("DELETE FROM map_pins WHERE source=?", (source,)).rowcount
        con.execute("DELETE FROM map_areas WHERE source=?", (source,))
    return n


def colours() -> dict:
    """Pin colours by kind, the KML palette plus one for non-council issues."""
    out = {k: f"#{v}" for k, v in STYLES.items()}
    out.setdefault("issue", "#AD1457")
    out.setdefault("place", "#00838F")
    return out


# ------------------------------------------------ council portal reconciling

PORTAL_SOURCE = "portal"


def _portal_date(s: str) -> str:
    """'08/02/2026 10:31:46' (the portal's format) -> '2026-02-08 10:31'."""
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime((s or "").strip(), fmt).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    return ""


def reconcile_portal(cases: list, cfg: dict | None = None) -> dict:
    """Bring the council portal's "My forms" list (forms.northnorthants.gov.uk/
    MyRequests) into line with the pins.

    Each case is {id, start, status, street, photo, lat_lon, w3w, type, details,
    comments, location_info}. A case whose reference is already a pin gets the
    council's own date (the real one, replacing any estimate), type and text; a
    case the map never had becomes a new pin. Afterwards every FLY pin that still
    only has an estimated date is re-estimated against the now much larger set
    of known dates."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    out = {"cases": len(cases), "updated": 0, "added": 0, "geocoded": [], "no_location": [],
           "redated": 0}
    with store.connect() as con:
        for c in cases:
            ref = (c.get("id") or "").strip().upper()
            if not ref:
                continue
            when = _portal_date(c.get("start") or "")
            status = "completed" if (c.get("status") or "").lower() == "closed" else "open"
            lat = lon = None
            ll = (c.get("lat_lon") or "").strip()
            if "," in ll and ll != ",":
                try:
                    lat, lon = (float(x) for x in ll.split(",")[:2])
                except ValueError:
                    lat = lon = None
            street = re.sub(r"\s+Corby$", "", (c.get("street") or "").strip())
            w3w = (c.get("w3w") or "").strip()
            w3w = re.sub(r"^(https?://w3w\.co/|/+)", "", w3w)
            fly_type = (c.get("type") or "").strip()
            text = (c.get("details") or c.get("comments") or c.get("location_info") or "").strip()
            detail = f"{fly_type}: {text}" if fly_type and text else (fly_type or text)
            photo_note = (c.get("photo") or "").strip()

            row = con.execute("SELECT * FROM map_pins WHERE UPPER(reference)=?", (ref,)).fetchone()
            if row:
                sets = {"photographed": when or row["photographed"],
                        "date_source": "council" if when else row["date_source"],
                        "status": status if row["status"] in ("completed", "open", "") else row["status"],
                        "category": row["category"] or "Flytipping",
                        "street": row["street"] or street,
                        "w3w": row["w3w"] or w3w,
                        "detail": row["detail"] or detail,
                        "updated_at": now}
                if (row["lat"] is None or row["lon"] is None) and lat is not None:
                    sets["lat"], sets["lon"] = round(lat, 7), round(lon, 7)
                try:
                    raw = json.loads(row["raw"] or "{}")
                except ValueError:
                    raw = {}
                raw["portal"] = {"start": c.get("start"), "status": c.get("status"),
                                 "type": fly_type, "details": text, "photo": photo_note,
                                 "street": street, "w3w": w3w, "lat_lon": ll}
                sets["raw"] = json.dumps(raw)
                con.execute("UPDATE map_pins SET " + ",".join(f"{k}=?" for k in sets) + " WHERE id=?",
                            [*sets.values(), row["id"]])
                out["updated"] += 1
                continue

            approx = ""
            if lat is None and street and cfg is not None:
                try:
                    from . import geo
                    hit = (geo.forward_geocode(f"{street}, Corby", cfg, limit=1) or {}).get("results") or []
                    if hit:
                        lat, lon = float(hit[0]["lat"]), float(hit[0]["lon"])
                        approx = " (location approximate: the form had no map pin, so this is the street)"
                        out["geocoded"].append(ref)
                except Exception:                                   # noqa: BLE001
                    pass
            if lat is None:
                out["no_location"].append(ref)
                continue
            con.execute(
                "INSERT INTO map_pins (key, source, folder, name, category, kind, status, reference, "
                "lat, lon, street, postcode, w3w, photographed, date_source, detail, report_url, "
                "photo_url, photo_urls, colour, icon, raw, updated_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{PORTAL_SOURCE}:{ref}", PORTAL_SOURCE, "Council portal", ref, "Flytipping",
                 "flytipping", status, ref, round(lat, 7), round(lon, 7), street, "", w3w,
                 when, "council" if when else "", (detail + approx)[:1200],
                 "https://forms.northnorthants.gov.uk/MyRequests", "", "[]", "", "",
                 json.dumps({"portal": c}), now))
            out["added"] += 1

        # Re-estimate: every FLY reference with a real date is now an anchor.
        anchors = _anchors(store.all_reports())
        for r in con.execute("SELECT reference, photographed FROM map_pins "
                             "WHERE date_source IN ('exact','council') AND reference LIKE 'FLY%'"):
            m = re.match(r"^FLY(\d+)$", r["reference"].upper())
            if m and r["photographed"]:
                try:
                    anchors.append((int(m.group(1)), datetime.fromisoformat(r["photographed"][:16])))
                except ValueError:
                    pass
        dater = RefDater(anchors)
        for r in con.execute("SELECT id, reference FROM map_pins WHERE reference LIKE 'FLY%' "
                             "AND (date_source='estimated' OR date_source='' OR date_source IS NULL)").fetchall():
            est = dater.estimate(r["reference"].upper())
            if est:
                con.execute("UPDATE map_pins SET photographed=?, date_source='estimated', updated_at=? WHERE id=?",
                            (est.strftime("%Y-%m-%d"), now, r["id"]))
                out["redated"] += 1
        out["anchors"] = len(anchors)
    return out


# ------------------------------------------------- highways site reconciling

HIGHWAYS_SOURCE = "highways"
HIGHWAYS_BASE = "https://highways.northnorthants.gov.uk"

# FixMyStreet's public states -> the map's buckets.
HIGHWAYS_STATES = {
    "fixed": "completed", "closed": "completed", "no further action": "completed",
    "not responsible": "completed", "duplicate": "completed", "unable to fix": "completed",
    "investigating": "open", "in progress": "open", "action scheduled": "open",
    "planned": "open", "open": "open", "confirmed": "open", "unknown": "open",
}


def _highways_date(meta: str) -> str:
    """'... by Alex Lock at 17:02, Sunday 12 July 2026 (...)' -> '2026-07-12 17:02'."""
    m = re.search(r"at (\d{1,2}:\d{2}), \w+ (\d{1,2} \w+ \d{4})", meta or "")
    if not m:
        return ""
    try:
        return datetime.strptime(f"{m.group(2)} {m.group(1)}", "%d %B %Y %H:%M").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ""


def reconcile_highways(reports: list) -> dict:
    """Bring the highways site's "Your reports" list into line with the pins.

    Each report is {id, title, desc, meta, state, lat, lon, photos, updates}
    as read from highways.northnorthants.gov.uk/report/<id>. A report the tool
    made itself (its id is in reports.db) only has its council state noted; one
    that is already a pin is updated; anything else becomes a pin."""
    now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    by_ref = {_norm_ref(r["reference"]): r for r in store.all_reports() if r.get("reference")}
    out = {"reports": len(reports), "tool_reports": 0, "updated": 0, "added": 0, "skipped": []}
    with store.connect() as con:
        for h in reports:
            ref = str(h.get("id") or "").strip()
            if not ref:
                continue
            state = (h.get("state") or "").strip()
            bucket = HIGHWAYS_STATES.get(state.lower(), "open")
            when = _highways_date(h.get("meta") or "")
            cat = (re.search(r"in the (.+?) category", h.get("meta") or "") or [None, ""])[1]
            kind = _style_for(cat)
            outcome = ""
            for u in reversed(h.get("updates") or []):
                if "Investigation:" in u:
                    outcome = u.split(" - ")[0].strip()[:160]
                    break
            detail = (h.get("desc") or "").strip()
            if outcome:
                detail = f"{detail}\nCouncil: {state}. {outcome}".strip()
            photos = [HIGHWAYS_BASE + p.split("?")[0] for p in (h.get("photos") or [])]
            url = f"{HIGHWAYS_BASE}/report/{ref}"

            if ref in by_ref:
                r = by_ref[ref]
                store.update(r["id"], {"council_status": state, "council_checked_at": now,
                                       "report_url": r.get("report_url") or url})
                out["tool_reports"] += 1
                continue

            row = con.execute("SELECT * FROM map_pins WHERE reference=?", (ref,)).fetchone()
            if row:
                try:
                    raw = json.loads(row["raw"] or "{}")
                except ValueError:
                    raw = {}
                raw["highways"] = {"state": state, "meta": h.get("meta"), "updates": h.get("updates")}
                sets = {"name": (h.get("title") or row["name"])[:200], "category": cat or row["category"],
                        "kind": kind if cat else row["kind"], "status": bucket,
                        "photographed": when or row["photographed"],
                        "date_source": "council" if when else row["date_source"],
                        "detail": detail or row["detail"], "report_url": url,
                        "photo_url": row["photo_url"] or (photos[0] if photos else ""),
                        "photo_urls": json.dumps(photos) if photos else row["photo_urls"],
                        "raw": json.dumps(raw), "updated_at": now}
                if (row["lat"] is None) and h.get("lat"):
                    sets["lat"], sets["lon"] = float(h["lat"]), float(h["lon"])
                con.execute("UPDATE map_pins SET " + ",".join(f"{k}=?" for k in sets) + " WHERE id=?",
                            [*sets.values(), row["id"]])
                out["updated"] += 1
                continue

            if not h.get("lat") or not h.get("lon"):
                out["skipped"].append(f"{ref}: no location")
                continue
            con.execute(
                "INSERT INTO map_pins (key, source, folder, name, category, kind, status, reference, "
                "lat, lon, street, postcode, w3w, photographed, date_source, detail, report_url, "
                "photo_url, photo_urls, colour, icon, raw, updated_at) VALUES "
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"{HIGHWAYS_SOURCE}:{ref}", HIGHWAYS_SOURCE, "Highways site", (h.get("title") or ref)[:200],
                 cat or "Highways report", kind, bucket, ref, round(float(h["lat"]), 7),
                 round(float(h["lon"]), 7), (h.get("street") or ""), "", "", when,
                 "council" if when else "", detail[:1200], url, photos[0] if photos else "",
                 json.dumps(photos), "", "", json.dumps({"highways": h}), now))
            out["added"] += 1
    return out
