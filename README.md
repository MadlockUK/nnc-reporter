# NNC Reporter

A web app for a group of invited people to report street issues to North
Northamptonshire Council. Drop in geotagged photos and it reads the GPS, works out the
street and What3Words address, suggests the right council category and drafts the
wording. It fills in the council's own form on the server, and you check it and approve
each submission. Everything the group sends goes onto a shared map and summary, by ward,
town council and town council ward.

- **Invite-only sign-in.** There is no sign-up. Strong passwords, two-factor sign-in,
  lockout after repeated failures, and password reset by email.
- **Your own reports stay yours.** Each person has their own reports, photos and
  council sign-in. What they *submit* joins the group's shared layer.
- **Submitting happens in the web app.** The server's browser fills each form to its
  Submit button and shows you the form. Nothing is sent until you press
  **Submit to council** for that report.
- **Every boundary that matters:** the council itself, its 31 unitary wards, the town
  councils, and the town councils' own wards where they differ from the unitary wards.

Design notes and the security model are in
[`docs/NNC-Reporter-Web-App.md`](docs/NNC-Reporter-Web-App.md).

## Running it for your group

You need a small Linux server (2 vCPU / 2–4 GB is plenty for a group) with Docker, and
a DNS name pointing at it.

```
git clone https://github.com/MadlockUK/nnc-reporter.git
cd nnc-reporter
cp .env.example .env        # then edit it - see "Settings" below
NNC_DOMAIN=reports.example.org docker compose up -d --build
docker compose logs app     # the first admin's invitation link is here
```

[`docker-compose.yml`](docker-compose.yml) runs the app behind
[Caddy](https://caddyserver.com), which gets and renews the HTTPS certificate itself.
All data (accounts, reports, photos, boundaries) lives in the `nnc-data` volume, so
**back that volume up**.

Any host that runs a container works the same way: Fly.io, Render, Railway, Azure
Container Apps, AWS Lightsail or ECS. Run the image built from the
[`Dockerfile`](Dockerfile), give it a persistent volume at `/data`, set the
environment variables, and put HTTPS in front of it.
[`docs/NNC-Reporter-AWS-Requirements.md`](docs/NNC-Reporter-AWS-Requirements.md) covers
a larger AWS set-up if the group grows.

### The first admin

Set `NNC_BOOTSTRAP_ADMIN=you@example.com` before the first start. With no accounts
yet, the app invites that address and prints the link in its log (and emails it, if
email is set up). Alternatively, run this inside the container:

```
python -m nncr.web.manage create-admin --email you@example.com
```

Open the link, choose a password and set up two-factor sign-in (admins must have it).
Then invite everyone else from **Admin**.

### Settings

Everything is set with environment variables. Use your host's secret store, or the
git-ignored `.env` file. **Never commit a secret to the repository.**
[`.env.example`](.env.example) lists them all; these are the ones that matter:

| Variable | What it does |
|---|---|
| `NNC_BASE_URL` | The address people use, e.g. `https://reports.example.org`. Used in emailed links; an `https://` address also turns on secure cookies and HSTS. |
| `NNC_SECRET_KEY` | 32+ random characters. Encrypts the council passwords people save. Keep it with your other secrets; if it is lost, people re-enter those passwords. |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `SMTP_SECURITY` | Email for invitations, password resets and security notices. Any provider works. Without it, links are written to the server log instead. |
| `MAPBOX_SECRET_TOKEN` | The secret Mapbox token (`sk.…`). See below. |
| `ANTHROPIC_API_KEY` | Suggests the category and wording from each photo. Optional. |
| `WHAT3WORDS_API_KEY` | Adds the `///three.word.address`. Optional. |
| `NNC_MFA_REQUIRED` | Who must use two-factor sign-in: `admins` (default), `all` or `none`. |
| `NNC_SUBMIT_MODE` | `server` (default): forms are filled on the server and approved in the app. `visible`: a browser window opens on the computer running the app, for desktop use. |
| `NNC_MAX_BROWSERS` | How many headless browsers may run at once (~1 GB each). Default 2. |
| `NNC_BOOTSTRAP_ADMIN` | The first admin, invited on first start if there are no accounts. |

`python -m nncr.web.manage check` shows what is set, without printing any secret.

### The Mapbox token

The secret token is kept on the server only, in `MAPBOX_SECRET_TOKEN`. It is never in
the repository and never in a web page. Signed-in browsers are given a **temporary
token** minted from it through Mapbox's Tokens API. That token has read-only map
scopes, lasts under an hour, and is renewed automatically. Anyone who copies one out of
a browser gets a map-viewing token that expires within the hour.

For this to work:

1. The secret token needs the **`tokens:write`** scope (tick it when creating the
   token at account.mapbox.com/access-tokens). It also needs the usual
   `styles:read`, `styles:tiles` and `fonts:read`.
2. Put it in the server's environment as `MAPBOX_SECRET_TOKEN`. If you deploy with
   GitHub Actions, store it as a repository secret (Settings → Secrets and variables →
   Actions) and pass it to the host from there.
3. Admin → **Test the Mapbox token** confirms it works.

Mapbox URL restrictions only apply to *public* (`pk.`) tokens, and they check the
address of the page the map is on, so a restriction to a GitHub address would not match
the app. If you also want a public fallback, make a `pk.` token restricted to your
`NNC_BASE_URL` and set it as `MAPBOX_PUBLIC_TOKEN`.

## Accounts and security

- **Invite only.** Admins invite people by email; there is no sign-up page.
  Invitations work once, for 7 days. Admins can resend, suspend, reactivate, change
  roles, unlock, reset two-factor, sign someone out everywhere, or remove an account.
  Removing an account either keeps that person's reports (set aside on the server) or
  purges them.
- **Roles.** *Editors* report issues and see the shared map. *Viewers* (councillors,
  colleagues) see the shared map and summaries only, and cannot change anything.
  *Admins* also manage people and settings.
- **Passwords must be strong.** A password needs:
  - at least 12 characters;
  - upper and lower case letters, a number and a symbol;
  - none of your name or email address;
  - no runs like `aaaa` or `1234`;
  - not to be guessable, judged by [zxcvbn](https://github.com/dropbox/zxcvbn);
  - not to appear in a known data breach. This is checked with Have I Been Pwned's
    k-anonymity API: only the first five characters of the password's SHA-1 hash
    leave the server.

  Passwords are stored as Argon2id hashes only.
- **Two-factor sign-in** uses an authenticator app (TOTP), with one-time recovery codes.
  It is required for admins and available to everyone. Set `NNC_MFA_REQUIRED=all` to
  require it for everyone. A code can't be used twice.
- **Brute force is stopped.**
  - Five wrong passwords lock the account for 15 minutes, doubling on repeats, and
    the owner is emailed.
  - Twenty failures from one address in 15 minutes block that address for a while.
  - Wrong-email and wrong-password failures give the same answer in the same time,
    so nobody can find out who has an account.
- **Password reset by email.** The reset link works once, for an hour. Using it signs
  every device out and emails a notice. **Account** also has change password,
  two-factor set-up, where you are signed in, and *download my data*.
- **Every page and API call needs a valid session.** The gatekeeper middleware is
  deny-by-default, so a new route is protected without anyone remembering to protect
  it. The tests check every route.
  - Sessions are random 256-bit values, stored only as hashes.
  - Session cookies are `HttpOnly`, `Secure` (`__Host-`) and `SameSite=Lax`.
  - A session ends after 2 hours idle or 12 hours at most.
  - Changes must come from the site itself (Origin check) and carry the session's
    CSRF token.
  - Pages are served with a strict Content-Security-Policy, no framing, and HSTS.
- **Each person's data is separate on disk** (`/data/users/u<id>/`). Each request is
  bound to the signed-in person's folder, so report code cannot read anyone else's.
  Council passwords are encrypted and are never sent back to a browser.
- **An audit trail** records sign-ins, failures, lockouts, invitations, role changes,
  resets and each "Submit to council" approval. It is on the Admin page.

## Submitting to the council, from the web

The council has no API, so the tool fills in its web forms as a person would. In the
web app this happens on the server:

1. Tick reports and press **Prefill selected for review**. A headless browser on the
   server walks each form to its Submit button (one browser per person, a few at
   most across the server).
2. For each report, the **Submitting** panel shows a picture of the filled-in form
   and every value in its fields.
3. Press **Submit to council** on the ones to send. The server presses that form's
   Submit button and records the reference from the confirmation page. **Don't send**
   closes a form unsent, and nothing is ever sent without that press.
4. If a form stops part-way (a new question, an asset to pick), open the **Live view**.
   It is the server's browser, shown in the app: click on it, type into it, scroll it,
   finish it, then submit.

The server's browser only visits the council's own sites. Clicking a link elsewhere in
the live view is refused and logged. Unused forms close after 15 minutes. The tool
still never submits on its own.

If running this from a server isn't wanted, the alternatives are:

- **Desktop mode.** Run the app on your own computer with `NNC_SUBMIT_MODE=visible`.
  The forms open in a real browser window and you press Submit yourself, as before.
- **Hybrid.** Host everything else, and run submissions from a small local agent on
  each person's PC. This is option B in the AWS requirements doc.

Council traffic then comes from a home connection rather than a server. That matters
only if the council's sites ever start refusing server addresses.

## The shared map and summary

- **Shared map** (for every role) is a Mapbox map of every report the group has sent
  to the council. Drafts are never shared.
  - Pins are clustered, colour-coded and filterable by type, stage, date and person.
  - Popups give the ward, town council, the council's latest position and the
    reference.
  - The boundary layers are the council, unitary wards, town councils, town council
    wards (where they differ, or all of them), and parish councils.
  - **Shade wards by reports shown** turns it into a choropleth.
  - **Pick ward** picks any ward, town council or town council ward for area
    statistics, with breakdowns by ward, town council, town council ward and reporter.
- **Summary** has tables of the same reports by unitary ward, town council, town
  council ward, parish, category, type, month, reporter or the council's position.
  You can filter by date, category, ward and town, and download each table as CSV.
- **Downloads.** The whole layer, or any filtered slice of it, downloads as GeoJSON,
  KML (for Google My Maps) or CSV.
- **Duplicate warnings span the group.** A photo of something a team-mate already
  reported within 25 m is flagged at upload.
- Reports show as "Team member" unless that person ticks *Show my name against my
  reports on the shared map* on their account page. Admins always see who reported
  what.

## Boundaries

**Admin → Boundaries → Fetch boundaries from ONS** downloads the council boundary, its
wards (May 2025) and every parish in the area (December 2025) from the ONS Open
Geography Portal. You can also run `python -m nncr.web.manage fetch-boundaries`. The
server needs internet access to do this; until then, the ward file bundled with the app
is used.

- **Town councils.** Tick which parishes are town councils on the same page.
  Burton Latimer, Corby, Desborough, Finedon, Higham Ferrers, Irthlingborough,
  Kettering, Oundle, Raunds, Rothwell, Rushden, Thrapston and Wellingborough are
  ticked to start with. **Check this list**, because community governance reviews
  change it.
- **Town council wards** are not published nationally. The app derives them by cutting
  each town council along the unitary ward lines, which is how the
  [North Northamptonshire (Electoral Changes) Order 2024](https://www.legislation.gov.uk/uksi/2024/157/made)
  drew most of them. Where a town's wards are drawn differently (Kettering Town's
  Hanwood Park, Ise Lodge and Wicksteed, for example), upload the official boundaries
  as GeoJSON in WGS84 with a `name` (and ideally a `parish`) on each shape. The
  council's GIS team or the LGBCE can supply them. Uploaded wards replace the derived
  ones for that town, and the app works out which of them differ from the unitary
  wards; those are drawn on the map by default.

## Running it on your own computer (Windows)

1. Install Python: `winget install -e --id Python.Python.3.12`, then open a new
   Command Prompt. See *If setup won't run* below.
2. Double-click **`setup.bat`**. It builds the environment, installs the packages and
   the browser, and creates `.env`.
3. Edit **`.env`**: add the API keys and the Mapbox token.
4. Double-click **`add-admin.bat`** and enter your email. It prints your invitation
   link.
5. Double-click **`run.bat`**. The app opens at <http://127.0.0.1:8712>; open the
   invitation link and set your password.

Locally, `NNC_SUBMIT_MODE=visible` in `.env` gives the old behaviour: a browser window
you submit from yourself.

### Bringing over your existing reports

Your desktop install's history (reports, references, photos, My Map pins) can move into
your web account:

```
python -m nncr.web.manage import-local --email you@example.com --from path\to\nnc-reporter\data
```

Add `--profile name` for one of the old extra profiles. The old folder is left
untouched.

## Using it

1. **Add photos** — drag a batch in. For each one it reads EXIF GPS, looks up the
   street/postcode (OpenStreetMap) and the `///three.word.address`, then has a look
   at the picture and proposes a category, a summary and a description.
2. **Review the "To submit" tab.** Every field is editable, and new photos arrive
   already ticked. Check the flagged ones especially: low AI confidence, coordinates
   outside North Northamptonshire, a number plate or a person visible in shot, or
   something you've already reported within 25m.
3. **Prefill selected for review.** Each report is driven through its council form,
   photo attached, and stopped at its Submit button (see *Submitting to the council,
   from the web* above). Check each and press **Submit to council**, in any order; the
   tool notices each as it goes through and records its reference number — the report id from the highways site,
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
6. Download `NNC_Reports.kml` (it downloads as you write the layer, and there is a
   link on the right), then in your Google My Map (the **Open Google My Map** button
   appears once its link is on your account page):
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

**Reporting dashboard** (toolbar) opens a page of your own reports set
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

**It needs your home address** for the "Your details" step: put your postcode and
enough of the first line (e.g. `12 Example Road`) on your **Account** page for the
tool to pick the right result. Leave them blank and it
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

**It signs in to the highways site for you.** Put your highways sign-in email and
password on your **Account** page (the password is stored encrypted and never shown
again) and each run signs in first, so reports land in your account, appear under "My
reports", and skip the email-confirmation step. When signed in, the name and email
fields are left to the account rather than typed in. Untick *Sign in to the highways
site automatically* to report as a guest instead.

**One submission run per person at a time.** Each person's signed-in highways session
is a Chromium profile in their own data folder, never shared. Stale locks left by a
crash are cleared automatically.


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
lists of candidate selectors per field, tried in order. Run **`run-inspect.bat`** (or
`python app.py --inspect --url <form address>`) to dump the live form's real field ids
to `data/form-inspect.txt`, then add the correct selector to the top of the relevant
list. In the web app, the **Live view** shows exactly where a form stopped.

The **Submitting** panel's log (with its *Copy log* button) and `run-inspect.bat` are
the places to look when something misbehaves — the log names the exact field it
couldn't find.

## Checking the install

**`check-keys.bat`** — confirms your What3Words key, the street lookup and the photo
categorisation all respond, using one test coordinate in Kettering. Run this after
editing `.env`.

**`selfcheck.py`** (`.venv\Scripts\python.exe selfcheck.py`) — offline tests of EXIF
parsing, category lookup, report wording and KML generation. Touches no data, submits
nothing, needs no keys.

**`checkcode.py`** — scans every module for names used but never defined, which is
what a bad edit usually leaves behind. Both run automatically at the end of setup.

**`tests/`** (`python -m pytest tests`) — the web app's tests:
- **Sign-in can't be bypassed.** Every route is checked without a session. Also
  covered: CSRF and origin, the password rules, lockout, resets and invitations,
  two-factor (including replay), and role limits.
- **Data and the shared layer.** One person can't see another's reports; the shared
  layer, summaries and downloads; Mapbox's secret token never reaching a page; the
  boundary processing.
- **Server-side submission.** A real headless browser fills a form, waits for
  approval, presses Submit, captures the reference, and refuses to leave the council's
  sites.

`python -m nncr.web.manage check` shows the server's configuration without printing a
secret.

## Layout

```
.env                 settings and secrets for running locally (git-ignored)
selectors.json       form field selectors, editable when a council form changes
app.py               starts the web app
Dockerfile, docker-compose.yml, deploy/Caddyfile   hosting
nncr/web/server.py   the app: routes and pages wired together
nncr/web/auth.py     sign-in, sessions, the deny-by-default gatekeeper
nncr/web/security.py password rules and hashing, two-factor, encryption
nncr/web/account.py  your account; admin.py - people and settings
nncr/web/reports.py  the reporting tool's API, per signed-in person
nncr/web/org.py      the shared map, summary and downloads (aggregate.py)
nncr/web/boundaries.py council, wards, town councils and their wards
nncr/web/mapbox.py   short-lived browser tokens from the secret token
nncr/web/manage.py   command line: first admin, imports, boundary fetch
selfcheck.py         offline tests; tests/ - the web app's tests
nncr/photos.py       EXIF GPS, timestamps, HEIC, thumbnails, dedupe hashing
nncr/geo.py          OpenStreetMap reverse geocoding + What3Words (cached)
nncr/classify.py     photo → category, summary, description
nncr/categories.py   the 69 live NNC categories (refreshable from the site)
nncr/submit.py       Playwright form prefill, stops at Submit, captures reference
nncr/kml.py          KML + CSV export for Google My Maps
nncr/webmap.py       interactive Mapbox map, live and exported
nncr/dashboard.py    reporting dashboard - council positions, ageing, chase list
nncr/store.py        SQLite queue and permanent report history
data/                accounts.db, boundaries/, caches, and users/u<id>/ - each
                     person's reports.db, photos, thumbnails and browser profile
```

Each person's report history lives in their `reports.db` and is what stops the same
photo, or the same issue at the same spot, being reported twice. Back up the whole
data folder (the `nnc-data` volume when using Docker).

## Attribution

Boundaries: Office for National Statistics (wards May 2025, parishes December 2025),
Open Government Licence v3.0. Contains OS data © Crown copyright and database right
2026. Map data © Mapbox © OpenStreetMap contributors. Leaflet (BSD-2-Clause) is
bundled in `nncr/web/assets/vendor/leaflet`.
