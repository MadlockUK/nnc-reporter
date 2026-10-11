"""Each person's reports are theirs alone; what they send to the council joins
the shared, reportable layer; roles limit who can change what."""
import json

import pytest

from conftest import ORIGIN, STRONG, login, make_user, photo


def upload(c, path):
    with open(path, "rb") as f:
        r = c.post("/api/upload", files=[("files", (path.name, f, "image/jpeg"))])
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def two(env):
    from fastapi.testclient import TestClient
    from nncr.web import server
    make_user("ann@example.com", name="Ann Able")
    make_user("bob@example.com", name="Bob Baker")
    with TestClient(server.app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                    follow_redirects=False) as a, \
            TestClient(server.app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                       follow_redirects=False) as b:
        login(a, "ann@example.com")
        login(b, "bob@example.com")
        yield a, b


def test_reports_are_private_to_their_owner(two, env):
    a, b = two
    res = upload(a, photo(env / "pothole.jpg"))
    rid = res["results"][0]["added"]
    assert rid
    assert [r["id"] for r in a.get("/api/state").json()["reports"]] == [rid]
    assert b.get("/api/state").json()["reports"] == []
    # Bob's "report 1" is his own (empty) list, never Ann's.
    assert b.get(f"/api/photo/{rid}").status_code == 404
    assert b.patch(f"/api/reports/{rid}", json={"title": "hijack"}).status_code == 404
    b.delete(f"/api/reports/{rid}")
    assert a.get("/api/state").json()["reports"][0]["id"] == rid
    assert a.get(f"/api/photo/{rid}").status_code == 200
    # Separate folders on disk.
    assert (env / "data" / "users" / "u1" / "reports.db").exists()
    assert not list((env / "data" / "users" / "u2" / "photos").glob("*.jpg"))


def test_upload_rejects_non_images_and_duplicates(two, env):
    a, _ = two
    bad = env / "note.txt"
    bad.write_text("hello")
    with open(bad, "rb") as f:
        r = a.post("/api/upload", files=[("files", ("note.txt", f, "text/plain"))])
    assert r.json()["results"][0]["skipped"]
    fake = env / "fake.jpg"
    fake.write_text("<script>alert(1)</script>")
    with open(fake, "rb") as f:
        r = a.post("/api/upload", files=[("files", ("../../evil.jpg", f, "image/jpeg"))])
    assert r.json()["results"][0]["skipped"]
    p = photo(env / "one.jpg")
    upload(a, p)
    again = upload(a, p)
    assert "already in the list" in again["results"][0]["reason"]
    assert not (env / "evil.jpg").exists()


def test_submitted_reports_join_the_shared_layer(two, env):
    from nncr.web import db
    a, b = two
    rid = upload(a, photo(env / "flytip.jpg"))["results"][0]["added"]
    a.patch(f"/api/reports/{rid}", json={"category": "Flytipping"})
    # A draft is not shared.
    assert b.get("/api/org/reports.geojson").json()["features"] == []
    a.patch(f"/api/reports/{rid}", json={"status": "submitted", "reference": "FLY800000001"})
    feats = b.get("/api/org/reports.geojson").json()["features"]
    assert len(feats) == 1
    p = feats[0]["properties"]
    assert p["reference"] == "FLY800000001"
    assert p["reporter"] == "Team member"                     # Ann has not opted in
    assert p["ward"] == "Lloyds & Corby Village"               # placed in its ward
    # Ann opts in; admins always see names.
    a.patch("/api/account/profile", json={"share_name": True})
    assert b.get("/api/org/reports.geojson").json()["features"][0]["properties"]["reporter"] == "Ann Able"
    # Thumbnails of shared reports are visible to the group; originals only to the owner.
    path = feats[0]["properties"]["photo"]
    assert b.get(path).status_code == 200
    assert b.get(path + "?full=1").status_code == 200        # falls back to the thumbnail
    # The summary counts it by ward, and offers CSV.
    s = b.get("/api/org/summary?by=ward").json()
    assert s["total"] == 1 and s["rows"][0]["key"] == "Lloyds & Corby Village"
    assert "FLY800000001" in b.get("/api/org/reports.csv").text
    assert "<kml" in b.get("/api/org/reports.kml").text
    assert b.get("/api/org/summary.csv?by=month").status_code == 200
    # The shared map page carries it.
    assert "FLY800000001" in b.get("/map").text


def test_duplicate_warning_across_the_group(two, env):
    a, b = two
    rid = upload(a, photo(env / "a.jpg"))["results"][0]["added"]
    a.patch(f"/api/reports/{rid}", json={"category": "Flytipping", "status": "submitted",
                                         "reference": "FLY800000002"})
    res = upload(b, photo(env / "b.jpg", colour=(10, 200, 10)))
    rid2 = res["results"][0]["added"]
    b.patch(f"/api/reports/{rid2}", json={"category": "Flytipping"})
    r = b.post("/api/reports/manual", json={"category": "Flytipping", "detail": "Sofa dumped",
                                            "lat": 52.4914, "lon": -0.6923})
    assert any("someone else in the group" in w for w in r.json()["warnings"])


def test_viewer_is_read_only(env):
    from fastapi.testclient import TestClient
    from nncr.web import server
    make_user("val@example.com", role="viewer")
    with TestClient(server.app, base_url=ORIGIN, headers={"Origin": ORIGIN},
                    follow_redirects=False) as v:
        login(v, "val@example.com")
        assert v.get("/").status_code == 303 and v.get("/").headers["location"] == "/map"
        assert v.get("/map").status_code == 200
        assert v.get("/summary").status_code == 200
        assert v.get("/api/org/summary").status_code == 200
        assert v.get("/api/state").status_code == 403
        assert v.post("/api/upload", files=[("files", ("x.jpg", b"x", "image/jpeg"))]).status_code == 403
        assert v.post("/api/reports/manual", json={}).status_code == 403
        assert v.get("/api/admin/users").status_code == 403
        assert v.patch("/api/account/profile", json={"share_name": True}).status_code == 200


def test_editor_cannot_administer(two):
    a, _ = two
    assert a.get("/api/admin/users").status_code == 403
    assert a.post("/api/admin/users", json={"email": "x@example.com"}).status_code == 403
    assert a.get("/admin").status_code == 403


def test_admin_guards(env):
    from fastapi.testclient import TestClient
    from nncr.web import server, db
    boss = make_user("boss@example.com", "admin")
    other = make_user("ed@example.com")
    with TestClient(server.app, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        login(c, "boss@example.com")
        assert c.patch(f"/api/admin/users/{boss}", json={"role": "editor"}).status_code == 400
        assert c.patch(f"/api/admin/users/{boss}", json={"status": "suspended"}).status_code == 400
        assert c.delete(f"/api/admin/users/{boss}").status_code == 400
        r = c.patch(f"/api/admin/users/{other}", json={"role": "viewer"})
        assert r.status_code == 200 and r.json()["user"]["role"] == "viewer"
        r = c.post(f"/api/admin/users/{other}/reset-password")
        assert r.status_code == 200 and "token" not in r.text   # link goes to them, not the admin
        assert c.get("/api/admin/audit").json()["events"]


def test_export_downloads(two, env):
    a, _ = two
    rid = upload(a, photo(env / "a.jpg"))["results"][0]["added"]
    a.patch(f"/api/reports/{rid}", json={"category": "Flytipping", "status": "submitted"})
    r = a.post("/api/export", json={"ids": [rid]})
    assert r.status_code == 200 and r.json()["pins"] == 1
    assert "<kml" in a.get("/api/export/kml").text
    assert a.get("/api/export/csv").status_code == 200
    assert a.get("/api/export/photos.zip").content[:2] == b"PK"


def test_my_data_download(two, env):
    a, _ = two
    upload(a, photo(env / "a.jpg"))
    r = a.get("/api/account/export")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    import io, zipfile
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "account.json" in names and "reports.db" in names
    assert "password_hash" not in zipfile.ZipFile(io.BytesIO(r.content)).read("account.json").decode()


def test_mapbox_secret_token_never_reaches_a_browser(two, monkeypatch):
    from nncr.web import mapbox, settings
    a, _ = two
    secret = "sk.eyJ1IjoidGVzdHVzZXIiLCJhIjoiYWJjIn0.signature"
    monkeypatch.setenv("MAPBOX_SECRET_TOKEN", secret)
    settings.reset()
    calls = []

    class R:
        ok = True
        status_code = 200
        def json(self):
            return {"token": "tk.eyJ1IjoidGVzdHVzZXIifQ.temp"}
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)) or R())
    r = a.get("/api/mapbox/token").json()
    assert r["token"].startswith("tk.")
    assert calls and calls[0][0].endswith("/tokens/v2/testuser")
    assert calls[0][1]["params"]["access_token"] == secret          # used server-side only
    for page in ("/map", "/api/webmap", "/", "/api/me", "/api/state", "/api/mapbox/token"):
        assert "sk.eyJ" not in a.get(page).text
    # Cached: no second call to Mapbox.
    a.get("/api/mapbox/token")
    assert len(calls) == 1


def test_mapbox_failure_explained_not_leaked(two, monkeypatch):
    from nncr.web import settings
    a, _ = two
    monkeypatch.setenv("MAPBOX_SECRET_TOKEN", "sk.eyJ1IjoidGVzdHVzZXIifQ.sig")
    settings.reset()

    class R:
        ok = False
        status_code = 403
        text = '{"message":"This endpoint requires a token with tokens:write scope"}'
        def json(self):
            return json.loads(self.text)
    import requests
    monkeypatch.setattr(requests, "post", lambda url, **kw: R())
    r = a.get("/api/mapbox/token").json()
    assert r["token"] == "" and "tokens:write" in r["problem"]


def test_boundaries_fetch_and_town_council_wards(env, monkeypatch):
    """With the ONS services stubbed: wards, the council and parishes are stored,
    town councils are flagged and their wards derived along the unitary lines."""
    from nncr.web import boundaries
    bundled = json.loads(boundaries.BUNDLED_WARDS.read_text())
    corby_wards = [f for f in bundled["features"]
                   if f["properties"]["name"] in ("Corby West", "Kingswood", "Oakley",
                                                  "Lloyds & Corby Village")]
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union
    corby = unary_union([shape(f["geometry"]).buffer(0) for f in corby_wards]).buffer(-0.0005)

    def fake(url, timeout=90):
        if "WD_" in url:
            return [{"type": "Feature", "geometry": f["geometry"],
                     "properties": {"WD25CD": f["properties"]["code"],
                                    "WD25NM": f["properties"]["name"], "LAD25CD": "E06000061"}}
                    for f in bundled["features"]] + [
                {"type": "Feature", "geometry": bundled["features"][0]["geometry"],
                 "properties": {"WD25CD": "E05999999", "WD25NM": "Elsewhere", "LAD25CD": "E06000062"}}]
        if "LAD_" in url:
            raise RuntimeError("service down")
        return [{"type": "Feature", "geometry": mapping(corby),
                 "properties": {"PARNCP25CD": "E04999001", "PARNCP25NM": "Corby"}}]
    monkeypatch.setattr(boundaries, "_arcgis", fake)
    got = boundaries.fetch_from_ons()
    assert got["wards"] == 31 and got["lad"] == 1 and got["parishes"] == 1
    assert got["parish_wards"] == 4
    names = sorted(f["properties"]["name"] for f in
                   boundaries.layers()["parish_wards"]["features"])
    assert "Lloyds" in names                       # the town council's own name for it
    where = boundaries.locate(52.4914, -0.6923)
    assert where["parish"] == "Corby" and where["town_council"] and where["parish_ward"] == "Lloyds"
    # An official upload that differs from the unitary wards is drawn; WGS84 only.
    with pytest.raises(ValueError):
        boundaries.upload("parish_wards", json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"name": "X"},
             "geometry": {"type": "Polygon", "coordinates": [[[488000, 289000], [489000, 289000],
                                                             [489000, 290000], [488000, 289000]]]}}]}).encode())
    lloyds = next(f for f in boundaries.layers()["parish_wards"]["features"]
                  if f["properties"]["name"] == "Lloyds")
    half = shape(corby_wards[0]["geometry"]).buffer(0)
    minx, miny, maxx, maxy = half.bounds
    from shapely.geometry import box
    piece = half.intersection(box(minx, miny, (minx + maxx) / 2, maxy))
    res = boundaries.upload("parish_wards", json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "West Half", "parish": "Corby"},
         "geometry": mapping(piece)}]}).encode())
    assert res["differ"] == 1
    assert boundaries.display("parish_wards_differ")["features"][0]["properties"]["name"] == "West Half"
    # Uploaded wards replace that town's derived ones; one that follows the
    # unitary ward exactly is recognised as not differing.
    res = boundaries.upload("parish_wards", json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": "Lloyds", "parish": "Corby"},
         "geometry": lloyds["geometry"]}]}).encode())
    assert res["differ"] == 0
