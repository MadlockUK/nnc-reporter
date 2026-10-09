"""Reporting dashboard - where the council has got to with everything reported.

Served live at GET /api/dashboard and built the same way as the interactive map
(nncr/webmap.py): one self-contained HTML page with the figures embedded, so it
opens in its own browser tab and filters without asking the app for anything.

The question it answers is "what needs chasing":

  * where every submitted report sits on the council's own journey - awaiting
    triage, defect found and no repair yet, fixed, closed as a duplicate;
  * how old the live ones are against the council's 90-day repair promise, and
    which have already passed it;
  * which reports the council has never said a word about, which on the
    Granicus side (fly-tipping, bins, street cleansing) means there is no
    council-side record to chase at all;
  * where the duplicates come from - several photos of one street, submitted
    minutes apart, closed as duplicates of each other.

Pins merged in from the Google My Map and the council portal are counted too,
but kept in their own panel: the council never gave those an outcome, so mixing
them into the status figures would flatter them.
"""
import json
import re
from datetime import date, datetime, timedelta

from . import config, store

# The council's promise: a confirmed carriageway defect repaired within 90 days
# of the report. Everything on this page counts from the day the report was
# logged, which is how the council's own correspondence counts it.
PROMISE_DAYS = 90
DUE_SOON_DAYS = 14          # inside this many days of the promise = worth chasing now
SILENT_AFTER_DAYS = 14      # no word from the council after this long = a gap, not a wait

# Where a report can sit. Order is the journey, not the size - the chart reads
# the same whatever the filters do to the counts.
POSITIONS = [
    ("silent",    "No word from the council",            "critical", True),
    ("triage",    "With the council, awaiting triage",   "info",     True),
    ("assessed",  "Investigated, outcome not stated",    "info",     True),
    ("defect",    "Defect found - repair not started",   "warning",  True),
    ("repairing", "Repair under way",                    "serious",  True),
    ("fixed",     "Fixed",                               "good",     False),
    ("duplicate", "Closed - duplicate of another report", "muted",   False),
    ("noaction",  "Closed - no action necessary",        "muted",    False),
    ("monitor",   "Closed - on the inspection programme", "muted",   False),
    ("closed",    "Closed - no reason given",            "muted",    False),
]
POS_LABEL = {k: lbl for k, lbl, _t, _l in POSITIONS}
POS_TONE = {k: t for k, _lbl, t, _l in POSITIONS}
LIVE = {k for k, _lbl, _t, live in POSITIONS if live}

SERVICES = [
    ("fixmystreet", "Highways (potholes, footways, drains)"),
    ("granicus", "Fly-tipping"),
    ("granicus_bins", "Bins"),
    ("granicus_streetcare", "Street cleansing"),
]
SERVICE_LABEL = dict(SERVICES)

# The council's own page for a highways case. The tool stores the form URL it
# filled in rather than the case page, so the case page is built from the
# reference - the address the council's own emails give.
HIGHWAYS_CASE = "https://northnorthants.fixmystreet.com/report/{}"

INVEST_RE = re.compile(r"Investigation:\s*Completed\s*(?:\(([^)]*)\))?", re.I)
REPAIR_RE = re.compile(r"Defect Repair:\s*(Not Started|Work Completed|Ongoing|In Progress)", re.I)
LOGGED_RE = re.compile(r"report has been logged|reference number is", re.I)


def _d(value) -> date | None:
    """The first 10 characters of anything the database calls a date."""
    s = str(value or "").strip()
    if len(s) < 10:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _updates_by_report() -> dict:
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT report_id, source, happened, body, state FROM updates "
            "ORDER BY report_id, happened, id")]
    out = {}
    for r in rows:
        out.setdefault(r["report_id"], []).append(r)
    return out


def _outcome(ups: list) -> tuple:
    """The council's latest investigation result and repair line, as it wrote
    them. Update bodies are stored trimmed, so a repair line can be missing from
    a case that has one on the report page - hence 'not stated' as its own
    answer rather than an assumption either way."""
    invest = repair = ""
    for u in ups:
        body = u.get("body") or ""
        m = INVEST_RE.search(body)
        if m:
            invest = (m.group(1) or "stated").strip()
        m = REPAIR_RE.search(body)
        if m:
            repair = m.group(1).strip().lower()
    return invest, repair


def position_for(row: dict, ups: list) -> str:
    """Which bucket this report sits in, from the council's own words first and
    the banner state second."""
    council = (row.get("council_status") or "").strip().lower()
    if not ups:
        return "silent"
    invest, repair = _outcome(ups)
    low = invest.lower()
    if repair == "work completed" or council == "fixed":
        return "fixed"
    if not invest:
        return "triage"
    if "duplicate" in low:
        return "duplicate"
    if "no action" in low:
        return "noaction"
    if "monitor" in low or "inspection" in low:
        return "monitor"
    if "defect found" in low:
        if repair in ("ongoing", "in progress"):
            return "repairing"
        return "closed" if council == "closed" else "defect"
    return "closed" if council == "closed" else "assessed"


def reported_on(row: dict, ups: list) -> tuple:
    """When the council logged it, and how sure we are of that. The council's
    own "your report has been logged" email is the truth where we have it;
    otherwise the day the tool took the photo in, which is the day of the walk -
    the tool submits in the same sitting - and is marked approximate."""
    for u in ups:
        if LOGGED_RE.search(u.get("body") or ""):
            d = _d(u.get("happened"))
            if d:
                return d, "council"
    for field, how in (("submitted_at", "tool"), ("created_at", "tool"),
                       ("taken_at", "photo"), ("mapped_at", "tool")):
        d = _d(row.get(field))
        if d:
            return d, how
    return None, ""


def _last_words(ups: list) -> str:
    """The tail of the council's most recent update, tidied for one line."""
    for u in reversed(ups):
        body = " ".join((u.get("body") or "").split())
        m = INVEST_RE.search(body)
        if m:
            return body[m.start():m.start() + 220].strip()
    if ups:
        return " ".join((ups[-1].get("body") or "").split())[:220]
    return ""


def cases(today: date | None = None) -> list:
    """Every report the tool holds, with the council's position on it."""
    today = today or date.today()
    ups_by = _updates_by_report()
    out = []
    for row in store.all_reports():
        ups = ups_by.get(row["id"], [])
        pos = position_for(row, ups)
        when, how = reported_on(row, ups)
        age = (today - when).days if when else None
        live = pos in LIVE
        # The 90-day promise is the highways repair standard. Fly-tipping,
        # bins and street cleansing are a different service with no such
        # undertaking, so they get an age but no countdown.
        promised = (row.get("service") or "fixmystreet") == "fixmystreet"
        deadline = when + timedelta(days=PROMISE_DAYS) if when else None
        days_left = (deadline - today).days if (deadline and live and promised) else None
        invest, repair = _outcome(ups)
        ref = (row.get("reference") or "").strip()
        last = _d(ups[-1]["happened"]) if ups else None
        out.append({
            "id": row["id"],
            "ref": ref,
            "url": (HIGHWAYS_CASE.format(ref)
                    if ref.isdigit() and (row.get("service") or "") == "fixmystreet" else ""),
            "service": row.get("service") or "fixmystreet",
            "service_label": SERVICE_LABEL.get(row.get("service") or "", "Other"),
            "street": row.get("street") or "",
            "title": row.get("title") or "",
            "category": row.get("category") or "",
            "position": pos,
            "position_label": POS_LABEL[pos],
            "tone": POS_TONE[pos],
            "live": live,
            "reported": when.isoformat() if when else "",
            "reported_from": how,
            "age": age,
            "days_left": days_left,
            "deadline": deadline.isoformat() if (deadline and promised) else "",
            "promised": promised,
            "overdue": bool(live and promised and age is not None and age > PROMISE_DAYS),
            "due_soon": bool(days_left is not None and 0 <= days_left <= DUE_SOON_DAYS),
            "silent": bool(pos == "silent" and age is not None and age > SILENT_AFTER_DAYS),
            "invest": invest,
            "repair": repair,
            "updates": len(ups),
            "last_heard": last.isoformat() if last else "",
            "last_words": _last_words(ups),
            "lat": row.get("lat"),
            "lon": row.get("lon"),
        })
    return out


def pin_summary() -> dict:
    """The merged My Map and council-portal pins. Counted, never mixed into the
    council figures - none of them carries a council outcome."""
    with store.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT source, folder, kind, status, date_source, reference "
            "FROM map_pins")]
    by_source, by_kind = {}, {}
    for r in rows:
        by_source[r["source"] or "?"] = by_source.get(r["source"] or "?", 0) + 1
        by_kind[r["kind"] or "other"] = by_kind.get(r["kind"] or "other", 0) + 1
    return {
        "total": len(rows),
        "by_source": sorted(by_source.items(), key=lambda kv: -kv[1]),
        "by_kind": sorted(by_kind.items(), key=lambda kv: -kv[1]),
        "dated": sum(1 for r in rows if (r.get("date_source") or "") == "exact"),
        "estimated": sum(1 for r in rows if (r.get("date_source") or "") == "estimated"),
    }


def _h(s) -> str:
    import html
    return html.escape(str(s or ""))


def build_html(label: str, rows: list, pins: dict, cfg: dict) -> str:
    """One self-contained page. Everything the filters need is embedded, so the
    page never calls back into the app."""
    stamp = datetime.now().strftime("%d %b %Y %H:%M")
    meta = {
        "promise_days": PROMISE_DAYS,
        "due_soon_days": DUE_SOON_DAYS,
        "silent_after_days": SILENT_AFTER_DAYS,
        "positions": [{"key": k, "label": lbl, "tone": t, "live": live}
                      for k, lbl, t, live in POSITIONS],
        "services": [{"key": k, "label": lbl} for k, lbl in SERVICES],
        "today": date.today().isoformat(),
        "profile": cfg.get("profile") or "",
    }
    return (_HTML.replace("__JS__", _JS)
            .replace("__TITLE__", _h(label))
            .replace("__STAMP__", _h(stamp))
            .replace("__CASES_JSON__", json.dumps(rows))
            .replace("__PINS_JSON__", json.dumps(pins))
            .replace("__META_JSON__", json.dumps(meta)))


def page(cfg: dict) -> str:
    label = (cfg.get("reporter") or {}).get("full_name") or cfg.get("profile") or "NNC Reporter"
    return build_html(label, cases(), pin_summary(), cfg)


# ------------------------------------------------------------------ the page

_HTML = r"""<!DOCTYPE html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reporting dashboard - NNC Reporter</title>
<style>
  :root{
    color-scheme:light;
    --plane:#f6f7f9; --surface:#ffffff; --line:#dfe3e8; --grid:#eceff2;
    --ink:#1b1f24; --mut:#5c6672; --faint:#8a929b; --accent:#00693e;
    --series-1:#2a78d6; --series-2:#eb6834;
    --good:#0ca30c; --warning:#fab219; --serious:#ec835a; --critical:#d03b3b;
    --info:#2a78d6; --muted:#898781;
  }
  @media (prefers-color-scheme:dark){
    :root:where(:not([data-theme="light"])){
      color-scheme:dark;
      --plane:#0e1013; --surface:#16181b; --line:#2a2e33; --grid:#23272c;
      --ink:#f2f4f6; --mut:#a8b0b8; --faint:#8a929b; --accent:#3ea77a;
      --series-1:#3987e5; --series-2:#d95926;
    }
  }
  :root[data-theme="dark"]{
    color-scheme:dark;
    --plane:#0e1013; --surface:#16181b; --line:#2a2e33; --grid:#23272c;
    --ink:#f2f4f6; --mut:#a8b0b8; --faint:#8a929b; --accent:#3ea77a;
    --series-1:#3987e5; --series-2:#d95926;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--plane);color:var(--ink);
       font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
  header{background:var(--accent);color:#fff;padding:14px 20px;display:flex;
         align-items:baseline;gap:14px;flex-wrap:wrap}
  header h1{font-size:18px;margin:0;font-weight:600}
  header .who{font-size:14px;opacity:.92}
  header .stamp{font-size:13px;opacity:.75;margin-left:auto}
  header a{color:#fff;font-size:13px;text-decoration:none;border:1px solid rgba(255,255,255,.45);
           border-radius:7px;padding:3px 10px}
  header a:hover{background:rgba(255,255,255,.15)}
  main{max-width:1500px;margin:0 auto;padding:18px}
  .card{background:var(--surface);border:1px solid var(--line);border-radius:10px;
        padding:16px;margin-bottom:16px}
  h2{font-size:15px;margin:0 0 4px}
  .sub{font-size:13px;color:var(--mut);margin:0 0 14px}
  .muted{color:var(--mut);font-size:13px}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media(max-width:1000px){.grid{grid-template-columns:1fr}}

  .filters{display:flex;gap:10px;align-items:center;flex-wrap:wrap;background:var(--surface);
           border:1px solid var(--line);border-radius:10px;padding:11px 14px;margin-bottom:16px}
  .filters label{font-size:13px;color:var(--mut);display:flex;gap:6px;align-items:center}
  select,input[type=search]{font:inherit;font-size:13.5px;padding:5px 8px;border:1px solid var(--line);
        border-radius:7px;background:var(--surface);color:var(--ink)}
  input[type=search]{min-width:250px}
  button{font:inherit;font-size:13.5px;border:1px solid var(--line);background:var(--surface);
         color:var(--ink);border-radius:7px;padding:5px 11px;cursor:pointer}
  button:hover{border-color:var(--faint)}
  .linky{border:none;background:none;color:var(--mut);padding:2px 0;font-size:13px;
         text-decoration:underline;text-underline-offset:3px}
  .linky:hover{color:var(--ink)}

  .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}
  .tile{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:13px 14px;
        text-align:left;cursor:pointer}
  .tile:hover{border-color:var(--faint)}
  .tile.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
  .tile .lab{font-size:12.5px;color:var(--mut);display:flex;gap:6px;align-items:center;
             min-height:32px;line-height:1.3}
  .tile .val{font-size:30px;font-weight:600;letter-spacing:-.5px;margin-top:6px}
  .tile .note{font-size:12px;color:var(--faint);margin-top:2px}
  .dot{width:9px;height:9px;border-radius:50%;flex:none;display:inline-block}
  .dot.good{background:var(--good)} .dot.warning{background:var(--warning)}
  .dot.serious{background:var(--serious)} .dot.critical{background:var(--critical)}
  .dot.info{background:var(--info)} .dot.muted{background:var(--muted)}

  .chart{display:flex;flex-direction:column;gap:9px}
  .brow{display:grid;grid-template-columns:minmax(120px,215px) 1fr;gap:12px;align-items:center}
  .brow .lab{font-size:13px;color:var(--mut);display:flex;gap:7px;align-items:center;line-height:1.25}
  .brow .track{display:grid;grid-template-columns:1fr 40px;gap:8px;align-items:center;min-width:0}
  .brow .barcell{display:block;min-width:0}
  .bars{display:flex;gap:2px;height:16px;min-width:2px}
  .seg{height:16px;border-radius:0 4px 4px 0;min-width:2px}
  .seg:not(:last-child){border-radius:0}
  .bars.zero .seg{background:var(--grid)!important;width:2px}
  .brow .num{font-size:13px;color:var(--ink);font-variant-numeric:tabular-nums;text-align:right}
  .legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--mut);margin:0 0 12px}
  .legend span{display:flex;gap:6px;align-items:center}
  .key{width:11px;height:11px;border-radius:3px;display:inline-block}
  .empty{color:var(--mut);font-size:13px;padding:6px 0}

  table{border-collapse:collapse;width:100%;font-size:13px}
  th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--grid);vertical-align:top}
  th{color:var(--mut);font-weight:600;font-size:12.5px;white-space:nowrap;cursor:pointer;
     position:sticky;top:0;background:var(--surface)}
  th.plain{cursor:default}
  th:hover:not(.plain){color:var(--ink)}
  td.num{font-variant-numeric:tabular-nums;white-space:nowrap}
  tbody tr:hover{background:var(--grid)}
  .scroll{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:8px}
  .tbl{margin-top:12px;border-top:1px solid var(--grid);padding-top:10px}
  .pos{display:flex;gap:7px;align-items:center;white-space:nowrap}
  .flag{font-size:11.5px;padding:1px 7px;border-radius:99px;border:1px solid var(--line);
        color:var(--mut);white-space:nowrap;display:inline-block}
  .flag.over{color:var(--critical);border-color:var(--critical)}
  .flag.soon{color:var(--serious);border-color:var(--serious)}
  a{color:var(--accent)}
  #tip{position:fixed;pointer-events:none;background:var(--ink);color:var(--plane);
       padding:7px 10px;border-radius:7px;font-size:12.5px;line-height:1.4;display:none;z-index:9;
       max-width:280px;box-shadow:0 2px 10px rgba(0,0,0,.22)}
  footer{color:var(--mut);font-size:12.5px;line-height:1.6;padding:4px 2px 30px}
  footer b{color:var(--ink);font-weight:600}
</style>
</head>
<body>
<header>
  <h1>Reporting dashboard</h1>
  <span class="who">__TITLE__</span>
  <a href="/">Back to the tool</a>
  <span class="stamp">as at __STAMP__</span>
</header>

<main>
<div class="filters">
  <label>Service <select id="f-service"></select></label>
  <label>Reported <select id="f-since"></select></label>
  <label>Position <select id="f-pos"></select></label>
  <input id="f-q" type="search" placeholder="Street, reference or words in the title">
  <button id="f-clear">Clear</button>
  <span class="muted" id="f-count"></span>
</div>

<section class="tiles" id="tiles"></section>

<div class="grid">
  <div class="card">
    <h2>Where the council has got to</h2>
    <p class="sub">Every report in the filter, on the council's own journey.
      Click a row to filter the page by it.</p>
    <div class="chart" id="c-pos"></div>
    <button class="linky" data-table="t-pos">Show as a table</button>
    <div class="tbl" id="t-pos" hidden></div>
  </div>
  <div class="card">
    <h2>How old the live cases are</h2>
    <p class="sub">Days since the council logged them - anything still open,
      awaiting triage or waiting on a repair. The council's promise is
      <b>90 days</b> from the report.</p>
    <div class="chart" id="c-age"></div>
    <p class="sub" id="age-note" style="margin:16px 0 0"></p>
    <button class="linky" data-table="t-age" style="margin-top:10px">Show as a table</button>
    <div class="tbl" id="t-age" hidden></div>
  </div>
</div>

<div class="grid">
  <div class="card">
    <h2>Reported by month</h2>
    <p class="sub">What each walk has come to.</p>
    <div class="legend">
      <span><i class="key" style="background:var(--series-1)"></i>Still live</span>
      <span><i class="key" style="background:var(--series-2)"></i>Fixed or closed</span>
    </div>
    <div class="chart" id="c-month"></div>
    <button class="linky" data-table="t-month">Show as a table</button>
    <div class="tbl" id="t-month" hidden></div>
  </div>
  <div class="card">
    <h2>Streets with the most live cases</h2>
    <p class="sub">Where a chase would cover the most ground at once.</p>
    <div class="chart" id="c-street"></div>
    <button class="linky" data-table="t-street">Show as a table</button>
    <div class="tbl" id="t-street" hidden></div>
  </div>
</div>

<section class="card">
  <h2>The chase list</h2>
  <p class="sub" id="chase-sub"></p>
  <div class="scroll"><table id="chase"></table></div>
</section>

<div class="grid">
  <div class="card">
    <h2>Closed as duplicates of each other</h2>
    <p class="sub">The council treats a run of defects on one street as one job.
      Several photos of the same street submitted minutes apart become one case
      and a pile of duplicates - these are the streets it happened on.</p>
    <div class="chart" id="c-dup"></div>
  </div>
  <div class="card">
    <h2>Pins from the map</h2>
    <p class="sub">Merged from the Google My Map and the council portal. The
      council gave no outcome for any of these, so they are counted here and
      kept out of the figures above.</p>
    <div id="pins"></div>
  </div>
</div>

<footer id="notes"></footer>
</main>

<div id="tip"></div>
<script>
const CASES = __CASES_JSON__;
const PINS  = __PINS_JSON__;
const META  = __META_JSON__;
__JS__
</script>
</body>
</html>
"""


_JS = r"""
const $ = s => document.querySelector(s);
const esc = s => (s ?? '').toString().replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const POS = Object.fromEntries(META.positions.map(p => [p.key, p]));
const TODAY = META.today;
const S1 = 'var(--series-1)', S2 = 'var(--series-2)';
const TONE = t => 'var(--' + t + ')';

let FILTER = {service: '', since: '', pos: '', q: '', focus: ''};
let SORT = {col: 'age', dir: -1};

// ---- filters --------------------------------------------------------------
const SINCE = [
  ['', 'any time'], ['30', 'the last 30 days'], ['90', 'the last 90 days'],
  ['365', 'the last 12 months'], ['year', 'this year'],
];

function fillFilters(){
  $('#f-service').innerHTML = '<option value="">every service</option>' +
    META.services.filter(s => CASES.some(c => c.service === s.key))
      .map(s => `<option value="${s.key}">${esc(s.label)}</option>`).join('');
  $('#f-since').innerHTML = SINCE.map(([v, l]) =>
    `<option value="${v}">${l}</option>`).join('');
  $('#f-pos').innerHTML = '<option value="">any position</option>' +
    '<option value="~live">still live (anything not fixed or closed)</option>' +
    META.positions.map(p => `<option value="${p.key}">${esc(p.label)}</option>`).join('');
  ['f-service', 'f-since', 'f-pos'].forEach(id =>
    $('#' + id).onchange = () => { FILTER[id.slice(2)] = $('#' + id).value; draw(); });
  $('#f-q').oninput = () => { FILTER.q = $('#f-q').value.trim().toLowerCase(); draw(); };
  $('#f-clear').onclick = () => {
    FILTER = {service: '', since: '', pos: '', q: '', focus: ''};
    $('#f-service').value = ''; $('#f-since').value = '';
    $('#f-pos').value = ''; $('#f-q').value = '';
    draw();
  };
}

function sinceDate(){
  if (!FILTER.since) return '';
  if (FILTER.since === 'year') return TODAY.slice(0, 4) + '-01-01';
  const d = new Date(TODAY + 'T00:00:00');
  d.setDate(d.getDate() - parseInt(FILTER.since, 10));
  return d.toISOString().slice(0, 10);
}

const FOCUS = {
  live:      c => c.live,
  overdue:   c => c.overdue,
  due_soon:  c => c.due_soon && !c.overdue,
  defect:    c => c.position === 'defect',
  silent:    c => c.position === 'silent',
  fixed:     c => c.position === 'fixed',
  duplicate: c => c.position === 'duplicate',
};

// noFocus: everything the selects and the search box allow, before the tile
// chips narrow it further - so the chip counts stay readable once one is on.
function shown(noFocus){
  const from = sinceDate();
  return CASES.filter(c => {
    if (FILTER.service && c.service !== FILTER.service) return false;
    if (FILTER.pos === '~live' && !c.live) return false;
    if (FILTER.pos && FILTER.pos !== '~live' && c.position !== FILTER.pos) return false;
    if (from && (!c.reported || c.reported < from)) return false;
    if (!noFocus && FILTER.focus && !FOCUS[FILTER.focus](c)) return false;
    if (FILTER.q){
      const hay = [c.ref, c.street, c.title, c.category, c.position_label]
        .join(' ').toLowerCase();
      if (!hay.includes(FILTER.q)) return false;
    }
    return true;
  });
}

// ---- the bar component ----------------------------------------------------
// One horizontal bar per row. Segments are separated by a 2px gap in the page
// colour rather than a border, and only the data end is rounded.
function bars(el, rows, opts){
  opts = opts || {};
  el.innerHTML = '';
  if (!rows.length){ el.innerHTML = '<p class="empty">Nothing in this filter.</p>'; return; }
  const max = Math.max(1, ...rows.map(r => r.total));
  rows.forEach(r => {
    const row = document.createElement('div');
    row.className = 'brow';
    const segs = (r.segs || []).filter(s => s.v > 0);
    row.innerHTML =
      `<span class="lab">${r.tone ? `<i class="dot ${r.tone}"></i>` : ''}` +
        `<span>${esc(r.label)}</span></span>` +
      `<span class="track"><span class="barcell"><span class="bars${segs.length ? '' : ' zero'}"` +
        ` style="width:${(Math.max(r.total, 0) / max * 100).toFixed(2)}%">` +
        (segs.length ? segs.map(s =>
          `<i class="seg" style="flex:${s.v};background:${s.color}" data-t="${esc(s.name)}: ${s.v}"></i>`
        ).join('') : '<i class="seg"></i>') +
      `</span></span><span class="num">${r.total}</span></span>`;
    if (opts.click) { row.style.cursor = 'pointer'; row.onclick = () => opts.click(r); }
    row.querySelectorAll('.seg[data-t]').forEach(s => {
      s.onmousemove = e => tip(e, `${esc(r.label)}\n${s.dataset.t}`);
      s.onmouseleave = hideTip;
    });
    el.appendChild(row);
  });
}

function tip(e, html){
  const t = $('#tip');
  t.innerHTML = html.split('\n').map(l => `<div>${l}</div>`).join('');
  t.style.display = 'block';
  const w = t.offsetWidth, h = t.offsetHeight;
  t.style.left = Math.min(e.clientX + 14, innerWidth - w - 10) + 'px';
  t.style.top = Math.max(8, e.clientY - h - 12) + 'px';
}
const hideTip = () => $('#tip').style.display = 'none';

function table(el, cols, rows){
  el.innerHTML = '<table><thead><tr>' +
    cols.map(c => `<th class="plain">${esc(c)}</th>`).join('') +
    '</tr></thead><tbody>' + rows.map(r => '<tr>' +
      r.map((v, i) => `<td${i ? ' class="num"' : ''}>${esc(v)}</td>`).join('') +
      '</tr>').join('') + '</tbody></table>';
}

// ---- the panels -----------------------------------------------------------
function tiles(rows){
  const n = k => rows.filter(FOCUS[k]).length;
  const defs = [
    ['live', 'Live cases needing an answer', 'critical', n('live'),
     rows.length + ' reports in this filter'],
    ['overdue', 'Past the ' + META.promise_days + '-day promise', 'critical', n('overdue'),
     'live highways reports over ' + META.promise_days + ' days old'],
    ['due_soon', 'Promise falls due within ' + META.due_soon_days + ' days', 'serious',
     n('due_soon'), 'chase these before the date passes'],
    ['defect', 'Defect found, repair not started', 'warning', n('defect'),
     'the council agrees there is a fault'],
    ['silent', 'No word from the council', 'critical', n('silent'),
     'no acknowledgement, no update, nothing'],
    ['fixed', 'Fixed', 'good', n('fixed'), 'confirmed repaired'],
    ['duplicate', 'Closed as a duplicate', 'muted', n('duplicate'),
     'of another of your own reports'],
  ];
  $('#tiles').innerHTML = defs.map(([k, lab, tone, v, note]) =>
    `<button class="tile${FILTER.focus === k ? ' on' : ''}" data-focus="${k}">` +
    `<span class="lab"><i class="dot ${tone}"></i>${esc(lab)}</span>` +
    `<div class="val">${v}</div><div class="note">${esc(note)}</div></button>`).join('');
  $('#tiles').querySelectorAll('.tile').forEach(b => b.onclick = () => {
    FILTER.focus = FILTER.focus === b.dataset.focus ? '' : b.dataset.focus;
    draw();
  });
}

function positionChart(rows){
  const data = META.positions.map(p => ({
    label: p.label, tone: p.tone, key: p.key,
    total: rows.filter(c => c.position === p.key).length,
    segs: [{v: rows.filter(c => c.position === p.key).length,
            color: S1, name: 'reports'}],
  }));
  bars($('#c-pos'), data, {click: r => {
    FILTER.pos = FILTER.pos === r.key ? '' : r.key;
    $('#f-pos').value = FILTER.pos; draw();
  }});
  table($('#t-pos'), ['Council position', 'Reports', 'Share'],
    data.map(d => [d.label, d.total,
      rows.length ? (d.total / rows.length * 100).toFixed(0) + '%' : '0%']));
}

const AGE_BUCKETS = [
  ['0-7 days', 0, 7], ['8-30 days', 8, 30], ['31-60 days', 31, 60],
  ['61-90 days', 61, 90], ['over 90 days', 91, 1e9],
];

function ageChart(rows){
  const live = rows.filter(c => c.live && c.age !== null);
  const data = AGE_BUCKETS.map(([lab, lo, hi], i) => {
    const n = live.filter(c => c.age >= lo && c.age <= hi).length;
    const over = i === AGE_BUCKETS.length - 1;
    return {label: lab, total: n, tone: over ? 'critical' : '',
            segs: [{v: n, color: over ? TONE('critical') : S1, name: 'live cases'}]};
  });
  bars($('#c-age'), data);
  const oldest = live.slice().sort((a, b) => b.age - a.age)[0];
  $('#age-note').innerHTML = oldest
    ? `<b>Oldest live case:</b> ${esc(oldest.ref || 'no reference')} on ` +
      `${esc(oldest.street || 'an unrecorded street')}, reported ${esc(oldest.reported)} ` +
      `&mdash; ${oldest.age} days ago. ${esc(oldest.position_label)}.` +
      (oldest.deadline ? ` The ${META.promise_days}-day promise falls due ` +
        `${new Date(oldest.deadline + 'T00:00:00').toLocaleDateString('en-GB',
          {day: 'numeric', month: 'long', year: 'numeric'})}.` : '')
    : '';
  table($('#t-age'), ['Age of live case', 'Cases'], data.map(d => [d.label, d.total]));
}

function monthChart(rows){
  const months = {};
  rows.forEach(c => {
    if (!c.reported) return;
    const k = c.reported.slice(0, 7);
    months[k] = months[k] || {live: 0, done: 0};
    months[k][c.live ? 'live' : 'done']++;
  });
  const keys = Object.keys(months).sort();
  const name = k => new Date(k + '-01T00:00:00')
    .toLocaleDateString('en-GB', {month: 'short', year: 'numeric'});
  const data = keys.map(k => ({
    label: name(k), total: months[k].live + months[k].done,
    segs: [{v: months[k].live, color: S1, name: 'still live'},
           {v: months[k].done, color: S2, name: 'fixed or closed'}],
  }));
  bars($('#c-month'), data);
  table($('#t-month'), ['Month reported', 'Still live', 'Fixed or closed', 'Total'],
    keys.map(k => [name(k), months[k].live, months[k].done,
                   months[k].live + months[k].done]));
}

function streetChart(rows){
  const by = {};
  rows.filter(c => c.live).forEach(c => {
    const s = c.street || 'street not recorded';
    by[s] = (by[s] || 0) + 1;
  });
  const data = Object.entries(by).sort((a, b) => b[1] - a[1]).slice(0, 10)
    .map(([s, n]) => ({label: s, total: n,
                       segs: [{v: n, color: S1, name: 'live cases'}]}));
  bars($('#c-street'), data, {click: r => {
    FILTER.q = r.label.toLowerCase(); $('#f-q').value = r.label; draw();
  }});
  table($('#t-street'), ['Street', 'Live cases'], data.map(d => [d.label, d.total]));
}

function dupChart(rows){
  const by = {};
  rows.filter(c => c.position === 'duplicate').forEach(c => {
    const s = c.street || 'street not recorded';
    by[s] = (by[s] || 0) + 1;
  });
  const data = Object.entries(by).sort((a, b) => b[1] - a[1]).slice(0, 10)
    .map(([s, n]) => ({label: s, total: n,
                       segs: [{v: n, color: TONE('muted'), name: 'duplicates'}]}));
  bars($('#c-dup'), data, {click: r => {
    FILTER.pos = 'duplicate'; $('#f-pos').value = 'duplicate';
    FILTER.q = r.label.toLowerCase(); $('#f-q').value = r.label; draw();
  }});
}

const COLS = [
  ['ref', 'Reference'], ['street', 'Street'], ['title', 'What was reported'],
  ['position_label', 'Council position'], ['reported', 'Reported'],
  ['age', 'Days open'], ['days_left', '90-day promise'], ['last_heard', 'Last heard'],
];

function chase(rows){
  const dir = SORT.dir, col = SORT.col;
  const sorted = rows.slice().sort((a, b) => {
    // The chase list is for chasing: live cases lead, whatever the age order.
    if (col === 'age' && a.live !== b.live) return a.live ? -1 : 1;
    const x = a[col], y = b[col];
    if (x === y) return 0;
    if (x === null || x === undefined || x === '') return 1;
    if (y === null || y === undefined || y === '') return -1;
    return (x > y ? 1 : -1) * dir;
  });
  const head = COLS.map(([k, l]) =>
    `<th data-col="${k}">${esc(l)}${SORT.col === k ? (dir < 0 ? ' ↓' : ' ↑') : ''}</th>`
  ).join('');
  const body = sorted.map(c => {
    const flags =
      (c.overdue ? '<span class="flag over">past 90 days</span>' :
       c.due_soon ? `<span class="flag soon">${c.days_left}d left</span>` :
       c.days_left !== null ? `${c.days_left} days left` : '—');
    const ref = c.url
      ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.ref)}</a>`
      : esc(c.ref || 'no reference');
    return `<tr title="${esc(c.last_words || '')}">` +
      `<td class="num">${ref}</td>` +
      `<td>${esc(c.street || '—')}</td>` +
      `<td>${esc(c.title || '—')}</td>` +
      `<td><span class="pos"><i class="dot ${c.tone}"></i>${esc(c.position_label)}</span></td>` +
      `<td class="num">${esc(c.reported || '—')}${c.reported_from === 'council' ? '' : ' ≈'}</td>` +
      `<td class="num">${c.age === null ? '—' : c.age}</td>` +
      `<td class="num">${flags}</td>` +
      `<td class="num">${esc(c.last_heard || '—')}</td></tr>`;
  }).join('');
  $('#chase').innerHTML = `<thead><tr>${head}</tr></thead><tbody>${body}</tbody>`;
  $('#chase').querySelectorAll('th[data-col]').forEach(th => th.onclick = () => {
    const k = th.dataset.col;
    SORT = {col: k, dir: SORT.col === k ? -SORT.dir : (k === 'age' ? -1 : 1)};
    chase(shown());
  });
  $('#chase-sub').innerHTML = sorted.length
    ? `${sorted.length} report${sorted.length === 1 ? '' : 's'} in this filter, ` +
      `oldest first by default. Hover a row for the council's own last words on it; ` +
      `a highways reference opens the council's page for it. A date marked ≈ is the day of the ` +
      `walk rather than the council's own logged date.`
    : 'Nothing in this filter.';
}

const KIND_NAMES = {flytipping: 'Fly-tipping and waste', pothole: 'Road and footway',
  drain: 'Drainage and flooding', vegetation: 'Vegetation', lighting: 'Lighting and signals',
  sign: 'Signs and graffiti', issue: 'Community issue', place: 'Landmark', other: 'Other'};
const SRC_NAMES = {mymaps: 'Added by hand to the Google My Map',
  portal: 'Reconciled from the council portal'};

function pinsPanel(){
  const rows = list => list.map(([k, n]) =>
    `<tr><td>${esc(k)}</td><td class="num">${n}</td></tr>`).join('');
  $('#pins').innerHTML =
    `<p class="muted" style="margin:0 0 12px">` +
    `<b style="font-size:26px;color:var(--ink);font-weight:600">${PINS.total}</b> pins &mdash; ` +
    `${PINS.dated} carrying the council's own date, ${PINS.estimated} with a date ` +
    `estimated from the reference number.</p>` +
    `<table><thead><tr><th class="plain">Where they came from</th>` +
    `<th class="plain">Pins</th></tr></thead><tbody>` +
    rows(PINS.by_source.map(([k, n]) => [SRC_NAMES[k] || k, n])) + `</tbody></table>` +
    `<table style="margin-top:14px"><thead><tr><th class="plain">What they are</th>` +
    `<th class="plain">Pins</th></tr></thead><tbody>` +
    rows(PINS.by_kind.map(([k, n]) => [KIND_NAMES[k] || k, n])) + `</tbody></table>`;
}

function notes(){
  $('#notes').innerHTML = [
    `<p><b>Where the positions come from.</b> The council's own words on each case &mdash; ` +
    `the <i>Investigation: Completed (&hellip;)</i> and <i>Defect Repair</i> lines in its ` +
    `updates &mdash; and the banner state on the report page second. Update bodies are ` +
    `stored trimmed, so a case can have a repair line on the council's page that has not ` +
    `reached here; that shows as <i>Investigated, outcome not stated</i> rather than a guess.</p>`,
    `<p><b>Dates.</b> The day the council logged the report, taken from its own ` +
    `&ldquo;your report has been logged&rdquo; email where there is one &mdash; ` +
    `${CASES.filter(c => c.reported_from === 'council').length} of ` +
    `${CASES.length} reports. For the rest the day of the walk stands in, marked ` +
    `&asymp;: the tool takes the photos in and submits them in the same sitting, so it ` +
    `is right to within a day.</p>`,
    `<p><b>The ${META.promise_days}-day promise.</b> The council undertakes to repair a ` +
    `confirmed carriageway defect within ${META.promise_days} days of the report. It is a ` +
    `highways standard, so the countdown runs on highways reports only &mdash; fly-tipping, ` +
    `bins and street cleansing go through a different service with no such undertaking, and ` +
    `show an age but no date to hold the council to.</p>`,
    `<p><b>No word from the council.</b> Every fly-tipping, bin and street-cleansing case ` +
    `goes through a different council system, which acknowledges by email to whichever ` +
    `address the form carried. Cases sitting here with nothing against them are ones with ` +
    `no council-side record to chase &mdash; worth checking the address on the form before ` +
    `the next walk.</p>`,
    `<p><b>Keeping it current.</b> The figures move when the council's updates reach the ` +
    `tool: <i>Check council updates</i> reads the report pages, and dropping the council's ` +
    `emails in matches them to cases by reference. Reload this page afterwards.</p>`,
  ].join('');
}

function draw(){
  const rows = shown(), base = shown(true);
  $('#f-count').textContent = `showing ${rows.length} of ${CASES.length} reports`;
  tiles(base);
  positionChart(rows);
  ageChart(rows);
  monthChart(rows);
  streetChart(rows);
  dupChart(rows);
  chase(rows);
}

document.querySelectorAll('[data-table]').forEach(b => b.onclick = () => {
  const t = $('#' + b.dataset.table);
  t.hidden = !t.hidden;
  b.textContent = t.hidden ? 'Show as a table' : 'Hide the table';
});

fillFilters();
pinsPanel();
notes();
draw();
"""
