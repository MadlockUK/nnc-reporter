# NNC Reporter

Drop in geotagged photos → it reads the GPS, works out the street and What3Words
address, suggests the right council category, drafts the wording, prefills the
council's own form for you to check and submit, and writes a KML layer of pins for
your Google My Map.

## Setting up from a fresh clone

```
git clone https://github.com/MadlockUK/nnc-reporter.git
cd nnc-reporter
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python -m playwright install chromium
copy config.example.json config.json
```

Then edit `config.json` (your details, and the keys you want to use) and run
`run.bat`. (`setup.bat` does the same steps for you; see below.)

- **Config.** `config.json` is private to your machine and is git-ignored. Never commit it.
- **Keys.** The app looks for API keys in this order: environment variables
  (`ANTHROPIC_API_KEY`, `WHAT3WORDS_API_KEY`, `NNC_HIGHWAYS_PASSWORD`), then
  `%USERPROFILE%\.nnc-reporter\keys.json`, then the `keys` block in `config.json`.
- **My Map.** To link a Google My Map, put its id in `config.json` as
  `"mymap": {"mid": "..."}`. Leave it blank and the My Map buttons stay hidden.
- **Data.** The `data\` folder (reports database, photos, thumbnails, caches) is
  created on first run and is git-ignored. Back it up yourself; it is the only copy of
  your report history.
- **Design notes** are in [`docs/`](docs/).

## First-time setup (Windows)

1. Install Python. Open Command Prompt and paste:

   ```
   winget install -e --id Python.Python.3.12
   ```

   Then **close the window and open a new one** so Windows picks up the change.
   (Or download from [python.org](https://www.python.org/downloads/windows/) and
   **tick "Add python.exe to PATH"** during install.)

   Windows pretends to have Python already: typing `python` opens the Microsoft
   Store instead of running anything. That stub is not usable, which is why the
   real install is step one.
2. Double-click **`setup.bat`**. It builds a private environment, installs the
   packages and the automation browser, and creates `config.json`.
3. Open **`config.json`** and fill in:
   - `reporter` — your name, email, phone. The council forms need these; email is
     how you get the reference number and updates.
   - `keys.what3words` — free key from
     [developer.what3words.com](https://developer.what3words.com/public-api).
     Leave blank and it just uses street + postcode + coordinates instead.
   - `keys.anthropic` — key from [console.anthropic.com](https://console.anthropic.com)
     for the photo categorisation. Leave blank and you pick categories yourself.
4. Double-click **`run.bat`**. Your browser opens at <http://127.0.0.1:8712>.

### Keeping the keys out of a synced folder

If this folder is in OneDrive (or any synced or shared folder), anything in
`config.json` — including a live, billable Anthropic key — syncs to the cloud and
travels with the folder if it's ever shared with anyone.

Run **`move-keys-out.bat`** once. It lifts the keys into
`%USERPROFILE%\.nnc-reporter\keys.json`, blanks them in `config.json`, and confirms
the app still reads them. That file sits outside the synced folder and is not backed up, so
note the keys somewhere safe. Environment variables `ANTHROPIC_API_KEY` and
`WHAT3WORDS_API_KEY` override both if you'd rather use those.

## Using it

1. **Add photos** — drag a batch in. For each one it reads EXIF GPS, looks up the
   street/postcode (OpenStreetMap) and the `///three.word.address`, then has a look
   at the picture and proposes a category, a summary and a description.
2. **Review the "To submit" tab.** Every field is editable, and new photos arrive
   already ticked. Check the flagged ones especially: low AI confidence, coordinates
   outside North Northamptonshire, a number plate or a person visible in shot, or
   something you've already reported within 25m.
3. **Prefill & submit selected.** One browser opens with **a tab per report** — each
   driven through its form, photo attached, stopped at its Submit button. Read each
   one and click Submit yourself, in any order; the tool notices each as it goes
   through and records its reference number — the report id from the highways site,
   or the `FLY…` reference from the fly-tipping form's confirmation page (which it
   reads from inside the form's iframe). If a confirmation ever appears without a
   reference, the report is still marked submitted and you can paste the reference
   into the box on the Submitted tab. Reports for any of the council's forms —
   highways, fly-tipping, bins, street care, dog issues, grounds, derelict
   properties, abandoned vehicles, environmental nuisances — can be mixed in the
   same run. Untick anything you're not ready to send.
4. **The "Submitted" tab** lists everything that went through, with its reference and
   a link to the published report. Tick the ones you want on the map, then
   **Write map layer from selected** — they move to **On the map**.

   **List ref, street & description** turns the ticked reports into plain lines —
   `FLY800000001 — Example Road, Corby — Abandoned shopping trolley left on grass
   verge` — one per report, with a Copy button. Handy for casework notes, a
   councillor's email or a surgery list. The same button is on the On the map and
   Completed tabs.
5. **"On the map"** is exactly what's in `NNC_Reports.kml` right now, waiting to be
   imported. **"Completed"** is everything a later export replaced: already imported
   into My Maps as its own layer, kept as history so you're still warned about
   re-reporting the same spot. Re-exporting a completed report brings it back onto
   the map.
6. In your Google My Map (the **Open Google My Map** button appears once `mymap.mid`
   is set in `config.json`):
   **Add layer → Import → `NNC_Reports.kml`**. Pins are colour-coded by type and each
   one carries the category, status, reference, date, street, What3Words and description.
   The file contains exactly what you ticked, so delete the previous layer before
   re-importing or you'll get duplicates.

### Reconciling council updates with your cases

Once a report is submitted, the council's side of the story comes back two ways,
and both now land on the case's own timeline:

- **Check council updates** (on the Submitted, On the map and Completed tabs) reads
  each highways case's **public report page** — the same page the council's emails
  link to — and records the current state (investigating, action scheduled, fixed,
  closed, no further action…) plus any updates it hasn't seen before. Tick cases to
  check just those, or leave nothing ticked to check them all. It reads one page a
  second, deliberately, so a big history takes a moment.
- **Add council email…** takes the emails for cases with no public page — the
  Granicus forms: fly-tipping, bins, and the environmental forms. Save the email
  from Outlook as a `.eml` file and choose it (several at once is fine), or just
  paste the email's text. The reference (`FLY…`, a highways report number, or
  anything after "your reference is") is matched to the case; if no reference
  appears, a **unique** street-name match is used and labelled as such — an email
  that could belong to two cases is handed back rather than guessed at. Re-adding
  the same email is harmless; duplicates are recognised and skipped.

What the council thinks lives in its own **council:** tag on the case (green when
fixed, red when closed/refused, amber while in hand) — deliberately separate from
the tool's own To submit → Submitted → On the map → Completed lifecycle, which is
about your map workflow, not theirs. Each case's **Updates (n)** button shows the
full timeline: who said what, when, from which source.

### Reporting dashboard — what needs chasing

**Reporting dashboard** (toolbar) opens a page of the active profile's reports set
against what the council has done with them. It is built from `reports.db` alone —
the council states and update timelines put there by *Check council updates* and by
dropping the council's emails in — so it costs nothing to open and needs no network.

What it shows:

- **Where the council has got to.** Every report on the council's own journey: no
  word at all, awaiting triage, investigated with no outcome stated, defect found
  with no repair started, repair under way, fixed, or closed as a duplicate / no
  action / on the inspection programme. Read from the council's own
  *Investigation: Completed (…)* and *Defect Repair* lines first and the banner
  state second.
- **How old the live cases are**, in buckets, against the council's **90-day**
  repair promise — with the oldest live case named and the date its promise falls
  due. The countdown runs on highways reports only: fly-tipping, bins and street
  cleansing go through a different service with no such undertaking.
- **No word from the council.** Cases with no acknowledgement and no update. On the
  Granicus side that means there is no council-side record to chase at all, which
  is usually the email address on the form rather than the council losing it.
- **Duplicates by street** — the same-street, same-batch problem, where several
  photos of one road submitted minutes apart become one case and a pile of
  duplicates.
- **The chase list**: every case, live ones first and oldest first, with the days
  open, the promise countdown, when the council last said anything, and the
  council's own last words on hover. A highways reference links to the council's
  page for that case.
- **Pins from the map** are counted in their own panel — the council never gave
  those an outcome, so they are kept out of the status figures.

Filters (service, when it was reported, position, and a search box) apply to the
whole page at once, and the tiles across the top double as one-click filters. Every
chart has a *Show as a table* link beside it. The page is a single self-contained
HTML document, so it prints and saves as it stands.

### Interactive Mapbox maps — one per profile

Alongside the Google My Maps KML there are live, interactive maps built on Mapbox:

- **Interactive map** (toolbar) opens the **active profile's** submissions in a new
  browser tab, served live from the app — clustered pins that expand on click,
  colour-coded by the same broad types as the KML pins, popups with the photo,
  category, status, reference (linked to the council's report page), What3Words and
  description, and tick-box filters for To submit / Submitted / On the map /
  Completed (submissions on by default).
- **Write interactive maps** writes a **self-contained HTML file per profile** —
  `NNC_Reports_map.html` next to the main profile's KML, `NNC_Reports_<profile>_map.html`
  for the others — built from that profile's own reports. Each file opens anywhere,
  no tool running, so it can be emailed or dropped in a shared folder. Exported
  files show only the council's published photo URLs (a standalone file can't reach
  the app's local photos).

**It needs a Mapbox PUBLIC token** (starts `pk.`) — free at
[account.mapbox.com/access-tokens](https://account.mapbox.com/access-tokens). Give it
only the `styles:read`, `styles:tiles` and `fonts:read` scopes, and put it in
`keys.mapbox` in `config.json` (or `keys.json` / the `MAPBOX_ACCESS_TOKEN`
environment variable, same precedence as the other keys). The token is embedded in
the exported HTML — that is what public tokens are for, but it's also why the tool
**refuses a secret `sk.` token outright** rather than writing it into a shareable
file. If you ever host the maps on a website, add that site as a URL restriction on
the token. Without a token the map page explains what's missing instead of sitting
blank; the header shows a **Mapbox on/off** pill.

### Photos on the map

Google My Maps only displays an image it can fetch over the web — it cannot read a
file from your computer, and it ignores images packaged inside a KMZ. So:

- When a report is submitted, the tool grabs the **published photo address** from the
  council's own report page. Those pins show their photo automatically on import.
- Every export also writes copies of the photos to **`NNC_Reports_photos`**, named
  `<id>-<category>-<street>.jpg` to match the pin, and each pin lists its filename.
  Use those for pins with no published URL: open the pin in My Maps, click the camera
  icon, upload the matching file.

The export tells you how many of each you got.

## Things worth knowing

**It never submits on its own.** By design. Bulk-firing reports at a council without
looking risks wrong categories, duplicate reports and a wasted crew visit — and gets
accounts rate-limited. Prefilling is the slow part; you keep the judgement call.

**Every scenario on the council's ["Report an environmental issue"](https://www.northnorthants.gov.uk/report-environmental-issue)
page that has an online form is covered.** Each one routes to its own Granicus form,
exactly as the council's own pages signpost them:

| Scenario | Category to pick | Form it opens |
|---|---|---|
| Fly-tipping | Flytipping | `Report_fly_tipping` |
| Public litter / dog waste bins | Litter or dog waste bin | `Report_an_issue_with_a_public_litter_or_dog_waste_bin` |
| Litter, street cleaning | Street Cleansing | `Report_a_street_care_and_cleaning_issue` |
| Dead animals | Dead animal | `Report_a_street_care_and_cleaning_issue` |
| Graffiti / fly-posting | Graffiti, Flyposting | `Report_a_street_care_and_cleaning_issue` |
| Dog fouling | Dog fouling | `Report_a_dog_issue` |
| Grass, trees, hedges | Grass, trees or hedges (grounds) | `Report_issues_with_grass_cutting__trees_or_hedges` |
| Open / derelict properties | Open or derelict property | `Report_an_open_or_derelict_property` |
| Abandoned vehicles | Abandoned vehicles | `Abandoned_vehicle_report` |
| Accumulations / overgrown gardens | Accumulation or overgrown garden | `Tell_us_about_an_environmental_issue` |
| Noise, odours/drains, smoke/bonfires, light, dust/vibration | the matching *Nuisance* category | `Tell_us_about_an_environmental_issue` |

Like fly-tipping, these categories are **forced** to their form in `nncr/routing.py` —
the highways site lists some of them (graffiti, dog fouling, street cleansing,
abandoned vehicles) but the council's own reporting pages direct them to the dedicated
forms, so that is where they go. If you disagree for a category, the table at the top
of `nncr/routing.py` is the one place to change it.

Two of the scenarios stay deliberately out of scope: **knotweed on the highway** is
just a highways report (pick a vegetation category), and **untaxed vehicles** go to
the DVLA, not the council.

**The grounds form only covers Corby, Kettering and Wellingborough.** East
Northamptonshire grass/tree/hedge issues go to town and parish councils, so a grounds
report whose locality looks like the former East Northants district gets a warning —
a warning only, since locality names don't follow the old boundaries exactly.

**Reports without a photo.** Noise, smells, smoke, light and dust can't be
photographed, so the **New report without a photo…** button takes a category, a
description and a location — search an address or postcode, or click the spot on the
map. Everything after that is the same queue, the same duplicate checks and the same
prefill run; the form's "do you want to add a photo?" question is answered No. These
nuisance complaints are legal processes the council cannot take anonymously, and they
usually need dates and times — put them in the description.

**The new forms are driven by the same walker as the fly-tipping form** (same
Granicus platform), so an unfamiliar step gets filled by field-label matching and
handed to you rather than guessed at. Details only you can give truthfully — the make,
model and colour of an abandoned vehicle, the dates and times of a nuisance, a
description of a fouling dog — are left for you, and the log says so. The tool still
never records number plates. If the council redesigns one of these forms, run
`run-inspect.bat` against its URL and adjust that form's block in `selectors.json`.

**Public litter and dog waste bins have their own form** —
`Report_an_issue_with_a_public_litter_or_dog_waste_bin`. There's no bin category on
the highways site, so reports get there two ways: pick the **Litter or dog waste bin**
category (the AI can choose it from a photo), or let the description do it — an
overflowing, damaged or missing litter/dog bin is recognised from the wording.

Deliberately excluded: **grit bins** (highways), and **wheelie, recycling or household
bins** (refuse collection, a different service again). Those stay where they were.

**Fly-tipping has to go through the council's separate form.** The highways site
lists a `Flytipping` category, but picking it is a dead end — it just displays a
notice pointing at `forms.northnorthants.gov.uk`. So the routing is automatic and
enforced in four places: when a photo is categorised on intake, when you change a
category in the app, when the app starts (correcting anything queued earlier), and
once more just before each tab opens. It isn't a setting — an override could only
produce a report the council refuses. The rule lives in `nncr/routing.py`.

That form is a five-step wizard — **Introduction, Your details, Nearest street, Photo,
Fly-tipping** — and the tool walks all five: presses *I agree*, fills your name (split
into first and last), phone and email, searches your address by postcode and picks it
from the results, fills the nearest street and description, attaches the photo, fills
the last page and stops at Submit for you.

On the last page the What3Words box gets **only** the three words, the street and
coordinates go in the "further information about the location" box, and **Type of
fly-tipping** is matched against whatever options the dropdown actually offers —
locally by keyword where the summary makes it obvious (free, instant), and by asking
Claude only when it doesn't. If that lands on "Other", the details box it reveals is
filled with the description. Everything is capped at the form's 400-character limit.

Each step is recognised by the fields on it rather than by position, so an extra step
wouldn't derail it. If the form raises a validation message it stops there and repeats
what the form said, rather than pressing on.

**It needs your home address** for the "Your details" step: put your postcode in
`reporter.address_postcode` and enough of the first line in `reporter.address_line`
(e.g. `12 Example Road`) for the tool to pick the right result. Leave them blank and it
stops at that step for you to choose — it won't guess between addresses.

If a category ever turns out to be a dead end like this, the tool spots the notice,
stops without submitting, and switches that report to the fly-tipping form so the next
run goes to the right place.

**Tree, street light and bin reports need something picked off the map.** For those
categories the highways site shows the council's own assets as pins and won't accept
the report until you choose one. The tool asks the page's map which asset is nearest
the photo's GPS and clicks that pin, naming it and its distance in the log.

It only does that within **60 metres** (`asset_max_metres` in `selectors.json`).
Beyond that — or if the layer is empty, or the nearest pin is off-screen — it stops
and leaves the choice to you, rather than reporting the wrong tree. Always worth a
glance at the log line before you press Submit.

**Potholes photographed from the pavement.** The pin goes where you stood, which is
often the footway or verge, and the site then rejects it as somewhere it doesn't
maintain. When it says so, the tool collects every road line within **30 metres**
(`road_max_metres`), ranks them by distance, and tries them in turn — the closest
point on each, plus a spot 10m along in case the nearest point is a junction or the
end of a maintained length. After each move it re-picks the category and checks
whether the site now accepts the spot, stopping at the first that works. **Two moves
at most** (`road_max_tries`) — past that it is guessing rather than correcting.

If none are accepted, it stops and says so. That usually means the road genuinely
isn't the council's — private estate roads and unadopted service roads appear on the
same map — and the report may need to go to the managing agent instead.

Your photo's own coordinates are never changed: they stay in the report description
and on the map layer. Only the council's pin moves, onto the carriageway where the
pothole actually is.

**Emergencies aren't for this tool.** Flooding, fallen trees, unsafe bridges, all
signals out: NNC asks you to phone **0300 126 3000** (9–5 Mon–Fri) or
**01604 651074** out of hours. The tool flags these categories rather than queuing them.

**Google My Maps has no API**, so pins can't be pushed automatically. The KML is a
complete snapshot of every report, so delete the previous layer before re-importing
or you'll get duplicates. Same for What3Words: its API is read-only, so saved lists
in the w3w app can't be written to programmatically.

**The highways form is followed, not scripted.** It puts its current step in the URL
(`#duplicates`, `#details`, `#user`), so the tool reads that, does whatever that step
needs, and waits for the step to change before moving on. A slow-rendering page — the
"similar problems nearby" list especially — can no longer put everything out of step.
If a step fails to advance twice running it stops and names it, rather than typing
into the wrong boxes.

**It signs in to the highways site for you.** Put your account details in
`highways_login` in `config.json` and each submission signs in first, so reports land
in your account, appear under "My reports", and skip the email-confirmation step.
When signed in, the name and email fields are left to the account rather than typed in.

Run **`sign-in.bat`** once to sign in and confirm it works — the session is saved and
subsequent reports are already signed in. Set `highways_login.enabled` to `false` to
report as a guest instead.

**Only one automation browser at a time.** The signed-in session is a Chromium profile
in `%LOCALAPPDATA%\nnc-reporter\browser-profile` (outside any synced folder — a browser profile
in a synced folder gets its lock files copied around and Chromium then refuses to
start). If a window this tool opened is still open, the next report can't start; close
it first. Stale locks left by a crash are cleared automatically.

The password is a secret: run **`move-keys-out.bat`** to shift it (and your API keys)
to `%USERPROFILE%\.nnc-reporter\keys.json`, outside any synced folder. `NNC_HIGHWAYS_PASSWORD`
as an environment variable overrides both.

**Photos with no GPS are skipped**, with the reason shown — usually location
services were off, or the image was screenshotted/re-saved and lost its EXIF.
iPhone HEIC files are read directly and converted to JPEG for upload.

**Oversized photos are shrunk automatically.** The council rejects anything over
15MB, and modern phone photos often exceed that. Before uploading, any photo over
10MB is resized to 3000px on its longest edge and re-encoded, dropping to a few MB
in well under a second — locally, with no API involved. Rotation is applied first so
the stripped copy can't come out sideways, and the result is cached so a batch isn't
slowed down. Your original file is never touched.

## If setup won't run

**"Python was not found; run without arguments to install from the Microsoft
Store"** — that's the Windows stub, not real Python. Do step 1 above, then close and
reopen the Command Prompt window before retrying `setup.bat`.

**Setup can't create the virtual environment** — if this folder is in OneDrive, right-click
the `nnc-reporter` folder → **Always keep on this device**, then retry.

**Windows SmartScreen blocks the .bat files** — click *More info* → *Run anyway*, or
right-click each `.bat` → Properties → tick *Unblock*.

## If a form stops being filled in

Councils redesign forms. Nothing is hardcoded in the Python: `selectors.json` holds
lists of candidate selectors per field, tried in order. Run **`run-inspect.bat`** to
dump the live form's real field ids to `data/form-inspect.txt`, then add the correct
selector to the top of the relevant list.

`run-inspect.bat` and the log panel in the app are the two places to look when
something misbehaves — the log names the exact field it couldn't find.

## Checking the install

**`check-keys.bat`** — confirms your What3Words key, the street lookup and the photo
categorisation all respond, using one test coordinate in Kettering. Run this after
editing `config.json`.

**`selfcheck.py`** (`.venv\Scripts\python.exe selfcheck.py`) — offline tests of EXIF
parsing, category lookup, report wording and KML generation. Touches no data, submits
nothing, needs no keys.

**`checkcode.py`** — scans every module for names used but never defined, which is
what a bad edit usually leaves behind. Both run automatically at the end of setup.

## Layout

```
config.json          your details and API keys (local only)
selectors.json       form field selectors, editable when a council form changes
app.py               web server + photo intake pipeline
selfcheck.py         offline tests
nncr/photos.py       EXIF GPS, timestamps, HEIC, thumbnails, dedupe hashing
nncr/geo.py          OpenStreetMap reverse geocoding + What3Words (cached)
nncr/classify.py     photo → category, summary, description
nncr/categories.py   the 69 live NNC categories (refreshable from the site)
nncr/submit.py       Playwright form prefill, stops at Submit, captures reference
nncr/kml.py          KML + CSV export for Google My Maps
nncr/webmap.py       interactive Mapbox map, live and exported
nncr/dashboard.py    reporting dashboard - council positions, ageing, chase list
nncr/store.py        SQLite queue and permanent report history
data/                photos, thumbnails, reports.db, browser profile, caches
```

Report history lives in `data/reports.db` and is what stops the same photo, or the
same issue at the same spot, being reported twice. Keep it.

## Attribution

Ward boundaries: Office for National Statistics (May 2026), Open Government Licence
v3.0. Contains OS data © Crown copyright and database right 2026.
