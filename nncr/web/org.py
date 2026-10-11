"""The shared layer, for everyone signed in: the map, summary tables and
downloads of every report the group has sent to the council."""
import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from .. import config, webmap
from . import aggregate, auth, boundaries, mapbox, usercfg

router = APIRouter(dependencies=[Depends(auth.need("viewer"))])


def _rows(request: Request, since="", until="", category="", ward="", parish="") -> list:
    return aggregate.filtered(aggregate.collect(auth.me(request)), since, until,
                              category, ward, parish)


@router.get("/api/mapbox/token")
def mapbox_token():
    got = mapbox.browser_token()
    return {"token": got["token"], "expires": got["expires"], "problem": got["problem"]}


@router.get("/api/boundaries/{layer}")
def boundary_layer(layer: str, labels: int = 0):
    if layer not in ("lad", "wards", "parishes", "parish_wards", "town_councils",
                     "other_parishes", "parish_wards_differ"):
        raise HTTPException(404, "no such layer")
    fc = boundaries.labels(layer) if labels else boundaries.display(layer)
    return Response(json.dumps(fc, separators=(",", ":")),
                    media_type="application/geo+json",
                    headers={"Cache-Control": "private, max-age=300"})


@router.get("/api/boundaries")
def boundary_list():
    return {"layers": boundaries.map_layers(), "attribution": boundaries.ATTRIBUTION}


@router.get("/api/locate")
def locate(lat: float, lon: float):
    return boundaries.locate(lat, lon)


@router.get("/map", response_class=HTMLResponse)
def shared_map(request: Request):
    u = auth.me(request)
    cfg = usercfg.build(u)
    fc = aggregate.features(_rows(request))
    return webmap.build_html("Everyone's reports", fc, cfg, live=True, areas=[],
                             ward_fc=boundaries.wards_for_map(),
                             boundaries=boundaries.map_layers(),
                             token_url="/api/mapbox/token", shared=True)


@router.get("/api/org/summary")
def summary(request: Request, by: str = "ward", since: str = "", until: str = "",
            category: str = "", ward: str = "", parish: str = ""):
    if by not in aggregate.GROUPS:
        raise HTTPException(400, "unknown grouping")
    rows = _rows(request, since, until, category, ward, parish)
    out = aggregate.summary(rows, by)
    out["groups"] = {k: v[1] for k, v in aggregate.GROUPS.items()}
    out["categories"] = sorted({r.get("category") for r in aggregate.collect(auth.me(request))
                                if r.get("category")})
    out["wards"] = sorted(f["properties"]["name"] for f in
                          boundaries.layers()["wards"].get("features", []))
    out["towns"] = sorted(f["properties"]["name"] for f in
                          boundaries.layers()["parishes"].get("features", [])
                          if f["properties"].get("town_council"))
    return out


def _download(body: str, media: str, name: str) -> Response:
    return Response(body, media_type=media,
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/api/org/summary.csv")
def summary_csv(request: Request, by: str = "ward", since: str = "", until: str = "",
                category: str = "", ward: str = "", parish: str = ""):
    if by not in aggregate.GROUPS:
        raise HTTPException(400, "unknown grouping")
    s = aggregate.summary(_rows(request, since, until, category, ward, parish), by)
    return _download(aggregate.summary_csv(s), "text/csv", f"nnc-reports-by-{by}.csv")


@router.get("/api/org/reports.csv")
def reports_csv(request: Request, since: str = "", until: str = "", category: str = "",
                ward: str = "", parish: str = ""):
    return _download(aggregate.to_csv(_rows(request, since, until, category, ward, parish)),
                     "text/csv", "nnc-reports-shared.csv")


@router.get("/api/org/reports.geojson")
def reports_geojson(request: Request, since: str = "", until: str = "", category: str = "",
                    ward: str = "", parish: str = ""):
    fc = aggregate.features(_rows(request, since, until, category, ward, parish))
    return _download(json.dumps(fc), "application/geo+json", "nnc-reports-shared.geojson")


@router.get("/api/org/reports.kml")
def reports_kml(request: Request, since: str = "", until: str = "", category: str = "",
                ward: str = "", parish: str = ""):
    return _download(aggregate.to_kml(_rows(request, since, until, category, ward, parish)),
                     "application/vnd.google-earth.kml+xml", "nnc-reports-shared.kml")


@router.get("/api/org/photo/{uid}/{rid}")
def shared_photo(uid: int, rid: int, request: Request, full: int = 0):
    """A shared report's photo. Thumbnails for everyone signed in; the full-size
    original only for the person who took it, or an admin."""
    viewer = auth.me(request)
    row = next((r for r in aggregate.collect(viewer)
                if r["uid"] == uid and r["id"] == rid), None)
    if not row:
        raise HTTPException(404, "not found")
    want_full = bool(full) and (viewer["role"] == "admin" or viewer["id"] == uid)
    p = Path((row.get("photo_path") if want_full else row.get("thumb_path"))
             or row.get("thumb_path") or "")
    folder = auth.user_dir(uid).resolve()
    try:
        ok = str(p) and p.exists() and folder in p.resolve().parents
    except OSError:
        ok = False
    if not ok:
        raise HTTPException(404, "no photo")
    return FileResponse(p, headers={"Cache-Control": "private, max-age=3600"})
