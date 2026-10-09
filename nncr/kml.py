"""Export pins for the Google My Maps layer.

Google My Maps has no write API, so the tool writes a KML (and a CSV) that you
import as a layer. Import is two clicks and replaces the layer's contents, so the
file is always written as a complete snapshot of every report, not a delta.
"""
import csv
import html
import shutil
from datetime import datetime
from pathlib import Path

from . import config, store

# My Maps pin colours by broad type.
STYLES = {
    "flytipping": "E65100",
    "pothole": "C62828",
    "drain": "0277BD",
    "vegetation": "2E7D32",
    "lighting": "F9A825",
    "sign": "6A1B9A",
    "other": "455A64",
}


def _style_for(category: str) -> str:
    c = (category or "").lower()
    if "flytip" in c or "rubbish" in c or "cleansing" in c or "dog" in c:
        return "flytipping"
    if "pothole" in c or "road" in c or "kerb" in c or "slab" in c or "footway" in c:
        return "pothole"
    if "drain" in c or "flood" in c or "ditch" in c:
        return "drain"
    if "vegetation" in c or "tree" in c or "weed" in c or "verge" in c:
        return "vegetation"
    if "light" in c or "signal" in c or "lamp" in c:
        return "lighting"
    if "sign" in c or "graffiti" in c or "flypost" in c or "bollard" in c:
        return "sign"
    return "other"


def _esc(v) -> str:
    return html.escape(str(v)) if v not in (None, "") else ""


def photo_name(r: dict) -> str:
    """Stable, human-sortable filename for the exported copy of a photo."""
    import re
    bits = [str(r.get("id")), r.get("category") or "report", r.get("street") or ""]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", " ".join(b for b in bits if b)).strip("-")
    return f"{slug[:70]}.jpg"


def _placemark(r: dict) -> str:
    style = _style_for(r.get("category"))
    when = (r.get("taken_at") or r.get("created_at") or "")[:16].replace("T", " ")
    # Google My Maps can only show an image it can fetch over the web, so this is
    # the council's published copy. The local file is named underneath for the
    # times you'd rather attach it to the pin by hand.
    img = ""
    if r.get("photo_url"):
        img = (f'<img src="{_esc(r["photo_url"])}" width="420" /><br/>'
               f'<a href="{_esc(r["photo_url"])}">open photo</a><br/><br/>')
    desc_rows = [
        ("Category", r.get("category")),
        ("Status", r.get("status")),
        ("Reference", r.get("reference")),
        ("Photographed", when),
        ("Street", r.get("street")),
        ("Postcode", r.get("postcode")),
        ("What3Words", f"///{r['w3w']}" if r.get("w3w") else None),
        ("Detail", r.get("detail")),
        ("Report", r.get("report_url")),
        ("Photo file", photo_name(r) if r.get("photo_path") else None),
    ]
    body = img + "".join(f"<b>{k}:</b> {_esc(v)}<br/>" for k, v in desc_rows if v)
    name = r.get("title") or r.get("category") or "Report"
    return f"""    <Placemark>
      <name>{_esc(name[:100])}</name>
      <styleUrl>#s_{style}</styleUrl>
      <description><![CDATA[{body}]]></description>
      <ExtendedData>
        <Data name="category"><value>{_esc(r.get('category'))}</value></Data>
        <Data name="status"><value>{_esc(r.get('status'))}</value></Data>
        <Data name="reference"><value>{_esc(r.get('reference'))}</value></Data>
        <Data name="what3words"><value>{_esc(r.get('w3w'))}</value></Data>
        <Data name="photographed"><value>{_esc(when)}</value></Data>
      </ExtendedData>
      <Point><coordinates>{r['lon']:.7f},{r['lat']:.7f},0</coordinates></Point>
    </Placemark>
"""


def build_kml(rows: list) -> str:
    styles = "".join(f"""    <Style id="s_{k}">
      <IconStyle><color>ff{v[4:6]}{v[2:4]}{v[0:2]}</color><scale>1.1</scale>
        <Icon><href>https://maps.google.com/mapfiles/kml/shapes/placemark_circle.png</href></Icon>
      </IconStyle>
    </Style>
""" for k, v in STYLES.items())

    folders = {}
    for r in rows:
        folders.setdefault(_style_for(r.get("category")), []).append(r)

    body = ""
    for fname, frows in sorted(folders.items()):
        marks = "".join(_placemark(r) for r in frows)
        body += f"  <Folder>\n    <name>{fname.title()} ({len(frows)})</name>\n{marks}  </Folder>\n"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
  <name>NNC Reports - {datetime.now():%Y-%m-%d %H:%M}</name>
  <description>Issues reported to North Northamptonshire Council ({len(rows)} pins)</description>
{styles}{body}</Document>
</kml>
"""


def export(cfg: dict, ids: list | None = None,
           include_unsubmitted: bool = True, mark: bool = True) -> dict:
    """Write the KML + CSV. Pass `ids` to export only those reports.

    The KML file only ever holds the current selection, so writing it moves things
    along: whatever was in the previous file has by now been imported into My Maps
    and becomes "completed", while this selection becomes "mapped".
    """
    rows = [r for r in store.all_reports()
            if r.get("lat") is not None and r.get("lon") is not None
            and r.get("status") != "skipped"]
    if ids:
        wanted = {int(i) for i in ids}
        rows = [r for r in rows if r["id"] in wanted]
    elif not include_unsubmitted:
        rows = [r for r in rows if r["status"] in store.DONE_STATUSES]

    moved, marked = [], []
    if mark:
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        here = {r["id"] for r in rows}
        for r in store.all_reports():
            if r["status"] == "mapped" and r["id"] not in here:
                store.update(r["id"], {"status": "completed", "completed_at": now})
                moved.append(r["id"])
        for r in rows:
            # Anything that has been submitted and is in this file is "on the map",
            # including a completed one you have chosen to re-export.
            if r["status"] in store.DONE_STATUSES:
                store.update(r["id"], {"status": "mapped", "mapped_at": now,
                                       "completed_at": ""})
                r["status"] = "mapped"          # so the file itself says so
                marked.append(r["id"])

    out_dir = config.map_export_dir(cfg)
    kml_path = out_dir / (cfg["export"].get("kml_filename") or "NNC_Reports.kml")
    csv_path = out_dir / (cfg["export"].get("csv_filename") or "NNC_Reports.csv")

    kml_path.write_text(build_kml(rows), encoding="utf-8")
    photos_info = copy_photos(rows, out_dir, cfg)

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["Name", "Latitude", "Longitude", "Category", "Status", "Reference",
                    "Photographed", "Street", "Postcode", "What3Words", "Detail",
                    "Report URL", "Photo URL", "Photo file"])
        for r in rows:
            w.writerow([
                r.get("title") or r.get("category") or "Report",
                f"{r['lat']:.7f}", f"{r['lon']:.7f}",
                r.get("category") or "", r.get("status") or "",
                r.get("reference") or "",
                (r.get("taken_at") or r.get("created_at") or "")[:16].replace("T", " "),
                r.get("street") or "", r.get("postcode") or "",
                f"///{r['w3w']}" if r.get("w3w") else "",
                (r.get("detail") or "").replace("\n", " ")[:900],
                r.get("report_url") or "",
                r.get("photo_url") or "",
                photo_name(r) if r.get("photo_path") else "",
            ])

    return {"kml": str(kml_path), "csv": str(csv_path), "pins": len(rows),
            "now_mapped": marked, "moved_to_completed": moved, **photos_info}


def copy_photos(rows: list, out_dir: Path, cfg: dict) -> dict:
    """Put a named copy of each photo beside the map files.

    My Maps will only display an image it can fetch over the web, so pins carry the
    council's published photo URL where we have one. These local copies are for
    attaching to a pin by hand (Google lets you upload one image per place), and
    for anything not submitted yet.
    """
    from . import photos as photo_tools

    folder = out_dir / (cfg["export"].get("photo_folder") or "NNC_Reports_photos")
    copied, missing, with_url = 0, 0, 0
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return {"photo_folder": None, "photos_copied": 0, "photos_missing": len(rows)}

    for r in rows:
        if r.get("photo_url"):
            with_url += 1
        src = Path(r["photo_path"]) if r.get("photo_path") else None
        if not src or not src.exists():
            missing += 1
            continue
        try:
            jpeg = photo_tools.to_jpeg_for_upload(src, config.data_dir())
            dest = folder / photo_name(r)
            if not dest.exists() or dest.stat().st_size != jpeg.stat().st_size:
                shutil.copy2(jpeg, dest)
            copied += 1
        except Exception:
            missing += 1

    return {"photo_folder": str(folder), "photos_copied": copied,
            "photos_missing": missing, "photos_with_public_url": with_url}
