#!/usr/bin/env python3
"""Export the Gavdos decisions database to CSV and JSON.

Usage:
    python export.py --db gavdos.db --out exports/
"""
import argparse
import csv
import json
import os
import sqlite3

FIELDS = [
    "ada", "issue_date", "decision_type", "subject", "protocol_number",
    "signer", "unit", "submission_ts", "diavgeia_url", "document_url",
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="gavdos.db")
    ap.add_argument("--out", default="exports")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT {','.join(FIELDS)} FROM decisions ORDER BY issue_date DESC"
    ).fetchall()

    csv_path = os.path.join(args.out, "gavdos_decisions.csv")
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(dict(r))

    json_path = os.path.join(args.out, "gavdos_decisions.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump([dict(r) for r in rows], f, ensure_ascii=False, indent=2)

    print(f"Exported {len(rows)} acts to:\n  {csv_path}\n  {json_path}")
    conn.close()


if __name__ == "__main__":
    main()
