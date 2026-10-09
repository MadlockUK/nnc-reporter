"""NNC Reporter - local web app.

Run:  run.bat        (or: .venv\\Scripts\\python.exe app.py)
Then: http://127.0.0.1:8712
"""
import argparse
import json
import os
import shutil
import sys
import threading
import time
import webbrowser
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from nncr import (categories, classify, config, dashboard, geo, kml, mymaps, photos,
                  routing, store, submit, track, webmap)

cfg = config.load()
app = FastAPI(title="NNC Reporter")
STATIC = Path(__file__).parent / "nncr" / "static"


# ------------------------------------------------------------------ intake

def route_for(category: str) -> str:
    """Which council form a category has to go to (see nncr/routing.py)."""
    return routing.route_for(category, cfg)


def route_report(report: dict) -> str:
    """Which form a whole report belongs on - category and description."""
    return routing.route_for_report(report, cfg)


def process_file(src: Path, original_name: str) -> dict:
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
    if meta["lat"] is None:
        return {"skipped": original_name,
                "reason": meta.get("gps_error") or "no GPS data in this photo"}
    if not photos.in_north_northants(meta["lat"], meta["lon"]):
        warnings.append("coordinates look to be outside North Northamptonshire")

    stored = photos.store_photo(src, sha, config.data_dir())
    thumb = photos.make_thumb(stored, sha, config.data_dir())

    # Make the copy the council forms get now, while you are already waiting for
    # the upload, rather than in the middle of a submission run. The original is
    # kept untouched alongside it.
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
            warnings.append("NNC asks for emergencies (flooding, fallen trees) to be "
                            "phoned in: 0300 126 3000, or 01604 651074 out of hours")

    service = route_report({"category": category, "title": title, "detail": detail,
                            "notes": notes})
    area_warn = routing.grounds_area_warning(
        {"category": category, "title": title, "detail": detail, "notes": notes,
         "locality": place.get("locality")})
    if area_warn:
        warnings.append(area_warn)

    # The bins form asks what kind of bin, what is wrong with it, and whether it is
    # in a park. Work all three out now, while the photo is in front of us.
    bin_type, bin_issues, in_park = "", [], ""
    if service == "granicus_bins":
        bin_type = classify.normalise_bin_type(guess.get("bin_type"))
        bin_issues = classify.normalise_bin_issues(guess.get("bin_issues"))
        # The model often describes two problems but lists one, so take the words
        # of its own description into account as well.
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

    dupes = store.nearby_submitted(meta["lat"], meta["lon"], category) if category else []
    if dupes:
        warnings.append(f"you already reported this category {dupes[0]['distance_m']}m "
                        f"away (#{dupes[0]['id']})")

    rid = store.insert({
        "sha": sha, "original_name": original_name, "photo_path": str(stored),
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
    return {"added": rid, "name": original_name, "warnings": warnings,
            "shrunk": shrunk}


# ------------------------------------------------------------------ API

@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


def _profile_list() -> list:
    """Every profile, with the name to show for it."""
    out = []
    for name in config.profile_names():
        try:
            who = config.load(name)["reporter"]
        except Exception:
            who = {}
        out.append({"name": name,
                    "label": (who.get("full_name") or "").strip() or name})
    return out


@app.get("/api/profiles")
def profiles():
    return {"profiles": _profile_list(), "active": cfg["profile"]}


class NewProfile(BaseModel):
    name: str
    full_name: str = ""
    email: str = ""
    phone: str = ""
    address_line: str = ""
    address_postcode: str = ""
    show_name_publicly: bool = False
    highways_email: str = ""
    highways_password: str = ""
    map_folder: str = ""


@app.post("/api/profiles")
def add_profile(body: NewProfile):
    if not (body.name or "").strip():
        raise HTTPException(400, "give the profile a name")
    name = config.slug(body.name)
    if name in config.profile_names():
        raise HTTPException(400, f"there is already a profile called '{name}'")
    settings = {
        "reporter": {"full_name": body.full_name, "email": body.email,
                     "phone": body.phone, "address_line": body.address_line,
                     "address_postcode": body.address_postcode,
                     "show_name_publicly": body.show_name_publicly},
        "highways_login": {"email": body.highways_email or body.email,
                           "password": body.highways_password, "enabled": True},
    }
    if body.map_folder:
        settings["export"] = {"map_folder": body.map_folder}
    config.save_profile(name, settings)
    return {"created": name, "profiles": _profile_list()}


def _external_keys() -> dict:
    """What the out-of-folder keys file supplies, so the editor can say when a
    value it shows is not the one actually being used."""
    path = Path.home() / ".nnc-reporter" / "keys.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, json.JSONDecodeError):
        return {}


@app.get("/api/profiles/{name}")
def get_profile(name: str):
    if name not in config.profile_names():
        raise HTTPException(404, f"there is no profile called '{name}'")
    c = config.load(name)
    ext = _external_keys()
    overridden = []
    if name == config.MAIN_PROFILE:
        if ext.get("highways_password") or os.environ.get("NNC_HIGHWAYS_PASSWORD"):
            overridden.append("the Highways password")
        if ext.get("highways_email"):
            overridden.append("the Highways email")
    return {
        "name": name,
        "is_main": name == config.MAIN_PROFILE,
        "reporter": c["reporter"],
        "highways_login": c["highways_login"],
        "export": c["export"],
        "data_folder": str(config.profile_data_dir(name)),
        "map_folder": str(config.map_export_dir(c)),
        "report_count": _count_for(name),
        "overridden": overridden,
    }


def _count_for(name: str) -> int:
    """How many reports this profile holds, without switching to it."""
    import sqlite3
    db = config.profile_data_dir(name) / "reports.db"
    if not db.exists():
        return 0
    try:
        with sqlite3.connect(db) as con:
            return con.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
    except sqlite3.Error:
        return 0


class EditProfile(BaseModel):
    full_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address_line: str | None = None
    address_postcode: str | None = None
    show_name_publicly: bool | None = None
    highways_email: str | None = None
    highways_password: str | None = None
    highways_enabled: bool | None = None
    map_folder: str | None = None
    kml_filename: str | None = None
    csv_filename: str | None = None


@app.patch("/api/profiles/{name}")
def edit_profile(name: str, body: EditProfile):
    global cfg
    if name not in config.profile_names():
        raise HTTPException(404, f"there is no profile called '{name}'")
    given = {k: v for k, v in body.model_dump().items() if v is not None}
    if "full_name" in given and not given["full_name"].strip():
        raise HTTPException(400, "the council forms need a name")

    reporter = {k: v for k, v in given.items() if k in
                ("full_name", "email", "phone", "address_line", "address_postcode",
                 "show_name_publicly")}
    if "full_name" in reporter:
        # first_name / last_name are split from full_name unless set by hand, so
        # clear any old split - otherwise a rename leaves the old name on forms.
        reporter["first_name"] = ""
        reporter["last_name"] = ""
    login = {k[9:]: v for k, v in given.items() if k.startswith("highways_")}
    export = {k: v for k, v in given.items()
              if k in ("map_folder", "kml_filename", "csv_filename")}
    settings = {}
    if reporter:
        settings["reporter"] = reporter
    if login:
        settings["highways_login"] = login
    if export:
        settings["export"] = export
    if settings:
        config.save_profile(name, settings)
    if name == cfg["profile"]:
        cfg = config.load()
    return get_profile(name)


class UseProfile(BaseModel):
    name: str


@app.post("/api/profile")
def switch_profile(body: UseProfile):
    """Switch person: a different list of reports, logins and map file."""
    global cfg
    if submit.busy():
        raise HTTPException(409, "a browser session is open - finish or close it "
                                 "before switching profile")
    try:
        config.use_profile(body.name)
    except ValueError as e:
        raise HTTPException(404, str(e))
    cfg = config.load()
    return {"active": cfg["profile"], "reports": store.all_reports(),
            "stats": store.stats()}


@app.delete("/api/profiles/{name}")
def remove_profile(name: str):
    global cfg
    try:
        config.delete_profile(name)
    except ValueError as e:
        raise HTTPException(400, str(e))
    cfg = config.load()
    return {"profiles": _profile_list(), "active": cfg["profile"],
            "note": "the reports and photos are still on disk, in "
                    f"data/profiles/{config.slug(name)}"}


def _my_map_mid() -> str:
    return (cfg.get("mymap") or {}).get("mid") or ""


def _my_map_url() -> str:
    mid = _my_map_mid()
    return f"https://www.google.com/maps/d/u/0/edit?mid={mid}&usp=sharing" if mid else ""


@app.get("/api/state")
def state():
    return {
        "reports": store.all_reports(),
        "categories": categories.load(),
        "update_counts": store.update_counts(),
        "stats": store.stats(),
        "profiles": _profile_list(),
        "profile": cfg["profile"],
        "config": {
            "reporter": cfg["reporter"],
            "map_file": cfg["export"].get("kml_filename"),
            "flytipping_route": cfg["routing"].get("flytipping_route"),
            "classify_on": classify.available(cfg),
            "w3w_on": bool(cfg["keys"].get("what3words")),
            "mapbox_on": bool(webmap.token_for(cfg)[0]),
            "map_folder": str(config.map_export_dir(cfg)),
            "map": {"lat": config.centre(cfg)[0], "lon": config.centre(cfg)[1],
                    "zoom": int((cfg.get("map") or {}).get("zoom", 13))},
        },
        "my_map_url": _my_map_url(),
        "pins": len(mymaps.pins()),
        "photo_cache": mymaps.photo_cache_state(),
    }


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    inbox = config.data_dir() / "inbox"
    inbox.mkdir(exist_ok=True)
    results = []
    for f in files:
        tmp = inbox / (f.filename or f"upload-{int(time.time()*1000)}")
        with open(tmp, "wb") as out:
            shutil.copyfileobj(f.file, out)
        try:
            results.append(process_file(tmp, f.filename or tmp.name))
        except Exception as e:
            results.append({"skipped": f.filename, "reason": str(e)})
        finally:
            tmp.unlink(missing_ok=True)
    return {"results": results, "reports": store.all_reports()}


@app.get("/api/geocode")
def geocode_search(q: str):
    """Find coordinates for a typed address or postcode - for reports that start
    from a description rather than a geotagged photo."""
    res = geo.forward_geocode(q, cfg)
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


@app.post("/api/reports/manual")
def add_manual(body: ManualReport):
    """A report with no photograph - noise, smells, smoke, light and the like
    cannot be photographed, but they still have a place and a description.

    The location comes from the address search or a click on the map; everything
    else works exactly as it does for a photo: enrichment, routing, duplicate
    checks and the same review queue.
    """
    import uuid
    match = categories.find(body.category)
    if not match:
        raise HTTPException(400, f"unknown category: {body.category}")
    if not (body.detail or "").strip():
        raise HTTPException(400, "describe the issue - the council form needs it")

    warnings = []
    if not photos.in_north_northants(body.lat, body.lon):
        warnings.append("coordinates look to be outside North Northamptonshire")

    place = geo.describe(body.lat, body.lon, cfg)
    place["lat"], place["lon"] = body.lat, body.lon
    street = (body.street or "").strip() or place.get("street")
    locality = (body.locality or "").strip() or place.get("locality")
    postcode = (body.postcode or "").strip() or place.get("postcode")
    if place.get("w3w_error"):
        warnings.append(f"What3Words: {place['w3w_error']}")

    category, group = match["name"], match["group"]
    title = (body.title or "").strip() or category
    detail = classify.compose_detail(
        body.detail.strip(),
        {"street": street, "locality": locality, "postcode": postcode,
         "w3w": place.get("w3w"), "lat": body.lat, "lon": body.lon}, None)

    report = {"category": category, "title": title, "detail": detail,
              "notes": "", "locality": locality}
    service = route_report(report)
    area_warn = routing.grounds_area_warning(report)
    if area_warn:
        warnings.append(area_warn)
    if category in categories.EMERGENCY_HINTS:
        warnings.append("NNC asks for emergencies (flooding, fallen trees) to be "
                        "phoned in: 0300 126 3000, or 01604 651074 out of hours")

    dupes = store.nearby_submitted(body.lat, body.lon, category)
    if dupes:
        warnings.append(f"you already reported this category "
                        f"{dupes[0]['distance_m']}m away (#{dupes[0]['id']})")
    warnings.append("no photograph - the form's photo question will be answered "
                    "'No'")

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


@app.patch("/api/reports/{rid}")
def patch(rid: int, body: Patch):
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if "category" in changes:
        match = categories.find(changes["category"])
        if not match:
            raise HTTPException(400, f"unknown category: {changes['category']}")
        changes["category"] = match["name"]
        changes["cat_group"] = match["group"]
        # Keep the form in step with the category unless you chose one yourself.
        if "service" not in changes:
            merged = {**(store.get(rid) or {}), **changes}
            changes["service"] = route_report(merged)
    r = store.update(rid, changes)
    if not r:
        raise HTTPException(404, "not found")
    return r


def _bin_wording(r: dict, issue: str) -> tuple:
    """Title and description for one problem with one bin.

    The stored issue is the council's own option text ("Requires emptying"), which
    does not read as a sentence, so the title uses a plainer phrase for it.
    """
    kind = classify.normalise_bin_type(r.get("bin_type")) or "Public bin"
    said = classify.BIN_ISSUE_TITLE.get(issue, issue.lower())
    title = f"{kind} {said}"
    detail = r.get("detail") or ""
    lead = f"The {kind.lower()} {said}."
    if not detail.lower().startswith(lead.lower()):
        detail = f"{lead} {detail}".strip()
    return title, detail


@app.post("/api/reports/{rid}/split")
def split(rid: int):
    """One bin, two problems: the council form takes one at a time, so make a
    separate report for each. The original keeps the first problem."""
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
    """Delete the stored copies of photos belonging to removed reports."""
    gone = 0
    for r in rows:
        for key in ("photo_path", "thumb_path"):
            p = r.get(key)
            if not p:
                continue
            path = Path(p)
            for target in (path, path.with_name(path.stem + "_upload.jpg")):
                try:
                    if target.exists() and config.data_dir() in target.parents:
                        target.unlink()
                        gone += 1
                except OSError:
                    pass
    return gone


@app.delete("/api/reports/{rid}")
def remove(rid: int):
    row = store.delete(rid)
    if row:
        _purge_files([row])
    return {"ok": True}


@app.delete("/api/reports")
def remove_all(include_submitted: bool = False):
    """Clear the queue. Keeps submitted reports unless include_submitted=true."""
    rows = store.delete_all(include_submitted)
    files = _purge_files(rows)
    return {"removed": len(rows), "files_deleted": files,
            "kept": len(store.all_reports())}


class Ids(BaseModel):
    ids: list[int] = []


def _start(ids: list[int]):
    if submit.busy():
        raise HTTPException(409, "a browser session is already open - "
                                "close it or wait for it to finish")
    missing = [i for i in ids if not (store.get(i) or {}).get("category")]
    if missing:
        raise HTTPException(400, "pick a category first for #"
                            + ", #".join(map(str, missing)))
    threading.Thread(target=submit.submit, args=(ids, cfg), daemon=True).start()
    time.sleep(0.4)
    return {"started": True, "count": len(ids), "log": submit.log_for()}


@app.post("/api/reports/{rid}/submit")
def start_submit(rid: int):
    if not store.get(rid):
        raise HTTPException(404, "not found")
    return _start([rid])


@app.post("/api/submit")
def start_submit_many(body: Ids):
    if not body.ids:
        raise HTTPException(400, "nothing selected")
    return _start(body.ids)


@app.get("/api/log")
def get_batch_log():
    """Combined log for the whole run, plus the current state of every report."""
    return {"log": submit.log_for(), "busy": submit.busy(),
            "reports": store.all_reports()}


@app.get("/api/reports/{rid}/log")
def get_log(rid: int):
    return {"log": submit.log_for(rid), "report": store.get(rid)}


@app.get("/api/photo/{rid}")
def photo(rid: int, full: int = 0):
    r = store.get(rid)
    if not r:
        raise HTTPException(404, "not found")
    p = Path(r["photo_path"] if full else (r["thumb_path"] or r["photo_path"]))
    if not p.exists():
        raise HTTPException(404, "file missing")
    return FileResponse(p)


@app.post("/api/export")
def export(body: Ids | None = None):
    """Write the map layer. Pass ids to export just those reports."""
    ids = body.ids if body and body.ids else None
    return kml.export(cfg, ids=ids)


@app.post("/api/updates/check")
def check_council_updates(body: Ids | None = None):
    """Read the public highways report pages and reconcile what the council has
    done: current state into council_status, new updates into each case's
    timeline. One page a second, so a long history takes a little while."""
    ids = body.ids if body and body.ids else None
    res = track.check_pages(cfg, ids)
    return {**res, "reports": store.all_reports(),
            "update_counts": store.update_counts()}


@app.post("/api/updates/email")
async def reconcile_emails(files: list[UploadFile] = File(default=[]),
                           text: str = Form(default="")):
    """Council emails, dropped in as saved .eml files or pasted text. Each is
    matched to its case by reference and recorded on that case's timeline."""
    results = []
    for f in files:
        try:
            parsed = track.parse_eml(await f.read())
            res = track.reconcile_email(parsed["subject"], parsed["date"],
                                        parsed["body"], parsed["sender"])
            res["name"] = f.filename
        except Exception as e:
            res = {"name": f.filename, "matched": None, "note": str(e)[:150]}
        results.append(res)
    if (text or "").strip():
        lines = text.strip().split("\n", 1)
        res = track.reconcile_email(lines[0][:150], "",
                                    lines[1] if len(lines) > 1 else text)
        res["name"] = "pasted text"
        results.append(res)
    if not results:
        raise HTTPException(400, "drop in .eml files or paste the email text")
    return {"results": results, "reports": store.all_reports(),
            "update_counts": store.update_counts()}


@app.get("/api/reports/{rid}/updates")
def case_updates(rid: int):
    if not store.get(rid):
        raise HTTPException(404, "not found")
    return {"updates": store.updates_for(rid)}


@app.get("/api/dashboard", response_class=HTMLResponse)
def reporting_dashboard():
    """Where the council has got to with everything this profile has reported -
    positions, ageing against the 90-day promise, and what needs chasing."""
    return dashboard.page(cfg)


@app.get("/api/webmap", response_class=HTMLResponse)
def interactive_map():
    """The active profile's submissions - and the pins merged in from the Google
    My Map - on a live, interactive Mapbox map with ward borders and area stats."""
    rows = store.all_reports()
    label = (cfg.get("reporter") or {}).get("full_name") or cfg["profile"]
    fc = webmap.merge(webmap.features(rows, live=True),
                      webmap.pin_features(mymaps.pins(), live=True))
    return webmap.build_html(label, fc, cfg, live=True, areas=mymaps.areas(),
                             ward_fc=webmap.wards(), mymap_url=_my_map_url())


# ------------------------------------------------------ My Map pins & wards

class PinImport(BaseModel):
    url: str | None = None          # a My Maps link or map id; default: config


@app.post("/api/pins/import")
def import_pins(body: PinImport | None = None):
    """Fetch the Google My Map and merge its pins and drawn areas. Pins the tool
    exported itself are recognised and left alone; the rest land in map_pins.
    Photos are cached in the background afterwards."""
    mid = mymaps.mid_from((body.url if body and body.url else "") or _my_map_mid())
    if not mid:
        raise HTTPException(400, "no My Map set: put its id in config.json as "
                                 "mymap.mid (or paste a My Maps link)")
    try:
        summary = mymaps.import_from_mid(mid)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(502, f"could not fetch or read the My Map: {e}")
    if summary.get("photos_to_cache"):
        mymaps.cache_photos()
    return summary


@app.post("/api/pins/import/file")
async def import_pins_file(file: UploadFile = File(...)):
    """The same merge from a KML or KMZ exported by hand from My Maps."""
    data = await file.read()
    try:
        summary = mymaps.import_kml(mymaps.unwrap_kmz(data), source_label=file.filename)
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(400, f"could not read that file as KML/KMZ: {e}")
    if summary.get("photos_to_cache"):
        mymaps.cache_photos()
    return summary


@app.get("/api/pins")
def list_pins():
    return {"pins": mymaps.pins(), "areas": mymaps.areas(),
            "photo_cache": mymaps.photo_cache_state()}


@app.post("/api/pins/cache")
def cache_pin_photos():
    """Retry caching any pin photo that is still only a Google URL."""
    return mymaps.cache_photos()


@app.delete("/api/pins")
def remove_pins():
    """Drop every merged pin and area (re-import brings them back)."""
    return {"removed": mymaps.delete_source()}


class PortalCases(BaseModel):
    cases: list


@app.post("/api/pins/portal")
def reconcile_portal(body: PortalCases):
    """Reconcile the council portal's "My forms" cases (forms.northnorthants.gov.uk/
    MyRequests) with the map pins: real dates replace estimates, cases the map
    never had become pins, and the remaining estimated dates are re-fitted."""
    return mymaps.reconcile_portal(body.cases, cfg)


class HighwaysReports(BaseModel):
    reports: list


@app.post("/api/pins/highways")
def reconcile_highways(body: HighwaysReports):
    """Reconcile the highways site's "Your reports" list with the tool: council
    states are noted on the tool's own reports, pins are updated, and reports the
    map never had become pins."""
    r = mymaps.reconcile_highways(body.reports)
    mymaps.cache_photos()
    return r


@app.get("/api/pins/{pid}/photo")
def pin_photo(pid: int, full: int = 0):
    p = mymaps.pin(pid)
    if not p:
        raise HTTPException(404, "not found")
    path = Path(p["photo_path"] if full else (p["thumb_path"] or p["photo_path"] or ""))
    if not str(path) or not path.exists():
        raise HTTPException(404, "not cached")
    return FileResponse(path)


@app.get("/api/wards")
def ward_boundaries():
    """NNC ward boundaries (ONS, May 2026) as GeoJSON."""
    if not webmap.WARDS_FILE.exists():
        raise HTTPException(404, "wards file missing")
    return FileResponse(webmap.WARDS_FILE, media_type="application/geo+json")


@app.post("/api/webmap/export")
def export_webmaps():
    """One self-contained interactive map file per profile, next to its KML."""
    return webmap.export_all()


@app.post("/api/categories/refresh")
def refresh_categories():
    try:
        res = categories.refresh_from_site()
        return {"ok": True, "count": res["count"]}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)


# ------------------------------------------------------------------ main

def _catch_up_photos() -> int:
    """Shrink the photos of reports that are still waiting to be submitted."""
    done = 0
    for r in store.all_reports():
        if r.get("status") in store.DONE_STATUSES or not r.get("photo_path"):
            continue
        src = Path(r["photo_path"])
        ready = src.with_name(src.stem + "_upload.jpg")
        if not src.exists():
            continue
        try:
            if ready.exists() and ready.stat().st_mtime >= src.stat().st_mtime:
                continue                       # already done on a previous run
            _, shrunk = photos.prepare_upload(src, config.data_dir())
            done += 1 if shrunk.get("after") else 0
        except OSError:
            pass
    if done:
        print(f"Shrunk {done} queued photo(s) ready for submitting.")
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inspect", action="store_true",
                    help="dump the live council form's fields for fixing selectors")
    ap.add_argument("--login", action="store_true",
                    help="sign in to the highways site and save the session")
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    if args.inspect:
        submit.inspect_form(args.url)
        return
    if args.login:
        submit.sign_in_only(cfg)
        return

    fixed = routing.fix_existing(store, cfg)
    for rid, cat, was, now in fixed:
        print(f"   report #{rid} ({cat}): {was} -> {now}, "
              f"the highways site will not accept it")
    if fixed:
        print(f"Rerouted {len(fixed)} queued report(s) to the right council form.\n")

    restated = routing.restate_bins(store)
    if restated:
        print(f"Updated {len(restated)} bin report(s) to the council's own wording "
              f"(it says 'Requires emptying', not 'needs emptying').\n")

    # Reports queued before photos were shrunk at intake still hold their full-size
    # originals. Get them ready in the background so the next run is quick too.
    threading.Thread(target=_catch_up_photos, daemon=True).start()
    # Pin photos that never finished caching (the app was closed mid-download).
    threading.Thread(target=mymaps.cache_photos, daemon=True).start()

    import uvicorn
    port = int(cfg["server"].get("port", 8712))
    url = f"http://127.0.0.1:{port}"
    if not cfg["reporter"].get("email"):
        print("!! No email in config.json - the council forms need one.")
    print(f"\nNNC Reporter running at {url}   (Ctrl+C to stop)\n")
    if cfg["server"].get("open_browser"):
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    sys.exit(main())
