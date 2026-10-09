"""SQLite queue + history. Keeps a permanent record so nothing is double-reported."""
import json
import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sha           TEXT UNIQUE NOT NULL,
    original_name TEXT,
    photo_path    TEXT,
    thumb_path    TEXT,
    lat           REAL,
    lon           REAL,
    taken_at      TEXT,
    street        TEXT,
    locality      TEXT,
    postcode      TEXT,
    w3w           TEXT,
    service       TEXT DEFAULT 'fixmystreet',
    category      TEXT,
    cat_group     TEXT,
    title         TEXT,
    detail        TEXT,
    confidence    REAL,
    severity      TEXT,
    notes         TEXT,
    status        TEXT DEFAULT 'new',
    reference     TEXT,
    report_url    TEXT,
    photo_url     TEXT,
    bin_type      TEXT,
    bin_issues    TEXT,
    in_park       TEXT,
    submitted_at  TEXT,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    warnings      TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON reports(status);
CREATE TABLE IF NOT EXISTS updates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id     INTEGER NOT NULL,
    source        TEXT,               -- 'page' (highways site) or 'email'
    happened      TEXT,               -- when the council said it, as shown
    body          TEXT,
    state         TEXT,               -- state this update names, if any
    hash          TEXT UNIQUE,        -- so re-checking never duplicates
    recorded_at   TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_upd_report ON updates(report_id);
-- Pins merged in from the Google My Map (see nncr/mymaps.py). Kept apart from
-- reports: they were never submitted through the tool, so they must not join
-- the queue, the KML export or the duplicate guard's "already submitted" set.
CREATE TABLE IF NOT EXISTS map_pins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT UNIQUE NOT NULL,   -- source:reference, or source:hash
    source        TEXT,                   -- 'mymaps'
    folder        TEXT,                   -- layer it came from on the My Map
    name          TEXT,
    category      TEXT,
    kind          TEXT,                   -- flytipping / pothole / ... / issue
    status        TEXT,                   -- open / completed / mapped ...
    reference     TEXT,
    lat           REAL,
    lon           REAL,
    street        TEXT,
    postcode      TEXT,
    w3w           TEXT,
    photographed  TEXT,
    date_source   TEXT,                   -- exact / estimated / ''
    detail        TEXT,
    report_url    TEXT,
    photo_url     TEXT,                   -- Google-hosted original
    photo_urls    TEXT,                   -- JSON list, if a pin has several
    photo_path    TEXT,                   -- local cached copy
    thumb_path    TEXT,
    colour        TEXT,
    icon          TEXT,
    raw           TEXT,
    imported_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pins_ref ON map_pins(reference);
CREATE TABLE IF NOT EXISTS map_areas (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT UNIQUE NOT NULL,
    source        TEXT,
    name          TEXT,
    kind          TEXT,                   -- area / ward-sketch
    folder        TEXT,
    geojson       TEXT,                   -- GeoJSON geometry
    colour        TEXT,
    detail        TEXT,
    imported_at   TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at    TEXT
);
"""

FIELDS = ["sha", "original_name", "photo_path", "thumb_path", "lat", "lon", "taken_at",
          "street", "locality", "postcode", "w3w", "service", "category", "cat_group",
          "title", "detail", "confidence", "severity", "notes", "status", "reference",
          "report_url", "photo_url", "bin_type", "bin_issues", "in_park",
          "submitted_at", "warnings"]

EDITABLE = {"service", "category", "cat_group", "title", "detail", "status",
            "notes", "lat", "lon", "reference", "report_url", "photo_url",
            "mapped_at", "completed_at", "bin_type", "bin_issues", "in_park",
            "council_status", "council_checked_at"}

# Columns added after the first release, for databases created before them.
LATER_COLUMNS = {"photo_url": "TEXT", "mapped_at": "TEXT", "completed_at": "TEXT",
                 "bin_type": "TEXT", "bin_issues": "TEXT", "in_park": "TEXT",
                 "council_status": "TEXT", "council_checked_at": "TEXT"}

# The life of a report:
#   new / ready / awaiting -> submitted -> mapped -> completed
# "mapped" means it is in the KML file as written right now; "completed" means a
# later export replaced that file, so it has already been imported into My Maps.
DONE_STATUSES = ("submitted", "mapped", "completed")


def db_path() -> Path:
    return config.data_dir() / "reports.db"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path(), timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r[1] for r in con.execute("PRAGMA table_info(reports)")}
    for name, ddl in LATER_COLUMNS.items():          # upgrade older databases
        if name not in have:
            con.execute(f"ALTER TABLE reports ADD COLUMN {name} {ddl}")
    return con


def insert(row: dict) -> int | None:
    """Returns new id, or None if this exact photo is already in the database."""
    cols = [f for f in FIELDS if f in row]
    sql = (f"INSERT OR IGNORE INTO reports ({','.join(cols)}) "
           f"VALUES ({','.join('?' * len(cols))})")
    vals = [_enc(row[c]) for c in cols]
    with connect() as con:
        cur = con.execute(sql, vals)
        return cur.lastrowid if cur.rowcount else None


def _enc(v):
    return json.dumps(v) if isinstance(v, (list, dict)) else v


def get(rid: int) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    return dict(r) if r else None


def by_sha(sha: str) -> dict | None:
    with connect() as con:
        r = con.execute("SELECT * FROM reports WHERE sha=?", (sha,)).fetchone()
    return dict(r) if r else None


def all_reports(status: str | None = None) -> list:
    q = "SELECT * FROM reports"
    args = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += (" ORDER BY (status IN ('submitted','mapped','completed')), "
          "COALESCE(taken_at, created_at) DESC")
    with connect() as con:
        return [dict(r) for r in con.execute(q, args).fetchall()]


def update(rid: int, changes: dict) -> dict | None:
    changes = {k: v for k, v in changes.items() if k in EDITABLE}
    if changes:
        sets = ",".join(f"{k}=?" for k in changes)
        with connect() as con:
            con.execute(f"UPDATE reports SET {sets} WHERE id=?",
                        [*changes.values(), rid])
    return get(rid)


def delete(rid: int) -> dict | None:
    """Removes the row and returns it, so the caller can tidy up its photo files."""
    row = get(rid)
    with connect() as con:
        con.execute("DELETE FROM reports WHERE id=?", (rid,))
        con.execute("DELETE FROM updates WHERE report_id=?", (rid,))
    return row


def delete_all(include_submitted: bool = False) -> list:
    """Clear the queue. Submitted reports are kept unless explicitly included -
    that history is what stops the same issue being reported twice.

    Returns the deleted rows so their photo files can be tidied up.
    """
    q = "SELECT * FROM reports"
    if not include_submitted:
        q += " WHERE status NOT IN ('submitted','mapped','completed')"
    with connect() as con:
        rows = [dict(r) for r in con.execute(q).fetchall()]
        ids = [r["id"] for r in rows]
        if ids:
            marks = ",".join("?" * len(ids))
            con.execute(f"DELETE FROM reports WHERE id IN ({marks})", ids)
            con.execute(f"DELETE FROM updates WHERE report_id IN ({marks})", ids)
    return rows


def nearby_submitted(lat: float, lon: float, category: str, metres: float = 25.0) -> list:
    """Same category already reported within X metres - likely a duplicate."""
    from .geo import metres_between
    out = []
    with connect() as con:
        rows = con.execute(
            "SELECT id, lat, lon, category, reference, submitted_at, title "
            "FROM reports WHERE status IN ('submitted','mapped','completed') "
            "AND lat IS NOT NULL").fetchall()
    for r in rows:
        if (r["category"] or "").lower() != (category or "").lower():
            continue
        d = metres_between(lat, lon, r["lat"], r["lon"])
        if d <= metres:
            row = dict(r)
            row["distance_m"] = round(d, 1)
            out.append(row)
    return out


def add_update(rid: int, source: str, happened: str, body: str,
               state: str = "") -> int | None:
    """Record one council update against a case. The hash makes re-reading a
    page or re-dropping an email harmless - the same update never lands twice."""
    import hashlib
    key = hashlib.sha1(
        f"{rid}|{source}|{happened}|{body}".encode("utf-8")).hexdigest()
    with connect() as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO updates (report_id, source, happened, body, "
            "state, hash) VALUES (?,?,?,?,?,?)",
            (rid, source, happened, body, state, key))
        return cur.lastrowid if cur.rowcount else None


def updates_for(rid: int) -> list:
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM updates WHERE report_id=? ORDER BY id", (rid,))]


def update_counts() -> dict:
    """report id -> how many council updates it holds, for the list view."""
    with connect() as con:
        return {r["report_id"]: r["c"] for r in con.execute(
            "SELECT report_id, COUNT(*) c FROM updates GROUP BY report_id")}


def stats() -> dict:
    with connect() as con:
        rows = con.execute(
            "SELECT status, COUNT(*) c FROM reports GROUP BY status").fetchall()
    return {r["status"]: r["c"] for r in rows}
