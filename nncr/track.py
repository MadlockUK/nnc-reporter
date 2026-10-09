"""Reconcile council updates with the cases logged here.

Two sources, one timeline per case:

  * The highways site's own report pages. Every submitted highways report stores
    its public URL, and that page shows the council's current state and every
    update - the same page the council's emails link to. "Check council updates"
    reads them politely (one a second) and records anything new.

  * Council emails, dropped in. The Granicus cases (fly-tipping, bins, and the
    environmental forms) have no public page - their updates only arrive by
    email. Save the email as a .eml file and drag it in, or paste its text: the
    reference is matched to the case and the update recorded.

The council's opinion of a case lives in `council_status`, deliberately separate
from the tool's own lifecycle (new -> submitted -> mapped -> completed), which is
about the map workflow. A fixed pothole stays "completed" here; it just also says
what the council thinks.
"""
import re
import time
from email import policy
from email.parser import BytesParser

from . import store

# FixMyStreet's states, as its banner classes and their display wording.
FMS_STATES = {
    "confirmed": "open",
    "investigating": "investigating",
    "action-scheduled": "action scheduled",
    "in-progress": "in progress",
    "fixed": "fixed",
    "closed": "closed",
    "no-further-action": "no further action",
    "unable-to-fix": "unable to fix",
    "not-responsible": "not responsible",
    "duplicate": "duplicate",
    "internal-referral": "referred internally",
}
STATE_PHRASES = tuple(FMS_STATES.values()) + ("open",)

# Wording in an email that tells us where the case got to.
EMAIL_STATE_WORDS = (
    ("fixed", ("has been fixed", "marked as fixed", "been resolved",
               "now been resolved", "been completed", "work has been completed",
               "works have been completed")),
    ("closed", ("has been closed", "now closed", "case is closed",
                "no further action", "will not be taking further action")),
    ("in progress", ("in progress", "been scheduled", "passed to our contractor",
                     "passed to the contractor", "crew has been", "job has been raised")),
    ("investigating", ("being investigated", "will investigate",
                       "under investigation", "looking into")),
)


def _strip_tags(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
    html = re.sub(r"(?i)<(br|/p|/li|/h[1-6]|/div)[^>]*>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    import html as _h
    text = _h.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


# ------------------------------------------------------------- highways pages

def parse_report_page(html: str) -> dict:
    """The state and updates on a FixMyStreet report page.

    Markup first (the banner class names the state), then wording - the NNC
    cobrand can restyle the page, but the phrases survive.
    """
    out = {"state": "", "updates": []}
    m = re.search(r"banner--([a-z-]+)", html or "")
    if m and m.group(1) in FMS_STATES:
        out["state"] = FMS_STATES[m.group(1)]

    text = _strip_tags(html or "")

    if not out["state"]:
        m = re.search(r"(?im)^\s*(?:this report is currently|state:)\s*[:\-]?\s*"
                      r"([a-z ()\-]+)$", text)
        if m:
            low = m.group(1).strip().lower()
            for phrase in STATE_PHRASES:
                if phrase in low:
                    out["state"] = phrase
                    break

    # Updates: "Posted [by whoever] at HH:MM, Day D Month YYYY" (or a bare
    # "HH:MM, date" line, or "HH:MM today") heads each entry.
    lines = text.split("\n")
    entry, when, poster = [], "", ""
    head = re.compile(r"^\s*(?:Posted by (.{1,80}?) at |Posted anonymously at )?"
                      r"(\d{1,2}:\d{2}(?:, [^,\n]{3,40}?\d{4}| today)?)\s*$")

    def flush():
        if not when and not entry:
            return
        body = " ".join(x.strip() for x in entry if x.strip())
        state = ""
        m2 = re.search(r"(?i)state changed to:\s*([a-z ()\-]+?)(?:\.|$)", body)
        if m2:
            state = m2.group(1).strip().lower()
        elif re.search(r"(?i)investigation:\s*completed", body):
            state = "investigated"
        elif re.search(r"(?i)still open", body):
            state = "open"
        if body or state:
            out["updates"].append({"when": when, "poster": poster,
                                   "body": body[:600], "state": state})

    started = False
    for ln in lines:
        h = head.match(ln)
        if h:
            if started:
                flush()
            started, entry = True, []
            poster = (h.group(1) or "").strip()
            when = h.group(2).strip()
        elif started:
            entry.append(ln)
    if started:
        flush()

    # The last state change on the page wins when the banner said nothing.
    if not out["state"]:
        for u in reversed(out["updates"]):
            if u["state"] and u["state"] not in ("investigated",):
                out["state"] = u["state"]
                break
    return out


def _page_url(r: dict) -> str:
    url = r.get("report_url") or ""
    if "highways.northnorthants.gov.uk/report/" in url:
        return url.split("?")[0]
    # A highways reference with no stored URL still names the page.
    ref = (r.get("reference") or "").strip()
    if r.get("service") == "fixmystreet" and ref.isdigit():
        return f"https://highways.northnorthants.gov.uk/report/{ref}"
    return ""


def check_pages(cfg: dict, ids: list | None = None) -> dict:
    """Read each highways case's public page and record what changed."""
    import requests

    rows = [r for r in store.all_reports()
            if r.get("status") in store.DONE_STATUSES and _page_url(r)]
    if ids:
        wanted = {int(i) for i in ids}
        rows = [r for r in rows if r["id"] in wanted]

    checked, changed, failed = 0, [], []
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for i, r in enumerate(rows):
        if i:
            time.sleep(1.0)                      # politely, one page a second
        url = _page_url(r)
        try:
            resp = requests.get(url, timeout=20,
                                headers={"User-Agent": "nnc-reporter/1.0"})
            resp.raise_for_status()
        except Exception as e:
            failed.append({"id": r["id"], "error": str(e)[:120]})
            continue
        parsed = parse_report_page(resp.text)
        checked += 1
        fresh = 0
        for u in parsed["updates"]:
            if store.add_update(r["id"], "page", u["when"],
                                (f"{u['poster']}: " if u["poster"] else "") + u["body"],
                                u["state"]):
                fresh += 1
        was = (r.get("council_status") or "").strip().lower()
        new_state = parsed["state"] or was
        store.update(r["id"], {"council_status": new_state,
                               "council_checked_at": now})
        if fresh or (new_state and new_state != was):
            changed.append({"id": r["id"], "state": new_state, "new_updates": fresh})
    return {"checked": checked, "changed": changed, "failed": failed,
            "skipped": "no highways page" if not rows else ""}


# ------------------------------------------------------------- council emails

_FLY = re.compile(r"\b(FLY\d{5,})\b", re.I)
_URL_ID = re.compile(r"highways\.northnorthants\.gov\.uk/report/(\d+)")
_NEAR_REF = re.compile(r"\bref(?:erence)?\b\W{0,4}(?:number|no\.?)?\W{0,4}(?:is|was)?"
                       r"\W{0,4}([A-Za-z]{2,6}[-/]?\d{4,}[A-Za-z0-9\-/]*|\d{5,12})",
                       re.I)


def refs_in(text: str) -> list:
    """Every case reference the email mentions, strongest signals first."""
    found = []
    for m in _FLY.finditer(text or ""):
        found.append(m.group(1).upper())
    for m in _URL_ID.finditer(text or ""):
        found.append(m.group(1))
    for m in _NEAR_REF.finditer(text or ""):
        found.append(m.group(1).upper().strip(".,;:/-"))
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def parse_eml(raw: bytes) -> dict:
    """Subject, date and readable body text from a saved .eml file."""
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    body = ""
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is not None:
        content = part.get_content()
        body = _strip_tags(content) if part.get_content_type() == "text/html" \
            else str(content)
    return {"subject": str(msg.get("Subject") or ""),
            "date": str(msg.get("Date") or ""),
            "sender": str(msg.get("From") or ""),
            "body": body}


def state_in_email(text: str) -> str:
    low = " ".join((text or "").lower().split())
    m = re.search(r"state changed to:\s*([a-z ()\-]+?)(?:\.|$)", low)
    if m:
        return m.group(1).strip()
    for state, phrases in EMAIL_STATE_WORDS:
        if any(p in low for p in phrases):
            return state
    return ""


def _match_case(refs: list, body: str) -> tuple:
    """(report, how). Reference first; a unique street mention as a fallback."""
    rows = store.all_reports()
    by_ref = {}
    for r in rows:
        ref = (r.get("reference") or "").strip().upper()
        if ref:
            by_ref[ref] = r
        m = _URL_ID.search(r.get("report_url") or "")
        if m:
            by_ref.setdefault(m.group(1), r)
    for ref in refs:
        if ref in by_ref:
            return by_ref[ref], f"reference {ref}"

    low = " ".join((body or "").lower().split())
    hits = [r for r in rows
            if r.get("status") in store.DONE_STATUSES
            and r.get("street") and len(r["street"]) >= 5
            and r["street"].lower() in low]
    if len(hits) == 1:
        return hits[0], f"street match ({hits[0]['street']}) - check it's right"
    return None, ""


def reconcile_email(subject: str, date: str, body: str, sender: str = "") -> dict:
    """Match one council email to a case and record it. Never guesses between
    two cases - an ambiguous email is handed back instead."""
    text = f"{subject}\n{body}"
    refs = refs_in(text)
    r, how = _match_case(refs, body)
    if not r:
        return {"matched": None, "refs": refs,
                "note": ("no case carries " + ", ".join(refs) if refs
                         else "no reference found in the email, and no single "
                              "street matched")}
    state = state_in_email(text)
    summary = subject.strip() or (body.strip().split("\n")[0][:120])
    snippet = " ".join(body.split())[:400]
    added = store.add_update(r["id"], "email", date or "",
                            (summary + (" - " + snippet if snippet else ""))[:600],
                            state)
    changes = {"council_checked_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if state:
        changes["council_status"] = state
    store.update(r["id"], changes)
    return {"matched": r["id"], "how": how, "state": state,
            "duplicate": not added, "refs": refs}
