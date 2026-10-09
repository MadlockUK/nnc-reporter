"""Safe self-check: EXIF reading, category lookup, KML building, report wording.

Touches nothing - no config, no database, no network, no submissions.
Run:  .venv\\Scripts\\python.exe selfcheck.py
"""
import sys
import tempfile
import xml.dom.minidom
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nncr import (categories, classify, dashboard, kml, photos, routing, track,  # noqa: E402
                  webmap)

FAILED = []


def check(label, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}{'  ' + extra if extra else ''}")
    if not cond:
        FAILED.append(label)


def _dms(v):
    v = abs(v)
    d = int(v)
    m = int((v - d) * 60)
    f = Fraction(round((v - d - m / 60) * 3600, 4)).limit_denominator(10000)
    return ((d, 1), (m, 1), (f.numerator, f.denominator))


def make_photo(path, lat, lon, gps=True):
    try:
        import piexif
    except ImportError:
        return False
    from PIL import Image
    exif = {"0th": {}, "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:07:20 14:05:11"},
            "GPS": {}, "1st": {}, "thumbnail": None}
    if gps:
        exif["GPS"] = {piexif.GPSIFD.GPSLatitudeRef: b"N" if lat >= 0 else b"S",
                       piexif.GPSIFD.GPSLatitude: _dms(lat),
                       piexif.GPSIFD.GPSLongitudeRef: b"E" if lon >= 0 else b"W",
                       piexif.GPSIFD.GPSLongitude: _dms(lon)}
    Image.new("RGB", (600, 450), (130, 130, 130)).save(
        path, "JPEG", exif=piexif.dump(exif))
    return True


print("\n1. Council category list")
cats = categories.load()
check(f"{len(cats)} categories loaded", len(cats) > 30)
check("exact-name lookup works",
      (categories.find("Potholes / Highway Condition") or {}).get("group") == "Roads")
check("'Group > Name' lookup works",
      (categories.find("Roads > Flooding") or {}).get("name") == "Flooding")
check("unknown category rejected", categories.find("made up thing") is None)

check("synthetic form categories present",
      all(categories.find(n) for n in
          ("Litter or dog waste bin", "Dead animal",
           "Accumulation or overgrown garden", "Open or derelict property",
           "Grass, trees or hedges (grounds)", "Noise nuisance",
           "Odour or drain nuisance", "Smoke or bonfire", "Light nuisance",
           "Dust or vibration")))
check("nuisances are never auto-picked from a photo",
      {"Noise nuisance", "Odour or drain nuisance", "Light nuisance",
       "Dust or vibration"} <= categories.NEVER_AUTO)

print("\n1b. Routing to the council's forms")
want = {
    "Flytipping": "granicus", "Litter or dog waste bin": "granicus_bins",
    "Graffiti": "granicus_streetcare", "Flyposting": "granicus_streetcare",
    "Street Cleansing": "granicus_streetcare", "Dead animal": "granicus_streetcare",
    "Dog fouling": "granicus_dog", "Abandoned vehicles": "granicus_vehicle",
    "Open or derelict property": "granicus_derelict",
    "Grass, trees or hedges (grounds)": "granicus_grounds",
    "Accumulation or overgrown garden": "granicus_env",
    "Noise nuisance": "granicus_env", "Odour or drain nuisance": "granicus_env",
    "Smoke or bonfire": "granicus_env", "Light nuisance": "granicus_env",
    "Dust or vibration": "granicus_env",
    "Potholes / Highway Condition": "fixmystreet",
}
bad = {c: routing.route_for(c) for c, s in want.items() if routing.route_for(c) != s}
check("every forced category reaches its own form", not bad, str(bad))
check("a dead animal is recognised from the wording",
      routing.route_for_report({"category": "", "title": "Dead badger on the verge",
                                "detail": ""}) == "granicus_streetcare")
check("a bin is still recognised from the wording",
      routing.route_for_report({"category": "", "title": "",
                                "detail": "the litter bin is overflowing"})
      == "granicus_bins")
check("East Northants grounds reports are warned about",
      bool(routing.grounds_area_warning(
          {"category": "Grass, trees or hedges (grounds)", "locality": "Oundle"})))
check("Corby grounds reports are not warned about",
      routing.grounds_area_warning(
          {"category": "Grass, trees or hedges (grounds)", "locality": "Corby"})
      is None)

print("\n1c. Every routed service has a selectors block")
import json as _json
sels = _json.loads((Path(__file__).parent / "selectors.json")
                   .read_text(encoding="utf-8"))
from nncr import submit as _submit
services = set(routing.FORCED.values()) | {routing.DEFAULT}
missing = [s for s in services if _submit.SERVICE_KEYS.get(s) not in sels]
check("all services covered", not missing, str(missing))
u = [s for s in services if s != "fixmystreet"
     and not sels[_submit.SERVICE_KEYS[s]].get("url", "").startswith(
         "https://forms.northnorthants.gov.uk/service/")]
check("granicus form URLs look right", not u, str(u))

print("\n1d. Type matching for the new forms")
pick, how = classify.pick_from_list(
    ["Litter", "Dead animal", "Graffiti", "Fly posting"],
    {"title": "Graffiti sprayed on the wall", "detail": "", "category": "Graffiti"},
    {"classify": {"enabled": False}, "keys": {}}, form="streetcare")
check("street care picks graffiti", pick == "Graffiti", f"got {pick} ({how})")
pick, how = classify.pick_from_list(
    ["Noise", "Odour", "Smoke", "Light", "Other"],
    {"title": "Bonfire smoke every evening", "detail": "burning waste in a garden",
     "category": "Smoke or bonfire"},
    {"classify": {"enabled": False}, "keys": {}}, form="env")
check("env form picks smoke", pick == "Smoke", f"got {pick} ({how})")

print("\n2. EXIF GPS + timestamp")
with tempfile.TemporaryDirectory() as td:
    p = Path(td) / "a.jpg"
    if make_photo(p, 52.39620, -0.72590):
        m = photos.read_metadata(p)
        check("latitude read", m["lat"] is not None and abs(m["lat"] - 52.3962) < 1e-4,
              f"got {m['lat']}")
        check("longitude read", m["lon"] is not None and abs(m["lon"] + 0.7259) < 1e-4,
              f"got {m['lon']}")
        check("timestamp read", m["taken_at"] == "2026-07-20T14:05:11", str(m["taken_at"]))
        q = Path(td) / "b.jpg"
        make_photo(q, 0, 0, gps=False)
        m2 = photos.read_metadata(q)
        check("photo with no GPS is reported, not crashed",
              m2["lat"] is None and bool(m2["gps_error"]))
    else:
        print("  SKIP  (pip install piexif to test EXIF writing)")
check("Kettering is inside the area check", photos.in_north_northants(52.3962, -0.7259))
check("London is outside the area check",
      not photos.in_north_northants(51.5072, -0.1276))

print("\n3. Report wording")
txt = classify.compose_detail(
    "The footway slab is cracked and rocking underfoot.",
    {"street": "Rockingham Road", "locality": "Kettering", "postcode": "NN16 8LA",
     "w3w": "filled.count.soap", "lat": 52.3962, "lon": -0.7259},
    "2026-07-20T14:05:11")
check("includes street", "Rockingham Road" in txt)
check("includes postcode", "NN16 8LA" in txt)
check("includes What3Words", "///filled.count.soap" in txt)
check("includes date photographed", "2026-07-20" in txt)

print("\n4. Google My Maps KML")
rows = [
    dict(lat=52.4, lon=-0.7, title='Slab "cracked" & <rocking>',
         category="Footway/Footpath", status="submitted", reference="123456",
         taken_at="2026-07-20T14:05:11", street="St John's Rd", postcode="NN16 8LA",
         w3w="a.b.c", detail='5m of <slabs> & "loose" kerbs', report_url=None),
    dict(lat=52.5, lon=-0.6, title="Café sign — damaged", category=None,
         status="new", created_at="2026-07-26 10:00"),
]
x = kml.build_kml(rows)
try:
    doc = xml.dom.minidom.parseString(x)
    check("valid XML with awkward characters and missing fields", True)
    marks = doc.getElementsByTagName("Placemark")
    check("one placemark per report", len(marks) == 2, f"got {len(marks)}")
    lon, lat, _ = marks[0].getElementsByTagName(
        "coordinates")[0].firstChild.data.split(",")
    check("coordinates in KML's lon,lat order",
          -1.1 < float(lon) < 0 and 52 < float(lat) < 53, f"{lon},{lat}")
except Exception as e:
    check("valid XML", False, str(e))

print("\n5. Interactive Mapbox web map")
wm_rows = [
    dict(id=1, lat=52.4, lon=-0.7, title='Pothole <b>"deep"</b>', category="Potholes / Highway Condition",
         status="submitted", reference="123", photo_url="https://example.com/p.jpg",
         street="High St", w3w="a.b.c", taken_at="2026-07-20T14:05:11", detail="x"),
    dict(id=2, lat=52.5, lon=-0.6, title="Queued one", category="Graffiti",
         status="new", photo_path="C:/x.jpg"),
    dict(id=3, lat=None, lon=None, title="No location", category="Other", status="new"),
    dict(id=4, lat=52.6, lon=-0.5, title="Skipped", category="Other", status="skipped"),
]
fc = webmap.features(wm_rows, live=True)
check("no-location and skipped reports left off the map", len(fc["features"]) == 2,
      f"got {len(fc['features'])}")
props = {f["properties"]["id"]: f["properties"] for f in fc["features"]}
check("statuses bucketed like the app's tabs",
      props[1]["status"] == "submitted" and props[2]["status"] == "queued")
check("broad type matches the KML pin colours", props[1]["kind"] == "pothole")
check("live map falls back to the app's own photo endpoint",
      props[2]["photo"] == "/api/photo/2")
check("exported map only uses public photo URLs",
      webmap.features(wm_rows, live=False)["features"][1]["properties"]["photo"] == "")

tok, prob = webmap.token_for({"keys": {"mapbox": ""}})
check("missing token explained, not a silent blank map", not tok and "pk." in prob)
tok, prob = webmap.token_for({"keys": {"mapbox": "sk.SECRET"}})
check("secret sk. token refused - never written into a file",
      not tok and "SECRET" in prob)
tok, prob = webmap.token_for({"keys": {"mapbox": "pk.abc"}})
check("public pk. token accepted", tok == "pk.abc" and not prob)

page = webmap.build_html("Alexander Lock", fc,
                         {"keys": {"mapbox": "pk.abc"}, "export": {},
                          "map": {"centre_lat": 52.49, "centre_lon": -0.69}},
                         live=True)
check("page carries the data and the Mapbox GL loader",
      '"FeatureCollection"' in page and "api.mapbox.com/mapbox-gl-js" in page)
check("page guards against a missing token before building the map",
      "The map needs a Mapbox token" in page)
check("titles are HTML-escaped in the page shell", "<b>" not in page.split("<script>")[0])
no_tok_page = webmap.build_html("x", fc, {"keys": {}, "export": {}, "map": {}})
check("token problem surfaces on the page itself", "No Mapbox token set" in no_tok_page)

print("\n5b. Reporting dashboard")
LOGGED = {"source": "email", "happened": "2026-07-26T14:49:34Z",
          "body": "Your report has been logged. The report's reference number is 9000001."}


def _upd(body, when="2026-08-27T09:00:00Z"):
    return {"source": "email", "happened": when, "body": body}


check("no council word at all is its own answer, not 'awaiting triage'",
      dashboard.position_for({"council_status": None}, []) == "silent")
check("acknowledged but not yet investigated reads as awaiting triage",
      dashboard.position_for({"council_status": "open"}, [LOGGED]) == "triage")
check("defect found with no repair line is the pile worth chasing",
      dashboard.position_for(
          {"council_status": "open"},
          [LOGGED, _upd("Investigation: Completed (Defect Found)")]) == "defect")
check("a completed repair reads as fixed, whatever the banner says",
      dashboard.position_for(
          {"council_status": "open"},
          [LOGGED, _upd("Investigation: Completed (Defect Found) "
                        "Defect Repair: Work Completed")]) == "fixed")
check("closed as a duplicate is kept apart from closed as no action",
      dashboard.position_for({"council_status": "closed"},
                             [_upd("Investigation: Completed (Duplicate)")]) == "duplicate"
      and dashboard.position_for(
          {"council_status": "closed"},
          [_upd("Investigation: Completed (No Action Necessary)")]) == "noaction")
check("an investigation with no stated outcome is never guessed at",
      dashboard.position_for({"council_status": "open"},
                             [_upd("Investigation: Completed")]) == "assessed")

when, how = dashboard.reported_on({"taken_at": "2026-07-25T13:20:31"}, [LOGGED])
check("the council's own logged date wins over the photo's",
      how == "council" and when.isoformat() == "2026-07-26")
when, how = dashboard.reported_on({"taken_at": "2026-07-25T13:20:31"}, [])
check("with no council email the walk's own date stands in, marked as such",
      how == "tool" or (how == "photo" and when.isoformat() == "2026-07-25"))

dash_rows = [{"id": 1, "ref": "9000001", "street": 'The <b>"Nook"</b>', "title": "x",
              "service": "fixmystreet", "position": "defect", "position_label": "Defect found",
              "tone": "warning", "live": True, "reported": "2026-07-26", "age": 45,
              "days_left": 45, "overdue": False, "due_soon": False, "reported_from": "council",
              "updates": 3, "last_heard": "2026-08-27", "url": "", "last_words": ""}]
page = dashboard.build_html('Alexander <b>Lock</b>', dash_rows,
                            {"total": 0, "by_source": [], "by_kind": [],
                             "dated": 0, "estimated": 0}, {"profile": "alex"})
check("dashboard page carries its own figures - no call back to the app",
      '"9000001"' in page and "fetch(" not in page)
check("the profile name is HTML-escaped in the page shell",
      "<b>Lock</b>" not in page.split("<script>")[0])
check("the 90-day promise is stated on the page, not just implied",
      str(dashboard.PROMISE_DAYS) in page and "promise" in page)

print("\n6. Council update reconciliation")
FMS_HTML = """
<html><body><div class="banner banner--investigating"><p>Investigating</p></div>
<h1>Large pothole</h1>
<ul class="item-list">
<li><p>Posted by North Northamptonshire Council at 15:06, Sunday 5 April 2026</p>
<div>Officers will now investigate the concerns and this may take up to 5 working
days.</div><p>State changed to: Investigating</p></li>
<li><p>Posted anonymously at 17:48, Sunday 31 May 2026</p>
<div>Road surface still atrocious.</div><p>Still open, via questionnaire</p></li>
<li><p>Posted by North Northamptonshire Council at 16:21, Tuesday 7 July 2026</p>
<div>Works have been scheduled to be completed by 5th October.</div>
<p>Investigation: Completed (Defect Found)</p></li>
</ul></body></html>"""
parsed = track.parse_report_page(FMS_HTML)
check("state read from the page banner", parsed["state"] == "investigating",
      f"got {parsed['state']!r}")
check("all three updates captured", len(parsed["updates"]) == 3,
      f"got {len(parsed['updates'])}")
check("state-change lines recognised inside updates",
      parsed["updates"][0]["state"] == "investigating"
      and parsed["updates"][2]["state"] == "investigated")
check("council poster names kept",
      "North Northamptonshire Council" in parsed["updates"][0]["poster"])
fixed = track.parse_report_page(
    FMS_HTML.replace("banner--investigating", "banner--fixed"))
check("a fixed banner reads as fixed", fixed["state"] == "fixed")

check("FLY reference found in email text",
      track.refs_in("Thank you. Your reference number is: FLY800000001.")
      == ["FLY800000001"])
check("highways URL in an email names the case",
      "4521879" in track.refs_in(
          "view it at https://highways.northnorthants.gov.uk/report/4521879 now"))
check("plain reference-nearby number found",
      "70012345" in track.refs_in("Your ref no. 70012345 has been updated"))
check("email wording maps to a state",
      track.state_in_email("The issue you reported has been fixed") == "fixed"
      and track.state_in_email("This has been passed to our contractor")
      == "in progress")

eml = (b"From: noreply@northnorthants.gov.uk\r\nTo: a@b.c\r\n"
       b"Subject: Update on your report FLY800000001\r\n"
       b"Date: Mon, 24 Aug 2026 09:00:00 +0100\r\n"
       b"Content-Type: text/plain\r\n\r\n"
       b"Your fly-tipping report has been resolved. Ref FLY800000001.\r\n")
p = track.parse_eml(eml)
check(".eml parsed: subject, date and body",
      "FLY800000001" in p["subject"] and "resolved" in p["body"]
      and "2026" in p["date"])

# Matching, against a stubbed store - the self-check touches no database.
class _StubStore:
    DONE_STATUSES = ("submitted", "mapped", "completed")
    def all_reports(self):
        return [
            {"id": 7, "reference": "FLY800000001", "report_url": "", "street":
             "Rockingham Road", "status": "submitted"},
            {"id": 8, "reference": "4521879",
             "report_url": "https://highways.northnorthants.gov.uk/report/4521879",
             "street": "Elizabeth Street", "status": "mapped"},
        ]
_real_store = track.store
track.store = _StubStore()
try:
    r, how = track._match_case(["FLY800000001"], "")
    check("reference matches its case", r and r["id"] == 7, str(how))
    r, how = track._match_case(["4521879"], "")
    check("highways id matches via the stored URL", r and r["id"] == 8)
    r, how = track._match_case([], "regarding the issue on Elizabeth Street ...")
    check("unique street mention matches, flagged for checking",
          r and r["id"] == 8 and "check" in how)
    r, how = track._match_case(["FLY0000000"], "no street named here")
    check("an unknown reference is handed back, never guessed", r is None)
finally:
    track.store = _real_store

print("\n" + ("ALL CHECKS PASSED" if not FAILED else f"FAILED: {', '.join(FAILED)}"))
sys.exit(1 if FAILED else 0)
