# NNC Reporter — Environmental Forms Extension

**Date** 23 August 2026 · **Applied to** the local `nnc-reporter` application

The tool now covers **every scenario with an online submission form** on
https://www.northnorthants.gov.uk/report-environmental-issue — six new Granicus
form destinations on top of the original three (highways/FixMyStreet,
fly-tipping, litter/dog bins).

## New form destinations (service → Granicus form)

| Service | Form | Scenarios |
|---|---|---|
| `granicus_streetcare` | `Report_a_street_care_and_cleaning_issue` | litter, dead animals, street cleaning, graffiti, fly-posting |
| `granicus_dog` | `Report_a_dog_issue` | dog fouling |
| `granicus_grounds` | `Report_issues_with_grass_cutting__trees_or_hedges` | grass/trees/hedges (Corby, Kettering, Wellingborough only — East Northants localities get a warning; those go to town/parish councils) |
| `granicus_derelict` | `Report_an_open_or_derelict_property` | open/derelict properties |
| `granicus_vehicle` | `Abandoned_vehicle_report` | abandoned vehicles (untaxed → DVLA, out of scope) |
| `granicus_env` | `Tell_us_about_an_environmental_issue` | accumulations/overgrown gardens, noise, odours/drains, smoke/bonfires, light, dust/vibration |

Knotweed on the highway stays a highways report (existing route).

## What changed in the code

- `nncr/routing.py` — the FORCED table now routes 16 categories to their dedicated forms (graffiti, dog fouling, street cleansing, abandoned vehicles etc. included, matching the council's own signposting); dead animals also recognised from report wording; East-Northants grounds-area warning.
- `nncr/categories.py` — 9 new synthetic categories (Dead animal, Accumulation or overgrown garden, Open or derelict property, Grass/trees/hedges (grounds), and 5 Nuisance categories); synthetic entries now survive a live category refresh (also fixes a latent bug where "Litter or dog waste bin" was lost after refresh). Nuisances that a photo cannot show are never auto-picked by the classifier.
- `nncr/classify.py` — prompt broadened to the new scenarios; per-form word tables for matching each form's "type of issue" dropdown, with the existing model fallback.
- `selectors.json` — six new selector blocks built on the proven Granicus walker pattern; `run-inspect.bat` remains the fix-up path when the council redesigns a form.
- `nncr/submit.py` — new services wired in; the Granicus final/issue steps generalised (form-specific type matching, human-only fields called out in the log — vehicle make/model/colour, nuisance dates/times; number plates still never recorded).
- **Manual reports (no photo)**: new UI button + `POST /api/reports/manual` + `GET /api/geocode` (Nominatim forward geocoding, cached and rate-limited) — for noise/odour/smoke/light/dust, located by address search or a click on the map. Photo question on the form is answered "No".
- `selfcheck.py` — new offline checks: routing table, synthetic categories, selectors coverage per service, type matching. All pass; `checkcode.py` clean.

## Interactive Mapbox maps (added the same day)

New `nncr/webmap.py` renders each profile's submissions as an interactive Mapbox
GL map: clustered pins (click to expand), colours matching the KML broad types,
popups with photo/category/status/reference link/W3W/description, and status
filters (To submit / Submitted / On the map / Completed; submissions on by
default).

- **In-app**: `GET /api/webmap` serves the active profile's map live ("Interactive map" toolbar button).
- **Per-profile export**: `POST /api/webmap/export` ("Write interactive maps" button) writes a self-contained `NNC_Reports[_<profile>]_map.html` per profile next to that profile's KML, readable from each profile's own `reports.db` without switching. Exported files use only the council's published photo URLs.
- **Token handling**: `keys.mapbox` (config.json / keys.json / `MAPBOX_ACCESS_TOKEN`) must be a **public pk. token** — secret sk. tokens are refused so they can never be embedded in a shareable file; recommended scopes styles:read, styles:tiles, fonts:read with URL restrictions if hosted. Missing token or unreachable Mapbox CDN both surface an on-page explanation instead of a blank map. Header shows a "Mapbox on/off" pill.

## Council update reconciliation (added the same day)

New `nncr/track.py` reconciles the council's side of each case into a per-case
timeline (`updates` table; `council_status` + `council_checked_at` columns —
deliberately separate from the tool's own map lifecycle):

- **Highways page sync** — `POST /api/updates/check` ("Check council updates" button on the done tabs): reads each highways case's public FixMyStreet report page (stored `report_url`, or reconstructed from a numeric reference), parses the state banner (`banner--<state>`) with a wording fallback, captures new timeline updates ("Posted by … at …", "State changed to: …", "Investigation: Completed"), dedupes by hash, throttled to one page/second. Verified against a live NNC report page's structure.
- **Email drop-in** — `POST /api/updates/email` ("Add council email…" modal): saved `.eml` files (stdlib email parser, HTML bodies stripped) or pasted text. References matched in priority order: `FLY…` tokens, highways `/report/<id>` URLs, "reference…"-proximity tokens; fallback to a **unique** street-name match (labelled "check it's right"); ambiguous emails handed back, never guessed. Email wording mapped to a state (fixed/closed/in progress/investigating). Duplicate drops recognised and skipped.
- **UI** — "council: <state>" tag per case (green fixed / red closed / amber in-hand), "Updates (n)" expandable timeline, buttons on Submitted / On the map / Completed tabs.
- Full-mailbox integration (Microsoft Graph OAuth for a personal mailbox) was considered and deferred — pages + drop-in cover the need without an app registration.

For the AWS spec: three more baseline behaviours to preserve (per-user interactive
map generation; the manual no-photo intake path; council-update reconciliation with
its `updates` table and page-sync worker — the latter a natural SQS/scheduled job
in the cloud). The `mapbox` public token is genuinely public and may live in
client config rather than Secrets Manager.
