"""The reporting tool itself, per signed-in person.

These are the endpoints of the original single-user app, unchanged in what they
do. The difference is who they do it for: the gatekeeper has bound the signed-in
person's own data folder to the request (config.use_user), so `store`, `photos`,
`kml` and the rest read and write only that person's reports - and `cfg` is
built from their account, not from a shared config file.

Every route here needs the editor role; viewers see the shared map only.
"""
import contextvars
import io
import json
import threading
import time
import uuid
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, Response,
                               StreamingResponse)
from pydantic import BaseModel

from .. import (categories, classify, config, dashboard, geo, kml, mymaps, photos,
                routing, store, submit, track, webmap)
from . import aggregate, auth, boundaries, db, settings, usercfg

router = APIRouter(dependencies=[Depends(auth.need("editor"))])

EMERGENCY_NOTE = ("NNC asks for emergencies (flooding, fallen trees) to be phoned in: "
                  "0300 126 3000, or 01604 651074 out of hours")


def cfg_of(request: Request) -> dict:
    return usercfg.build(auth.me(request))


def _within(path: Path) -> bool:
    """Is this file inside the signed-in person's own folder? Paths come from
    their database, but an imported database could hold anything."""
    try:
        return config.data_dir().resolve() in Path(path).resolve().parents
    except OSError:
        return False


def _duplicates(lat, lon, category, u) -> list:
    out = []
    mine = store.nearby_submitted(lat, lon, category) if category else []
    if mine:
        out.append(f"you already reported this category {mine[0]['distance_m']}m "
                   f"away (#{mine[0]['id']})")
    theirs = aggregate.nearby(lat, lon, category, u["id"]) if category else []
    if theirs:
        t = theirs[0]
        out.append(f"someone else in the group already reported this category "
                   f"{t['distance_m']}m away"
                   + (f" (council ref {t['reference']})" if t.get("reference") else ""))
    return out


# ------------------------------------------------------------------ intake

def process_file(src: Path, original_name: str, cfg: dict, u: dict) -> dict:
    warnings = []
    if src.suffix.lower() not in photos.SUPPORTED:
        return {"skipped": original_name, "reason": f"unsupported file type {src.suffix}"}

    sha = photos.sha256(src)
    existing = store.by_sha(sha)
    if existing:
        return {"skipped": original_name,
                "reason": f"already in the list as #{existing['id']} "
                          f"({existing['status']})", "id": existing["id"]}

    meta = photos.read_metadata(src)
    if meta.get("width") is None:
        return {"skipped": original_name, "reason": meta.get("gps_error")
                or "not a readable image"}
    if meta["lat"] is None:
        return {"skipped": original_name,
                "reason": meta.get("gps_error") or "no GPS data in this photo"}
    if not photos.in_north_northants(meta["lat"], meta["lon"]):
        warnings.append("coordinates look to be outside North Northamptonshire")

    stored = photos.store_photo(src, sha, config.data_dir())
    thumb = photos.make_thumb(stored, sha, config.data_dir())
    ready, shrunk = photos.prepare_upload(stored, config.data_dir())
    if shrunk.get("problem"):
        warnings.append(shrunk["problem"])

    place = geo.describe(meta["lat"], meta["lon"], cfg)
    place["lat"], place["lon"] = meta["lat"], meta["lon"]
    if place.get("w3w_error"):
        warnings.append(f"What3Words: {place['w3w_error']}")
    if place.get("geo_error"):
        warnings.append(f"Street lookup: {place['geo_error']}")
    if place.get("street_is_nearest"):
        warnings.append(f"this spot has no street of its own - using the nearest "
                        f"road, {place['street_is_nearest']}")

    guess = classify.classify(stored, place, cfg)
    if guess.get("error"):
        warnings.append(guess["error"])
        category, group, title, detail = "", "", "", ""
        conf, sev, notes = 0.0, "", ""
    else:
        category = guess["category"]
        group = guess.get("group", "")
        title = guess["title"]
        detail = classify.compose_detail(guess["detail"], place, meta["taken_at"])
        conf = guess["confidence"]
        sev = guess.get("severity", "")
        notes = guess.get("notes", "") or ""
        if not category:
            warnings.append("could not match this to a council category - "
                            "pick one from the list below")
        elif conf < 0.5:
            warnings.append("low confidence - check the category")
        if category in categories.EMERGENCY_HINTS:
            warnings.append(EMERGENCY_NOTE)

    service = routing.route_for_report({"category": category, "title": title,
                                        "detail": detail, "notes": notes}, cfg)
    area_warn = routing.grounds_area_warning(
        {"category": category, "title": title, "detail": detail, "notes": notes,
         "locality": place.get("locality")})
    if area_warn:
        warnings.append(area_warn)

    bin_type, bin_issues, in_park = "", [], ""
    if service == "granicus_bins":
        bin_type = classify.normalise_bin_type(guess.get("bin_type"))
        bin_issues = classify.normalise_bin_issues(guess.get("bin_issues"))
        spotted = classify.bin_details_from_text(f"{title} {detail} {notes}")
        bin_type = bin_type or spotted["bin_type"]
        bin_issues = classify.normalise_bin_issues(bin_issues + spotted["bin_issues"])
        park = geo.nearby_park(meta["lat"], meta["lon"], cfg)
        if park.get("error"):
            warnings.append(f"could not check for a park: {park['error']}")
        else:
            in_park = "yes" if park["in_park"] else "no"
            if park["in_park"]:
                warnings.append(f"in or beside {park.get('name') or 'a park'} "
                                f"(within {park['radius']}m)")
        if len(bin_issues) > 1:
            warnings.append(f"{len(bin_issues)} problems with this bin "
                            f"({', '.join(bin_issues)}) - the form takes one at a "
                            f"time, so use Split below")

    warnings += _duplicates(meta["lat"], meta["lon"], category, u)

    rid = store.insert({
        "sha": sha, "original_name": original_name[:200], "photo_path": str(stored),
        "thumb_path": str(thumb) if thumb else "", "lat": meta["lat"], "lon": meta["lon"],
        "taken_at": meta["taken_at"], "street": place.get("street"),
        "locality": place.get("locality"), "postcode": place.get("postcode"),
        "w3w": place.get("w3w"), "service": service, "category": category,
        "cat_group": group, "title": title, "detail": detail, "confidence": conf,
        "severity": sev, "notes": notes, "status": "new",
        "bin_type": bin_type, "bin_issues": json.dumps(bin_issues) if bin_issues else "",
        "in_park": in_park,
        "warnings": "; ".join(warnings),
    })
    return {"added": rid, "name": original_name, "warnings": warnings, "shrunk": shrunk}


# ------------------------------------------------------------------ state

@router.get("/api/state")
def state(request: Request):
    cfg = cfg_of(request)
    u = auth.me(request)
    return {
        "reports": store.all_reports(),
        "categories": categories.load(),
        "update_counts": store.update_counts(),
        "stats": store.stats(),
        "user": auth.public_user(u),
        "config": {
            "reporter": cfg["reporter"],
            "classify_on": classify.available(cfg),
            "w3w_on": bool(cfg["keys"].get("what3words")),
            "map": {"lat": config.centre(cfg)[0], "lon": config.centre(cfg)[1],
                    "zoom": int((cfg.get("map") or {}).get("zoom", 13))},
            "submit_mode": submit.mode(),
            "highways_signin": bool(cfg["highways_login"]["email"]
                                    and cfg["highways_login"]["password"]),
        },
        "my_map_url": _my_map_url(cfg),
        "pins": len(mymaps.pins()),
        "photo_cache": mymaps.photo_cache_state(),
    }


def _my_map_url(cfg: dict) -> str:
    mid = mymaps.mid_from((cfg.get("mymap") or {}).get("mid") or "")
    return f"https://www.google.com/maps/d/u/0/edit?mid={mid}&usp=sharing" if mid else ""


@router.post("/api/upload")
async def upload(request: Request, files: list[UploadFile] = File(...)):
    cfg, u = cfg_of(request), auth.me(request)
    limit = settings.get().max_upload_mb * 1024 * 1024
    if len(files) > 80:
        raise HTTPException(400, "send at most 80 photos at a time")
    inbox = config.data_dir() / "inbox"
    inbox.mkdir(exist_ok=True)
    results = []
    for f in files:
        name = Path(f.filename or "photo").name[:200]
        suffix = Path(name).suffix.lower()
        if suffix not in photos.SUPPORTED:
            results.append({"skipped": name, "reason": f"unsupported file type {suffix}"})
            continue
        tmp = inbox / f"{uuid.uuid4().hex}{suffix}"       # never the browser's name
        size, too_big = 0, False
        with open(tmp, "wb") as out:
            while True:
                chunk = await f.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    too_big = True
                    break
                out.write(chunk)
        try:
            if too_big:
                results.append({"skipped": name, "reason": f"over "
                                f"{settings.get().max_upload_mb}MB"})
                continue
            from starlette.concurrency import run_in_threadpool
            results.append(await run_in_threadpool(process_file, tmp, name, cfg, u))
        except Exception as e:                                  # noqa: BLE001
            results.append({"skipped": name, "reason": str(e)[:200]})
        finally:
            tmp.unlink(missing_ok=True)
    return {"results": results, "reports": store.all_reports()}


@router.get("/api/geocode")
def geocode_search(q: str, request: Request):
    res = geo.forward_geocode(q[:200], cfg_of(request))
    if res.get("error"):
        return JSONResponse({"results": [], "error": res["error"]}, status_code=502)
    return res


class ManualReport(BaseModel):
    category: str
    title: str = ""
    detail: str = ""
    lat: float
    lon: float
    street: str = ""
    locality: str = ""
    postcode: str = ""


@router.post("/api/reports/manual")
def add_manual(body: ManualReport, request: Request):
    cfg, u = cfg_of(request), auth.me(request)
    match = categories.find(body.category)
    if not match:
        raise HTTPException(400, f"unknown category: {body.category}")
    if not (body.detail or "").strip():
        raise HTTPException(400, "describe the issue - the council form needs it")
    if not (-90 <= body.lat <= 90 and -180 <= body.lon <= 180):
        raise HTTPException(400, "those coordinates are not valid")

    warnings = []
    if not photos.in_north_northants(body.lat, body.lon):
        warnings.append("coordinates look to be outside North Northamptonshire")
    place = geo.describe(body.lat, body.lon, cfg)
    street = (body.street or "").strip()[:200] or place.get("street")
    locality = (body.locality or "").strip()[:120] or place.get("locality")
    postcode = (body.postcode or "").strip()[:12] or place.get("postcode")
    if place.get("w3w_error"):
        warnings.append(f"What3Words: {place['w3w_error']}")

    category, group = match["name"], match["group"]
    title = (body.title or "").strip()[:200] or category
    detail = classify.compose_detail(
        body.detail.strip()[:4000],
        {"street": street, "locality": locality, "postcode": postcode,
         "w3w": place.get("w3w"), "lat": body.lat, "lon": body.lon}, None)
    report = {"category": category, "title": title, "detail": detail, "notes": "",
              "locality": locality}
    service = routing.route_for_report(report, cfg)
    area_warn = routing.grounds_area_warning(report)
    if area_warn:
        warnings.append(area_warn)
    if category in categories.EMERGENCY_HINTS:
        warnings.append(EMERGENCY_NOTE)
    warnings += _duplicates(body.lat, body.lon, category, u)
    warnings.append("no photograph - the form's photo question will be answered 'No'")

    rid = store.insert({
        "sha": f"manual-{uuid.uuid4().hex}", "original_name": "", "photo_path": "",
        "thumb_path": "", "lat": body.lat, "lon": body.lon, "taken_at": None,
        "street": street, "locality": locality, "postcode": postcode,
        "w3w": place.get("w3w"), "service": service, "category": category,
        "cat_group": group, "title": title, "detail": detail, "confidence": 0,
        "severity": "", "notes": "", "status": "new", "bin_type": "",
        "bin_issues": "", "in_park": "", "warnings": "; ".join(warnings),
    })
    return {"added": rid, "warnings": warnings, "reports": store.all_reports()}


class Patch(BaseModel):
    category: str | None = None
    title: str | None = None
    detail: str | None = None
    status: str | None = None
    service: str | None = None
    lat: float | None = None
    lon: float | None = None
    reference: str | None = None
    bin_type: str | None = None
    bin_issues: str | None = None
    in_park: str | None = None


STATUSES = {"new", "ready", "awaiting", "submitted", "mapped", "completed"}


@router.patch("/api/reports/{rid}")
def patch(rid: int, body: Patch, request: Request):
    cfg = cfg_of(request)
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "status" in changes and changes["status"] not in STATUSES:
        raise HTTPException(400, "unknown status")
    if "service" in changes and changes["service"] not in submit.SERVICE_KEYS:
        raise HTTPException(400, "unknown council form")
    for k in ("title", "reference", "bin_type", "in_park"):
        if k in changes:
            changes[k] = str(changes[k])[:300]
    if "detail" in changes:
        changes["detail"] = changes["detail"][:6000]
    if "category" in changes:
        match = categories.find(changes["category"])
        if not match:
            raise HTTPException(400, f"unknown category: {changes['category']}")
        changes["category"] = match["name"]
        changes["cat_group"] = match["group"]
        if "service" not in changes:
            merged = {**(store.get(rid) or {}), **changes}
            changes["service"] = routing.route_for_report(merged, cfg)
    if changes.get("status") == "submitted" and not (store.get(rid) or {}).get("submitted_at"):
        changes["submitted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    r = store.update(rid, changes)
    if not r:
        raise HTTPException(404, "not found")
    return r


def _bin_wording(r: dict, issue: str) -> tuple:
    kind = classify.normalise_bin_type(r.get("bin_type")) or "Public bin"
    said = classify.BIN_ISSUE_TITLE.get(issue, issue.lower())
    title = f"{kind} {said}"
    detail = r.get("detail") or ""
    lead = f"The {kind.lower()} {said}."
    if not detail.lower().startswith(lead.lower()):
        detail = f"{lead} {detail}".strip()
    return title, detail


@router.post("/api/reports/{rid}/split")
def split(rid: int):
    r = store.get(rid)
    if not r:
        raise HTTPException(404, "not found")
    issues = classify.normalise_bin_issues(r.get("bin_issues"))
    if len(issues) < 2:
        raise HTTPException(400, "this report only has one problem to raise")
    made = []
    for issue in issues[1:]:
        copy = {f: r.get(f) for f in store.FIELDS if r.get(f) is not None}
        title, detail = _bin_wording(r, issue)
        copy.update({"sha": f"{r['sha']}-{issue.replace(' ', '-')}",
                     "status": "new", "reference": "", "report_url": "",
                     "photo_url": "", "submitted_at": None,
                     "bin_issues": json.dumps([issue]),
                     "title": title, "detail": detail,
                     "warnings": f"split from #{rid}"})
        new_id = store.insert(copy)
        if new_id:
            made.append(new_id)
    title, detail = _bin_wording(r, issues[0])
    store.update(rid, {"bin_issues": json.dumps([issues[0]]),
                       "title": title, "detail": detail})
    return {"split_into": made, "reports": store.all_reports()}


def _purge_files(rows: list) -> int:
    gone = 0
    for r in rows:
        for key in ("photo_path", "thumb_path"):
            p = r.get(key)
            if not p:
                continue
            path = Path(p)
            for target in (path, path.with_name(path.stem + "_upload.jpg")):
                try:
                    if target.exists() and _within(target):
                        target.unlink()
                        gone += 1
                except OSError:
                    pass
        preview = config.data_dir() / "previews" / f"{r.get('id')}.jpg"
        preview.unlink(missing_ok=True)
    return gone


@router.delete("/api/reports/{rid}")
def remove(rid: int):
    if submit.busy() and (store.get(rid) or {}).get("status") == "awaiting":
        raise HTTPException(409, "this report is open in a submission run - cancel it first")
    row = store.delete(rid)
    if row:
        _purge_files([row])
    return {"ok": True}


@router.delete("/api/reports")
def remove_all(include_submitted: bool = False):
    if submit.busy():
        raise HTTPException(409, "a submission run is open - finish or cancel it first")
    rows = store.delete_all(include_submitted)
    files = _purge_files(rows)
    return {"removed": len(rows), "files_deleted": files,
            "kept": len(store.all_reports())}


# ------------------------------------------------------------ submitting

class Ids(BaseModel):
    ids: list[int] = []


def _start(ids: list[int], cfg: dict):
    if submit.mode() == "off":
        raise HTTPException(400, "submitting from the server is switched off here")
    if submit.busy():
        raise HTTPException(409, "a submission run is already open - finish or cancel it")
    if not submit.capacity_left():
        raise HTTPException(503, "the server is busy with other people's submissions - "
                                 "try again in a few minutes")
    if len(ids) > 12:
        raise HTTPException(400, "submit at most 12 reports in one run")
    missing = [i for i in ids if not (store.get(i) or {}).get("category")]
    if missing:
        raise HTTPException(400, "pick a category first for #"
                            + ", #".join(map(str, missing)))
    # The run thread carries this person's data folder with it.
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(submit.submit, ids, cfg), daemon=True).start()
    time.sleep(0.4)
    return {"started": True, "count": len(ids), "log": submit.log_for(),
            "mode": submit.mode()}


@router.post("/api/reports/{rid}/submit")
def start_submit(rid: int, request: Request):
    if not store.get(rid):
        raise HTTPException(404, "not found")
    return _start([rid], cfg_of(request))


@router.post("/api/submit")
def start_submit_many(body: Ids, request: Request):
    if not body.ids:
        raise HTTPException(400, "nothing selected")
    return _start(body.ids, cfg_of(request))


@router.get("/api/log")
def get_batch_log():
    return {"log": submit.log_for(), "busy": submit.busy(), "run": submit.run_state(),
            "reports": store.all_reports()}


@router.get("/api/reports/{rid}/log")
def get_log(rid: int):
    return {"log": submit.log_for(rid), "report": store.get(rid)}


@router.get("/api/run/{rid}/preview")
def run_preview(rid: int):
    path = submit.previews_dir() / f"{int(rid)}.jpg"
    if not path.exists():
        raise HTTPException(404, "no preview")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/api/run/{rid}/frame")
def run_frame(rid: int):
    got = submit.frame(int(rid))
    if got is None:
        raise HTTPException(404, "this form is not open")
    if not got:
        return Response(status_code=204)            # the first picture is on its way
    return Response(got, media_type="image/jpeg")


class RunInput(BaseModel):
    kind: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None
    dy: float | None = None


@router.post("/api/run/{rid}/submit")
def run_approve(rid: int, request: Request):
    """You have checked this form: press its Submit button."""
    if not submit.command("submit", int(rid)):
        raise HTTPException(409, "this form is not open in a run any more")
    db.audit("report.submit_approved", auth.me(request)["id"], ip=auth.client_ip(request),
             detail=f"report #{rid}")
    return {"queued": True}


@router.post("/api/run/{rid}/cancel")
def run_cancel(rid: int):
    if not submit.command("cancel", int(rid)):
        raise HTTPException(409, "this form is not open in a run any more")
    return {"queued": True}


@router.post("/api/run/cancel")
def run_cancel_all():
    if not submit.command("cancel_all"):
        raise HTTPException(409, "no run is open")
    return {"queued": True}


@router.post("/api/run/{rid}/input")
def run_input(rid: int, body: RunInput):
    """The live view: a click, some typing, a key or a scroll on one form."""
    if body.kind not in ("click", "type", "key", "scroll"):
        raise HTTPException(400, "unknown input")
    extra = {k: v for k, v in body.model_dump().items() if v is not None and k != "kind"}
    if body.kind == "click":
        vp = submit.VIEWPORT
        if body.x is None or body.y is None or not (0 <= body.x <= vp["width"]
                                                   and 0 <= body.y <= vp["height"]):
            raise HTTPException(400, "click outside the form")
    if not submit.command(body.kind, int(rid), **extra):
        raise HTTPException(409, "this form is not open in a run any more")
    return {"queued": True}


# ----------------------------------------------------------------- photos

@router.get("/api/photo/{rid}")
def photo(rid: int, full: int = 0):
    r = store.get(rid)
    if not r:
        raise HTTPException(404, "not found")
    p = Path(r["photo_path"] if full else (r["thumb_path"] or r["photo_path"]) or "")
    if not str(p) or not p.exists() or not _within(p):
        raise HTTPException(404, "file missing")
    return FileResponse(p)


# ------------------------------------------------------------ map layers

@router.post("/api/export")
def export(request: Request, body: Ids | None = None):
    """Write your map layer (KML + CSV + named photos), ready to download."""
    cfg = cfg_of(request)
    ids = body.ids if body and body.ids else None
    res = kml.export(cfg, ids=ids)
    return {**res, "kml": "/api/export/kml", "csv": "/api/export/csv",
            "photo_folder": "/api/export/photos.zip"}


def _export_file(cfg: dict, key: str) -> Path:
    return config.map_export_dir(cfg) / cfg["export"][key]


@router.get("/api/export/kml")
def export_kml(request: Request):
    p = _export_file(cfg_of(request), "kml_filename")
    if not p.exists():
        raise HTTPException(404, "write a map layer first")
    return FileResponse(p, media_type="application/vnd.google-earth.kml+xml",
                        filename="NNC_Reports.kml")


@router.get("/api/export/csv")
def export_csv(request: Request):
    p = _export_file(cfg_of(request), "csv_filename")
    if not p.exists():
        raise HTTPException(404, "write a map layer first")
    return FileResponse(p, media_type="text/csv", filename="NNC_Reports.csv")


@router.get("/api/export/photos.zip")
def export_photos(request: Request):
    cfg = cfg_of(request)
    folder = config.map_export_dir(cfg) / cfg["export"]["photo_folder"]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        if folder.exists():
            for p in sorted(folder.glob("*.jpg")):
                z.write(p, p.name)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/zip", headers={
        "Content-Disposition": 'attachment; filename="NNC_Reports_photos.zip"'})


# ------------------------------------------------------- council updates

@router.post("/api/updates/check")
def check_council_updates(request: Request, body: Ids | None = None):
    ids = body.ids if body and body.ids else None
    res = track.check_pages(cfg_of(request), ids)
    return {**res, "reports": store.all_reports(), "update_counts": store.update_counts()}


@router.post("/api/updates/email")
async def reconcile_emails(files: list[UploadFile] = File(default=[]),
                           text: str = Form(default="")):
    results = []
    for f in files[:50]:
        try:
            raw = await f.read(5 * 1024 * 1024 + 1)
            if len(raw) > 5 * 1024 * 1024:
                raise ValueError("over 5MB")
            parsed = track.parse_eml(raw)
            res = track.reconcile_email(parsed["subject"], parsed["date"],
                                        parsed["body"], parsed["sender"])
            res["name"] = f.filename
        except Exception as e:                                  # noqa: BLE001
            res = {"name": f.filename, "matched": None, "note": str(e)[:150]}
        results.append(res)
    if (text or "").strip():
        text = text[:200000]
        lines = text.strip().split("\n", 1)
        res = track.reconcile_email(lines[0][:150], "",
                                    lines[1] if len(lines) > 1 else text)
        res["name"] = "pasted text"
        results.append(res)
    if not results:
        raise HTTPException(400, "drop in .eml files or paste the email text")
    return {"results": results, "reports": store.all_reports(),
            "update_counts": store.update_counts()}


@router.get("/api/reports/{rid}/updates")
def case_updates(rid: int):
    if not store.get(rid):
        raise HTTPException(404, "not found")
    return {"updates": store.updates_for(rid)}


@router.get("/api/dashboard", response_class=HTMLResponse)
def reporting_dashboard(request: Request):
    return dashboard.page(cfg_of(request))


@router.get("/api/webmap", response_class=HTMLResponse)
def interactive_map(request: Request):
    """Your own reports - every stage - and your merged My Map pins."""
    cfg, u = cfg_of(request), auth.me(request)
    fc = webmap.merge(webmap.features(store.all_reports(), live=True),
                      webmap.pin_features(mymaps.pins(), live=True))
    for f in fc["features"]:
        c = f["geometry"]["coordinates"]
        f["properties"].update(boundaries.locate(c[1], c[0]))
    label = (cfg.get("reporter") or {}).get("full_name") or u["name"]
    return webmap.build_html(label, fc, cfg, live=True, areas=mymaps.areas(),
                             ward_fc=boundaries.wards_for_map(),
                             mymap_url=_my_map_url(cfg),
                             boundaries=boundaries.map_layers(),
                             token_url="/api/mapbox/token")


# ------------------------------------------------------ My Map pins

class PinImport(BaseModel):
    url: str | None = None


@router.post("/api/pins/import")
def import_pins(request: Request, body: PinImport | None = None):
    cfg = cfg_of(request)
    mid = mymaps.mid_from((body.url if body and body.url else "")
                          or (cfg.get("mymap") or {}).get("mid") or "")
    if not mid or not all(c.isalnum() or c in "-_." for c in mid) or len(mid) > 120:
        raise HTTPException(400, "no My Map set: add your My Map link on your account "
                                 "page (or paste a My Maps link)")
    try:
        summary = mymaps.import_from_mid(mid)
    except Exception as e:                                       # noqa: BLE001
        raise HTTPException(502, f"could not fetch or read the My Map: {e}")
    if summary.get("photos_to_cache"):
        mymaps.cache_photos()
    return summary


@router.post("/api/pins/import/file")
async def import_pins_file(file: UploadFile = File(...)):
    data = await file.read(30 * 1024 * 1024 + 1)
    if len(data) > 30 * 1024 * 1024:
        raise HTTPException(400, "that file is over 30MB")
    try:
        summary = mymaps.import_kml(mymaps.unwrap_kmz(data), source_label=file.filename)
    except Exception as e:                                       # noqa: BLE001
        raise HTTPException(400, f"could not read that file as KML/KMZ: {e}")
    if summary.get("photos_to_cache"):
        mymaps.cache_photos()
    return summary


@router.get("/api/pins")
def list_pins():
    return {"pins": mymaps.pins(), "areas": mymaps.areas(),
            "photo_cache": mymaps.photo_cache_state()}


@router.post("/api/pins/cache")
def cache_pin_photos():
    return mymaps.cache_photos()


@router.delete("/api/pins")
def remove_pins():
    return {"removed": mymaps.delete_source()}


class PortalCases(BaseModel):
    cases: list


@router.post("/api/pins/portal")
def reconcile_portal(body: PortalCases, request: Request):
    return mymaps.reconcile_portal(body.cases[:5000], cfg_of(request))


class HighwaysReports(BaseModel):
    reports: list


@router.post("/api/pins/highways")
def reconcile_highways(body: HighwaysReports):
    r = mymaps.reconcile_highways(body.reports[:5000])
    mymaps.cache_photos()
    return r


@router.get("/api/pins/{pid}/photo")
def pin_photo(pid: int, full: int = 0):
    p = mymaps.pin(pid)
    if not p:
        raise HTTPException(404, "not found")
    path = Path(p["photo_path"] if full else (p["thumb_path"] or p["photo_path"] or ""))
    if not str(path) or not path.exists() or not _within(path):
        raise HTTPException(404, "not cached")
    return FileResponse(path)
