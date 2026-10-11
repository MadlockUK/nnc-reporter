"""The shared, reportable layer: everyone's submitted reports, on one map.

Each person's reports live in their own database. This module reads them all
(read-only), keeps only what has gone to the council - drafts are private - and
tags every report with the ward, town council and town council ward it falls in,
so the layer can be counted, charted and exported by area.

Whose report it was is shown by name only where that person has said so on their
account page (or to admins, who run the group); otherwise it reads "Team member".
"""
import csv
import io
import sqlite3
import threading
import time
from pathlib import Path

from .. import kml, store
from ..geo import metres_between
from . import auth, boundaries, db

_lock = threading.Lock()
_cache: dict = {}                    # uid -> (db mtime, boundaries version, rows)

SHARED_FIELDS = ("id", "lat", "lon", "taken_at", "created_at", "submitted_at", "street",
                 "locality", "postcode", "w3w", "service", "category", "cat_group",
                 "title", "detail", "status", "reference", "report_url", "photo_url",
                 "council_status", "council_checked_at", "thumb_path", "photo_path")


def _rows_for(u: dict) -> list:
    path: Path = auth.user_dir(u["id"]) / "reports.db"
    if not path.exists():
        return []
    try:
        mtime = path.stat().st_mtime
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            mtime = max(mtime, wal.stat().st_mtime)
    except OSError:
        return []
    version = boundaries.version()
    with _lock:
        hit = _cache.get(u["id"])
        if hit and hit[0] == mtime and hit[1] == version:
            return hit[2]
    try:
        con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        con.row_factory = sqlite3.Row
        have = {r[1] for r in con.execute("PRAGMA table_info(reports)")}
        cols = [c for c in SHARED_FIELDS if c in have]
        marks = ",".join("?" * len(store.DONE_STATUSES))
        rows = [dict(r) for r in con.execute(
            f"SELECT {','.join(cols)} FROM reports WHERE status IN ({marks}) "
            f"AND lat IS NOT NULL AND lon IS NOT NULL", store.DONE_STATUSES)]
        con.close()
    except sqlite3.Error:
        return []
    for r in rows:
        r.update(boundaries.locate(r["lat"], r["lon"]))
    with _lock:
        _cache[u["id"]] = (mtime, version, rows)
    return rows


def collect(viewer: dict | None = None) -> list:
    """Every shared report, newest first, labelled with its reporter as the
    viewer is allowed to see them."""
    out = []
    admin = bool(viewer and viewer.get("role") == "admin")
    for u in db.users():
        if u["status"] == "invited":
            continue
        prof = db.profile(u)
        named = admin or prof.get("share_name") or (viewer and viewer["id"] == u["id"])
        label = (prof.get("full_name") or u["name"] or "Team member") if named else "Team member"
        for r in _rows_for(u):
            row = dict(r)
            row["uid"] = u["id"]
            row["key"] = f"u{u['id']}-{r['id']}"
            row["reporter"] = label
            row["mine"] = bool(viewer and viewer["id"] == u["id"])
            out.append(row)
    out.sort(key=lambda r: r.get("submitted_at") or r.get("taken_at") or
             r.get("created_at") or "", reverse=True)
    return out


def nearby(lat: float, lon: float, category: str, exclude_uid: int,
           metres: float = 25.0) -> list:
    """Someone else in the group already reported this category within X metres -
    the duplicate the one-person tool could never see."""
    if not category:
        return []
    hits = []
    for u in db.users():
        if u["id"] == exclude_uid or u["status"] == "invited":
            continue
        for r in _rows_for(u):
            if (r.get("category") or "").lower() != category.lower():
                continue
            d = metres_between(lat, lon, r["lat"], r["lon"])
            if d <= metres:
                hits.append({"distance_m": round(d, 1), "reference": r.get("reference"),
                             "submitted_at": r.get("submitted_at")})
    return sorted(hits, key=lambda h: h["distance_m"])


# ------------------------------------------------------------ outputs

def features(rows: list) -> dict:
    """The layer as GeoJSON, in the shape the interactive map expects."""
    from ..webmap import _bucket
    feats = []
    for r in rows:
        when = (r.get("taken_at") or r.get("created_at") or "")[:16].replace("T", " ")
        photo = r.get("photo_url") or ""
        full = photo
        if not photo and (r.get("thumb_path") or r.get("photo_path")):
            photo = full = f"/api/org/photo/{r['uid']}/{r['id']}"
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(float(r["lon"]), 7),
                                                          round(float(r["lat"]), 7)]},
            "properties": {
                "id": r["key"], "src": "report",
                "title": (r.get("title") or r.get("category") or "Report")[:120],
                "category": r.get("category") or "",
                "kind": kml._style_for(r.get("category")),
                "status": _bucket(r.get("status")),
                "council": r.get("council_status") or "",
                "reference": r.get("reference") or "",
                "report_url": r.get("report_url") or "",
                "photo": photo, "photo_full": full,
                "street": r.get("street") or "", "locality": r.get("locality") or "",
                "postcode": r.get("postcode") or "", "w3w": r.get("w3w") or "",
                "when": when, "when_est": False, "folder": "",
                "detail": (r.get("detail") or "").replace("\r", "")[:400],
                "reporter": r.get("reporter") or "",
                "ward": r.get("ward") or "", "ward_code": r.get("ward_code") or "",
                "parish": r.get("parish") or "",
                "town_council": bool(r.get("town_council")),
                "parish_ward": r.get("parish_ward") or "",
            }})
    return {"type": "FeatureCollection", "features": feats}


GROUPS = {
    "ward": ("ward", "Unitary ward"),
    "town_council": ("parish", "Town council"),
    "parish": ("parish", "Parish"),
    "parish_ward": ("parish_ward", "Town council ward"),
    "category": ("category", "Category"),
    "kind": ("kind", "Type"),
    "council_status": ("council_status", "Council's position"),
    "month": ("month", "Month"),
    "reporter": ("reporter", "Reporter"),
}


def filtered(rows: list, since: str = "", until: str = "", category: str = "",
             ward: str = "", parish: str = "") -> list:
    out = []
    for r in rows:
        d = (r.get("submitted_at") or r.get("taken_at") or r.get("created_at") or "")[:10]
        if since and d and d < since:
            continue
        if until and d and d > until:
            continue
        if category and (r.get("category") or "") != category:
            continue
        if ward and (r.get("ward") or "") != ward:
            continue
        if parish and (r.get("parish") or "") != parish:
            continue
        out.append(r)
    return out


def summary(rows: list, by: str) -> dict:
    field, label = GROUPS.get(by, GROUPS["ward"])
    counts: dict = {}
    for r in rows:
        if by == "town_council" and not r.get("town_council"):
            key = "(not in a town council)"
        elif by == "month":
            key = (r.get("submitted_at") or r.get("taken_at") or r.get("created_at")
                   or "")[:7] or "(undated)"
        elif by == "kind":
            key = kml._style_for(r.get("category"))
        elif by == "parish_ward" and not r.get("parish_ward"):
            key = f"{r.get('parish') or 'Outside a town council'} (no wards)" \
                if r.get("parish") else "(not in a town council ward)"
        else:
            key = r.get(field) or "(unknown)"
        c = counts.setdefault(key, {"key": key, "count": 0, "fixed": 0, "open": 0,
                                    "streets": {}})
        c["count"] += 1
        state = (r.get("council_status") or "").lower()
        if "fixed" in state or "completed" in state:
            c["fixed"] += 1
        elif not any(w in state for w in ("closed", "no further", "duplicate", "unable",
                                          "not responsible")):
            c["open"] += 1
        if r.get("street"):
            c["streets"][r["street"]] = c["streets"].get(r["street"], 0) + 1
    items = []
    for c in counts.values():
        top = sorted(c.pop("streets").items(), key=lambda kv: -kv[1])[:3]
        c["top_streets"] = ", ".join(f"{s} ({n})" for s, n in top)
        items.append(c)
    items.sort(key=lambda c: (c["key"] if by == "month" else "", -c["count"], c["key"]))
    if by == "month":
        items.sort(key=lambda c: c["key"])
    return {"by": by, "label": label, "total": len(rows), "rows": items,
            "generated": time.strftime("%Y-%m-%d %H:%M")}


def to_csv(rows: list) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Reference", "Category", "Title", "Street", "Locality", "Postcode",
                "What3Words", "Latitude", "Longitude", "Submitted", "Photographed",
                "Council status", "Unitary ward", "Parish / town", "Town council",
                "Town council ward", "Reporter", "Report URL", "Description"])
    for r in rows:
        w.writerow([r.get("reference") or "", r.get("category") or "", r.get("title") or "",
                    r.get("street") or "", r.get("locality") or "", r.get("postcode") or "",
                    f"///{r['w3w']}" if r.get("w3w") else "",
                    f"{r['lat']:.6f}", f"{r['lon']:.6f}",
                    (r.get("submitted_at") or "")[:16].replace("T", " "),
                    (r.get("taken_at") or "")[:16].replace("T", " "),
                    r.get("council_status") or "", r.get("ward") or "",
                    r.get("parish") or "", "yes" if r.get("town_council") else "",
                    r.get("parish_ward") or "", r.get("reporter") or "",
                    r.get("report_url") or "",
                    (r.get("detail") or "").replace("\n", " ")[:900]])
    return buf.getvalue()


def summary_csv(s: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([s["label"], "Reports", "Still open", "Fixed", "Most-reported streets"])
    for r in s["rows"]:
        w.writerow([r["key"], r["count"], r["open"], r["fixed"], r["top_streets"]])
    w.writerow(["Total", s["total"], "", "", ""])
    return buf.getvalue()


def to_kml(rows: list) -> str:
    copy = []
    for r in rows:
        x = dict(r)
        x["photo_path"] = ""                 # no local file names in a shared layer
        copy.append(x)
    return kml.build_kml(copy)
