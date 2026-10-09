"""Prefill the council's own forms in a real, visible browser - you press Submit.

Deliberately never clicks the final Submit button. The tool drives the form to the
last step, scrolls the Submit button into view, and then watches the page until it
sees a confirmation, recording the reference number against the report.

Everything is matched on visible label text via JavaScript rather than CSS ids,
because the council's form overlays its map sidebar on top of controls - which
defeats ordinary Playwright clicks - and because ids change between releases.
"""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from . import config, photos, routing, store

SELECTORS_PATH = config.BASE_DIR / "selectors.json"
OLD_PROFILE_DIR = config.BASE_DIR / "data" / "browser-profile"
_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie", "lockfile")

_lock = threading.Lock()          # one browser session at a time
_log: dict[int, list] = {}        # report id -> progress lines
_active: dict[int, str] = {}      # report id -> state


def selectors() -> dict:
    return json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))


# Which selectors block drives which service.
SERVICE_KEYS = {
    "fixmystreet": "fixmystreet",
    "granicus": "granicus_flytipping",
    "granicus_bins": "granicus_bins",
    "granicus_streetcare": "granicus_streetcare",
    "granicus_dog": "granicus_dog",
    "granicus_grounds": "granicus_grounds",
    "granicus_derelict": "granicus_derelict",
    "granicus_vehicle": "granicus_vehicle",
    "granicus_env": "granicus_env",
}


def _sel_key(service: str) -> str:
    return SERVICE_KEYS.get(service or "fixmystreet", "fixmystreet")


def profile_dir() -> Path:
    """Where the signed-in browser session lives.

    Deliberately NOT inside the project folder: that folder is in OneDrive, and a
    Chromium profile in a synced folder gets its lock files copied around, after
    which Chromium refuses to start with "Opening in existing browser session".
    """
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or str(Path.home())
    who = config.active_profile()
    # A signed-in session belongs to one person, so each profile gets its own.
    d = Path(base) / "nnc-reporter" / (
        "browser-profile" if who == config.MAIN_PROFILE
        else f"browser-profile-{config.slug(who)}")
    d.mkdir(parents=True, exist_ok=True)
    # One-off migration from the old, synced location.
    if who == config.MAIN_PROFILE and OLD_PROFILE_DIR.exists() and not any(d.iterdir()):
        try:
            shutil.copytree(OLD_PROFILE_DIR, d, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*_LOCK_FILES))
        except Exception:
            pass
        try:
            shutil.rmtree(OLD_PROFILE_DIR, ignore_errors=True)
        except Exception:
            pass
    return d


def _clear_locks(prof: Path) -> int:
    """Remove lock files left behind by a browser that did not close cleanly."""
    gone = 0
    for name in _LOCK_FILES:
        p = prof / name
        try:
            if p.exists() or p.is_symlink():
                p.unlink()
                gone += 1
        except OSError:
            pass
    return gone


def _launch(p, rid: int | None = None):
    """Start the browser, recovering from stale locks and explaining real clashes."""
    try:
        from playwright.sync_api import Error as PWError
    except ImportError:
        PWError = Exception
    prof = profile_dir()
    for attempt in (1, 2):
        try:
            return p.chromium.launch_persistent_context(
                str(prof), headless=False, viewport={"width": 1360, "height": 950},
                args=["--window-position=60,40"])
        except PWError as e:
            msg = str(e)
            stale = ("already in use" in msg or "existing browser session" in msg
                     or "ProcessSingleton" in msg)
            if attempt == 1 and stale:
                n = _clear_locks(prof)
                if rid is not None:
                    _say(rid, f"cleared {n} stale browser lock file(s), retrying")
                time.sleep(1.5)
                continue
            raise RuntimeError(
                "Could not start the automation browser. If a window opened by this "
                "tool is still open - including one left over from sign-in.bat - "
                "close it and try again."
            ) from e


BATCH = -1          # log/state key for the whole run, whether it's 1 report or 20
MAX_LOG = 500


def log_for(rid: int = BATCH) -> dict:
    return {"state": _active.get(rid, "idle"), "lines": _log.get(rid, [])}


def _say(rid: int, msg: str) -> None:
    """Log against the report and mirror into the combined run log the app shows."""
    stamp = time.strftime("%H:%M:%S")
    for key, line in ((rid, f"{stamp}  {msg}"),
                      *(() if rid == BATCH else ((BATCH, f"{stamp}  #{rid}: {msg}"),))):
        lines = _log.setdefault(key, [])
        lines.append(line)
        if len(lines) > MAX_LOG:
            del lines[:-MAX_LOG]
    print(f"[report {rid}] {msg}", flush=True)


# ---------------------------------------------------------------- JS helpers
# The council page puts #map_sidebar over the controls, so real mouse clicks get
# intercepted. Clicking through the DOM avoids that entirely.

_PRELUDE = """
  const norm = s => (s||'').replace(/\\s+/g,' ').trim().toLowerCase();
  const txt = e => (e && (e.innerText || e.textContent)) || '';
  const esc = s => (typeof CSS !== 'undefined' && CSS.escape)
                   ? CSS.escape(s) : String(s).replace(/["\\\\]/g, '\\\\$&');
  const labelOf = i => {
    let t = '';
    if (i.id) { const l = document.querySelector('label[for="' + esc(i.id) + '"]'); if (l) t = txt(l); }
    if (!t) { const l = i.closest && i.closest('label'); if (l) t = txt(l); }
    return t || i.value || '';
  };
  // Hidden steps of the wizard must never be clicked. In a real browser a
  // zero-size rect means hidden; without a layout engine, walk for display:none.
  const hasLayout = (() => {
    try { const b = document.body.getBoundingClientRect();
          return !!(b.width || b.height); } catch (err) { return false; }
  })();
  const shown = e => {
    if (e.hidden || e.disabled) return false;
    if (hasLayout && e.getBoundingClientRect) {
      const r = e.getBoundingClientRect();
      return !!(r.width || r.height);
    }
    for (let n = e; n; n = n.parentElement) {
      if (n.style && n.style.display === 'none') return false;
    }
    return true;
  };
  // A control's text also belongs to every wrapper around it, and clicking the
  // wrapper does nothing. Prefer real controls, and the innermost match.
  const findControl = targets => {
    const rank = e => ({BUTTON: 0, INPUT: 0, A: 1, LABEL: 1, SPAN: 2, DIV: 3})[e.tagName] ?? 4;
    const depth = e => { let d = 0; for (let n = e; n; n = n.parentElement) d++; return d; };
    const cands = [...document.querySelectorAll('button, a, div, span, label, input')]
      .filter(e => shown(e) && targets.includes(norm(txt(e) || e.value || '')));
    cands.sort((a, b) => rank(a) - rank(b) || depth(b) - depth(a));
    return cands[0] || null;
  };"""

JS_CLICK_TEXT = """
({wants, avoid}) => {
  %s
  // Trailing arrows and dashes are decoration, not part of the label.
  const clean = s => norm(s).replace(/[\\s»«>›→\\-–—:]+$/g, '');
  const targets = (wants || []).map(clean);
  const banned = (avoid || []).map(norm);
  const els = [...document.querySelectorAll("button, input[type=submit], input[type=button], a")];
  for (const e of els) {
    const t = clean(txt(e) || e.value || e.getAttribute('aria-label') || '');
    if (!t) continue;
    if (banned.some(b => t.includes(b))) continue;
    // The label must BE the target, or begin with it as a whole word. Never the
    // other way round: a plain "Continue" button must not satisfy a request for
    // "Continue - report a new problem".
    if (!targets.some(w => t === w || t.startsWith(w + ' '))) continue;
    if (!shown(e)) continue;
    if (e.scrollIntoView) e.scrollIntoView({block: 'center'});
    e.click();
    return t;
  }
  return null;
}""" % _PRELUDE

JS_CLICK_OPTION = """
(want) => {
  %s
  const target = norm(want);
  const inputs = [...document.querySelectorAll("input[type=radio], input[type=checkbox]")];
  // exact label match first, then exact value, then a contains match
  for (const pass of [0, 1, 2]) {
    for (const i of inputs) {
      const lt = norm(labelOf(i)), vt = norm(i.value);
      const hit = pass === 0 ? lt === target
                : pass === 1 ? vt === target
                : (lt.includes(target) && target.length > 4);
      if (!hit) continue;
      if (i.scrollIntoView) i.scrollIntoView({block: 'center'});
      i.click();
      i.dispatchEvent(new Event('change', {bubbles: true}));
      return labelOf(i).replace(/\\s+/g, ' ').trim();
    }
  }
  return null;
}""" % _PRELUDE

JS_CHECKED = """
() => [...document.querySelectorAll("input[type=radio]:checked")]
        .map(i => (i.value || '').trim()).filter(Boolean)"""

JS_FILL = """
({sels, labels, value, force}) => {
  %s
  let el = null;
  for (const s of sels) {
    const e = document.querySelector(s);
    if (e && !e.disabled && e.type !== 'hidden' && shown(e)) { el = e; break; }
  }
  if (!el) {
    for (const c of document.querySelectorAll('input, textarea')) {
      if (c.disabled || ['hidden','file','radio','checkbox','submit','button'].includes(c.type)) continue;
      if (!shown(c)) continue;              // a later wizard step is not ours to fill
      if (!force && norm(c.value)) continue;             // don't overwrite a filled field
      let t = ' ' + labelOf(c) + ' ' + (c.getAttribute('placeholder')||'')
                  + ' ' + (c.name||'') + ' ' + (c.id||'');
      if (labels.map(norm).some(L => norm(t).includes(L))) { el = c; break; }
    }
  }
  if (!el) return null;
  if (el.scrollIntoView) el.scrollIntoView({block: 'center'});
  el.focus();
  el.value = value;
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return el.id || el.name || el.tagName.toLowerCase();
}""" % _PRELUDE

JS_ERRORS = """
() => {
  %s
  const out = [];
  const sel = '.form-error, .error, .errors, .alert, [role=alert], .field-error, '
            + '.js-error, label.error, .govuk-error-message';
  document.querySelectorAll(sel).forEach(e => {
    if (!shown(e)) return;
    // Granicus wraps ordinary parts of the page in these same classes, so a
    // container holding a control is part of the form, not a complaint about
    // it - the address step's Search button sits in one, and reading its label
    // back as an error is what used to stop a form that was working.
    if (/^(button|a|input|select|textarea)$/i.test(e.tagName)) return;
    if (e.querySelector('button, input, select, textarea, a[href]')) return;
    const t = txt(e).replace(/\\s+/g, ' ').trim();
    if (t && t.length < 180) out.push(t);
  });
  return [...new Set(out)];
}""" % _PRELUDE

JS_VISIBLE_OPTIONS = """
() => {
  %s
  const out = [];
  document.querySelectorAll("input[type=radio]").forEach(i => {
    out.push(labelOf(i).replace(/\\s+/g, ' ').trim());
  });
  return out.filter(Boolean);
}""" % _PRELUDE


def _js(page, script, arg=None):
    try:
        return page.evaluate(script, arg) if arg is not None else page.evaluate(script)
    except Exception:
        return None


def _click_text(page, wants, what, rid, settle=1200, avoid=None) -> bool:
    """Click a button by its visible label. `avoid` skips look-alike buttons."""
    hit = _js(page, JS_CLICK_TEXT, {"wants": list(wants), "avoid": list(avoid or [])})
    if hit:
        _say(rid, f"clicked {what} ('{hit}')")
        page.wait_for_timeout(settle)
        return True
    return False


def _fill(page, sels, labels, value, what, rid, warn: bool = True,
          wait_seconds: float = 0, force: bool = False) -> bool:
    """Fill a field.

    `warn=False` for fields a page may legitimately not have. `wait_seconds` waits
    for a step of the wizard to finish appearing - the fields exist in the page
    from the start but stay hidden until their step is reached. `force` overwrites
    a field that already has something in it, for a second go at a search box.
    """
    if value in (None, ""):
        return True
    deadline = time.time() + max(0.0, wait_seconds)
    while True:
        got = _js(page, JS_FILL, {"sels": list(sels), "labels": list(labels),
                                  "value": str(value), "force": bool(force)})
        if got:
            _say(rid, f"filled {what} (field '{got}')")
            return True
        if time.time() >= deadline:
            break
        page.wait_for_timeout(700)
    if warn:
        _say(rid, f"WARNING: no field found for {what}"
                  + (f" after waiting {int(wait_seconds)}s" if wait_seconds else ""))
    return False


JS_SET_CHECKBOX = """
({wants, value}) => {
  %s
  const targets = wants.map(norm);
  for (const cb of document.querySelectorAll("input[type=checkbox]")) {
    const lt = norm(labelOf(cb));
    if (!targets.some(w => lt === w || lt.includes(w))) continue;
    if (cb.scrollIntoView) cb.scrollIntoView({block: 'center'});
    if (cb.checked !== !!value) {          // only click when it needs changing
      cb.click();
      cb.dispatchEvent(new Event('change', {bubbles: true}));
    }
    return {label: labelOf(cb).replace(/\\s+/g, ' ').trim(), checked: cb.checked};
  }
  return null;
}""" % _PRELUDE

JS_WHICH_PAGE = """
() => {
  %s
  const body = norm(txt(document.body));
  const hasField = words => [...document.querySelectorAll('input, textarea, select')]
    .some(f => shown(f) && words.some(w => norm(labelOf(f)).includes(w)));
  const hasFile = [...document.querySelectorAll("input[type=file]")].length > 0;

  // What the FIELDS say comes first. Note the tab strip repeats every step name
  // in the body text on every page, so body text alone cannot identify a step.
  if (body.includes('do you agree')) return 'intro';
  if (hasField(['first name', 'forename'])) return 'details';
  if (hasFile && !hasField(['first name'])) return 'photo';
  if (hasField(['nearest street', 'street search', 'street', 'road'])) return 'street';
  // The bins form has its own "Issue" step: bin type, what is wrong, in a park.
  if (hasField(['type of bin', 'bin type', 'what is the issue', 'issue with the bin',
                'is the bin', 'in a park'])) return 'issue';
  if ([...document.querySelectorAll('button, input[type=submit]')]
        .some(e => shown(e) && /submit/i.test(txt(e) || e.value || ''))) return 'final';

  // Otherwise fall back to whichever wizard tab is highlighted.
  const tab = [...document.querySelectorAll('[role=tab], li, a, button, div')]
    .filter(e => shown(e) && /\\b(active|current|selected)\\b/i.test(e.className || ''))
    .map(e => norm(txt(e))).find(t => t && t.length < 40);
  if (tab) {
    if (tab.includes('introduction')) return 'intro';
    if (tab.includes('your details')) return 'details';
    if (tab.includes('nearest street') || tab.includes('location')) return 'street';
    if (tab.includes('photo')) return 'photo';
    if (tab.includes('issue')) return 'issue';
    if (tab.includes('fly-tipping') || tab.includes('flytipping')) return 'final';
  }
  return 'unknown';
}""" % _PRELUDE

JS_PAGE_SIG = """
() => {
  %s
  // A fingerprint of what is on screen. Two steps of the same wizard can share a
  // name, so the step alone is not enough to tell whether the form has moved on.
  const labels = [...document.querySelectorAll('input, textarea, select')]
    .filter(f => shown(f)).map(f => norm(labelOf(f)).slice(0, 40));
  const heads = [...document.querySelectorAll('h1, h2, h3, legend')]
    .filter(e => shown(e)).map(e => norm(txt(e)).slice(0, 60));
  return [...heads, ...labels].join('|').slice(0, 600);
}""" % _PRELUDE

JS_QUESTION = """
({question, answer, probe}) => {
  %s
  // Answer one question, found by the words of the question itself. The answers
  // are sometimes a dropdown and sometimes styled buttons, so handle both.
  const wants = (question || []).map(norm).filter(Boolean);
  const want = norm(answer || '');
  const exact = t => t === want || t.startsWith(want + ' ') || want.startsWith(t + ' ');
  const loose = t => want.length > 3 && (t.includes(want) || want.includes(t));

  // Each wording is tried in turn, most specific first, because one question can
  // contain the words of another: "Is the location of the issue in a park?".
  const cands = [];
  for (const w of wants.length ? wants : ['']) {
    for (const sel of document.querySelectorAll('select')) {
      if (!shown(sel)) continue;
      if (w && !norm(labelOf(sel)).includes(w)) continue;
      const opts = [...sel.options].filter(o => o.value && !/^\\s*select/i.test(o.text));
      const cur = sel.options[sel.selectedIndex];
      const now = norm(cur ? cur.text : '');
      if (probe) return {found: true, kind: 'select', on: exact(now) || loose(now),
                         text: cur ? cur.text.trim() : ''};
      const best = opts.find(o => exact(norm(o.text)))
                || opts.find(o => loose(norm(o.text)));
      if (!best) return {found: true, kind: 'select', status: 'nomatch',
                         options: opts.map(o => o.text.trim()).slice(0, 12)};
      sel.value = best.value;
      sel.dispatchEvent(new Event('input', {bubbles: true}));
      sel.dispatchEvent(new Event('change', {bubbles: true}));
      return {found: true, kind: 'select', status: 'picked', on: true,
              text: best.text.trim()};
    }

    // Otherwise the answers live in the block of the page that asks the
    // question. The smallest block containing it is the one that owns them.
    const blocks = [...document.querySelectorAll('fieldset, div, li, section, tr, p')]
      .filter(e => shown(e) && (!w || norm(txt(e)).includes(w)))
      .sort((a, b) => txt(a).length - txt(b).length);
    for (const block of blocks.slice(0, 8)) {
      for (const o of block.querySelectorAll('input, button, label, a, span, div')) {
        if (!shown(o)) continue;
        const isInput = o.tagName === 'INPUT';
        if (isInput && o.type !== 'radio' && o.type !== 'checkbox') continue;
        const t = norm(isInput ? (labelOf(o) || o.value) : txt(o));
        if (!t || t.length > 70) continue;
        const quality = exact(t) ? 0 : (loose(t) ? 1 : -1);
        if (quality < 0) continue;
        const rank = isInput ? 0 : (o.tagName === 'BUTTON' ? 1 : 2);
        // Prefer the real control over any wrapper that repeats its text.
        cands.push({el: o, t: t,
                    score: quality * 1000 + rank * 100 + o.querySelectorAll('*').length});
      }
      if (cands.length) break;
    }
    if (cands.length) break;
  }
  if (!cands.length) return {found: false};
  cands.sort((a, b) => a.score - b.score);
  const el = cands[0].el;
  const inner = el.querySelector ? el.querySelector('input') : null;
  const on = e => {
    if (!e) return false;
    if (e.tagName === 'INPUT') return !!e.checked;
    if (e.getAttribute('aria-pressed') === 'true') return true;
    if (/(selected|active|checked)/i.test(e.className || '')) return true;
    try {
      const m = (getComputedStyle(e).backgroundColor || '')
        .match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
      if (m) { const r = +m[1], g = +m[2], b = +m[3];
               return g > r + 20 && g > b + 20; }   // the control turns green
    } catch (err) {}
    return false;
  };
  const set = on(inner) || on(el);
  if (probe) return {found: true, kind: 'buttons', on: set, text: cands[0].t};
  document.querySelectorAll('[data-nncr]').forEach(e => e.removeAttribute('data-nncr'));
  el.setAttribute('data-nncr', '1');
  if (el.scrollIntoView) el.scrollIntoView({block: 'center'});
  return {found: true, kind: 'buttons', status: 'marked', text: cands[0].t, on: set};
}""" % _PRELUDE

JS_CLICK_MARKED = """
() => {
  const el = document.querySelector('[data-nncr="1"]');
  if (!el) return null;
  const t = el.querySelector('input') || el;
  ['pointerdown', 'mousedown', 'mouseup', 'click'].forEach(type => {
    try { t.dispatchEvent(new MouseEvent(type,
            {bubbles: true, cancelable: true, view: window})); } catch (e) {}
  });
  try { t.dispatchEvent(new Event('change', {bubbles: true})); } catch (e) {}
  return (t.tagName || '') + ':' + ((t.innerText || t.value || '').trim().slice(0, 40));
}"""

JS_TOGGLE_STATE = """
(wants) => {
  %s
  const el = findControl(wants.map(norm));
  if (!el) return {found: false};
  let checked = el.tagName === 'INPUT' ? !!el.checked : null;
  const inner = el.querySelector && el.querySelector('input');
  if (inner) checked = !!inner.checked;
  let green = false, bg = '';
  try {
    bg = getComputedStyle(el).backgroundColor || '';
    const m = bg.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
    if (m) { const r = +m[1], g = +m[2], b = +m[3];
             green = g > r + 20 && g > b + 20; }        // the control turns green
  } catch (e) {}
  const pressed = el.getAttribute('aria-pressed') === 'true'
    || /\\b(selected|active|checked|on|success)\\b/i.test(el.className || '');
  const near = txt(el.parentElement || el);
  const tick = /\\u2713|\\u2714/.test(near);
  return {found: true, agreed: !!(checked || green || pressed || tick), bg: bg};
}""" % _PRELUDE

JS_FORCE_CLICK = """
(wants) => {
  %s
  const el = findControl(wants.map(norm));
  if (!el) return null;
  const fire = t => {
    const ev = new MouseEvent(t, {bubbles: true, cancelable: true, view: window});
    el.dispatchEvent(ev);
  };
  try { el.dispatchEvent(new PointerEvent('pointerdown', {bubbles: true})); } catch (e) {}
  fire('mousedown'); fire('mouseup'); fire('click');
  try { el.dispatchEvent(new PointerEvent('pointerup', {bubbles: true})); } catch (e) {}
  const inner = el.querySelector && el.querySelector('input, button, span');
  if (inner) { try { inner.click(); } catch (e) {} }
  return txt(el).trim().slice(0, 30);
}""" % _PRELUDE

JS_DESCRIBE = """
() => {
  %s
  const fields = [...document.querySelectorAll('input, textarea, select')]
    .filter(shown).map(f => (labelOf(f) || f.name || f.id || f.type).trim().slice(0, 30));
  const buttons = [...document.querySelectorAll('button, input[type=submit], a.btn')]
    .filter(shown).map(b => (txt(b) || b.value || '').trim().slice(0, 25)).filter(Boolean);
  return {
    text: norm(txt(document.body)).slice(0, 220),
    fields: fields.slice(0, 12), buttons: buttons.slice(0, 10),
    frames: document.querySelectorAll('iframe').length,
  };
}""" % _PRELUDE

JS_PICK_OPTION = """
({labels, tokens, allowFirst}) => {
  %s
  // Options read like "Example Road Corby", so score them on the street name
  // (essential) plus the town (a tie-breaker between roads of the same name).
  const toks = (tokens || []).map(norm).filter(Boolean);
  for (const sel of document.querySelectorAll('select')) {
    if (!shown(sel)) continue;
    const lab = norm(labelOf(sel));
    if (labels.length && !labels.map(norm).some(l => lab.includes(l))) continue;
    const opts = [...sel.options].filter(o => o.value && !/^\\s*select/i.test(o.text));
    if (!opts.length) return {status: 'empty', options: []};

    let best = null, bestScore = 0;
    for (const o of opts) {
      const t = norm(o.text);
      let score = 0;
      toks.forEach((tk, i) => { if (t.includes(tk)) score += (i === 0 ? 2 : 1); });
      if (score > bestScore) { bestScore = score; best = o; }
    }
    const matched = bestScore >= 2;               // the street name itself matched
    if (!matched) best = null;
    if (!best && opts.length === 1) best = opts[0];
    if (!best && allowFirst) best = opts[0];
    if (!best) return {status: 'choose',
                       options: opts.slice(0, 8).map(o => o.text.trim()),
                       count: opts.length};
    sel.value = best.value;
    sel.dispatchEvent(new Event('input', {bubbles: true}));
    sel.dispatchEvent(new Event('change', {bubbles: true}));
    return {status: 'picked', text: best.text.trim(), matched: matched,
            count: opts.length};
  }
  return {status: 'none', options: []};
}""" % _PRELUDE

JS_NEAREST_ASSET = """
({lat, lon, layerPattern, snapToLine}) => {
  // Some categories (trees, street lights, bins, grit bins) put council assets on
  // the map and will not accept the report until one is picked. The page's own
  // OpenLayers map knows where they are, so ask it for the closest to the photo.
  const fms = window.fixmystreet, OL = window.OpenLayers;
  if (!fms || !fms.map || !OL) return {ok: false, why: 'the map is not ready yet'};
  const map = fms.map;
  let target;
  try {
    target = new OL.LonLat(lon, lat)
      .transform(new OL.Projection('EPSG:4326'), map.getProjectionObject());
  } catch (e) { return {ok: false, why: 'could not place the photo on the map'}; }

  // Closest point ON a line - a pothole pin has to land on the carriageway, not
  // on the pavement the photo was taken from.
  const nearestOnSegment = (p, a, b) => {
    const vx = b.x - a.x, vy = b.y - a.y;
    const len2 = vx * vx + vy * vy;
    if (!len2) return {x: a.x, y: a.y};
    let t = ((p.lon - a.x) * vx + (p.lat - a.y) * vy) / len2;
    t = Math.max(0, Math.min(1, t));
    return {x: a.x + t * vx, y: a.y + t * vy};
  };
  const linesOf = g => {                       // walk down to runs of points
    if (!g || !g.components) return [];
    const own = g.components.filter(c => c && c.x !== undefined && c.y !== undefined);
    const out = own.length ? [own.map(c => ({x: c.x, y: c.y}))] : [];
    g.components.forEach(c => {
      if (c && c.x === undefined) out.push(...linesOf(c));
    });
    return out;
  };

  const wantLayer = layerPattern ? new RegExp(layerPattern, 'i') : null;
  let best = null, bestD = Infinity, count = 0;
  (map.layers || []).forEach(layer => {
    if (!layer.features || !layer.features.length) return;
    if (layer.visibility === false) return;
    const name = layer.name || '';
    if (/pin|report|problem/i.test(name)) return;               // our own pin
    if (wantLayer && !wantLayer.test(name)) return;
    layer.features.forEach(f => {
      if (!f.geometry) return;
      count++;
      let c = null, d = Infinity;
      if (snapToLine) {
        linesOf(f.geometry).forEach(pts => {
          for (let i = 0; i + 1 < pts.length; i++) {
            const q = nearestOnSegment(target, pts[i], pts[i + 1]);
            const dx = q.x - target.lon, dy = q.y - target.lat;
            const dd = Math.sqrt(dx * dx + dy * dy);
            if (dd < d) { d = dd; c = {lon: q.x, lat: q.y}; }
          }
        });
      }
      if (!c) {
        if (typeof f.geometry.getBounds !== 'function') return;
        c = f.geometry.getBounds().getCenterLonLat();
        const dx = c.lon - target.lon, dy = c.lat - target.lat;
        d = Math.sqrt(dx * dx + dy * dy);
      }
      if (d < bestD) { bestD = d; best = {f: f, c: c, layer: name || 'assets'}; }
    });
  });
  if (!best) return {ok: false, why: snapToLine ? 'no roads are shown on the map'
                                                : 'no assets are shown on the map',
                     count: count};

  const px = map.getPixelFromLonLat(best.c);
  const el = map.div || document.getElementById('map');
  if (!el || !px) return {ok: false, why: 'could not work out where to click'};
  const r = el.getBoundingClientRect();
  const inView = px.x >= 0 && px.y >= 0 && px.x <= r.width && px.y <= r.height;
  // Web-mercator metres are stretched by latitude; correct so the log is honest.
  const metres = bestD * Math.cos(lat * Math.PI / 180);
  let label = '';
  try {
    const a = best.f.attributes || {};
    label = a.name || a.description || a.asset_id || a.id ||
            Object.values(a).filter(v => typeof v === 'string')[0] || '';
  } catch (e) {}
  return {ok: true, x: r.left + px.x, y: r.top + px.y, inView: inView,
          metres: Math.round(metres), layer: best.layer, count: count,
          label: String(label).slice(0, 60)};
}"""

JS_ROAD_CANDIDATES = """
({lat, lon, layerPattern, maxMetres, limit}) => {
  // Several roads may be within reach and only some are maintained by the council,
  // so return a ranked list of places to try rather than just the closest.
  const fms = window.fixmystreet, OL = window.OpenLayers;
  if (!fms || !fms.map || !OL) return {ok: false, why: 'the map is not ready yet'};
  const map = fms.map;
  let target;
  try {
    target = new OL.LonLat(lon, lat)
      .transform(new OL.Projection('EPSG:4326'), map.getProjectionObject());
  } catch (e) { return {ok: false, why: 'could not place the photo on the map'}; }

  const stretch = 1 / Math.cos(lat * Math.PI / 180);      // mercator -> real metres
  const budget = maxMetres * stretch;
  const nearestOnSegment = (p, a, b) => {
    const vx = b.x - a.x, vy = b.y - a.y, len2 = vx * vx + vy * vy;
    if (!len2) return {x: a.x, y: a.y, t: 0, len: 0};
    let t = ((p.lon - a.x) * vx + (p.lat - a.y) * vy) / len2;
    t = Math.max(0, Math.min(1, t));
    return {x: a.x + t * vx, y: a.y + t * vy, t: t, len: Math.sqrt(len2),
            vx: vx, vy: vy};
  };
  const linesOf = g => {
    if (!g || !g.components) return [];
    const own = g.components.filter(c => c && c.x !== undefined);
    const out = own.length ? [own.map(c => ({x: c.x, y: c.y}))] : [];
    g.components.forEach(c => { if (c && c.x === undefined) out.push(...linesOf(c)); });
    return out;
  };

  const wantLayer = layerPattern ? new RegExp(layerPattern, 'i') : null;
  const found = [];
  (map.layers || []).forEach(layer => {
    if (!layer.features || !layer.features.length) return;
    if (layer.visibility === false) return;
    const name = layer.name || '';
    if (/pin|report|problem/i.test(name)) return;
    if (wantLayer && !wantLayer.test(name)) return;
    layer.features.forEach(f => {
      let bestD = Infinity, bestPt = null, bestSeg = null;
      linesOf(f.geometry).forEach(pts => {
        for (let i = 0; i + 1 < pts.length; i++) {
          const q = nearestOnSegment(target, pts[i], pts[i + 1]);
          const dx = q.x - target.lon, dy = q.y - target.lat;
          const dd = Math.sqrt(dx * dx + dy * dy);
          if (dd < bestD) { bestD = dd; bestPt = q; bestSeg = q; }
        }
      });
      if (!bestPt || bestD > budget) return;
      const a = f.attributes || {};
      const label = String(a.name || a.description || a.usrn || a.asset_id ||
                           a.id || Object.values(a).find(v => typeof v === 'string')
                           || '').slice(0, 40);
      const pts = [{x: bestPt.x, y: bestPt.y, note: ''}];
      // A point a few metres along the road helps when the closest spot is a
      // junction or the very end of a maintained length.
      if (bestSeg && bestSeg.len > 0) {
        const step = 10 * stretch / bestSeg.len;
        [step, -step].forEach(s => {
          const t2 = Math.max(0, Math.min(1, bestSeg.t + s));
          if (Math.abs(t2 - bestSeg.t) > 1e-6) {
            pts.push({x: bestPt.x + (t2 - bestSeg.t) * bestSeg.vx,
                      y: bestPt.y + (t2 - bestSeg.t) * bestSeg.vy,
                      note: ' (10m along)'});
          }
        });
      }
      pts.forEach(p => found.push({d: bestD, p: p, label: label, layer: name}));
    });
  });
  if (!found.length) return {ok: false, why: 'no roads within reach on the map',
                             count: 0};

  found.sort((a, b) => a.d - b.d);
  const el = map.div || document.getElementById('map');
  const rect = el.getBoundingClientRect();
  const out = [];
  found.forEach(c => {
    if (out.length >= (limit || 5)) return;
    const px = map.getPixelFromLonLat({lon: c.p.x, lat: c.p.y});
    if (!px) return;
    if (px.x < 0 || px.y < 0 || px.x > rect.width || px.y > rect.height) return;
    const x = rect.left + px.x, y = rect.top + px.y;
    if (out.some(o => Math.abs(o.x - x) < 6 && Math.abs(o.y - y) < 6)) return;
    out.push({x: x, y: y, metres: Math.round(c.d / stretch),
              label: c.label + (c.p.note || ''), layer: c.layer});
  });
  return {ok: out.length > 0, candidates: out, count: found.length,
          why: out.length ? '' : 'the roads found are off the visible map'};
}"""

JS_FMS_STEP = """
() => {
  %s
  // The highways form puts the step in the URL (#duplicates, #details, #user),
  // which is far more reliable than guessing from a fixed running order.
  const hash = ((location.hash || '').replace('#', '') || '').toLowerCase();
  if (hash === 'duplicates') return 'duplicates';
  if (hash === 'details') return 'details';
  if (hash === 'user') return 'user';
  if (hash === 'photo') return 'photo';
  if (hash === 'report' || hash === 'category') return 'category';

  const vis = q => [...document.querySelectorAll(q)].some(shown);
  const body = norm(txt(document.body));
  if (vis('input#form_title, input[name=title]')) return 'details';
  if (vis('input#form_name, input[name=name]')) return 'user';
  if (body.includes('already been reported') || body.includes('is one of them yours')
      || body.includes('similar problems nearby')) return 'duplicates';
  if (vis("input[type=radio][name=category], select#form_category")) return 'category';
  return 'unknown';
}""" % _PRELUDE

JS_ASSET_PROMPT = """
(wants) => {
  %s
  const body = norm(txt(document.body));
  return wants.map(norm).some(w => body.includes(w));
}""" % _PRELUDE

JS_LIST_OPTIONS = """
({labels}) => {
  %s
  for (const sel of document.querySelectorAll('select')) {
    if (!shown(sel)) continue;
    const lab = norm(labelOf(sel));
    if (labels.length && !labels.map(norm).some(l => lab.includes(l))) continue;
    return [...sel.options].map(o => o.text.trim())
      .filter(t => t && !/^select\\.*$/i.test(t));
  }
  return [];
}""" % _PRELUDE

JS_END_OF_FORM = """
() => {
  %s
  // Are we at the end of the form? A wizard step with a Next button is not the
  // end, however submit-like anything on it looks: the Location step's Next
  // button once got mistaken for Submit and the walker stopped three steps early.
  const clean = s => norm(s).replace(/[\\s»«>›→\\-–—:]+$/g, '');
  // A disabled button is not one you can press: while a photo uploads, the form
  // disables Next, and treating that as "no Next button" gave up on the step.
  const btns = [...document.querySelectorAll(
      'button, input[type=submit], input[type=button], a')]
    .filter(e => shown(e) && !e.disabled && e.getAttribute('aria-disabled') !== 'true');
  const label = e => clean(txt(e) || e.value || e.getAttribute('aria-label') || '');
  const is = (e, re) => re.test(label(e));
  return {
    submit: btns.some(e => is(e, /^submit\\b/)),
    next: btns.some(e => is(e, /^(next|continue)\\b/)),
    labels: btns.map(label).filter(Boolean).slice(0, 12),
  };
}""" % _PRELUDE

JS_QUESTION_OPTIONS = """
({question}) => {
  %s
  // The answers this question offers, in the form's own wording - which is not
  // ours ("Requires emptying" for what we call "needs emptying").
  //
  // The wordings are tried one at a time, most specific first: the live form
  // asks "Is the location of the issue in a park?", so a loose search for the
  // word "issue" finds the park question and answers the wrong one.
  const wants = (question || []).map(norm).filter(Boolean);
  const nav = /^(next|previous|back|cancel|submit|continue|search|home|save)\\b/;

  for (const want of wants) {
    for (const sel of document.querySelectorAll('select')) {
      if (!shown(sel) || !norm(labelOf(sel)).includes(want)) continue;
      return [...sel.options].filter(o => o.value && !/^\\s*select/i.test(o.text))
        .map(o => o.text.trim());
    }

    const blocks = [...document.querySelectorAll('fieldset, div, li, section, tr, p')]
      .filter(e => shown(e) && norm(txt(e)).includes(want))
      .sort((a, b) => txt(a).length - txt(b).length);
    for (const block of blocks.slice(0, 8)) {
      const seen = new Set();
      for (const o of block.querySelectorAll('input, button, label, a, span, div')) {
        if (!shown(o)) continue;
        const isInput = o.tagName === 'INPUT';
        if (isInput && o.type !== 'radio' && o.type !== 'checkbox') continue;
        // Leaf controls only, so a wrapper repeating its child's text is not
        // counted as an option of its own.
        if (!isInput && o.querySelector('button, label, input, a, select')) continue;
        const t = (isInput ? (labelOf(o) || o.value) : txt(o))
          .replace(/\\s+/g, ' ').replace(/\\s*\\*\\s*$/, '').trim();
        if (!t || t.length > 70 || nav.test(norm(t))) continue;
        if (wants.some(w => norm(t).includes(w))) continue;   // that is the question
        seen.add(t);
      }
      if (seen.size >= 2) return [...seen];
    }
  }
  return [];
}""" % _PRELUDE

JS_HAS_SUBMIT = """
() => {
  %s
  return [...document.querySelectorAll('button, input[type=submit]')]
    .some(e => shown(e) && /submit/i.test(txt(e) || e.value || ''));
}""" % _PRELUDE

JS_PUBLISHED_PHOTO = """
() => {
  const abs = s => { try { return new URL(s, document.baseURI).href; }
                     catch (e) { return s; } };
  for (const i of document.querySelectorAll('img')) {
    const src = i.getAttribute('src') || '';
    if (/\\/photo\\//i.test(src) || /\\/photos?\\//i.test(src)) {
      const full = abs(src);
      // prefer the full-size version FixMyStreet also publishes
      return full.replace(/\\.(\\d+)\\.(jpeg|jpg|png)$/i, '.$1.full.$2');
    }
  }
  for (const a of document.querySelectorAll('a')) {
    const href = a.getAttribute('href') || '';
    if (/\\/photo\\//i.test(href)) return abs(href);
  }
  return null;
}"""

JS_SIGNED_IN = """
() => {
  const b = document.body;
  const t = ((b && (b.innerText || b.textContent)) || '').toLowerCase();
  if (/sign out|log out|your account/.test(t)) return true;   // checked first
  if (/sign in|log in/.test(t)) return false;
  return null;                                                 // can't tell
}"""


# Words that are a control on this form, never a complaint about what we typed.
# The council's forms label their own buttons inside the same wrappers they use
# for validation messages, so these have to be discounted by name as well.
NOT_ERRORS = {
    "search", "next", "previous", "back", "continue", "start", "cancel",
    "submit", "save", "close", "clear", "ok", "yes", "no", "add", "remove",
    "edit", "print", "select address", "find address", "select", "choose",
}


def _errors(page) -> list:
    """What the form is actually complaining about.

    Anything that is only the label of one of the form's own buttons is dropped:
    a form that has moved on to the next step is not an error, and treating one
    as an error used to send the walker back to redo a step it had passed.
    """
    out = []
    for e in _js(page, JS_ERRORS) or []:
        low = " ".join(str(e).lower().split()).strip(" .:*")
        if not low or low in NOT_ERRORS:
            continue
        out.append(e)
    return out


def _cookies(page, sel, rid) -> None:
    if _js(page, "() => typeof updateCookies === 'function'"):
        _js(page, "() => updateCookies(true)")
        page.wait_for_timeout(500)
    else:
        _click_text(page, sel["cookie_accept_text"], "cookie banner", rid, 600)


def sign_in(page, cfg: dict, sel: dict, rid: int) -> bool:
    """Sign in to the highways site so reports attach to the account and show up
    in 'My reports'. Returns True only if sign-in is confirmed."""
    login = cfg.get("highways_login") or {}
    if not login.get("enabled", True):
        return False
    if not (login.get("email") and login.get("password")):
        _say(rid, "no highways login in config - reporting as a guest")
        return False

    if _js(page, JS_SIGNED_IN) is True:
        _say(rid, f"already signed in as {login['email']}")
        return True

    # Go straight to the sign-in page: it is the cheapest page that tells us
    # whether the saved session is still good.
    page.goto(sel.get("auth_url", "https://highways.northnorthants.gov.uk/auth"),
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1800)
    _cookies(page, sel, rid)
    if _js(page, JS_SIGNED_IN) is True:
        _say(rid, f"session still valid, signed in as {login['email']}")
        return True
    _say(rid, f"signing in as {login['email']}")

    # The page offers 'sign in with a password' and 'email me a link'. Filling the
    # password field is what selects the password route.
    _fill(page, sel["signin_email"], ["your email", "email address", "email"],
          login["email"], "sign-in email", rid)
    filled = _fill(page, sel["signin_password"], ["your password", "password"],
                   login["password"], "sign-in password", rid)
    if not filled:
        _say(rid, "WARNING: no password box found - sign in manually in the browser")
        return False

    if not _click_text(page, sel["signin_button_text"], "Sign in", rid, 3000):
        _js(page, """() => {
              const p = document.querySelector("input[type=password]");
              const f = p && p.form; if (f) f.submit();
            }""")
        _say(rid, "submitted the sign-in form")
    page.wait_for_timeout(3500)

    if _js(page, JS_SIGNED_IN) is True:
        _say(rid, "signed in")
        return True
    for e in _errors(page):
        _say(rid, f"sign-in page says: {e}")
    _say(rid, "WARNING: could not confirm sign-in - carrying on as a guest. "
              "Check the email/password in config.json, or sign in manually.")
    return False


def _report_errors(page, rid, step) -> list:
    errs = [e for e in _errors(page) if len(e) > 3]
    for e in errs:
        _say(rid, f"form says: {e}")
    return errs


# ---------------------------------------------------------------- category

def _set_category(page, category: str, group: str, rid: int) -> bool:
    """North Northants shows categories as radio buttons. Grouped ones are a
    two-step: pick the group, wait for the sub-list, then pick the category."""
    # A plain <select> version exists on some FixMyStreet builds - try it first.
    for cand in ("select#form_category", "select#category", "select[name='category']"):
        try:
            s = page.locator(cand).first
            s.wait_for(state="visible", timeout=1200)
            for o in s.locator("option").all_inner_texts():
                if o.strip().lower() == category.lower():
                    s.select_option(label=o)
                    _say(rid, f"selected category '{category}' from dropdown")
                    page.wait_for_timeout(900)
                    return True
        except Exception:
            pass

    steps = ([group] if group else []) + [category]
    for i, want in enumerate(steps):
        hit = _js(page, JS_CLICK_OPTION, want)
        if not hit:
            visible = _js(page, JS_VISIBLE_OPTIONS) or []
            _say(rid, f"WARNING: '{want}' is not one of the options this form offers")
            if i > 0:
                _say(rid, f"the group '{group}' IS selected - just pick the exact "
                          f"item in the browser and carry on")
            if visible:
                shortlist = [v for v in visible
                             if v.lower()[:4] in (want or "    ").lower()][:6]
                _say(rid, "options now: " + ", ".join(visible[:14])
                          + (" …" if len(visible) > 14 else ""))
                if shortlist:
                    _say(rid, f"closest looking: {', '.join(shortlist)}")
            return False
        _say(rid, f"selected {'group' if i == 0 and group else 'category'} '{hit}'")
        page.wait_for_timeout(1400)   # sub-list is fetched after the group click

    checked = _js(page, JS_CHECKED) or []
    if any(category.lower() == c.lower() for c in checked):
        _say(rid, f"confirmed category is set to '{category}'")
        return True
    _say(rid, f"WARNING: clicked '{category}' but the form reports "
              f"{checked or 'nothing'} selected")
    return bool(checked)


# ---------------------------------------------------------------- flows
# One browser, one tab per report. Every tab is prefilled and left at its Submit
# button; a single watcher then notices whichever ones you submit, in any order.

def _fms_step(page) -> str:
    return _js(page, JS_FMS_STEP) or "unknown"


def _wait_step_change(page, was: str, seconds: float = 10) -> str:
    """Wait for the form to move on. Returns the step it settled on."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        page.wait_for_timeout(700)
        now = _fms_step(page)
        if now != was:
            return now
    return was


def _prefill_fms(page, r: dict, cfg: dict, sel: dict, signed: bool, rid: int) -> bool:
    """Drive the highways form to its last step. True if it reached Submit.

    Step-driven rather than a fixed running order: the form tells us where it is
    (the step is in the URL), so a slow-loading page cannot put us out of sync.
    """
    lab = sel.get("labels", {})
    rep = cfg["reporter"]
    url = sel["url"].format(lat=f"{r['lat']:.6f}", lon=f"{r['lon']:.6f}")
    upload = photos.to_jpeg_for_upload(Path(r["photo_path"]), config.data_dir()) \
        if r.get("photo_path") else None
    avoid = sel.get("continue_avoid_text", [])

    _say(rid, f"opening {url}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    _cookies(page, sel, rid)

    attached = False
    category_done = False
    stalled = 0

    for _ in range(int(sel.get("max_steps", 14))):
        step = _fms_step(page)

        if step == "category":
            if not category_done:
                if not _set_category(page, r["category"] or "", r["cat_group"] or "",
                                     rid):
                    _say(rid, "STOPPED before Continue - pick the category in this "
                              "tab and carry on. Nothing has been submitted.")
                    return False
                notice = _dead_end(page, sel)
                if notice:
                    _say(rid, f"this category cannot be reported here - the form "
                              f"says: {notice}")
                    if r.get("service") != "granicus":
                        store.update(r["id"], {"service": "granicus"})
                        _say(rid, "switched this report to the fly-tipping form - "
                                  "run it again and it will go to the right place")
                    return False
                if _js(page, JS_ASSET_PROMPT, sel.get("asset_prompt_text", [])):
                    _say(rid, "this category needs something picked from the map")
                    _select_nearest_asset(page, sel, r, rid)
                if _off_highway(page, sel):
                    _say(rid, "the site says this spot is not one it looks after - "
                              "trying the roads nearby")
                    if not _try_road_candidates(page, sel, r, rid):
                        _say(rid, "STOPPED - move the pin onto a road the council "
                                  "maintains in this tab, or the road here may not "
                                  "be theirs. Nothing has been submitted.")
                        return False
                category_done = True
            _click_text(page, sel["continue_text"], "Continue (category)", rid, 1500,
                        avoid=avoid)

        elif step == "duplicates":
            _say(rid, "the form is showing similar reports nearby")
            if not _click_text(page, sel.get("report_new_problem_text", []),
                               "Continue - report a new problem", rid, 1500):
                _say(rid, "WARNING: could not find the 'report a new problem' button")
                _describe(page, rid)
                return False

        elif step in ("photo", "details"):
            if upload and upload.exists() and not attached:
                try:
                    page.locator("input[type=file]").first.set_input_files(
                        str(upload), timeout=8000)
                    attached = True
                    _say(rid, f"attached photo {upload.name}")
                    page.wait_for_timeout(1500)
                    _wait_for_upload(page, sel, rid)
                except Exception as e:
                    _say(rid, f"WARNING: photo upload failed: "
                              f"{str(e).splitlines()[0]}")
            if step == "details":
                _fill(page, sel["title"], lab.get("title", []), r["title"], "summary",
                      rid, wait_seconds=float(sel.get("step_wait_seconds", 12)))
                _fill(page, sel["detail"], lab.get("detail", []), r["detail"],
                      "description", rid, wait_seconds=4)
            _click_text(page, sel["continue_text"],
                        f"Continue ({step})", rid, 1500, avoid=avoid)
            _report_errors(page, rid, step)

        elif step == "user":
            _fill(page, sel["name"], lab.get("name", []), rep.get("full_name"),
                  "full name", rid, wait_seconds=float(sel.get("step_wait_seconds", 12)))
            if signed:
                _say(rid, "signed in, so the email box is filled by your account")
            else:
                _fill(page, sel["email"], lab.get("email", []), rep.get("email"),
                      "your email", rid, wait_seconds=4)
            _fill(page, sel["phone"], lab.get("phone", []), rep.get("phone"),
                  "phone number", rid, wait_seconds=4)

            want_public = bool(rep.get("show_name_publicly"))
            box = _js(page, JS_SET_CHECKBOX,
                      {"wants": lab.get("may_show_name", ["show my name publicly"]),
                       "value": want_public})
            if box:
                _say(rid, f"'{box['label']}' is "
                          f"{'ticked' if box['checked'] else 'unticked'}")
            elif want_public:
                _say(rid, "WARNING: could not find the 'show my name publicly' tickbox")
            return _highlight_submit(page, rid)

        else:
            _say(rid, f"unrecognised step ('{step}') - describing what is on screen")
            _describe(page, rid)
            if not _click_text(page, sel["continue_text"], "Continue", rid, 1500,
                               avoid=avoid):
                _say(rid, "nothing to click - finish this one in the tab")
                return False

        moved = _wait_step_change(page, step, float(sel.get("step_wait_seconds", 12)))
        if moved == step:
            stalled += 1
            errs = _report_errors(page, rid, step)
            if errs:
                _say(rid, f"STOPPED on the '{step}' step - the form wants something "
                          f"else. Finish it in this tab.")
                return False
            if stalled >= 2:
                _say(rid, f"STOPPED - the form stayed on '{step}'. "
                          f"Finish it in this tab; nothing has been submitted.")
                _describe(page, rid)
                return False
        else:
            stalled = 0
            _say(rid, f"now on the '{moved}' step")

    _say(rid, "ran out of steps - finish this one in the tab")
    return False


def _granicus_step(page) -> tuple:
    """Where the Granicus wizard is: (step name, fingerprint of the screen)."""
    return (_js(page, JS_WHICH_PAGE) or "unknown", _js(page, JS_PAGE_SIG) or "")


def _wait_granicus_change(page, was: tuple, seconds: float = 12) -> tuple:
    """Wait for the form to actually move on. Returns where it settled."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        page.wait_for_timeout(700)
        now = _granicus_step(page)
        if now != was:
            return now
    return was


def _answer_question(page, rid: int, question: list, answer: str, what: str,
                     required: bool = True) -> bool:
    """Answer one question on a Granicus form, matched on the question's wording.

    Dropdowns are set directly. The styled button choices only respond to a real
    mouse click, so mark the right one in the page and let Playwright click it.
    """
    if not answer:
        return False
    info = _js(page, JS_QUESTION,
               {"question": question, "answer": answer, "probe": False}) or {}
    if not info.get("found"):
        if required:
            _say(rid, f"WARNING: could not find the '{what}' question on this page")
        return False
    if info.get("status") == "nomatch":
        offered = ", ".join(info.get("options", [])[:8])
        _say(rid, f"WARNING: no '{answer}' option for {what} - choose one yourself"
                  + (f" (the form offers: {offered})" if offered else ""))
        return False
    if info.get("kind") == "select":
        _say(rid, f"{what}: {info.get('text')}")
        page.wait_for_timeout(800)
        return True
    if info.get("on"):
        _say(rid, f"{what} is already set to '{info.get('text')}'")
        return True

    for attempt in (1, 2):
        if attempt == 1:
            try:
                page.locator("[data-nncr='1']").first.click(timeout=3000)
            except Exception:
                _js(page, JS_CLICK_MARKED)
        else:
            _js(page, JS_CLICK_MARKED)       # full mouse-event sequence, in the page
        page.wait_for_timeout(800)
        st = _js(page, JS_QUESTION,
                 {"question": question, "answer": answer, "probe": True}) or {}
        if st.get("on"):
            _say(rid, f"{what}: {info.get('text')}")
            return True
    _say(rid, f"WARNING: could not set {what} to '{answer}' - do it in this tab")
    return False


def _choose_option(page, rid: int, question: list, kind: str, name: str,
                   what: str) -> bool:
    """Answer a question whose options are worded the council's way, not ours.

    The form says "Requires emptying" and "Dog bin"; we hold "needs emptying" and
    "dog waste". So read what the form actually offers and match on the words the
    two have in common, rather than hoping the phrases line up.
    """
    from . import classify
    if not name:
        return False
    options = _js(page, JS_QUESTION_OPTIONS, {"question": question}) or []
    if options:
        pick = classify.best_option(options, classify.bin_words(kind, name))
        if not pick:
            _say(rid, f"WARNING: nothing on the form matches '{name}' for {what} - "
                      f"choose one yourself: {', '.join(options[:8])}")
            return False
        return _answer_question(page, rid, question, pick, what)
    # No list read back: try our own wording, which works when it happens to match.
    return _answer_question(page, rid, question, name, what)


def _bins_issue_step(page, r: dict, sel: dict, rid: int) -> bool:
    """The bins form's own step: in a park?, what kind of bin, what is wrong.

    Returns False if a required answer could not be given - better to stop with
    the tab open than to press Next into "This field is required".
    """
    from . import classify
    lab = sel.get("labels", {})
    ok = True

    # 1. In a park? Answered from the park check done when the photo was read.
    park = (r.get("in_park") or "").strip().lower()
    if park not in ("yes", "no"):
        park = "no"
        _say(rid, "no park check on this report - answering 'No', which is right for "
                  "a bin on the street")
    _answer_question(page, rid, lab.get("park_question", ["in a park"]), park,
                     "in a park", required=False)

    # 2. What kind of bin. Colour is a hint only: litter bins here are black or
    #    blue, dog waste bins red, but lettering beats colour.
    bin_type = classify.normalise_bin_type(r.get("bin_type"))
    issues = classify.normalise_bin_issues(r.get("bin_issues"))
    if not bin_type or not issues:
        guessed = classify.bin_details_from_text(
            f"{r.get('title') or ''} {r.get('detail') or ''} {r.get('category') or ''}")
        bin_type = bin_type or guessed["bin_type"]
        issues = issues or guessed["bin_issues"]

    if bin_type:
        ok &= _choose_option(page, rid, lab.get("bin_type", ["type of bin"]),
                             "type", bin_type, "type of bin")
    else:
        _say(rid, "WARNING: could not tell whether this is a litter or dog waste bin "
                  "- pick it yourself in this tab")
        ok = False

    # 3. What is wrong with it. The form takes one problem at a time.
    if issues:
        ok &= _choose_option(page, rid, lab.get("bin_issue", ["what is the issue"]),
                             "issue", issues[0], "what is wrong with the bin")
        if len(issues) > 1:
            _say(rid, f"NOTE: this bin is also {', '.join(issues[1:])}. The form takes "
                      f"one problem at a time - press 'Split' on this report to raise "
                      f"the other{'s' if len(issues) > 2 else ''} separately.")
    else:
        _say(rid, "WARNING: could not tell what is wrong with the bin - pick it "
                  "yourself in this tab")
        ok = False

    # Free-text boxes, where the form has them.
    cap = int(sel.get("max_chars", 400))
    _fill(page, [], lab.get("description", []), (r.get("detail") or "")[:cap],
          "description", rid, warn=False)
    _fill(page, [], lab.get("other_details", []), (r.get("detail") or "")[:cap],
          "other details", rid, warn=False)
    return ok


def _prefill_granicus(page, r: dict, cfg: dict, sel: dict, rid: int) -> bool:
    """The council's fly-tipping form is undocumented and fully JavaScript-driven,
    so fill what can be matched by label and hand the tab over."""
    rep = cfg["reporter"]
    where = ", ".join([b for b in (r.get("street"), r.get("locality"),
                                   r.get("postcode")) if b]) or \
        f"{r['lat']:.5f}, {r['lon']:.5f}"
    if r.get("w3w"):
        where += f" (What3Words ///{r['w3w']})"
    values = {"location": where, "description": r.get("detail") or "",
              "email": rep.get("email") or "", "name": rep.get("full_name") or "",
              "phone": rep.get("phone") or ""}

    page.goto(sel["url"], wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    _cookies(page, sel, rid)
    _say(rid, f"opened the {sel.get('name', 'council form')}")

    # The form is rendered by JavaScript and may sit inside an iframe, so find the
    # document that actually contains it and drive that.
    doc = _find_form(page, rid, tuple(sel.get("ready_text", ["do you agree"])),
                     int(sel.get("ready_seconds", 25)))
    page = doc

    upload = photos.to_jpeg_for_upload(Path(r["photo_path"]), config.data_dir()) \
        if r.get("photo_path") else None
    lab = sel.get("labels", {})
    first, last = config.names(rep)
    attached = False
    seen = []

    # A wizard: Introduction, Your details, Nearest street, Photo, then the form's
    # own questions. Which step we are on is read from the page every time round
    # rather than counted off, so a slow-loading step cannot put us out of sync -
    # and a step that will not move on is noticed instead of being redone until
    # the page budget runs out.
    wait = float(sel.get("step_wait_seconds", 12))
    where_now = _granicus_step(page)
    stalled = 0

    for _ in range(int(sel.get("max_pages", 12))):
        which = where_now[0]
        seen.append(which)
        _say(rid, f"on the '{which}' step")

        if which == "intro":
            # Whether the agreement registered is told by the wizard leaving this
            # step - not by what is on screen afterwards. The next step carries
            # its own headings and buttons, and reading one of those as a
            # complaint used to send us back to redo an agreement already given.
            moved_on = False
            for attempt in (1, 2):
                _agree(page, sel, rid)
                if not _click_text(page, sel["continue_text"], "Next (after intro)",
                                   rid, 2500):
                    _say(rid, "no Next button on the agreement step")
                    return False
                moved = _wait_granicus_change(page, where_now, wait)
                if moved[0] != "intro":
                    _say(rid, f"now on the '{moved[0]}' step")
                    where_now, stalled, moved_on = moved, 0, True
                    break
                errs = _errors(page)
                if attempt == 1:
                    _say(rid, "still on the agreement step"
                              + (f" - the form says: {errs[0]}" if errs else "")
                              + " - setting the agreement again")
                else:
                    if errs:
                        _say(rid, f"the form still says: {errs[0]}")
                    _say(rid, "click 'I agree' yourself in this tab, then Next - "
                              "nothing has been submitted")
                    return False
            if moved_on:
                continue

        elif which == "details":
            _fill(page, [], lab.get("first_name", []), first, "first name", rid)
            _fill(page, [], lab.get("last_name", []), last, "last name", rid)
            _fill(page, [], lab.get("phone", []), rep.get("phone"),
                  "telephone number", rid)
            _fill(page, [], lab.get("email", []), rep.get("email"), "email", rid)
            if not _address_lookup(page, sel, rid,
                                   rep.get("address_postcode"),
                                   [rep.get("address_line") or "",
                                    rep.get("address_postcode") or ""],
                                   lab.get("your_address_search", []),
                                   lab.get("address_select", []), "your address"):
                return False

        elif which == "street":
            # Street search -> Search -> pick from "Select address", trying the
            # postcode if the street name finds nothing (and the street name may
            # be missing altogether, for a bin on a footpath or in a park).
            if not _street_lookup(page, sel, rid, r, lab):
                return False
            # These only exist on some versions of the page.
            _fill(page, [], lab.get("location_notes", []), values["location"],
                  "where it is", rid, warn=False)
            _fill(page, [], lab.get("description", []), values["description"],
                  "description", rid, warn=False)

        elif which == "photo":
            # "Do you want to add a photo?" is another styled Yes/No toggle, and
            # the upload control only appears once Yes is set.
            has_photo = bool(upload and upload.exists())
            _set_toggle(page, sel, rid,
                        sel.get("yes_text", ["yes"]) if has_photo
                        else sel.get("no_text", ["no"]),
                        "'Yes'" if has_photo else "'No'")
            page.wait_for_timeout(1500)
            if has_photo and not attached:
                try:
                    page.locator("input[type=file]").first.set_input_files(
                        str(upload), timeout=8000)
                    attached = True
                    _say(rid, f"attached photo {upload.name} "
                              f"({photos.describe_size(upload)})")
                    page.wait_for_timeout(1500)
                    _wait_for_upload(page, sel, rid)
                except Exception as e:
                    _say(rid, f"WARNING: photo upload failed: "
                              f"{str(e).splitlines()[0]}")

        elif which == "issue":
            if sel.get("form") == "bins":
                # The bins form's own step: in a park?, kind of bin, what is wrong.
                answered = _bins_issue_step(page, r, sel, rid)
                if r.get("w3w"):
                    _fill(page, [], lab.get("w3w", []), f"///{r['w3w']}",
                          "What3Words location", rid, warn=False)
                if not answered:
                    _say(rid, "STOPPED on the 'issue' step - I could not answer all "
                              "of it. Finish this tab by hand; nothing has been "
                              "submitted.")
                    return False
            else:
                # Another form's own questions: pick the type where there is one,
                # fill what can be matched by label, and let the human check it.
                if lab.get("type_select"):
                    opts = _js(page, JS_LIST_OPTIONS,
                               {"labels": lab.get("type_select", [])}) or []
                    if opts:
                        from . import classify
                        choice, how = classify.pick_from_list(
                            opts, r, cfg, form=sel.get("form") or "")
                        if choice:
                            res = _js(page, JS_PICK_OPTION,
                                      {"labels": lab.get("type_select", []),
                                       "tokens": [choice], "allowFirst": False}) or {}
                            if res.get("status") == "picked":
                                _say(rid, f"type of issue: {res['text']} ({how})")
                            page.wait_for_timeout(1200)
                if r.get("w3w"):
                    _fill(page, [], lab.get("w3w", []), f"///{r['w3w']}",
                          "What3Words location", rid, warn=False)
                _fill_hints(page, sel.get("field_hints", {}), values, rid)

        elif which == "final":
            cap = int(sel.get("max_chars", 400))

            # What3Words box takes the three words and nothing else.
            if r.get("w3w"):
                _fill(page, [], lab.get("w3w", []), f"///{r['w3w']}",
                      "What3Words location", rid)
            else:
                _say(rid, "no What3Words address for this report - leaving that box "
                          "empty (the location is in the notes below)")

            # Street and the rest of the location go in the "further information" box.
            where = ", ".join([b for b in (r.get("street"), r.get("locality"),
                                           r.get("postcode")) if b])
            notes = f"{where}. Coordinates {r['lat']:.5f}, {r['lon']:.5f}." \
                if where else f"Coordinates {r['lat']:.5f}, {r['lon']:.5f}."
            if r.get("taken_at"):
                notes += f" Photographed {r['taken_at'][:10]}."
            _fill(page, [], lab.get("further_location", []), notes[:cap],
                  "further location information", rid)

            # The bins form asks its own questions on this page instead.
            if sel.get("form") == "bins":
                if not _bins_issue_step(page, r, sel, rid):
                    _say(rid, "some of the bin questions are unanswered - finish "
                              "them in this tab before pressing Submit")
                if not attached and upload:
                    _say(rid, "WARNING: never found a photo field - attach it yourself")
                _highlight_submit(page, rid)
                return True

            # The form's "type of ..." dropdown: read the real options, then match.
            # Which words identify each option depends on the form - kinds of waste
            # on the fly-tipping form, kinds of mess on the street care form.
            form = sel.get("form") or "flytipping"
            type_name = sel.get("type_name") or (
                "type of fly-tipping" if form == "flytipping" else "type of issue")
            if lab.get("type_select"):
                opts = _js(page, JS_LIST_OPTIONS,
                           {"labels": lab.get("type_select", [])}) or []
                if opts:
                    from . import classify
                    choice, how = classify.pick_from_list(opts, r, cfg, form=form)
                    if choice:
                        res = _js(page, JS_PICK_OPTION,
                                  {"labels": lab.get("type_select", []),
                                   "tokens": [choice], "allowFirst": False}) or {}
                        if res.get("status") == "picked":
                            _say(rid, f"{type_name}: {res['text']} ({how})")
                        else:
                            _say(rid, f"WARNING: could not select '{choice}' - "
                                      f"pick the type yourself")
                        page.wait_for_timeout(1200)
                    else:
                        _say(rid, f"WARNING: none of the {len(opts)} types match - "
                                  f"choose one yourself: {', '.join(opts[:6])}")
                else:
                    _say(rid, f"WARNING: could not find the '{type_name}' list")

            # Details only a human can give truthfully are left to the human.
            for note in sel.get("human_fields_note", []):
                _say(rid, f"NOTE: {note}")

            # Selecting "other" often reveals a box asking for details.
            _fill(page, [], lab.get("other_details", []),
                  (r.get("detail") or values["description"])[:cap],
                  "other details", rid, warn=False)
            _fill(page, [], lab.get("comments", []),
                  (r.get("detail") or values["description"])[:cap], "comments", rid,
                  warn=False)

            if not attached and upload:
                _say(rid, "WARNING: never found a photo field - attach it yourself")
            _highlight_submit(page, rid)
            return True

        else:
            # Not a step we recognise: say what is on screen so it can be fixed,
            # then fill whatever can be matched by label and press on.
            _describe(page, rid)
            if (_js(page, JS_TOGGLE_STATE, sel.get("agree_text", ["i agree"]))
                    or {}).get("found"):
                _agree(page, sel, rid)
            _fill_hints(page, sel["field_hints"], values, rid)

        errs = _report_errors(page, rid, which)
        if errs:
            _say(rid, f"the form is not happy with the '{which}' step - "
                      f"fix it in this tab and carry on")
            return False

        # Only the intro step presses its own Next, above; anything that reaches
        # here still has to be moved on. Only the last page has Submit and no
        # Next - anything with a Next button is a step in the middle, whatever
        # its buttons are called.
        ends = _js(page, JS_END_OF_FORM) or {}
        if ends.get("submit") and not ends.get("next"):
            _highlight_submit(page, rid)
            return True
        if not _click_text(page, sel["continue_text"], f"Next (after {which})",
                           rid, 2500):
            # Not there yet is not the same as not there: the form takes its
            # buttons away while it is busy. Give it a few seconds.
            page.wait_for_timeout(4000)
            if _click_text(page, sel["continue_text"],
                           f"Next (after {which}, second try)", rid, 2500):
                moved = _wait_granicus_change(page, where_now, wait)
                where_now, stalled = moved, 0
                continue
            _say(rid, f"no Next button on the '{which}' step - "
                      f"carry on in this tab")
            if which == "unknown":
                _say(rid, "if the form looks filled in, this is a naming problem "
                          "- send me the lines above and I can fix selectors.json")
            return False

        # Only move on when the form itself has moved. Redoing a step it refused
        # is what used to leave a tab going round in circles.
        moved = _wait_granicus_change(page, where_now, wait)
        if moved == where_now:
            stalled += 1
            if _report_errors(page, rid, which):
                _say(rid, f"STOPPED on the '{which}' step - the form wants something "
                          f"else. Finish it in this tab; nothing has been submitted.")
                return False
            if stalled >= 2:
                _say(rid, f"STOPPED - the form stayed on the '{which}' step. Finish "
                          f"it in this tab; nothing has been submitted.")
                _describe(page, rid)
                return False
            _say(rid, f"the '{which}' step has not moved on - one more try")
        else:
            stalled = 0
            if moved[0] != which:
                _say(rid, f"now on the '{moved[0]}' step")
        where_now = moved

    _say(rid, f"worked through {' > '.join(seen)} - finish the rest in this tab")
    return False


def _search_terms(r: dict) -> list:
    """What to type into a council street search, best first.

    Never the full location blurb: "Corby, NN17 1BT (What3Words ///a.b.c)" is not
    an address, and searching for it returns nothing at all. When the photo's
    coordinates gave us no street name, the postcode is the next best thing.
    """
    street = (r.get("street") or "").strip()
    town = (r.get("locality") or "").strip()
    postcode = (r.get("postcode") or "").strip()
    terms = []
    for t in (street, postcode, f"{street} {town}".strip() if street and town else "",
              town):
        if t and t not in terms:
            terms.append(t)
    return terms


def _wait_for_upload(page, sel: dict, rid: int) -> bool:
    """Wait for a photo to finish uploading.

    The form takes its Next button away while the file goes up, so a big photo on
    a slow connection looked exactly like a step with no Next button - which is
    how two fly-tips were left sitting on the photo page.
    """
    seconds = float(sel.get("photo_upload_seconds", 90))
    deadline = time.time() + seconds
    started = time.time()
    said = False
    while time.time() < deadline:
        ends = _js(page, JS_END_OF_FORM) or {}
        if ends.get("next") or ends.get("submit"):
            took = time.time() - started
            if took > 4:
                _say(rid, f"photo uploaded ({took:.0f}s)")
            return True
        if not said:
            _say(rid, "waiting for the photo to finish uploading")
            said = True
        page.wait_for_timeout(1500)
    _say(rid, f"the photo was still going up after {int(seconds)}s - let it finish "
              f"in this tab, then press Next")
    return False


def _street_lookup(page, sel: dict, rid: int, r: dict, lab: dict) -> bool:
    """Find the nearest street, trying the postcode if the street name draws a blank."""
    terms = _search_terms(r)
    if not terms:
        _say(rid, "this report has no street or postcode to search with - type the "
                  "location into this tab yourself, then carry on")
        return False
    tokens = [t for t in ((r.get("street") or r.get("postcode") or ""),
                          r.get("locality") or "") if t]
    for i, term in enumerate(terms):
        if i:
            _say(rid, f"nothing found - searching for '{term}' instead")
        if _address_lookup(page, sel, rid, term, tokens,
                           lab.get("street_search", []),
                           lab.get("street_select", []), "nearest street",
                           required=(i == len(terms) - 1),
                           first_if_unmatched=True, force=True):
            return True
    return False


def _address_lookup(page, sel: dict, rid: int, search: str, tokens,
                    search_labels: list, select_labels: list, what: str,
                    required: bool = True, first_if_unmatched: bool = False,
                    force: bool = False) -> bool:
    """Type into the search box, press Search, then choose from the results.

    True only when a result was chosen. `required` decides how loudly a failure is
    reported, not what is returned - the caller may have another term to try.

    The search box may already hold the right text (the form remembers it, or we
    filled it on a previous attempt), which is not a failure - press Search anyway.
    """
    if not search:
        if required:
            _say(rid, f"no {what} configured - type it in this tab, then carry on. "
                      f"(Add address_postcode and address_line to config.json to "
                      f"have this filled in future.)")
        return False

    if isinstance(tokens, str):
        tokens = [tokens]
    tokens = [t for t in (tokens or []) if t]

    _fill(page, [], search_labels, search, f"{what} search", rid, warn=False,
          force=force)
    if not _click_text(page, sel.get("search_text", ["search"]), "Search", rid, 2500):
        _say(rid, f"WARNING: no Search button for {what}")
        return False

    # The results take a few seconds to arrive, so wait for them rather than
    # reading an empty list and giving up.
    wait_for = int(sel.get("results_seconds", 20))
    deadline = time.time() + wait_for
    res = {"status": "empty"}
    while True:
        res = _js(page, JS_PICK_OPTION,
                  {"labels": select_labels, "tokens": tokens,
                   "allowFirst": bool(first_if_unmatched)}) or {"status": "none"}
        if res.get("status") != "empty" or time.time() > deadline:
            break
        page.wait_for_timeout(1500)

    if res["status"] == "picked":
        _say(rid, f"chose {what}: {res['text']}"
                  + (f" (from {res['count']} results)" if res.get("count") else ""))
        if not res.get("matched"):
            _say(rid, f"WARNING: nothing matched '{tokens[0] if tokens else search}' - "
                      f"that is just the first result, check it in this tab")
        page.wait_for_timeout(800)
        return True
    if res["status"] == "choose":
        _say(rid, f"none of the {res.get('count', '?')} results match "
                  f"'{tokens[0] if tokens else search}'"
                  + (" - pick one in this tab: " + ", ".join(res.get("options", [])[:5])
                     if required else ""))
        return False
    if res["status"] == "empty":
        _say(rid, f"the {what} search returned nothing for '{search}'"
                  + (f" after {wait_for}s - try it by hand in this tab"
                     if required else ""))
        return False
    if required:
        _say(rid, f"could not find the {what} results list - carry on in this tab")
    return False


def _find_form(page, rid: int, needles: tuple, seconds: int = 25):
    """Return the page - or the iframe within it - that actually holds the form.

    Council form platforms often embed the form in an iframe, and they render it
    with JavaScript after the page has loaded, so this waits for real content to
    appear and then works out where it lives.
    """
    deadline = time.time() + seconds
    tried = set()
    while time.time() < deadline:
        docs = [page]
        try:
            docs += [f for f in page.frames if f is not getattr(page, "main_frame", None)]
        except Exception:
            pass
        for doc in docs:
            try:
                body = (doc.inner_text("body", timeout=2000) or "").lower()
            except Exception:
                continue
            if any(n in body for n in needles):
                where = "the page"
                try:
                    if doc is not page:
                        where = f"a frame ({doc.url[:60]})"
                except Exception:
                    where = "a frame"
                _say(rid, f"found the form in {where}")
                return doc
            tried.add(getattr(doc, "url", "?"))
        time.sleep(1.5)

    _say(rid, f"the form did not appear within {seconds}s - "
              f"looked in {len(tried)} document(s)")
    return page


def _describe(page, rid: int) -> None:
    """Log what is actually on screen, for when the walker gets lost."""
    d = _js(page, JS_DESCRIBE) or {}
    if not d:
        _say(rid, "could not read the page at all")
        return
    _say(rid, f"page has {d.get('frames', 0)} iframe(s); "
              f"fields: {', '.join(d.get('fields') or []) or 'none'}")
    _say(rid, f"buttons: {', '.join(d.get('buttons') or []) or 'none'}")
    if d.get("text"):
        _say(rid, f"text starts: {d['text'][:150]}")


def _try_road_candidates(page, sel: dict, r: dict, rid: int) -> bool:
    """Move the pin onto nearby roads until the site accepts one.

    The closest line is not always a road the council maintains - private estate
    roads and unadopted service roads sit on the same map - so work outwards.
    """
    limit = int(sel.get("road_max_metres", 30))
    tries = int(sel.get("road_max_tries", 2))
    res = None
    for _ in range(4):                      # the road layer loads after the category
        res = _js(page, JS_ROAD_CANDIDATES,
                  {"lat": r["lat"], "lon": r["lon"],
                   "layerPattern": sel.get("road_layer_pattern", "road|highway"),
                   "maxMetres": limit, "limit": tries}) or {}
        if res.get("ok"):
            break
        page.wait_for_timeout(1500)
    if not res or not res.get("ok"):
        _say(rid, f"could not find a road to try: "
                  f"{(res or {}).get('why', 'unknown')} - move the pin yourself")
        return False

    cands = res.get("candidates") or []
    _say(rid, f"{len(cands)} nearby spot(s) to try, within {limit}m")
    for i, c in enumerate(cands, 1):
        label = f" ({c['label']})" if c.get("label") else ""
        try:
            page.mouse.click(c["x"], c["y"])
        except Exception as e:
            _say(rid, f"could not click the map: {str(e).splitlines()[0]}")
            return False
        page.wait_for_timeout(2500)
        if not _set_category(page, r.get("category") or "", r.get("cat_group") or "",
                             rid):
            _say(rid, f"try {i}: the category did not stick after moving the pin")
            continue
        if not _off_highway(page, sel):
            _say(rid, f"try {i}: accepted - pin is about {c['metres']}m from the "
                      f"photo, on {c.get('layer', 'a road')}{label}")
            return True
        _say(rid, f"try {i}: {c['metres']}m away{label} - still not a road the "
                  f"council looks after")

    _say(rid, f"none of the {len(cands)} nearby roads are ones the council accepts")
    return False


def _snap_pin_to_road(page, sel: dict, r: dict, rid: int) -> bool:
    """Move the report pin onto the nearest council-maintained road.

    A pothole photographed from the pavement lands the pin off the carriageway,
    and the site rejects it as outside the area it looks after. The road lines are
    on the map, so put the pin on the closest point of the nearest one.
    """
    limit = int(sel.get("road_max_metres", 30))
    pattern = sel.get("road_layer_pattern", "road|highway|carriageway|street")
    res = None
    for _ in range(4):
        res = _js(page, JS_NEAREST_ASSET,
                  {"lat": r["lat"], "lon": r["lon"], "layerPattern": pattern,
                   "snapToLine": True}) or {}
        if res.get("ok"):
            break
        page.wait_for_timeout(1500)
    if not res or not res.get("ok"):
        _say(rid, f"could not find a road on the map "
                  f"({(res or {}).get('why', 'unknown')}) - move the pin yourself")
        return False
    if res.get("metres", 0) > limit:
        _say(rid, f"nearest road is about {res['metres']}m away, further than "
                  f"{limit}m - not moving the pin, do it yourself")
        return False
    if not res.get("inView"):
        _say(rid, "nearest road is off the visible map - move the pin yourself")
        return False
    try:
        page.mouse.click(res["x"], res["y"])
    except Exception as e:
        _say(rid, f"could not click the map: {str(e).splitlines()[0]}")
        return False
    page.wait_for_timeout(2000)
    label = f" ({res['label']})" if res.get("label") else ""
    _say(rid, f"moved the pin about {res['metres']}m onto the nearest road"
              f"{label} - the photo's own location is kept in the description")
    return True


def _off_highway(page, sel: dict) -> bool:
    return bool(_js(page, JS_ASSET_PROMPT, sel.get("off_highway_text", [])))


def _select_nearest_asset(page, sel: dict, r: dict, rid: int) -> bool:
    """Click the council asset (tree, light, bin) nearest the photo's GPS."""
    limit = int(sel.get("asset_max_metres", 60))
    res = None
    for attempt in range(1, 5):                 # the layer loads after the category
        res = _js(page, JS_NEAREST_ASSET,
                  {"lat": r["lat"], "lon": r["lon"],
                   "layerPattern": sel.get("asset_layer_pattern", ""),
                   "snapToLine": False}) or {}
        if res.get("ok"):
            break
        page.wait_for_timeout(1500)
    if not res or not res.get("ok"):
        _say(rid, f"could not pick from the map: {(res or {}).get('why', 'unknown')} - "
                  f"select it yourself in this tab")
        return False

    what = res.get("layer") or "asset"
    label = f" ({res['label']})" if res.get("label") else ""
    if res.get("metres", 0) > limit:
        _say(rid, f"nearest {what}{label} is about {res['metres']}m from the photo, "
                  f"further than {limit}m - not guessing, pick it yourself")
        return False
    if not res.get("inView"):
        _say(rid, f"nearest {what} is off the visible map - pick it yourself")
        return False

    try:
        page.mouse.click(res["x"], res["y"])
    except Exception as e:
        _say(rid, f"could not click the map: {str(e).splitlines()[0]}")
        return False
    page.wait_for_timeout(1800)

    still_asking = _js(page, JS_ASSET_PROMPT, sel.get("asset_prompt_text", []))
    chose = _js(page, JS_ASSET_PROMPT, sel.get("asset_chosen_text", []))
    if chose or not still_asking:
        _say(rid, f"selected the nearest {what}{label}, about {res['metres']}m from "
                  f"where the photo was taken (of {res.get('count', '?')} on the map)")
        return True
    _say(rid, f"clicked the nearest {what} but the form still wants one - "
              f"select it yourself in this tab")
    return False


def _dead_end(page, sel: dict) -> str:
    """Return the notice text if this category is not accepted on this form."""
    phrases = [p.lower() for p in sel.get("dead_end_text", [])]
    if not phrases:
        return ""
    try:
        body = (page.inner_text("body", timeout=4000) or "")
    except Exception:
        return ""
    low = body.lower()
    for p in phrases:
        i = low.find(p)
        if i == -1:
            continue
        # The emergency-phone notice sits on every page; only treat it as a dead
        # end when it appears with a pointer to another form.
        if "do not log it on this system" in p and "online form" not in low:
            continue
        snippet = " ".join(body[max(0, i - 90):i + 130].split())
        return snippet
    return ""


def _agree(page, sel: dict, rid: int) -> bool:
    """Tick the data-protection agreement and make sure it actually took."""
    return _set_toggle(page, sel, rid, sel.get("agree_text", ["i agree"]), "'I agree'")


def _set_toggle(page, sel: dict, rid: int, wants: list, what: str) -> bool:
    """Set one of this form's styled button-toggles ('I agree', 'Yes', 'No').

    They turn green when set. A DOM-level click is not enough for them, so try a
    real mouse click first and verify the state rather than assuming.
    """
    def state():
        return _js(page, JS_TOGGLE_STATE, wants) or {}

    st = state()
    if not st.get("found"):
        _say(rid, f"WARNING: could not find the {what} control")
        _describe(page, rid)
        return False
    if st.get("agreed"):
        _say(rid, f"{what} is already set")
        return True

    # 1. a real mouse click, which is what the form is listening for
    for text in wants:
        for how in (lambda t=text: page.get_by_role("button", name=t, exact=False),
                    lambda t=text: page.get_by_text(t, exact=False),
                    lambda t=text: page.locator(f"button:has-text('{t}')")):
            try:
                how().first.click(timeout=2500)
                break
            except Exception:
                continue
        page.wait_for_timeout(700)
        if state().get("agreed"):
            _say(rid, f"clicked {what} - it is now set")
            return True

    # 2. full mouse event sequence through the DOM
    hit = _js(page, JS_FORCE_CLICK, wants)
    page.wait_for_timeout(700)
    st = state()
    if st.get("agreed"):
        _say(rid, f"set {what} ({hit})")
        return True

    _say(rid, f"WARNING: {what} would not set (background is {st.get('bg')}) - "
              f"click it yourself in this tab, then press Next")
    return False


def _fill_hints(page, hints: dict, values: dict, rid: int) -> None:
    """Fill whatever can be identified from label text, one value per field.

    Used for the fly-tipping form, whose field names are not published.
    """
    for key, words in hints.items():
        if not values.get(key):
            continue
        got = _js(page, JS_FILL, {"sels": [], "labels": words,
                                  "value": str(values[key])})
        if got:
            _say(rid, f"filled a field that looks like '{key}' ('{got}')")


def _highlight_submit(page, rid: int) -> bool:
    # The label has to BEGIN with "submit" - "Next ❯" on a wizard step is not the
    # end of the form, and outlining it in red only looks like it is.
    found = _js(page, """
      () => {
        const lab = e => ((e.innerText || e.value || '')
          .replace(/\\s+/g, ' ').trim().toLowerCase());
        const b = [...document.querySelectorAll('button, input[type=submit]')]
          .find(e => /^submit\\b/.test(lab(e)));
        if (!b) return false;
        b.scrollIntoView({block: 'center'});
        b.style.outline = '3px solid #b3261e';
        return true;
      }""")
    _say(rid, "READY - check this tab, then press Submit"
         if found else "prefilled as far as possible - finish this tab by hand")
    return bool(found)


def _prefill(page, r: dict, cfg: dict, signed: bool) -> bool:
    sels = selectors()
    rid = r["id"]
    key = _sel_key(r.get("service"))
    if key == "fixmystreet":
        return _prefill_fms(page, r, cfg, sels["fixmystreet"], signed, rid)
    return _prefill_granicus(page, r, cfg, sels[key], rid)


def run_batch(rids: list, cfg: dict) -> dict:
    """Prefill every report in its own tab, then wait for you to submit them."""
    from playwright.sync_api import sync_playwright

    reports, skipped = [], []
    for rid in rids:
        r = store.get(rid)
        if not r:
            skipped.append((rid, "not found"))
        elif r["status"] == "submitted":
            skipped.append((rid, "already submitted"))
        elif r.get("lat") is None:
            skipped.append((rid, "no location"))
        elif not r.get("category"):
            skipped.append((rid, "no category chosen"))
        else:
            # Last line of defence: a report queued before the routing rule
            # existed, or hand-set to a form that will refuse it.
            want = routing.must_move(r, cfg)
            if want:
                store.update(rid, {"service": want})
                r["service"] = want
                r["_rerouted"] = True
            reports.append(r)

    _log[BATCH] = []
    _active[BATCH] = "opening browser"
    for rid, why in skipped:
        _say(BATCH, f"skipping #{rid}: {why}")
    if not reports:
        _active[BATCH] = "nothing to do"
        return {"ok": False, "error": "nothing to prefill", "skipped": skipped}

    with sync_playwright() as p:
        ctx = _launch(p, BATCH)
        try:
            base = ctx.pages[0] if ctx.pages else ctx.new_page()
            sel = selectors()["fixmystreet"]

            # Sign in once for the whole run, before any tab is filled.
            signed = False
            if any(_sel_key(r.get("service")) == "fixmystreet" for r in reports):
                _active[BATCH] = "signing in"
                signed = sign_in(base, cfg, sel, BATCH)

            _active[BATCH] = "filling forms"
            open_pages = {}
            for i, r in enumerate(reports):
                rid = r["id"]
                _log[rid] = []
                _say(BATCH, f"tab {i + 1} of {len(reports)}: report #{rid} "
                            f"({r['category']})")
                if r.pop("_rerouted", False):
                    forms = {"granicus": "fly-tipping form",
                             "granicus_bins": "litter/dog bin form",
                             "granicus_streetcare": "street care & cleaning form",
                             "granicus_dog": "dog issue form",
                             "granicus_grounds": "grass/trees/hedges form",
                             "granicus_derelict": "derelict property form",
                             "granicus_vehicle": "abandoned vehicle form",
                             "granicus_env": "environmental issue form",
                             "fixmystreet": "highways site"}
                    _say(rid, f"this belongs on the "
                              f"{forms.get(r['service'], r['service'])} - using that")
                page = base if i == 0 else ctx.new_page()
                try:
                    _prefill(page, r, cfg, signed)
                except Exception as e:
                    _say(rid, f"ERROR while filling: {e}")
                store.update(rid, {"status": "awaiting"})
                open_pages[rid] = page

            _active[BATCH] = "waiting for you to submit"
            _say(BATCH, f"{len(open_pages)} tab(s) ready. Check each one and press "
                        f"Submit - they can be done in any order.")
            done = _watch_many(open_pages, cfg)
            _active[BATCH] = "finished"
            _say(BATCH, f"{len(done)} of {len(open_pages)} submitted")
            return {"ok": True, "submitted": list(done), "skipped": skipped}
        except Exception as e:
            _say(BATCH, f"ERROR: {e}")
            _active[BATCH] = "error"
            for rid in [r["id"] for r in reports]:
                if (store.get(rid) or {}).get("status") == "awaiting":
                    store.update(rid, {"status": "ready"})
            return {"ok": False, "error": str(e)}
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def page_wait(page, ms: int) -> None:
    try:
        page.wait_for_timeout(ms)
    except Exception:
        time.sleep(ms / 1000)


def _page_text(page) -> str:
    """All the visible text in a tab, including inside its iframes.

    The fly-tipping form runs inside an iframe, so its confirmation page - and the
    reference number on it - is not in the tab's own body text.
    """
    parts = []
    docs = [page]
    try:
        docs += [f for f in page.frames if f is not getattr(page, "main_frame", None)]
    except Exception:
        pass
    for doc in docs:
        try:
            parts.append(doc.inner_text("body", timeout=4000) or "")
        except Exception:
            continue
    return " ".join(parts).lower()


def _watch_many(pages: dict, cfg: dict) -> dict:
    """Poll every open tab until it shows a confirmation, or we run out of time."""
    seconds = max(120, int(cfg["submission"]["wait_for_submit_seconds"]))
    deadline = time.time() + seconds
    sels = selectors()
    pending, done = dict(pages), {}

    while pending and time.time() < deadline:
        for rid, page in list(pending.items()):
            try:
                if page.is_closed():
                    _say(rid, "tab closed without a confirmation")
                    store.update(rid, {"status": "ready"})
                    pending.pop(rid)
                    continue
                r = store.get(rid) or {}
                needles = sels[_sel_key(r.get("service"))]["success_text"]
                body = _page_text(page)
                if any(n.lower() in body for n in needles):
                    ref = _extract_ref(body, page.url)
                    # The reference sometimes renders a beat after the thank-you.
                    for _ in range(4):
                        if ref:
                            break
                        time.sleep(2)
                        body = _page_text(page)
                        if not body:
                            break
                        ref = _extract_ref(body, page.url)
                    if not ref:
                        snippet = " ".join(body.split())[:160]
                        _say(rid, f"submitted, but no reference found on the page. "
                                  f"It said: {snippet}")
                        _say(rid, "you can paste the reference into the report yourself")
                    # The published report shows the photo at a public URL, which is
                    # the only way an image can appear on a Google My Map.
                    photo_url = _js(page, JS_PUBLISHED_PHOTO) or ""
                    store.update(rid, {
                        "status": "submitted", "reference": ref or "",
                        "report_url": page.url, "photo_url": photo_url,
                        "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                    _say(rid, f"confirmed submitted{' - ref ' + ref if ref else ''}"
                              + (" (photo published)" if photo_url else ""))
                    done[rid] = ref
                    pending.pop(rid)
            except Exception:
                pass
        if pending:
            time.sleep(3)

    for rid in pending:
        store.update(rid, {"status": "ready"})
        _say(rid, "no confirmation seen - left as 'ready to submit'")
    if pending:
        _say(BATCH, f"stopped waiting after {seconds // 60} minutes")
    return done


# A reference looks like FLY800000001, ENQ123456, FS-12345 or a bare 12345678.
_REF_TOKEN = r"([A-Za-z]{2,6}[-/]?\d{4,}[A-Za-z0-9\-/]*|\d{6,12})"


def _extract_ref(body: str, url: str) -> str:
    """Pull the reference number out of a confirmation page.

    The highways site puts it in the URL; the fly-tipping form writes
    "Your reference number is: FLY800000001." - note the words between "reference"
    and the value, and that the value is not all digits.
    """
    m = re.search(r"/report/(\d+)", url)
    if m:
        return m.group(1)

    # Anything shortly after the word "reference" is the strongest signal.
    for m in re.finditer(r"\bref(?:erence)?\b\W{0,4}(?:number|no\.?)?\W{0,4}"
                         r"(?:is|was)?\W{0,4}", body, re.I):
        tail = body[m.end():m.end() + 40]
        t = re.search(_REF_TOKEN, tail)
        if t:
            return t.group(1).upper().strip(".,;:/-")

    # Otherwise a distinctive letters+digits code anywhere on the page.
    t = re.search(r"\b([A-Za-z]{2,6}[-/]?\d{6,})\b", body)
    return t.group(1).upper() if t else ""


def submit(rids, cfg: dict) -> dict:
    """Entry point for one report or many. Serialised - one browser at a time."""
    if isinstance(rids, int):
        rids = [rids]
    rids = [int(x) for x in rids]
    if not rids:
        return {"ok": False, "error": "nothing selected"}
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "a browser session is already open"}
    try:
        return run_batch(rids, cfg)
    except Exception as e:
        _say(BATCH, f"ERROR: {e}")
        _active[BATCH] = "error"
        return {"ok": False, "error": str(e)}
    finally:
        _lock.release()


def busy() -> bool:
    return _lock.locked()


def sign_in_only(cfg: dict) -> bool:
    """Sign in once and leave the session in the browser profile, so every later
    report is already signed in. Run via sign-in.bat."""
    from playwright.sync_api import sync_playwright
    sel = selectors()["fixmystreet"]
    _log[0] = []
    with sync_playwright() as p:
        ctx = _launch(p, 0)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto(sel["auth_url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1800)
            _cookies(page, sel, 0)
            ok = sign_in(page, cfg, sel, 0)
            print()
            for line in _log.get(0, []):
                print("  " + line)
            if ok:
                # Close automatically: a browser left open here holds the profile
                # lock and blocks the next report.
                print("\nSigned in. The session is saved, so reports will attach to "
                      "your account.\nClosing the browser…")
                page.wait_for_timeout(2500)
            else:
                print("\nNot signed in. Sign in by hand in this window if you like - "
                      "that session is saved too.")
                input("\nPress Enter to close the browser "
                      "(leaving it open will block the next report)...")
            return ok
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def inspect_form(url: str | None = None) -> str:
    """Diagnostic: dump every form field and radio label on the live page, so
    selectors.json can be corrected without guesswork. Run via run-inspect.bat."""
    from playwright.sync_api import sync_playwright
    sel = selectors()["fixmystreet"]
    clat, clon = config.centre(config.load())
    url = url or sel["url"].format(lat=f"{clat:.6f}", lon=f"{clon:.6f}")
    out = [f"URL: {url}", ""]
    with sync_playwright() as p:
        ctx = _launch(p)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        if _js(page, "() => typeof updateCookies === 'function'"):
            _js(page, "() => updateCookies(true)")
        page.wait_for_timeout(1000)

        out.append("== radio / checkbox options currently on screen ==")
        for t in (_js(page, JS_VISIBLE_OPTIONS) or []):
            out.append(f"  {t}")
        out.append("")
        out.append("== all form controls (V = visible) ==")
        js = """() => Array.from(document.querySelectorAll('input,select,textarea,button'))
            .map(e => {
              let lab = '';
              if (e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]');
                          if (l) lab = (l.innerText||'').replace(/\\s+/g,' ').trim(); }
              return {tag: e.tagName, type: e.type||'', id: e.id||'', name: e.name||'',
                      cls: (e.className||'').toString().slice(0,50), label: lab.slice(0,60),
                      text: (e.innerText||e.value||'').toString().replace(/\\s+/g,' ').slice(0,50),
                      visible: !!(e.offsetWidth||e.offsetHeight)};
            })"""
        for f in (_js(page, js) or []):
            out.append(f"{'V' if f['visible'] else '-'} <{f['tag'].lower()} "
                       f"type={f['type']} id={f['id']} name={f['name']} "
                       f"class={f['cls']}> label={f['label']!r} text={f['text']!r}")
        dest = config.BASE_DIR / "data" / "form-inspect.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("\n".join(out), encoding="utf-8")
        print(f"\nWrote {dest}\nLeaving the browser open - close it when done.")
        input("Press Enter to close...")
        ctx.close()
    return str(dest)
