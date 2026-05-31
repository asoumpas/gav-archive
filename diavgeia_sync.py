#!/usr/bin/env python3
"""
Gavdos Municipality (Δήμος Γαύδου) — Diavgeia decisions sync.

Fetches every published act (πράξη) of the Municipality of Gavdos from the
official Diavgeia open-data API and stores it in a local SQLite database.

- First run: downloads the full historical archive.
- Every run after that: incremental — only fetches acts newer than the most
  recent one already stored (fast, safe to run on a schedule).

No API key required. The Diavgeia open-data API is public (CC-BY license).

Usage:
    python diavgeia_sync.py            # incremental sync (or full on first run)
    python diavgeia_sync.py --full     # force a complete re-download
    python diavgeia_sync.py --db /path/to/gavdos.db
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Organization identifier for the Municipality of Gavdos on Diavgeia.
# The open-data API expects the NUMERIC organization uid (6064), not the
# latin name "gavdou_dimos" that appears in the website URL.
ORG_UID = "6064"

API_BASE = "https://opendata.diavgeia.gov.gr/luminapi/api"
SEARCH_URL = f"{API_BASE}/search"

PAGE_SIZE = 100          # max results per request
REQUEST_TIMEOUT = 30     # seconds
SLEEP_BETWEEN = 0.5      # be polite to the public API
HEADERS = {
    "Accept": "application/json",
    "User-Agent": "GavdosDiavgeiaSync/1.0 (personal archive)",
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    ada              TEXT PRIMARY KEY,   -- Αριθμός Διαδικτυακής Ανάρτησης (unique)
    protocol_number  TEXT,
    subject          TEXT,               -- θέμα
    decision_type    TEXT,               -- τύπος πράξης
    decision_type_id TEXT,
    org_uid          TEXT,
    org_label        TEXT,
    unit             TEXT,               -- μονάδα / οργανική θέση
    signer           TEXT,               -- υπογράφων
    amount           REAL,               -- ποσό σε ευρώ (αν υπάρχει)
    region           TEXT,               -- περιοχή/οικισμός (από το θέμα)
    issue_date       TEXT,               -- ημερομηνία έκδοσης (ISO)
    submission_ts    TEXT,               -- ημερομηνία ανάρτησης (ISO)
    document_url     TEXT,               -- link στο PDF / έγγραφο
    diavgeia_url     TEXT,               -- link στη σελίδα της πράξης
    raw_json         TEXT,               -- full record, for future-proofing
    fetched_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_issue_date ON decisions(issue_date);
CREATE INDEX IF NOT EXISTS idx_type       ON decisions(decision_type);
CREATE INDEX IF NOT EXISTS idx_submission ON decisions(submission_ts);
CREATE INDEX IF NOT EXISTS idx_region     ON decisions(region);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      TEXT,
    mode        TEXT,
    new_records INTEGER,
    total_seen  INTEGER,
    notes       TEXT
);
"""


def connect(db_path, rebuild=False):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if rebuild:
        conn.executescript("DROP TABLE IF EXISTS decisions;")
        conn.commit()
    _migrate(conn)            # add any missing columns to an existing DB
    conn.executescript(SCHEMA)
    return conn


REQUIRED_COLS = {
    "ada", "protocol_number", "subject", "decision_type", "decision_type_id",
    "org_uid", "org_label", "unit", "signer", "amount", "region",
    "issue_date", "submission_ts", "document_url", "diavgeia_url",
    "raw_json", "fetched_at",
}


def _migrate(conn):
    """Bring an older database up to date. If the existing table is missing
    several columns (structurally outdated), drop and recreate it so SCHEMA
    can build it cleanly. Otherwise just add the few missing columns."""
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
    except sqlite3.DatabaseError:
        return
    if not cols:
        return  # no table yet
    missing = REQUIRED_COLS - cols
    if not missing:
        return
    # If only the new optional columns are missing, add them in place.
    if missing <= {"amount", "region"}:
        for col, coltype in (("amount", "REAL"), ("region", "TEXT")):
            if col not in cols:
                conn.execute(
                    f"ALTER TABLE decisions ADD COLUMN {col} {coltype}")
        conn.commit()
        return
    # Otherwise the schema is too different — rebuild from scratch.
    conn.executescript("DROP TABLE IF EXISTS decisions;")
    conn.commit()


def latest_submission_ts(conn):
    row = conn.execute(
        "SELECT MAX(submission_ts) AS m FROM decisions"
    ).fetchone()
    return row["m"] if row and row["m"] else None


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _json_compact(value):
    """Last-resort: serialise a structure to compact JSON text."""
    import json as _json
    try:
        return _json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return str(value)


def _ms_to_iso(value):
    """Normalise a Diavgeia date to ISO (YYYY-MM-DD...). Handles:
      - epoch milliseconds (e.g. 1716800000000)
      - epoch seconds (e.g. 1716800000)
      - already-ISO strings ('2024-05-27...')
      - Greek date strings ('27/05/2024', '27-05-2024')
    Returns None when no valid date can be derived (so it's clearly 'unknown'
    rather than a garbage value that breaks the year filter)."""
    import re
    if value is None or value == "":
        return None

    # Numeric epoch (ms or seconds), possibly as a numeric string
    if isinstance(value, (int, float)) or (
            isinstance(value, str) and value.strip().isdigit()):
        try:
            n = int(value)
            # Heuristic: 13-digit -> ms, 10-digit -> seconds
            if n > 1_000_000_000_000:      # milliseconds
                n = n / 1000
            elif n > 1_000_000_000:        # seconds
                n = float(n)
            else:
                return None                # too small to be a real date
            return datetime.fromtimestamp(n, tz=timezone.utc).isoformat()
        except (ValueError, TypeError, OSError):
            return None

    if isinstance(value, str):
        s = value.strip()
        # Already ISO-ish? keep it.
        if re.match(r"^\d{4}-\d{2}-\d{2}", s):
            return s
        # Greek/European DD/MM/YYYY or DD-MM-YYYY (optional time)
        m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", s)
        if m:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
            except ValueError:
                return None
        # YYYY/MM/DD
        m = re.match(r"^(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})", s)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return datetime(y, mo, d, tzinfo=timezone.utc).isoformat()
            except ValueError:
                return None
    return None


# Οικισμοί / περιοχές του Δήμου Γαύδου. Κάθε περιοχή με τις παραλλαγές
# γραφής που μπορεί να εμφανιστούν στα θέματα των αποφάσεων.
GAVDOS_REGIONS = {
    "Καστρί": ["καστρι", "καστρί"],
    "Βατσιανά": ["βατσιανα", "βατσιανά", "βατσσιανα"],
    "Άμπελος": ["αμπελος", "άμπελος", "αμπελο"],
    "Καραβέ": ["καραβε", "καραβέ", "καραβές", "λιμανι καραβε", "λιμάνι"],
    "Φωκιά": ["φωκια", "φωκιά", "φοκια"],
    "Άγιος Ιωάννης": ["αγιος ιωαννης", "άγιος ιωάννης", "αη γιαννης",
                       "αη γιάννη", "αϊ γιαννης", "άι γιάννη"],
    "Σαρακήνικο": ["σαρακηνικο", "σαρακήνικο"],
    "Κόρφος": ["κορφος", "κόρφος"],
    "Λαύρακας": ["λαυρακας", "λαύρακας"],
    "Τρυπητή": ["τρυπητη", "τρυπητή"],
    "Ποταμός": ["ποταμος", "ποταμός"],
    "Μετόχια": ["μετοχια", "μετόχια"],
    "Ξενάκι": ["ξενακι", "ξενάκι"],
    "Γαυδοπούλα": ["γαυδοπουλα", "γαυδοπούλα"],
}


def _detect_region(subject):
    """Return any Gavdos place-names found in the subject text, comma-joined.
    Note: based on the subject only; many acts don't name a place, so a blank
    region does NOT mean the act is island-wide."""
    if not subject:
        return None
    low = subject.lower()
    found = []
    for canonical, variants in GAVDOS_REGIONS.items():
        if any(v in low for v in variants):
            found.append(canonical)
    return ", ".join(found) if found else None


def _extract_amount(d):
    """Best-effort numeric amount (euros) for an act. Diavgeia stores monetary
    values in several possible places depending on the decision type. We try
    the structured fields first; return a float or None."""
    # Direct numeric keys sometimes present on expense acts
    for key in ("awardAmount", "amountWithVAT", "amountWithoutVAT",
                "totalAmount", "budgettotal", "amount"):
        v = d.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            n = _parse_money(v)
            if n is not None:
                return n
    # Structured "extraFieldValues" used by the new system
    efv = d.get("extraFieldValues") or d.get("extraFields")
    if isinstance(efv, dict):
        for key in ("awardAmount", "amountWithVAT", "amount", "value",
                    "totalAmount"):
            v = efv.get(key)
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, dict):
                vv = v.get("amount") or v.get("value")
                if isinstance(vv, (int, float)):
                    return float(vv)
                if isinstance(vv, str):
                    n = _parse_money(vv)
                    if n is not None:
                        return n
            if isinstance(v, str):
                n = _parse_money(v)
                if n is not None:
                    return n
    return None


def _parse_money(s):
    """Parse a money-like string to float, handling Greek and intl formats:
        '1.234,56' -> 1234.56   (Greek: . thousands, , decimal)
        '1,234.56' -> 1234.56   (Intl:  , thousands, . decimal)
        '88.000'   -> 88000     (Greek thousands, no decimals)
        '500,00'   -> 500.00
    """
    import re
    if not s:
        return None
    txt = re.sub(r"[^0-9.,]", "", str(s))
    if not txt:
        return None

    has_dot = "." in txt
    has_comma = "," in txt

    if has_dot and has_comma:
        # The last-occurring separator is the decimal one.
        if txt.rfind(",") > txt.rfind("."):
            txt = txt.replace(".", "").replace(",", ".")   # Greek
        else:
            txt = txt.replace(",", "")                      # Intl
    elif has_comma:
        # Comma only -> decimal separator
        txt = txt.replace(",", ".")
    elif has_dot:
        # Dot only: decide if it's decimals or thousands.
        parts = txt.split(".")
        # exactly one dot with 1-2 trailing digits => decimal (e.g. 88.5)
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            pass  # keep as decimal
        else:
            txt = txt.replace(".", "")  # thousands separators -> 88.000 = 88000

    try:
        val = float(txt)
        return val if 0 < val < 1e12 else None
    except ValueError:
        return None


def parse_decision(d):
    """Map one API record to our flat row. The API field names can vary
    slightly between versions, so we look up several likely keys."""

    def first(*keys):
        for k in keys:
            if k in d and d[k] not in (None, ""):
                return d[k]
        return None

    def flat(value):
        """Coerce lists/dicts/numbers to a plain string the DB can store."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float, bool)):
            return str(value)
        if isinstance(value, (list, tuple)):
            return ", ".join(flat(v) for v in value if v not in (None, ""))
        if isinstance(value, dict):
            return value.get("label") or value.get("uid") or value.get("name") \
                or _json_compact(value)
        return str(value)

    ada = flat(first("ada", "ADA"))
    issue = _ms_to_iso(first("issueDate", "issue_date"))
    submission = _ms_to_iso(
        first("submissionTimestamp", "publishTimestamp", "submission_ts")
    )

    org = d.get("organizationId") or d.get("organizationUid") or ""
    org_label = d.get("organizationLabel") or d.get("organization") or ""

    doc_url = first("documentUrl", "url")
    diavgeia_url = f"https://diavgeia.gov.gr/doc/{ada}" if ada else None

    decision_type = ""
    decision_type_id = ""
    dt = d.get("decisionTypeId") or d.get("decisionType")
    if isinstance(dt, dict):
        decision_type = dt.get("label") or dt.get("uid") or ""
        decision_type_id = dt.get("uid") or ""
    elif dt:
        decision_type_id = str(dt)
        decision_type = str(dt)

    amount = _extract_amount(d)
    subject_text = flat(first("subject", "title")) or ""
    region = _detect_region(subject_text)

    return {
        "ada": ada,
        "protocol_number": flat(first("protocolNumber", "protocol")),
        "subject": subject_text,
        "decision_type": flat(decision_type),
        "decision_type_id": flat(decision_type_id),
        "org_uid": flat(org),
        "org_label": flat(org_label),
        "unit": flat(first("unitIds", "unit")),
        "signer": flat(first("signerIds", "signer")),
        "amount": amount,
        "region": region,
        "issue_date": issue,
        "submission_ts": submission,
        "document_url": flat(doc_url),
        "diavgeia_url": diavgeia_url,
        "raw_json": None,  # filled by caller (keeps parse_decision testable)
    }


def fetch_page(query, page, size=PAGE_SIZE):
    params = {
        "q": query,
        "page": page,
        "size": size,
        "sort": "recent",   # newest first
    }
    resp = requests.get(
        SEARCH_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT
    )
    resp.raise_for_status()
    return resp.json()


def iter_decisions(query, stop_at_submission=None):
    """Yield decision records page by page. If stop_at_submission is given,
    stop as soon as we reach an act we already have (incremental mode)."""
    page = 0
    total = None
    seen = 0
    while True:
        data = fetch_page(query, page)

        # The API nests results under "decisions"; total under "info"/"total".
        decisions = data.get("decisions") or data.get("results") or []
        if total is None:
            info = data.get("info") or {}
            total = info.get("total") or info.get("totalRecords") or 0
            print(f"  API reports {total} total acts for this query.")

        if not decisions:
            break

        for raw in decisions:
            rec = parse_decision(raw)
            import json as _json
            rec["raw_json"] = _json.dumps(raw, ensure_ascii=False)
            if stop_at_submission and rec["submission_ts"] and (
                rec["submission_ts"] <= stop_at_submission
            ):
                return  # reached known territory; nothing newer beyond here
            yield rec
            seen += 1

        page += 1
        if total and seen >= total:
            break
        time.sleep(SLEEP_BETWEEN)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

UPSERT = """
INSERT INTO decisions (
    ada, protocol_number, subject, decision_type, decision_type_id,
    org_uid, org_label, unit, signer, amount, region, issue_date, submission_ts,
    document_url, diavgeia_url, raw_json, fetched_at
) VALUES (
    :ada, :protocol_number, :subject, :decision_type, :decision_type_id,
    :org_uid, :org_label, :unit, :signer, :amount, :region, :issue_date, :submission_ts,
    :document_url, :diavgeia_url, :raw_json, :fetched_at
)
ON CONFLICT(ada) DO UPDATE SET
    subject       = excluded.subject,
    decision_type = excluded.decision_type,
    amount        = excluded.amount,
    region        = excluded.region,
    document_url  = excluded.document_url,
    raw_json      = excluded.raw_json,
    fetched_at    = excluded.fetched_at;
"""


def total_for_query(query):
    """Return how many acts a query matches (0 on any failure)."""
    try:
        data = fetch_page(query, 0, size=1)
        info = data.get("info") or {}
        return int(info.get("total") or info.get("totalRecords") or 0)
    except Exception:
        return 0


def find_working_query():
    """Diavgeia's API has shifted org identifiers over time. Try the known
    candidates for the Municipality of Gavdos and use whichever returns acts."""
    candidates = [
        'organizationUid:"6064"',          # legacy numeric uid
        'organizationUid:"gavdou_dimos"',   # latin name
        'organizationUid:6064',             # unquoted numeric
    ]
    for q in candidates:
        n = total_for_query(q)
        print(f"  Trying {q} -> {n} acts")
        if n > 0:
            print(f"  Using query: {q}")
            return q
    # Nothing matched; fall back to the first candidate so the run still
    # completes (with 0 acts) instead of crashing.
    print("  WARNING: no candidate returned acts; using default.")
    return candidates[0]


def sync(db_path, full=False):
    # On a full run, rebuild the table from scratch so the schema is always
    # current (avoids "no such column" errors from older databases).
    conn = connect(db_path, rebuild=full)
    query = find_working_query()

    stop_at = None if full else latest_submission_ts(conn)
    mode = "full" if (full or stop_at is None) else "incremental"
    print(f"[{datetime.now().isoformat(timespec='seconds')}] "
          f"Starting {mode} sync for {ORG_UID} ...")
    if stop_at:
        print(f"  Will fetch only acts newer than {stop_at}")

    new_count = 0
    now = datetime.now(timezone.utc).isoformat()
    try:
        for rec in iter_decisions(query, stop_at_submission=stop_at):
            if not rec["ada"]:
                continue
            rec["fetched_at"] = now
            cur = conn.execute(UPSERT, rec)
            if cur.rowcount and conn.total_changes:
                new_count += 1
            if new_count and new_count % 100 == 0:
                conn.commit()
                print(f"  ... {new_count} new acts stored")
        conn.commit()
    except requests.HTTPError as e:
        print(f"  ! API error: {e}", file=sys.stderr)
        conn.rollback()
    except KeyboardInterrupt:
        print("  Interrupted — committing what we have.")
        conn.commit()

    total_in_db = conn.execute("SELECT COUNT(*) AS c FROM decisions").fetchone()["c"]
    conn.execute(
        "INSERT INTO sync_log (ran_at, mode, new_records, total_seen, notes) "
        "VALUES (?,?,?,?,?)",
        (now, mode, new_count, total_in_db, ""),
    )
    conn.commit()
    print(f"[done] {new_count} new acts added. "
          f"Database now holds {total_in_db} acts total.")
    conn.close()
    return new_count


def main():
    ap = argparse.ArgumentParser(description="Sync Gavdos decisions from Diavgeia.")
    ap.add_argument("--db", default="gavdos.db", help="SQLite database path")
    ap.add_argument("--full", action="store_true",
                    help="Force full re-download instead of incremental")
    args = ap.parse_args()
    sync(args.db, full=args.full)


if __name__ == "__main__":
    main()
