# Reporting dashboard — built into the tool, 9 September 2026

**Where** `nncr/dashboard.py`, served at `GET /api/dashboard`, opened by a **Reporting dashboard** button in the toolbar beside *Interactive map*. Built the same way as `nncr/webmap.py`: one self-contained HTML page with the figures embedded, so it filters without calling back into the app and needs no network. Restart the app (port 8712) to pick up the new route.

**What it answers** — chosen over an activity view — is *what needs chasing*.

## Panels

| Panel | What it shows |
|---|---|
| Tiles | Live cases · past the 90-day promise · promise due within 14 days · defect found with no repair · no word from the council · fixed · closed as a duplicate. Each doubles as a one-click filter. |
| Where the council has got to | Every report on the council's journey, ten positions in journey order. Click a row to filter the page. |
| How old the live cases are | Age buckets for live cases, over-90 in red, with the oldest live case named and the date its promise falls due. |
| Reported by month | Each walk, split still-live against fixed-or-closed. |
| Streets with the most live cases | Where one chase covers the most ground. |
| The chase list | Every case: live first, oldest first. Days open, promise countdown, when the council last spoke, its own last words on hover. A highways reference links to the council's page for that case. |
| Closed as duplicates of each other | The same-street, same-batch problem, by street. |
| Pins from the map | The merged pins, counted separately — the council gave none of them an outcome. |

Filters: service, when reported, position, free-text search. Every chart has a *Show as a table* link. Light and dark, and it prints as it stands.

## How the positions are derived

`position_for()` reads the council's own words first — the `Investigation: Completed (…)` and `Defect Repair:` lines in the `updates` table — and the banner state in `council_status` second:

`silent` (no updates at all) · `triage` (acknowledged, not yet investigated) · `assessed` (investigated, outcome not stated) · `defect` (defect found, repair not started) · `repairing` · `fixed` · `duplicate` · `noaction` · `monitor` · `closed`. The first five count as live.

Update bodies are stored trimmed, so a repair line can exist on the council's page and not here; that lands in `assessed` rather than being guessed either way.

## Dates and the 90-day promise

`submitted_at` is never populated by the tool, so the reported date comes from the council's own *"your report has been logged"* email where there is one, otherwise `created_at` / `taken_at` — the day of the walk — and is marked ≈ on the page.

The promise countdown runs on highways reports only. Fly-tipping, bins and street cleansing go through Granicus, which gives no such undertaking; they get an age but no date to hold the council to.

## Other changes in the same edit

- `app.py` — `dashboard` added to the `nncr` import; new `GET /api/dashboard` route.
- `nncr/static/index.html` — the toolbar button.
- `README.md` — a *Reporting dashboard* section and the two new modules in the layout list.
- `selfcheck.py` — section 5b, eleven offline checks on the position rules, the date preference and the page shell. All pass, and they touch no database.

## Known limits

- The page is a read of `reports.db` as it stands. It only moves when the council's updates reach the tool — *Check council updates* against the report pages, or dropping the council's emails in. Reload after either.
- `nncr/track.py`'s `state_in_email()` has a known bug (see the issue tracker); emails dropped in through the app will write a mangled `council_status` and show up here as whatever that parses to.
- Map pins carry no council outcome, so they can never join the status figures — they stay in their own panel.
