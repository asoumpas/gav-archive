#!/usr/bin/env python3
"""Write the public-facing JSON that the static website reads.

Produces docs/data.json — a compact list of all acts, newest first.
Run after diavgeia_sync.py.
"""
import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone

FIELDS = [
    "ada", "issue_date", "decision_type", "subject",
    "signer", "diavgeia_url", "document_url",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="gavdos.db")
    ap.add_argument("--out", default="docs/data.json")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT {','.join(FIELDS)} FROM decisions ORDER BY issue_date DESC"
    ).fetchall()
    conn.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(rows),
        "acts": [dict(r) for r in rows],
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Wrote {len(rows)} acts to {args.out}")


if __name__ == "__main__":
    main()
