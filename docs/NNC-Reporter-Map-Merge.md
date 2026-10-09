# NNC Reporter — Interactive map: My Map merge, ward borders and area statistics

**Date** 6 September 2026 · **Author** Alexander Lock (with Claude) · **Applies to** `nnc-reporter` local app, profile `main`

## What changed

The tool's interactive Mapbox map (`/api/webmap`, and the "Write interactive maps" export) now shows three things together:

1. **Every report the tool has made** (as before — reports.db).
2. **Every pin from the configured Google My Map** (`mymap.mid` in `config.json`), merged without duplicating anything the tool already knows about.
3. **North Northamptonshire Council ward borders** — the current wards (ONS "Wards (May 2026) Boundaries UK BFC", i.e. the arrangements in force since the May 2025 elections), full-detail geometry for Corby West, Kingswood, Lloyds & Corby Village, Oakley, Gretton & Weldon and Geddington & Stanion, generalised for the other 25 NNC wards.

Plus an **Area statistics** panel: draw an area, pick a ward, or use the visible map; set *Since* / *Until*; get a count, fly-tipping vs road/footway split, open vs completed, a reports-over-time chart (month or week), a by-type breakdown and the most-reported streets. **Showcase** hides the controls and lays the card out for a screenshot; **Save PNG** writes the map plus the stat card as one image (`nnc-reports-<area>-<date>.png`).

## Where the merged pins came from — reconciliation of the My Map (6 Sep 2026)

| On the My Map | Count | What happened |
|---|---|---|
| Placemarks in total | 163 | |
| Pins the tool exported itself (the Pothole / Flytipping / Other folders, and a couple of FLY refs in "Completed") | 46 | **Matched to existing reports** by reference (or by position + type for the one without a reference) and left alone — reports.db stays the source of truth |
| Hand-placed pins ("Completed" history, "Outstanding Issues", early pothole and FLY refs, a landmark) | 115 | **Imported into `map_pins`** |
| Drawn polygons (hand-drawn areas) | 2 | **Imported into `map_areas`** — one is shown; the hand-drawn ward outline is kept but off by default, superseded by the official ward border |
| Photos on those pins | 102 | **All cached locally** in `data/pins/` (0 failures). Google's `mymaps.usercontent.google.com` image host refuses to serve them to any other site, so the map uses the local copies; exported HTML embeds thumbnails |

Notes on the data:

- **Same reference on two pins**: a reference can appear twice on the My Map for different spots. Both pins are kept and the importer reports the duplicate so it can be checked against the council's system.
- **Dates**: 17 pins carry an exact date (the 25 July 2026 batch and the tool's own). 79 pins had no date at all, only a FLY reference number; because Granicus issues those in sequence, the importer **estimates** a date from the references the tool does have dates for. These are flagged `≈` on the map and in the stats footer, and can be excluded with *include ≈ dated*. Precision is roughly a week for summer 2026, a month or more for 2025. 18 pins have no date and no usable reference.
- **Status**: "Completed" folder → completed; "Outstanding Issues" → open; anything else with a council reference → completed (it was reported and has since dropped off the tool's list).
- **Types**: FLY → fly-tipping, PB → litter/dog bin, GroundM → grounds maintenance (vegetation), AbnVeh → abandoned vehicle; speeding / parking / HMO / empty house → *Community issue* (new pink colour); the landmark pin → *Landmark* (teal).

## How to use it

- **Merge My Map pins** (main screen) or **Refresh My Map pins** (on the map) fetches the My Map again and re-runs the merge. Safe to repeat: pins are keyed by reference (or name + position) so they update rather than double.
- Manual file: `POST /api/pins/import/file` with a KML/KMZ exported from My Maps. (An export with "keep up to date" ticked is only a NetworkLink stub — the importer follows it.)
- `GET /api/pins` lists pins, areas and photo-cache progress; `POST /api/pins/cache` retries any photo that failed; `DELETE /api/pins` removes everything merged (re-import brings it back); `GET /api/wards` serves the ward GeoJSON.
- Config: `mymap.mid` in `config.json` (blank by default; the My Map buttons stay hidden until it is set).

## Files

| File | Purpose |
|---|---|
| `nncr/mymaps.py` | Fetch / parse the My Map, match against reports, upsert `map_pins` / `map_areas`, cache photos |
| `nncr/webmap.py` | The map page: merged layers, ward borders, area statistics, showcase, PNG export, HTML export with embedded thumbnails |
| `nncr/static/wards_nnc.geojson` | 31 NNC wards, WGS84, from the ONS Open Geography portal (OGL v3; contains OS data © Crown copyright 2026) |
| `nncr/store.py` | Schema: `map_pins`, `map_areas` tables added |
| `app.py` | Endpoints under `/api/pins/*`, `/api/wards`; `/api/webmap` merges everything |
| `nncr/config.py` | `mymap.mid` default (blank) |
| `nncr/static/index.html` | "Merge My Map pins" button |
| `_map_import/` (kept outside the repo) | The KML snapshot used, the patch scripts, and the pre-change originals |

## Design decisions

- Merged pins live in their **own table**, not in `reports`: they were never submitted through the tool, so they must not enter the queue, the KML export (which would double them on the My Map) or the duplicate guard's "already submitted" set. FR-28's org-wide duplicate guard could later read `map_pins` too.
- The tool's own pins on the My Map are **never** copied back — position/type matching is only attempted for pins carrying the tool's ExtendedData signature, so a hand pin on the same spot as a later report stays a separate event.
- Ward borders come from the ONS, not from the hand-drawn polygon, and the label is placed once per ward at its centroid.

## Open items

- Restart the app after pulling new code (it does not hot-reload).
- `photo_url` on the tool's own reports is still never populated (B-50 scraping) — the live map uses `/api/photo/{id}` and exports embed thumbnails, so nothing is lost, but the KML pins still have no picture on My Maps.
- `config.json` can hold the highways password; `move-keys-out.bat` moves it out of a synced folder.
