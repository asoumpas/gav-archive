#!/usr/bin/env python3
"""
Private web viewer for the Gavdos / Diavgeia decisions database.

- Password-protected (HTTP Basic auth). Set credentials via environment
  variables VIEWER_USER and VIEWER_PASSWORD before running.
- Full-text search over subject + ADA, filter by type and year, paginated.
- Read-only: never modifies the database.

Run:
    export VIEWER_USER=adam
    export VIEWER_PASSWORD='choose-a-strong-password'
    python app.py
Then open http://localhost:8000
"""

import os
import sqlite3
from functools import wraps

from flask import Flask, request, Response, g, render_template_string

DB_PATH = os.environ.get("GAVDOS_DB", "gavdos.db")
USER = os.environ.get("VIEWER_USER", "admin")
PASSWORD = os.environ.get("VIEWER_PASSWORD", "")

app = Flask(__name__)


# ----------------------------- auth ---------------------------------------

def check_auth(u, p):
    return u == USER and bool(PASSWORD) and p == PASSWORD


def authenticate():
    return Response(
        "Authentication required.", 401,
        {"WWW-Authenticate": 'Basic realm="Gavdos Diavgeia Archive"'},
    )


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not PASSWORD:
            return Response(
                "Server not configured: set VIEWER_PASSWORD.", 500
            )
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated


# ----------------------------- db -----------------------------------------

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()


# ----------------------------- view ---------------------------------------

PAGE = """
<!doctype html>
<html lang="el">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Αρχείο Πράξεων — Δήμος Γαύδου</title>
<style>
  :root{
    --ink:#13202a; --sea:#0a6e8c; --sea-deep:#073a4c;
    --sand:#f4efe6; --line:#d8d0c2; --accent:#d98f2b;
  }
  *{box-sizing:border-box}
  body{margin:0;font-family:"Georgia",serif;background:var(--sand);color:var(--ink)}
  header{background:linear-gradient(160deg,var(--sea-deep),var(--sea));
    color:#fff;padding:28px 22px;border-bottom:4px solid var(--accent)}
  header h1{margin:0;font-size:1.5rem;letter-spacing:.5px}
  header p{margin:6px 0 0;opacity:.85;font-size:.9rem}
  .wrap{max-width:1100px;margin:0 auto;padding:22px}
  form.search{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0;
    background:#fff;padding:16px;border:1px solid var(--line);border-radius:8px}
  form.search input,form.search select{
    padding:10px 12px;border:1px solid var(--line);border-radius:6px;
    font-family:inherit;font-size:.95rem}
  form.search input[type=text]{flex:1;min-width:220px}
  form.search button{background:var(--sea);color:#fff;border:0;padding:10px 20px;
    border-radius:6px;cursor:pointer;font-family:inherit;font-size:.95rem}
  .meta{font-size:.85rem;color:#5a5346;margin-bottom:12px}
  .card{background:#fff;border:1px solid var(--line);border-left:4px solid var(--sea);
    border-radius:6px;padding:14px 16px;margin-bottom:12px}
  .card .subj{font-size:1.02rem;font-weight:bold;margin:0 0 6px}
  .card .row{font-size:.82rem;color:#5a5346;display:flex;flex-wrap:wrap;gap:14px}
  .card a{color:var(--sea);text-decoration:none}
  .card a:hover{text-decoration:underline}
  .tag{display:inline-block;background:var(--sand);border:1px solid var(--line);
    padding:2px 8px;border-radius:20px;font-size:.74rem}
  .pager{display:flex;justify-content:center;gap:8px;margin:24px 0}
  .pager a,.pager span{padding:8px 14px;border:1px solid var(--line);
    border-radius:6px;text-decoration:none;color:var(--ink);background:#fff}
  .pager .cur{background:var(--sea);color:#fff;border-color:var(--sea)}
  footer{text-align:center;padding:24px;color:#7a7263;font-size:.8rem}
</style>
</head>
<body>
<header>
  <h1>Αρχείο Πράξεων — Δήμος Γαύδου</h1>
  <p>Επίσημες αποφάσεις & ανακοινώσεις από τη Δι@ύγεια · ιδιωτικό αρχείο</p>
</header>
<div class="wrap">
  <form class="search" method="get">
    <input type="text" name="q" placeholder="Αναζήτηση σε θέμα ή ΑΔΑ..."
           value="{{ q|e }}">
    <select name="type">
      <option value="">— Όλοι οι τύποι —</option>
      {% for t in types %}
        <option value="{{ t|e }}" {{ 'selected' if t==sel_type }}>{{ t|e }}</option>
      {% endfor %}
    </select>
    <select name="year">
      <option value="">— Όλα τα έτη —</option>
      {% for y in years %}
        <option value="{{ y }}" {{ 'selected' if y==sel_year }}>{{ y }}</option>
      {% endfor %}
    </select>
    <button type="submit">Αναζήτηση</button>
  </form>

  <div class="meta">{{ total }} αποτελέσματα · σελίδα {{ page }} από {{ pages }}</div>

  {% for d in rows %}
  <div class="card">
    <p class="subj">{{ d['subject'] or '(χωρίς θέμα)' }}</p>
    <div class="row">
      <span class="tag">{{ d['decision_type'] or '—' }}</span>
      <span>ΑΔΑ: {{ d['ada'] }}</span>
      <span>Έκδοση: {{ (d['issue_date'] or '')[:10] }}</span>
      {% if d['signer'] %}<span>Υπογράφων: {{ d['signer'] }}</span>{% endif %}
      <span><a href="{{ d['diavgeia_url'] }}" target="_blank" rel="noopener">Διαύγεια ↗</a></span>
      {% if d['document_url'] %}
        <span><a href="{{ d['document_url'] }}" target="_blank" rel="noopener">Έγγραφο ↗</a></span>
      {% endif %}
    </div>
  </div>
  {% else %}
  <p>Δεν βρέθηκαν αποτελέσματα.</p>
  {% endfor %}

  <div class="pager">
    {% if page > 1 %}<a href="{{ url(page-1) }}">‹ Προηγ.</a>{% endif %}
    <span class="cur">{{ page }}</span>
    {% if page < pages %}<a href="{{ url(page+1) }}">Επόμ. ›</a>{% endif %}
  </div>
</div>
<footer>
  Δεδομένα: Πρόγραμμα Δι@ύγεια (CC-BY) · Τελευταίος συγχρονισμός βάσει sync_log
</footer>
</body>
</html>
"""

PER_PAGE = 25


@app.route("/")
@requires_auth
def index():
    q = request.args.get("q", "").strip()
    sel_type = request.args.get("type", "").strip()
    sel_year = request.args.get("year", "").strip()
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    where, params = [], []
    if q:
        where.append("(subject LIKE ? OR ada LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if sel_type:
        where.append("decision_type = ?")
        params.append(sel_type)
    if sel_year:
        where.append("substr(issue_date,1,4) = ?")
        params.append(sel_year)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    conn = db()
    total = conn.execute(
        f"SELECT COUNT(*) c FROM decisions {clause}", params
    ).fetchone()["c"]
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    offset = (page - 1) * PER_PAGE

    rows = conn.execute(
        f"SELECT * FROM decisions {clause} "
        f"ORDER BY issue_date DESC LIMIT ? OFFSET ?",
        params + [PER_PAGE, offset],
    ).fetchall()

    types = [r["decision_type"] for r in conn.execute(
        "SELECT DISTINCT decision_type FROM decisions "
        "WHERE decision_type<>'' ORDER BY decision_type"
    ).fetchall()]
    years = [r["y"] for r in conn.execute(
        "SELECT DISTINCT substr(issue_date,1,4) y FROM decisions "
        "WHERE issue_date IS NOT NULL ORDER BY y DESC"
    ).fetchall() if r["y"]]

    def url(p):
        from urllib.parse import urlencode
        return "?" + urlencode(
            {"q": q, "type": sel_type, "year": sel_year, "page": p}
        )

    return render_template_string(
        PAGE, rows=rows, total=total, page=page, pages=pages,
        q=q, types=types, years=years, sel_type=sel_type, sel_year=sel_year,
        url=url,
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
