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

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at      TEXT,
    mode        TEXT,
    new_records INTEGER,
    total_seen  INTEGER,
    notes       TEXT
);
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


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
    """Diavgeia returns timestamps as epoch milliseconds. Normalise to ISO."""
    if value is None:
        return None
    try:
        # already a string date?
        if isinstance(value, str) and not value.isdigit():
            return value
        ms = int(value)
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return str(value)


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

    return {
        "ada": ada,
        "protocol_number": flat(first("protocolNumber", "protocol")),
        "subject": flat(first("subject", "title")),
        "decision_type": flat(decision_type),
        "decision_type_id": flat(decision_type_id),
        "org_uid": flat(org),
        "org_label": flat(org_label),
        "unit": flat(first("unitIds", "unit")),
        "signer": flat(first("signerIds", "signer")),
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
    org_uid, org_label, unit, signer, issue_date, submission_ts,
    document_url, diavgeia_url, raw_json, fetched_at
) VALUES (
    :ada, :protocol_number, :subject, :decision_type, :decision_type_id,
    :org_uid, :org_label, :unit, :signer, :issue_date, :submission_ts,
    :document_url, :diavgeia_url, :raw_json, :fetched_at
)
ON CONFLICT(ada) DO UPDATE SET
    subject       = excluded.subject,
    decision_type = excluded.decision_type,
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
    conn = connect(db_path)
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
