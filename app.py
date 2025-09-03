
"""E.A.S.I.E. — Expense & Savings–Income Engine (FastAPI Full)"""

import os, io, csv, calendar, sqlite3
import json
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple, List

from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles   # ✅ add this
from pydantic import BaseModel, condecimal
from jinja2 import Environment, FileSystemLoader, select_autoescape
import os
DB_PATH = os.getenv("EASIE_DB", "easie.db")

# ---------------- App ----------------
app = FastAPI(title="EASIE")

# ✅ Mount static so /static/... works
app.mount("/static", StaticFiles(directory="static"), name="static")

# ✅ Allow CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)

TEMPLATES = Environment(
    loader=FileSystemLoader(searchpath="."),
    autoescape=select_autoescape()
)

# ---------------- DB ----------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db(); cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS budgets(
        id INTEGER PRIMARY KEY, category TEXT UNIQUE NOT NULL, weekly_amount REAL NOT NULL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS transactions(
        id INTEGER PRIMARY KEY,
        ts TEXT NOT NULL,
        amount REAL NOT NULL,
        category TEXT NOT NULL,
        memo TEXT,
        kind TEXT NOT NULL DEFAULT 'expense'
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS params(
        key TEXT PRIMARY KEY, value TEXT
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS schedules(
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        frequency TEXT NOT NULL,
        dow INTEGER,
        dom INTEGER,
        start_date TEXT NOT NULL,
        end_date TEXT,
        anchor_date TEXT,
        color TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        version INTEGER NOT NULL DEFAULT 1
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS occurrences(
        id INTEGER PRIMARY KEY,
        schedule_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        kind TEXT NOT NULL,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'planned',
        memo TEXT DEFAULT '',
        UNIQUE(schedule_id, date),
        FOREIGN KEY(schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
    )""")
        # --- Wishlist (items you’re considering) ---
    cur.execute("""CREATE TABLE IF NOT EXISTS wishlist(
        id INTEGER PRIMARY KEY,
        item TEXT NOT NULL,
        category TEXT NOT NULL,
        price REAL NOT NULL,
        target_date TEXT NOT NULL,    -- when you intend to buy it
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    conn.commit()
 # --- Track AI Advisor interest ---
    cur.execute("""CREATE TABLE IF NOT EXISTS advisor_interest(
        id INTEGER PRIMARY KEY,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )""")
    # Budgets seed at 0
    cur.execute("SELECT COUNT(*) FROM budgets")
    if cur.fetchone()[0] == 0:
        cats = ["rent","utilities","subscriptions","food","entertainment","misc","transport","savings"]
        cur.executemany("INSERT INTO budgets(category,weekly_amount) VALUES(?,?)", [(c,0.0) for c in cats])
        conn.commit()

    # Starting balance default
    cur.execute("SELECT value FROM params WHERE key='starting_balance'")
    if not cur.fetchone():
        cur.execute("INSERT INTO params(key,value) VALUES('starting_balance','0')")
        conn.commit()
    conn.close()

# ---------------- Utils ----------------
def parse_date(s: str) -> str:
    s = s.strip().replace("\"", "")
    try: return datetime.fromisoformat(s).date().isoformat()
    except: pass
    try: m,d,y = s.split("/"); return date(int(y),int(m),int(d)).isoformat()
    except: pass
    try: y,m,d = s.split("-"); return date(int(y),int(m),int(d)).isoformat()
    except: raise ValueError(f"Unrecognized date: {s}")

def week_bounds(d: date) -> Tuple[date,date]:
    start = d - timedelta(days=d.weekday())
    return start, start+timedelta(days=6)

def spend_this_week(category: str, start: date, end: date) -> float:
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT COALESCE(SUM(amount),0) FROM transactions
                   WHERE kind='expense' AND category=? AND date(ts) BETWEEN ? AND ?""",
                   (category,start.isoformat(),end.isoformat()))
    val = float(cur.fetchone()[0] or 0.0); conn.close(); return val

def set_param(key: str, value: str):
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO params(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
    conn.commit(); conn.close()

def get_param(key: str, default: str="") -> str:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT value FROM params WHERE key=?",(key,))
    row = cur.fetchone(); conn.close(); return row[0] if row else default
    
    # -------- Daily Aggregates --------
def daily_aggregates(date_from: Optional[str]=None, date_to: Optional[str]=None):
    conn = get_db(); cur = conn.cursor()
    where=[]; params=[]
    if date_from: 
        where.append("date(ts)>=?"); params.append(date_from)
    if date_to: 
        where.append("date(ts)<=?"); params.append(date_to)
    w = ("WHERE "+ " AND ".join(where)) if where else ""
    cur.execute(f"""SELECT date(ts) d,
               SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) inc,
               SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) exp
        FROM transactions {w}
        GROUP BY date(ts) ORDER BY d ASC""", params)
    rows = cur.fetchall(); conn.close()
    return [{"date":r["d"],"income":float(r["inc"] or 0),"expense":float(r["exp"] or 0)} for r in rows]
def budget_for(category: str) -> float:
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT weekly_amount FROM budgets WHERE category=?", (category,))
    row = cur.fetchone(); conn.close()
    return float(row[0]) if row else 0.0

def current_running_balance_today() -> float:
    """Starting balance + posted net (income - expense) up to today (inclusive)."""
    today = date.today().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN kind='income'  THEN amount END),0) -
          COALESCE(SUM(CASE WHEN kind='expense' THEN amount END),0)
        FROM transactions
        WHERE date(ts) <= ?
    """, (today,))
    net = float(cur.fetchone()[0] or 0.0)
    start_bal = float(get_param("starting_balance", "0") or 0.0)
    conn.close()
    return start_bal + net
def running_balance_through(day: date) -> float:
    """Starting balance + (income - expense) up to and including `day`."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN kind='income'  THEN amount END), 0) -
          COALESCE(SUM(CASE WHEN kind='expense' THEN amount END), 0)
        FROM transactions
        WHERE date(ts) <= ?
    """, (day.isoformat(),))
    net = float(cur.fetchone()[0] or 0.0)
    conn.close()
    start_bal = float(get_param("starting_balance", "0") or 0)
    return start_bal + net
def forecast_negative_with_purchase(price: float, category: str, when: date) -> Tuple[bool, Dict]:
    """
    Simulate adding this purchase and walk the next 90 days using scheduled occurrences.
    Return (goes_negative, details_dict).
    """
    # Base running balance as of today
    running = current_running_balance_today()

    # Collect planned schedule occurrences for the next 90 days
    horizon_end = date.today() + timedelta(days=90)
    occs = all_occurrences(horizon_end)  # uses your existing schedules → occurrences
    daily_net = {}
    for o in occs:
        d = datetime.fromisoformat(o["date"]).date()
        if d < date.today():  # ignore past
            continue
        amt = float(o["amount"])
        delta = amt if o["kind"] == "income" else -amt
        daily_net[d] = daily_net.get(d, 0.0) + delta

    # Add the hypothetical wish purchase on its target_date
    daily_net[when] = daily_net.get(when, 0.0) - float(price)

    # Walk day-by-day
    goes_negative = False
    first_negative_day = None
    min_balance = running
    min_day = date.today()
    for d in daterange(date.today(), horizon_end):
        running += daily_net.get(d, 0.0)
        if running < min_balance:
            min_balance = running
            min_day = d
        if (not goes_negative) and running < 0.0:
            goes_negative = True
            first_negative_day = d

    return goes_negative, {
        "first_negative_day": (first_negative_day.isoformat() if first_negative_day else None),
        "min_balance": round(min_balance, 2),
        "min_balance_day": min_day.isoformat()
    }

def compute_wish_health(item: str, category: str, price: float, target_date: Optional[str] = None) -> Dict:
    """GREEN / YELLOW / RED based on weekly budget, plus 90-day forecast warning."""
    today = date.today()
    ws, we = week_bounds(today)
    weekly_budget = budget_for(category)
    spent = spend_this_week(category, ws, we)
    remainder_before = max(weekly_budget - spent, 0.0)
    remainder_after = remainder_before - float(price)

    if remainder_after >= 20:
        status = "green"
        message = f"Safe to buy {item} for ${price:.2f}. You’d still have ${remainder_after:.2f} left in {category} this week."
    elif remainder_after >= 0:
        status = "yellow"
        message = f"Borderline: buying leaves only ${remainder_after:.2f} in {category} this week."
    else:
        status = "red"
        message = f"Over budget: buying puts you ${abs(remainder_after):.2f} over {category} for this week."

    # Alternatives
    cheaper = round(max(price * 0.85, 1.00), 2)  # ~15% lower
    alt_msg = f"Consider a similar item at ${cheaper:.2f} and roll ${price - cheaper:.2f} into savings or another tight category."

    # 90-day forecast (warn if it drives balance < 0 at any point)
    td = datetime.fromisoformat(target_date).date() if (target_date and target_date.strip()) else today
    warn, details = forecast_negative_with_purchase(price, category, td)

    return {
        "status": status,                     # green | yellow | red
        "message": message,
        "weekly_budget": round(weekly_budget, 2),
        "spent_this_week": round(spent, 2),
        "remainder_before": round(remainder_before, 2),
        "remainder_after": round(remainder_after, 2),
        "alternative": alt_msg,
        "warn90": bool(warn),                 # green-with-warning case
        "warn_details": details
    }

# -------- Calendar Helpers --------
def month_start(year:int,month:int): 
    return date(year,month,1)

def month_end(year:int,month:int): 
    return date(year,month,calendar.monthrange(year,month)[1])

def daterange(d0:date,d1:date):
    cur = d0
    while cur <= d1:
        yield cur
        cur += timedelta(days=1)
def calendar_series_for_month(y: int, m: int):
    """Return labels, income, expense, running for month y-m with rollover balance."""
    start = month_start(y, m)
    end   = month_end(y, m)

    # posted transactions for the month
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT date(ts) AS d,
               SUM(CASE WHEN kind='income'  THEN amount ELSE 0 END) AS inc,
               SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) AS exp
        FROM transactions
        WHERE date(ts) BETWEEN ? AND ?
        GROUP BY date(ts)
        ORDER BY d ASC
    """, (start.isoformat(), end.isoformat()))
    posted = {r["d"]: (float(r["inc"] or 0), float(r["exp"] or 0)) for r in cur.fetchall()}
    conn.close()

    # add generated occurrences (recurring items)
    occs = all_occurrences(end)
    for o in occs:
        if start.isoformat() <= o["date"] <= end.isoformat():
            inc, exp = posted.get(o["date"], (0.0, 0.0))
            if o["kind"] == "income":
                inc += float(o["amount"])
            else:
                exp += float(o["amount"])
            posted[o["date"]] = (inc, exp)

    # Rollover: start with balance up to day before the 1st
    running = running_balance_through(start - timedelta(days=1))

    labels, incs, exps, runs = [], [], [], []
    for d in daterange(start, end):
        ds = d.isoformat()
        inc, exp = posted.get(ds, (0.0, 0.0))
        if inc or exp:
            running += (inc - exp)
        labels.append(ds)
        incs.append(round(inc, 2))
        exps.append(round(exp, 2))
        runs.append(round(running, 2))
        
    return labels, incs, exps, runs

def calendar_balance_for_day(d: date) -> float:
    # start with balance up to the day before
    running = running_balance_through(d - timedelta(days=1))

    # posted transactions for the day
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT SUM(CASE WHEN kind='income'  THEN amount ELSE 0 END) AS inc,
               SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) AS exp
        FROM transactions
        WHERE date(ts) = ?
    """, (d.isoformat(),))
    row = cur.fetchone()
    inc = float(row["inc"] or 0)
    exp = float(row["exp"] or 0)
    conn.close()
    running += (inc - exp)

    # add occurrences for the day
    occs = all_occurrences(d)
    for o in occs:
        if o["date"] == d.isoformat():
            if o["kind"] == "income":
                running += float(o["amount"])
            else:
                running -= float(o["amount"])

    return running

# ---------------- App ----------------
TEMPLATES = Environment(loader=FileSystemLoader(searchpath="."), autoescape=select_autoescape())

# -------- Theme (navy/gold/white) --------
STYLE = """
<style>
@import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@600&family=Rajdhani:wght@400&display=swap');

body {
  font-family: 'Rajdhani', sans-serif;
  margin: 2rem;
  background: #0a1f44;  /* deep matte navy */
  color: #f5f5f0;       /* soft off-white */
}

header {
  background: #0a1f44;  /* navy */
  padding: 1rem;
  text-align: center;
  position: sticky;
  top: 0;
  z-index: 1000;
}

header h1 {
  font-family: 'Orbitron', sans-serif;
  font-size: 2rem;
  font-weight: 600;
 color: #FFD700;  /* gold */
  margin: 0;
}

header small {
  color: #f5f5f0; /* off-white subtitle */
}

header a {
  color: #FFD700; /* gold nav links */
  text-decoration: none;
  margin: 0 0.5rem;
  font-weight: bold;
}

header a:hover {
  text-decoration: underline;
}

.card {
  border: 1px solid #B8860B; /* bronze border */
  border-radius: 12px;
  padding: 1rem;
  margin-bottom: 1rem;
  background: #fdfdf9; /* neutral beige background */
  color: #111;
  box-shadow: 0 2px 8px rgba(0,0,0,.2);
}

h2 {
  font-family: 'Orbitron', sans-serif;
  color: #FFD700;
}

button, input, select {
  padding: .4rem;
  border-radius: 6px;
  border: 1px solid #888;
  font-family: 'Rajdhani', sans-serif;
}

button {
  background: #FFD700;
  color: #0a1f44;
  font-weight: bold;
  cursor: pointer;
}

button:hover {
  background: #B8860B;
  color: #fff;
}

th {
  background: #0a1f44;
  color: #FFD700;
  font-family: 'Orbitron', sans-serif;
}

th, td {
  border: 1px solid #ddd;
  padding: .4rem .6rem;
}

table {
  border-collapse: collapse;
  width: 100%;
}

.ok { color: #0a0; }
.warn { color: #c90; }
.bad { color: #c00; }
.today {
  border: 2px solid #FFD700;
  background: #FFFACD;
}
.tooltip:hover .tooltiptext {
  visibility: visible;
  opacity: 1;
}

/* ---------- Intro.js Overrides for E.A.S.I.E. ---------- */
.introjs-tooltip {
  background: #0a1f44 !important;   /* deep navy */
  color: #FFD700 !important;        /* gold text */
  border: 2px solid #FFD700 !important;
  border-radius: 10px;
  font-family: 'Rajdhani', sans-serif;
  font-size: 1rem;
}

.introjs-tooltip .introjs-tooltiptext {
  color: #FFD700 !important;   /* ensure body text is gold */
}

.introjs-tooltip h1,
.introjs-tooltip h2,
.introjs-tooltip h3,
.introjs-tooltip h4 {
  color: #FFD700 !important;   /* gold headings */
  font-family: 'Orbitron', sans-serif;
}

.introjs-button {
  background: #FFD700 !important;
  color: #0a1f44 !important;
  border-radius: 6px !important;
  font-weight: bold;
  border: none !important;
}

.introjs-button:hover {
  background: #B8860B !important;  /* bronze hover */
  color: #fff !important;
}

@media (max-width: 768px) {
  body { margin: 1rem; }
  .card { padding: .8rem; }
  table { font-size: .9rem; }
  th, td { padding: .3rem; }
  header h1 { font-size: 1.4rem; }
  header small { font-size: .75rem; }
  button, input, select { font-size: .9rem; }
}

@media (max-width: 480px) {
  header h1 { font-size: 1.2rem; }
  .card { margin-bottom: .8rem; }
  .nav a { display:block; margin:.4rem 0; }
}
</style>
"""
# -------- Navbar (shared across all pages) --------
NAVBAR = """
<header>
  <h1>E.A.S.I.E</h1>
  <small>(Expense & Savings / Income Engine)</small><br>
  <a id="nav-home" href="/home">🏠 Home</a>
  <a id="nav-wishlist" href="/wishlist">Wishlist</a>
  <a id="nav-budgets" href="/budgets">Budgets</a>
  <a id="nav-daily" href="/daily">Daily</a>
  <a id="nav-calendar" href="/calendar">Calendar</a>
  <a id="nav-schedules" href="/schedules">Schedules</a>
  <a id="nav-settings" href="/settings">Settings</a>
  <a id="nav-glossary" href="/glossary">Glossary</a>
  <a href="javascript:void(0)" onclick="startTutorial()">❓ Help</a>
  <a id="nav-advisor" href="/advisor">AI Advisor</a>

</header>
<script>
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/static/sw.js").then(() => {
    console.log("✅ Service worker registered");
  });
}
</script>
<script>
let deferredPrompt;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;
  const btn = document.createElement("button");
  btn.textContent = "📲 Install E.A.S.I.E.";
  btn.style = "margin-top:1rem; padding:.6rem 1.2rem; font-weight:bold; background:#FFD700; border:none; border-radius:6px; color:#0a1f44;";
  btn.onclick = async () => {
    deferredPrompt.prompt();
    const { outcome } = await deferredPrompt.userChoice;
    console.log("User response:", outcome);
    deferredPrompt = null;
  };
  document.body.appendChild(btn);
});
</script>

"""
## -------- Landing animation --------
WELCOME_HTML = """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1">
  <meta charset="utf-8">
  <title>Welcome</title>
  <link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600&family=Rajdhani:wght@400&display=swap" rel="stylesheet">
 <style>
  body {
    margin:0; 
    background:#0a1f44; /* deep navy */
    overflow:hidden; 
    font-family:'Rajdhani', sans-serif; 
    color:#FFD700;
    text-align:center;
  }
  #splashGif {
    position: absolute;
    top:0; left:0;
    width:100%;
    height:100%;
    object-fit:cover;
    background:black;
    z-index:5;
    animation: fadeout 2s 3s forwards; /* hide gif after 3s */
  }
  #logo {
    position:absolute;
    top:50%; left:50%;
    transform:translate(-50%,-50%);
    font-family:'Orbitron', sans-serif;
    font-size:3rem;
    font-weight:600;
    color:#FFD700;   /* Solid gold text */
    opacity:0; 
    z-index:50;
    animation: fadein 2s 3.1s forwards;
  }
  #subtitle {
    font-family:'Rajdhani', sans-serif;
    font-size:1.2rem;
    color:#f5f5f0;  /* off-white */
  }
  #enterBtn {
    display:inline-block;
    margin-top:2rem;
    padding:.6rem 1.2rem;
    border:2px solid #FFD700;
    border-radius:8px;
    font-weight:bold;
    color:#FFD700;
    text-decoration:none;
    transition:all .2s ease-in-out;
  }
  #enterBtn:hover {
    background:#FFD700;
    color:#0a1f44;
  }
  @keyframes fadein { from {opacity:0;} to {opacity:1;} }
  @keyframes fadeout { from {opacity:1;} to {opacity:0;} }
</style>
</head>
<body>
  <img id="splashGif" src="/static/7ED1.gif" alt="Scrooge Splash">
  <div id="logo">
    E.A.S.I.E
    <div id="subtitle">(Expense & Savings / Income Engine)</div>
    <a id="enterBtn" href="/home">Launch App 🚀</a>
  </div>
</body>
</html>"""

@app.get("/", response_class=HTMLResponse)
def landing():
    return HTMLResponse(WELCOME_HTML)
# -------- Dashboard (Home) --------

@app.get("/home", response_class=HTMLResponse)
def dashboard():
    today = date.today()
    ws, we = week_bounds(today)

    # 1) weekly budgets left
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT category, weekly_amount FROM budgets ORDER BY category")
    rows = cur.fetchall()
    budgets = []
    for r in rows:
        wk = float(r[1])
        spent = spend_this_week(r[0], ws, we)
        budgets.append({
            "category": r[0],
            "weekly_amount": wk,
            "spent": spent,
            "remaining": max(wk - spent, 0.0)
        })
    conn.close()

    # 2) today's balance (same as calendar)
    todays_balance = calendar_balance_for_day(today)

    # Build budgets table rows without f-strings
    budgets_rows = []
    for b in budgets:
        budgets_rows.append(
            "<tr>"
            "<td>" + b["category"] + "</td>"
            "<td>$" + ("%.2f" % b["weekly_amount"]) + "</td>"
            "<td>$" + ("%.2f" % b["remaining"]) + "</td>"
            "</tr>"
        )
    budgets_rows_html = "".join(budgets_rows)

    # Build HTML (no f-strings, so JS braces are safe)
    html = (
        "<!doctype html><html><head>" + STYLE 
        + '<link rel="manifest" href="/static/manifest.json">'
        + '<meta name="theme-color" content="#0a1f44">'
        + '<link rel="apple-touch-icon" href="/static/icons/icon-192.png">'
        + '<meta name="apple-mobile-web-app-capable" content="yes">'
        + '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        + '<meta name="apple-mobile-web-app-title" content="E.A.S.I.E.">'
 + '<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/icon-180.png">'
    + '<link rel="apple-touch-icon" sizes="167x167" href="/static/icons/icon-167.png">'
    + '<link rel="apple-touch-icon" sizes="152x152" href="/static/icons/icon-152.png">'
    + '<link rel="apple-touch-icon" sizes="120x120" href="/static/icons/icon-120.png">'
        + "</head><body>"
    '<link href="https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css" rel="stylesheet">'
    + NAVBAR
    + "<h2>📊 Dashboard</h2>"

    # Today’s Balance card
    + "<a href='/calendar' style='text-decoration:none;'>"
      + "<div class='card' id='todayBalanceCard'>"
        + "<h3>Today's Balance</h3>"
        + "<p id='todayBalanceValue' style='font-size:1.6rem; font-weight:800; color:#000; margin:.25rem 0;'>"
          + "$" + ("%.2f" % todays_balance)
        + "</p>"
        + "<small style='opacity:.8;'>(click to open calendar)</small>"
      + "</div>"
    + "</a>"

    # Weekly budgets table
    + "<div class='card'>"
      + "<h3>📅 Week " + str(today.isocalendar()[1]) + " Budgets Left</h3>"
      + "<table>"
        + "<tr><th>Category</th><th>Weekly Budget</th><th>Remaining</th></tr>"
        + budgets_rows_html
      + "</table>"
    + "</div>"

    # Auto-refresh JS
    + "<script>"
      "async function refreshTodayBalance(){"
        "try{"
          "const r = await fetch('/api/today_balance', {cache:'no-store'});"
          "if(!r.ok) return;"
          "const j = await r.json();"
          "const el = document.getElementById('todayBalanceValue');"
          "if(el) el.textContent = '$' + Number(j.balance).toFixed(2);"
        "}catch(e){}"
      "}"
      "refreshTodayBalance();"
      "setInterval(refreshTodayBalance, 30000);"
      "window.addEventListener('focus', refreshTodayBalance);"
    + "</script>"

    # ✅ Tutorial scripts (fixed formatting!)
+ '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
+ "<script>"
+ "function startTutorial() {"
+ "  introJs().setOptions({"
+ "    steps: ["
+ "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
+ "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
+ "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
+ "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
+ "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
+ "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
+ "    ],"
+ "    showProgress: true,"
+ "    nextLabel: 'Next →',"
+ "    prevLabel: '← Back',"
+ "    doneLabel: 'Got it!'"
+ "  }).start();"
+ "}"
+ "</script>"
+ "</body></html>"
)
    return HTMLResponse(html)
# -------- Add Transaction --------
@app.post("/tx")
async def add_tx(date:str=Form(...), kind:str=Form(...), amount:float=Form(...), category:str=Form(...), memo:str=Form("")):
    kind="income" if kind.lower()=="income" else "expense"
    ts=datetime.fromisoformat(parse_date(date)+"T12:00:00").isoformat(timespec="seconds")
    conn=get_db(); cur=conn.cursor()
    cur.execute("INSERT INTO transactions(ts,amount,category,memo,kind) VALUES(?,?,?,?,?)",(ts,float(amount),category,memo,kind))
    conn.commit(); conn.close()
    return RedirectResponse("/home",status_code=303)
# -------- Manage Transactions --------
@app.get("/transactions", response_class=HTMLResponse)
async def list_transactions():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, ts, amount, category, kind, memo FROM transactions ORDER BY ts DESC")
    rows = cur.fetchall(); conn.close()

    table = "".join(
        f"<tr>"
        f"<td>{r['ts']}</td><td>{r['kind']}</td><td>{r['category']}</td><td>${r['amount']:.2f}</td><td>{r['memo']}</td>"
        f"<td><a href='/transactions/edit/{r['id']}'>✏️ Edit</a> | "
        f"<a href='/transactions/delete/{r['id']}' onclick=\"return confirm('Delete this transaction?');\">🗑 Delete</a></td>"
        f"</tr>"
        for r in rows
    )

    return HTMLResponse(
    "<!doctype html><html><head>"
    + STYLE
    + "</head><body>"
    + NAVBAR
    + "<div class='card'>"
    + "<h2>Transactions</h2>"
    + "<table>"
    + "<tr><th>Date</th><th>Kind</th><th>Category</th><th>Amount</th><th>Memo</th><th>Actions</th></tr>"
    + table
    + "</table>"
    + "<form method='post' action='/transactions/clear' onsubmit=\"return confirm('Delete ALL transactions?');\">"
    + "<button type='submit' style='background:#c00; color:#fff; margin-top:1rem;'>🗑 Clear All</button>"
    + "</form>"
    + "</div>"
    + "</body></html>"
)


@app.get("/transactions/edit/{tid}", response_class=HTMLResponse)
async def edit_transaction(tid:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE id=?", (tid,))
    r = cur.fetchone(); conn.close()
    if not r: return PlainTextResponse("Not found", status_code=404)

    return HTMLResponse("<!doctype html><html><head>"
        + STYLE
        + "</head><body>"
        + NAVBAR
        + "<div class='card'>"
        + "<h2>Edit Transaction</h2>"
        + "<form method='post' action='/transactions/update/" + str(tid) + "'>"
          + "<label>Date: <input type='date' name='date' value='" + r['ts'][:10] + "'></label><br>"
          + "<label>Kind: <select name='kind'>"
            + "<option value='income' " + ("selected" if r['kind']=="income" else "") + ">Income</option>"
            + "<option value='expense' " + ("selected" if r['kind']=="expense" else "") + ">Expense</option>"
          + "</select></label><br>"
          + "<label>Category: <input name='category' value='" + r['category'] + "'></label><br>"
          + "<label>Amount: <input type='number' step='0.01' name='amount' value='" + str(r['amount']) + "'></label><br>"
          + "<label>Memo: <input name='memo' value='" + (r['memo'] or '') + "'></label><br>"
          + "<button type='submit'>Save</button>"
        + "</form>"
        + "</div>"
        + '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
+ "<script>"
+ "function startTutorial() {"
+ "  introJs().setOptions({"
+ "    steps: ["
+ "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
+ "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
+ "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
+ "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
+ "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
+ "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
+ "    ],"
+ "    showProgress: true,"
+ "    nextLabel: 'Next →',"
+ "    prevLabel: '← Back',"
+ "    doneLabel: 'Got it!'"
+ "  }).start();"
+ "}"
+ "</script>"
+ "</body></html>"

    )

@app.post("/transactions/update/{tid}")
async def update_transaction(tid:int, date:str=Form(...), kind:str=Form(...), category:str=Form(...), amount:float=Form(...), memo:str=Form("")):
    ts=datetime.fromisoformat(parse_date(date)+"T12:00:00").isoformat(timespec="seconds")
    conn=get_db(); cur=conn.cursor()
    cur.execute("UPDATE transactions SET ts=?, kind=?, category=?, amount=?, memo=? WHERE id=?",
                (ts, kind, category, amount, memo, tid))
    conn.commit(); conn.close()
    return RedirectResponse("/transactions", status_code=303)

@app.get("/transactions/delete/{tid}")
async def delete_transaction(tid:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM transactions WHERE id=?", (tid,))
    conn.commit(); conn.close()
    return RedirectResponse("/transactions", status_code=303)

@app.post("/transactions/clear")
async def clear_all_transactions():
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM transactions")
    conn.commit(); conn.close()
    return RedirectResponse("/transactions", status_code=303)
# -------- Settings --------
@app.get("/settings", response_class=HTMLResponse)
async def settings_form():
    sb = get_param("starting_balance", "0")
    return HTMLResponse("<!doctype html><html><head>"
        + STYLE
        + '<link href="https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css" rel="stylesheet">'
        + "</head><body>"
        + NAVBAR
        + "<div class='card'>"
          + "<form method='post' action='/settings'>"
            + "<label>Starting Balance: <input name='starting_balance' value='" + sb + "' type='number' step='0.01'></label>"
            + "<button type='submit'>Save</button>"
          + "</form>"
        + "</div>"
        + "<div class='card' style='margin-top:1rem;'>"
          + "<form method='post' action='/clear_budgets' onsubmit=\"return confirm('Are you sure you want to reset ALL budgets to 0?');\">"
            + "<button type='submit' style='background:#c00; color:#fff;'>🗑 Reset All Budgets</button>"
          + "</form>"
        + "</div>"
        + '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
+ "<script>"
+ "function startTutorial() {"
+ "  introJs().setOptions({"
+ "    steps: ["
+ "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
+ "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
+ "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
+ "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
+ "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
+ "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
+ "    ],"
+ "    showProgress: true,"
+ "    nextLabel: 'Next →',"
+ "    prevLabel: '← Back',"
+ "    doneLabel: 'Got it!'"
+ "  }).start();"
+ "}"
+ "</script>"
+ "</body></html>"

    )
   
@app.post("/settings")
async def settings_save(starting_balance: float = Form(0.0)):
    set_param("starting_balance", str(starting_balance))
    return RedirectResponse("/home", status_code=303)

@app.post("/clear_budgets")
async def clear_budgets():
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE budgets SET weekly_amount = 0")  # ✅ Reset instead of delete
    conn.commit(); conn.close()
    return RedirectResponse("/budgets", status_code=303)
# -------- Budgets --------
@app.get("/budgets", response_class=HTMLResponse)
async def budgets_form():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT category,weekly_amount FROM budgets ORDER BY category")
    rows = [{"category": r[0], "weekly_amount": f"{float(r[1]):.2f}"} for r in cur.fetchall()]
    conn.close()

    table = "".join(
        f"<tr><td>{r['category']}</td><td><input name='amt_{r['category']}' value='{r['weekly_amount']}'></td></tr>"
        for r in rows
    )

    return HTMLResponse("<!doctype html><html><head>"
        + STYLE
        + '<link href="https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css" rel="stylesheet">'
        + "</head><body>"
        + NAVBAR
        + "<div class='card'><form method='post' action='/budgets'>"
        + "<table><tr><th>Category</th><th>Weekly Amount</th></tr>" + table + "</table>"
        + "<button type='submit'>Save</button></form></div>"
        + '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
+ "<script>"
+ "function startTutorial() {"
+ "  introJs().setOptions({"
+ "    steps: ["
+ "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
+ "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
+ "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
+ "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
+ "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
+ "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
+ "    ],"
+ "    showProgress: true,"
+ "    nextLabel: 'Next →',"
+ "    prevLabel: '← Back',"
+ "    doneLabel: 'Got it!'"
+ "  }).start();"
+ "}"
+ "</script>"
+ "</body></html>"

    )

@app.post("/budgets")
async def budgets_save(request: Request):
    form = await request.form()
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT category FROM budgets")
    cats = [r[0] for r in cur.fetchall()]

    for c in cats:
        key = f"amt_{c}"
        if key in form:
            try:
                val = float(form[key])
                cur.execute("UPDATE budgets SET weekly_amount=? WHERE category=?", (val, c))
            except:
                pass
    conn.commit(); conn.close()
    return RedirectResponse("/home", status_code=303)


# -------- Daily View --------

@app.get("/daily", response_class=HTMLResponse)
async def daily_page(month: str = ""):
    # pick month (YYYY-MM) or use today
    today = date.today()
    if month and len(month) == 7 and month[4] == "-":
        y, m = int(month[:4]), int(month[5:])
    else:
        y, m = today.year, today.month

    labels, inc, exp, run = calendar_series_for_month(y, m)

    # safely embed arrays as JSON (so Chart.js always gets valid JS)
    labels_js = json.dumps(labels)
    inc_js    = json.dumps(inc)
    exp_js    = json.dumps(exp)
    run_js    = json.dumps(run)

    # prev/next
    prev_month = (date(y, m, 1) - timedelta(days=1)).strftime("%Y-%m")
    next_month = (date(y, m, calendar.monthrange(y, m)[1]) + timedelta(days=1)).strftime("%Y-%m")

    return HTMLResponse(
        "<!doctype html><html><head>" + STYLE 
        + '<link rel="manifest" href="/static/manifest.json">'
        + '<meta name="theme-color" content="#0a1f44">'
        + '<link rel="apple-touch-icon" href="/static/icons/icon-192.png">'
        + '<meta name="apple-mobile-web-app-capable" content="yes">'
        + '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        + '<meta name="apple-mobile-web-app-title" content="E.A.S.I.E.">'
         + '<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/icon-180.png">'
    + '<link rel="apple-touch-icon" sizes="167x167" href="/static/icons/icon-167.png">'
    + '<link rel="apple-touch-icon" sizes="152x152" href="/static/icons/icon-152.png">'
    + '<link rel="apple-touch-icon" sizes="120x120" href="/static/icons/icon-120.png">'
"""
        '<link href="https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css" rel="stylesheet">'
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
          .daily-toolbar { display:flex; align-items:center; gap:.5rem; margin:.5rem 0 1rem; }
          .daily-toolbar input { padding:.35rem .5rem; }
          #lineChart { width:100%; height:340px; }
        </style>
        </head><body>""" +
        NAVBAR +
        f"<div class='card'><h2>📈 Daily (Graph of {calendar.month_name[m]} {y})</h2>"
        f"<div class='daily-toolbar'>"
        f"  <a href='/daily?month={prev_month}'>⬅ Prev</a>"
        f"  <form method='get' action='/daily' style='display:inline'>"
        f"    <input type='month' name='month' value='{y}-{m:02d}' />"
        f"    <button type='submit'>Go</button>"
        f"  </form>"
        f"  <a href='/daily?month={next_month}'>Next ➡</a>"
        f"</div>"
        "<canvas id='lineChart'></canvas></div>"
        "<script>"
        f"const labels = {labels_js};"
        f"const inc    = {inc_js};"
        f"const exp    = {exp_js};"
        f"const run    = {run_js};"
        """
        const ctx = document.getElementById('lineChart');
        new Chart(ctx, {
          type: 'line',
          data: {
            labels,
            datasets: [
              { label: 'Income',  data: inc, borderColor: 'green', fill: false, borderWidth: 2, pointRadius: 2 },
              { label: 'Expense', data: exp, borderColor: 'red',   fill: false, borderWidth: 2, pointRadius: 2 },
              { label: 'Running', data: run, borderColor: 'blue',  fill: false, borderWidth: 3, pointRadius: 2, tension: 0.2 }
            ]
          },
          options: {
            responsive: true,
            maintainAspectRatio: true,
            plugins: {
              legend: { position: 'top' },
              tooltip: {
                intersect: false,
                mode: 'index',
                callbacks: {
                  // Show the calendar-like details in the tooltip
                  title: (items) => items[0].label,
                  afterBody: (items) => {
                    const i = items[0].dataIndex;
                    const lines = [];
                    lines.push(`Income:  $${inc[i].toFixed(2)}`);
                    lines.push(`Expense: $${exp[i].toFixed(2)}`);
                    lines.push(`Running: $${run[i].toFixed(2)}`);
                    return lines;
                  }
                }
              }
            },
            interaction: { mode: 'nearest', intersect: false },
            scales: {
              y: { beginAtZero: false }
            }
          }
        });
        </script>
        </body></html>
        """
    )

    
   # -------- Occurrences Generator --------
def generate_occurrences_for_schedule(sched, until: date):
    """Generate all occurrences from a schedule up to a cutoff date, anchored to start_date + dow."""
    occurrences = []
    start = date.fromisoformat(sched["start_date"])
    end = date.fromisoformat(sched["end_date"]) if sched["end_date"] else until

    # Align first occurrence to requested day-of-week
    if sched["dow"] is not None:
        dow = int(sched["dow"])  # 0=Mon ... 6=Sun
        offset = (dow - start.weekday()) % 7
        start = start + timedelta(days=offset)

    current = start
    while current <= end:
        occurrences.append({
            "schedule_id": sched["id"],
            "date": current.isoformat(),
            "kind": sched["kind"],
            "name": sched["name"],
            "category": sched["category"],
            "amount": sched["amount"]
        })

        if sched["frequency"] == "weekly":
            # always +7 days, stays same weekday
            current += timedelta(days=7)
        elif sched["frequency"] == "biweekly":
            # always +14 days, stays every other same weekday
            current += timedelta(days=14)
        elif sched["frequency"] == "monthly":
            # step 1 month forward, same weekday in that month
            m = current.month + 1
            y = current.year + (m-1)//12
            m = (m-1)%12 + 1
            # find first occurrence of this weekday in new month
            first_day = date(y, m, 1)
            offset = (dow - first_day.weekday()) % 7
            new_date = first_day + timedelta(days=offset)
            current = new_date
        else:  # one-time
            break

    return occurrences

def all_occurrences(until: date):
    """Return all occurrences from all active schedules until date."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM schedules WHERE active=1")
    schedules = cur.fetchall(); conn.close()

    occs = []
    for s in schedules:
        occs.extend(generate_occurrences_for_schedule(s, until))
    return occs
# -------- Calendar (navigable + styled) --------
@app.get("/calendar", response_class=HTMLResponse)
async def calendar_page(month: str = ""):
    today = date.today()

    # --- determine year and month to render ---
    if month and len(month) == 7 and month[4] == "-":
        y, m = int(month[:4]), int(month[5:])
    else:
        y, m = today.year, today.month

    start = month_start(y, m)
    end = month_end(y, m)

    # --- pull real transactions ---
    conn = get_db(); cur = conn.cursor()
    cur.execute("""
        SELECT date(ts) as d,
               SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) as inc,
               SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) as exp
        FROM transactions
        WHERE date(ts) BETWEEN ? AND ?
        GROUP BY date(ts)
    """, (start.isoformat(), end.isoformat()))
    posted = {r["d"]: (float(r["inc"] or 0), float(r["exp"] or 0)) for r in cur.fetchall()}
    conn.close()

    # --- generate occurrences from schedules ---
    occs = all_occurrences(end)
    for o in occs:
        if start.isoformat() <= o["date"] <= end.isoformat():
            inc, exp = posted.get(o["date"], (0.0, 0.0))
            if o["kind"] == "income":
                inc += float(o["amount"])
            else:
                exp += float(o["amount"])
            posted[o["date"]] = (inc, exp)

    # --- build grid aligned to weekdays ---
    days = []

    # Rollover: start this month from balance as of day before the 1st
    running = running_balance_through(start - timedelta(days=1))

    # weekday of 1st of month (Mon=0 … Sun=6)
    first_weekday = start.weekday()

    # pad with blanks before the 1st
    for _ in range(first_weekday):
        days.append(None)

    # actual month days
    for d in daterange(start, end):
        ds = d.isoformat()
        inc, exp = posted.get(ds, (0.0, 0.0))
        if inc or exp:
            running += (inc - exp)
        days.append({"date": ds, "inc": inc, "exp": exp, "running": running})

    # labels & nav
    month_name = calendar.month_name[m]
    prev_month = (date(y, m, 1) - timedelta(days=1)).strftime("%Y-%m")
    next_month = (date(y, m, calendar.monthrange(y, m)[1]) + timedelta(days=1)).strftime("%Y-%m")

    # weekday header row
    dow_labels = "".join("<div class='dow'>" + lbl + "</div>" for lbl in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"])

    # grid cells (no f-strings; use %.2f and %02d)
    grid_parts = []
    for d in days:
        if d is None:
            grid_parts.append("<div class='cell' style='background:#eee;'></div>")
        else:
            day_num = int(d["date"][8:])
            is_today = " today" if d["date"] == today.isoformat() else ""
            cell = (
                "<div class='cell" + is_today + "' onclick=\"openDayModal('" + d["date"] + "')\">"
                "<div class='date'>" + ("%02d" % day_num) + "</div>"
                "<div class='income'>+$" + ("%.2f" % d["inc"]) + "</div>"
                "<div class='expense'>-$" + ("%.2f" % d["exp"]) + "</div>"
                "<div class='savings'></div>"
                "<div class='balance'>$" + ("%.2f" % d["running"]) + "</div>"
                "</div>"
            )
            grid_parts.append(cell)
    grid = "".join(grid_parts)

    # Build static CSS safely (no f-string, no .format)
    calendar_css = """
    <style>
    h2.calendar-header {
      font-family:'Orbitron', sans-serif;
      font-size:1.8rem;
      color:#FFD700;
      text-align:center;
      margin:1rem 0;
    }
    .nav { text-align:center; margin-bottom:1rem; }
    .nav a { margin: 0 1rem; color:#FFD700; font-weight:bold; text-decoration:none; }
    .grid { display:grid; grid-template-columns: repeat(7,1fr); gap:.5rem; }
    .dow { font-weight:bold; text-align:center; color:#f5f5f0; }
    .cell { border:1px solid #ddd; padding:.5rem; border-radius:6px; background:#fff; cursor:pointer; min-height:80px; color:#111; }
    .date { font-weight:bold; color:#0a1f44; }
    .income { color:green; font-weight:bold; font-size:0.9rem; }
    .expense { color:red; font-weight:bold; font-size:0.9rem; }
    .savings { color:blue; font-weight:bold; font-size:0.9rem; }
    .balance { color:#000; font-weight:800; font-size:1.2rem; line-height:1.1; }
    .today { border: 2px solid #FFD700; background:#FFFACD; }
    .modal { display:none; position:fixed; z-index:2000; padding-top:100px; left:0; top:0; width:100%; height:100%; overflow:auto; background-color:rgba(0,0,0,0.6); }
    .modal-content { background:#0a1f44; color:#FFD700; padding:20px; border-radius:12px; border:2px solid #FFD700; width:400px; margin:auto; animation: fadein .3s; }
    .modal-content h2 { color:#FFD700; font-family:'Orbitron', sans-serif; margin-top:0; }
    .modal-content label { display:block; margin-top:.5rem; color:#FFD700; font-weight:bold; }
    .modal-content input, .modal-content select { width:100%; margin:.2rem 0 .8rem; padding:.4rem; border-radius:6px; border:1px solid #FFD700; background:#fdfdf9; color:#0a1f44; }
    .modal-content button { background:#FFD700; color:#0a1f44; font-weight:bold; padding:.6rem 1rem; border:none; border-radius:6px; cursor:pointer; width:100%; margin-top:1rem; }
    .modal-content button:hover { background:#B8860B; color:#fff; }
    .close { color:#FFD700; float:right; font-size:1.5rem; font-weight:bold; cursor:pointer; }
    .close:hover { color:#fff; }
    @keyframes fadein { from {opacity:0;} to {opacity:1;} }
    </style>
    """

    # Build HTML without f-strings or .format
    html = (
        "<!doctype html><html><head>"
        + STYLE
        + calendar_css
        + "</head><body>"
        '<link href="https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css" rel="stylesheet">'
        + NAVBAR
        + "<h2 class='calendar-header'>" + month_name + " " + str(y) + "</h2>"
        + "<div class='nav'>"
            + "<a href='/calendar?month=" + prev_month + "'>⬅ Prev</a>"
            + "<a href='/calendar?month=" + next_month + "'>Next ➡</a>"
        + "</div>"
        + "<div class='grid'>" + dow_labels + grid + "</div>"

        # Modal (with recurrence controls)
        + "<div id='dayModal' class='modal' style='display:none;'>"
          + "<div class='modal-content'>"
            + "<span class='close' onclick=\"document.getElementById('dayModal').style.display='none'\">&times;</span>"
            + "<h2>Add Item</h2>"
            + "<form method='post' action='/calendar/save' id='calForm'>"

              # Core fields
              + "<label>Date: <input type='date' name='date' required></label>"
              + "<label>Kind: "
                + "<select name='kind'>"
                  + "<option value='income'>Income</option>"
                  + "<option value='expense'>Expense</option>"
                + "</select>"
              + "</label>"
              + "<label>Category: <input type='text' name='category' required></label>"
              + "<label>Amount: <input type='number' step='0.01' name='amount' required></label>"
              + "<label>Memo: <input type='text' name='memo' placeholder='optional note'></label>"

              # Recurrence toggle
              + "<div style='margin:.5rem 0;'>"
                + "<label style='display:inline-flex;align-items:center;gap:.4rem;'>"
                  + "<input type='checkbox' id='recurringChk'> Recurring"
                + "</label>"
              + "</div>"

              # Recurrence fields
              + "<div id='recurringFields' style='display:none;'>"
                + "<label>Name: <input type='text' name='name' placeholder='e.g., Friday Paycheck or Gym membership'></label>"
                + "<label>How often: "
                  + "<select name='frequency'>"
                    + "<option value='weekly'>Weekly</option>"
                    + "<option value='biweekly'>Biweekly</option>"
                    + "<option value='monthly'>Monthly</option>"
                  + "</select>"
                + "</label>"
                + "<label>Day of Week: "
                  + "<select name='dow'>"
                    + "<option value='0'>Monday</option>"
                    + "<option value='1'>Tuesday</option>"
                    + "<option value='2'>Wednesday</option>"
                    + "<option value='3'>Thursday</option>"
                    + "<option value='4'>Friday</option>"
                    + "<option value='5'>Saturday</option>"
                    + "<option value='6'>Sunday</option>"
                  + "</select>"
                + "</label>"
                + "<label>End date (optional): <input type='date' name='end_date'></label>"
                + "<input type='hidden' name='recurring' value='no'>"
              + "</div>"

              + "<button type='submit'>Save</button>"
            + "</form>"
          + "</div>"
        + "</div>"

        # JS
        + "<script>"
          + "function openDayModal(dateStr){"
          + "  const m=document.getElementById('dayModal');"
          + "  const di=document.querySelector(\"#dayModal input[name='date']\");"
          + "  di.value=dateStr; m.style.display='block';"
          + "}"
          + "window.onclick=function(ev){ const m=document.getElementById('dayModal'); if(ev.target==m) m.style.display='none'; };"
          + "function recChk(){"
          + "  const c=document.getElementById('recurringChk');"
          + "  const f=document.getElementById('recurringFields');"
          + "  const flag=document.querySelector(\"#dayModal input[name='recurring']\");"
          + "  if(!c||!f||!flag) return;"
          + "  f.style.display = c.checked ? 'block' : 'none';"
          + "  flag.value = c.checked ? 'yes' : 'no';"
          + "}"
          + "document.addEventListener('DOMContentLoaded',function(){"
          + "  const c=document.getElementById('recurringChk'); if(c){ c.addEventListener('change', recChk); }"
          + "});"
        + "</script>"

        + "</body></html>"
    )

    return HTMLResponse(html)


   
# -------- Schedules --------
@app.get("/schedules", response_class=HTMLResponse)
async def schedules_page():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM schedules ORDER BY id DESC")
    rows = cur.fetchall(); conn.close()

    table = "".join(
        f"<tr>"
        f"<td>{r['id']}</td>"
        f"<td>{r['name']}</td>"
        f"<td>{r['category']}</td>"
        f"<td>${r['amount']:.2f}</td>"
        f"<td>{r['kind']}</td>"
        f"<td>{r['frequency']}</td>"
        f"<td>{r['start_date']}</td>"
        f"<td>{['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][r['dow']] if r['dow'] is not None else '-'}</td>"
        f"<td><a href='/schedules/edit/{r['id']}'>✏️ Edit</a> | "
        f"<a href='/schedules/delete/{r['id']}' onclick=\"return confirm('Delete this schedule?');\">🗑 Delete</a></td>"
        f"</tr>"
        for r in rows
    )

    return HTMLResponse("<!doctype html><html><head>"
        + STYLE
        + '<link href="https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css" rel="stylesheet">'
        + "</head><body>"
        + NAVBAR
        + "<div class='card'>"
          + "<h2>Add Schedule</h2>"
          + "<form method='post' action='/schedules/new'>"
            + "<label>Name: <input name='name' required></label><br>"
            + "<label>Category: <input name='category' required></label><br>"
            + "<label>Amount: <input type='number' step='0.01' name='amount' required></label><br>"
            + "<label>Kind: <select name='kind'>"
              + "<option value='income'>Income</option>"
              + "<option value='expense'>Expense</option>"
            + "</select></label><br>"
            + "<label>Frequency: <select name='frequency'>"
              + "<option value='weekly'>Weekly</option>"
              + "<option value='biweekly'>Biweekly</option>"
              + "<option value='monthly'>Monthly</option>"
            + "</select></label><br>"
            + "<label>Start Date: <input type='date' name='start_date' required></label><br>"
            + "<label>Day of Week: <select name='dow'>"
              + "<option value='0'>Monday</option>"
              + "<option value='1'>Tuesday</option>"
              + "<option value='2'>Wednesday</option>"
              + "<option value='3'>Thursday</option>"
              + "<option value='4'>Friday</option>"
              + "<option value='5'>Saturday</option>"
              + "<option value='6'>Sunday</option>"
            + "</select></label><br>"
            + "<button type='submit'>Add</button>"
          + "</form>"
        + "</div>"

        + "<div class='card'>"
          + "<h2>Schedules</h2>"
          + "<table>"
            + "<tr><th>ID</th><th>Name</th><th>Category</th><th>Amount</th><th>Kind</th><th>Freq</th><th>Start</th><th>DOW</th><th>Actions</th></tr>"
            + table +
          "</table>"
        + "</div>"

       + '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
+ "<script>"
+ "function startTutorial() {"
+ "  introJs().setOptions({"
+ "    steps: ["
+ "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
+ "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
+ "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
+ "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
+ "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
+ "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
+ "    ],"
+ "    showProgress: true,"
+ "    nextLabel: 'Next →',"
+ "    prevLabel: '← Back',"
+ "    doneLabel: 'Got it!'"
+ "  }).start();"
+ "}"
+ "</script>"
+ "</body></html>"

    )


@app.post("/schedules/new")
async def schedule_new(name:str=Form(...), category:str=Form(...), amount:float=Form(...),
                       kind:str=Form(...), frequency:str=Form(...),
                       start_date:str=Form(...), dow:int=Form(...)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""INSERT INTO schedules(kind,name,category,amount,frequency,start_date,dow)
                   VALUES(?,?,?,?,?,?,?)""",
                (kind, name, category, amount, frequency, start_date, dow))
    conn.commit(); conn.close()
    return RedirectResponse("/schedules", status_code=303)


@app.get("/schedules/edit/{sid}", response_class=HTMLResponse)
async def schedule_edit(sid:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM schedules WHERE id=?", (sid,))
    r = cur.fetchone(); conn.close()
    if not r: return PlainTextResponse("Not found", status_code=404)

    return HTMLResponse("<!doctype html><html><head>"
        + STYLE
        + '<link href="https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css" rel="stylesheet">'
        + "</head><body>"
        + NAVBAR
        + "<div class='card'>"
          + "<h2>Edit Schedule</h2>"
          + "<form method='post' action='/schedules/update/" + str(sid) + "'>"
            + "<label>Name: <input name='name' value='" + r['name'] + "'></label><br>"
            + "<label>Category: <input name='category' value='" + r['category'] + "'></label><br>"
            + "<label>Amount: <input type='number' step='0.01' name='amount' value='" + str(r['amount']) + "'></label><br>"
            + "<label>Kind: <select name='kind'>"
              + "<option value='income'" + (" selected" if r['kind']=="income" else "") + ">Income</option>"
              + "<option value='expense'" + (" selected" if r['kind']=="expense" else "") + ">Expense</option>"
            + "</select></label><br>"
            + "<label>Frequency: <select name='frequency'>"
              + "<option value='weekly'"   + (" selected" if r['frequency']=="weekly" else "")   + ">Weekly</option>"
              + "<option value='biweekly'" + (" selected" if r['frequency']=="biweekly" else "") + ">Biweekly</option>"
              + "<option value='monthly'"  + (" selected" if r['frequency']=="monthly" else "")  + ">Monthly</option>"
            + "</select></label><br>"
            + "<label>Start Date: <input type='date' name='start_date' value='" + r['start_date'] + "'></label><br>"
            + "<label>Day of Week: <select name='dow'>" + dow_options + "</select></label><br>"
            + "<button type='submit'>Save</button>"
          + "</form>"
        + "</div>"

       + '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
+ "<script>"
+ "function startTutorial() {"
+ "  introJs().setOptions({"
+ "    steps: ["
+ "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
+ "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
+ "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
+ "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
+ "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
+ "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
+ "    ],"
+ "    showProgress: true,"
+ "    nextLabel: 'Next →',"
+ "    prevLabel: '← Back',"
+ "    doneLabel: 'Got it!'"
+ "  }).start();"
+ "}"
+ "</script>"
+ "</body></html>"

    )


@app.post("/schedules/update/{sid}")
async def schedule_update(sid:int, name:str=Form(...), category:str=Form(...), amount:float=Form(...),
                          kind:str=Form(...), frequency:str=Form(...), start_date:str=Form(...), dow:int=Form(...)):
    conn = get_db(); cur = conn.cursor()
    cur.execute("""UPDATE schedules 
                   SET name=?, category=?, amount=?, kind=?, frequency=?, start_date=?, dow=? 
                   WHERE id=?""",
                (name, category, amount, kind, frequency, start_date, dow, sid))
    conn.commit(); conn.close()
    return RedirectResponse("/schedules", status_code=303)


@app.get("/schedules/delete/{sid}")
async def schedule_delete(sid:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM schedules WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return RedirectResponse("/schedules", status_code=303)


@app.post("/schedules/clear")
async def schedules_clear():
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM schedules")
    conn.commit(); conn.close()
    return RedirectResponse("/schedules", status_code=303)



# -------- Occurrence View --------
@app.get("/occurrence/{oid}", response_class=HTMLResponse)
async def occ_view(oid:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM occurrences WHERE id=?", (oid,))
    o = cur.fetchone(); conn.close()
    if not o:
        return PlainTextResponse("Not found", status_code=404)

    return HTMLResponse("<!doctype html><html><head>" + STYLE 
      + '<link rel="manifest" href="/static/manifest.json">'
      + '<meta name="theme-color" content="#0a1f44">'
      + '<link rel="apple-touch-icon" href="/static/icons/icon-192.png">'
      + '<meta name="apple-mobile-web-app-capable" content="yes">'
      + '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
      + '<meta name="apple-mobile-web-app-title" content="E.A.S.I.E.">'
      + '<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/icon-180.png">'
      + '<link rel="apple-touch-icon" sizes="167x167" href="/static/icons/icon-167.png">'
      + '<link rel="apple-touch-icon" sizes="152x152" href="/static/icons/icon-152.png">'
      + '<link rel="apple-touch-icon" sizes="120x120" href="/static/icons/icon-120.png">'
    + "</head><body>"
    + NAVBAR
    + "<div class='card'>"
      + "<h2>" + o['name'] + " on " + o['date'] + "</h2>"
      + "<p>Amount: $" + str(o['amount']) + " | Category: " + o['category'] + " | Status: " + o['status'] + "</p>"
    + "</div>"
    + '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
+ "<script>"
+ "function startTutorial() {"
+ "  introJs().setOptions({"
+ "    steps: ["
+ "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
+ "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
+ "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
+ "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
+ "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
+ "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
+ "    ],"
+ "    showProgress: true,"
+ "    nextLabel: 'Next →',"
+ "    prevLabel: '← Back',"
+ "    doneLabel: 'Got it!'"
+ "  }).start();"
+ "}"
+ "</script>"
+ "</body></html>"

)
# -------- Glossary --------
@app.get("/glossary", response_class=HTMLResponse)
async def glossary_page():
    glossary = [
        ("Budget", "A weekly limit you set for each category (e.g., Food, Rent). Used to track overspending."),
        ("Transaction", "A single income or expense entry you add. Includes date, amount, category, and memo."),
        ("Income", "Money coming in (e.g., paycheck, side gig)."),
        ("Expense", "Money going out (e.g., groceries, rent, subscriptions)."),
        ("Schedule", "A recurring rule for income or expense (e.g., $1000 every Friday as paycheck)."),
        ("Occurrence", "An individual instance of a schedule (e.g., your paycheck on 2025-09-05)."),
        ("Daily View", "Breakdown of your income/expenses day by day, including running balance."),
        ("Calendar", "Month view showing income, expenses, and running balance for each day."),
        ("Starting Balance", "Your initial amount of money before tracking transactions."),
        ("Clear Transactions", "Removes ALL your past transactions (resets history)."),
        ("Clear Schedules", "Deletes all recurring rules (like paychecks or bills).")
    ]

    rows = "".join(f"<tr><td><strong>{term}</strong></td><td>{desc}</td></tr>" for term, desc in glossary)

    return HTMLResponse("<!doctype html><html><head>" + STYLE 
      + '<link rel="manifest" href="/static/manifest.json">'
      + '<meta name="theme-color" content="#0a1f44">'
      + '<link rel="apple-touch-icon" href="/static/icons/icon-192.png">'
      + '<meta name="apple-mobile-web-app-capable" content="yes">'
      + '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
      + '<meta name="apple-mobile-web-app-title" content="E.A.S.I.E.">'
    + '<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/icon-180.png">'
    + '<link rel="apple-touch-icon" sizes="167x167" href="/static/icons/icon-167.png">'
    + '<link rel="apple-touch-icon" sizes="152x152" href="/static/icons/icon-152.png">'
    + '<link rel="apple-touch-icon" sizes="120x120" href="/static/icons/icon-120.png">'
    + "</head><body>"
    + "<link href='https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css' rel='stylesheet'>"
    + NAVBAR
    + "<div class='card'>"
      + "<h2>📖 Glossary / Index</h2>"
      + "<p>Quick reference for terms used in E.A.S.I.E.</p>"
      + "<table>"
        + "<tr><th>Term</th><th>Definition</th></tr>"
        + rows
      + "</table>"
    + "</div>"
    + '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
+ "<script>"
+ "function startTutorial() {"
+ "  introJs().setOptions({"
+ "    steps: ["
+ "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
+ "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
+ "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
+ "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
+ "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
+ "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
+ "    ],"
+ "    showProgress: true,"
+ "    nextLabel: 'Next →',"
+ "    prevLabel: '← Back',"
+ "    doneLabel: 'Got it!'"
+ "  }).start();"
+ "}"
+ "</script>"
+ "</body></html>"

)
GLOSSARY = {
    "Budget": "A weekly limit you set for each category (e.g., Food, Rent).",
    "Transaction": "A single income or expense entry you add. Includes date, amount, category, and memo.",
    "Income": "Money coming in (e.g., paycheck, side gig).",
    "Expense": "Money going out (e.g., groceries, rent, subscriptions).",
    "Schedule": "A recurring rule for income or expense (e.g., $1000 every Friday as paycheck).",
    "Occurrence": "An individual instance of a schedule (e.g., paycheck on 2025-09-05).",
    "Daily View": "Breakdown of your income/expenses day by day, including running balance.",
    "Calendar": "Month view showing income, expenses, and running balance for each day.",
    "Starting Balance": "Your initial amount of money before tracking transactions.",
    "Clear Transactions": "Removes ALL your past transactions (resets history).",
    "Clear Schedules": "Deletes all recurring rules (like paychecks or bills)."
}

def tooltip(term: str) -> str:
    desc = GLOSSARY.get(term, "")
    if not desc: 
        return term
    return f'<span class="tooltip">{term}<span class="tooltiptext">{desc}</span></span>'
# -------- Wishlist --------
@app.get("/wishlist", response_class=HTMLResponse)
async def wishlist_page():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT id, item, category, price, target_date, created_at FROM wishlist ORDER BY created_at DESC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    # compute health for each row
    enriched = []
    for r in rows:
        h = compute_wish_health(r["item"], r["category"], float(r["price"]), r["target_date"])
        enriched.append({**r, "health": h})

    # Build table rows
    def badge(h):
        cls = "health-green" if h["status"]=="green" else ("health-yellow" if h["status"]=="yellow" else "health-red")
        extra = ""
        if h["status"]=="green" and h["warn90"]:
            extra = f""" <button class="warn-btn" title="This fits this week’s budget, but goes negative within 90 days. Click for details."
                         onclick="toggleWarn(this)">⚠</button>
                         <div class='warn-panel' style='display:none'>
                           <p><strong>Warning:</strong> Running balance goes negative.</p>
                           <ul>
                             <li>First negative day: {h['warn_details']['first_negative_day'] or '—'}</li>
                             <li>Lowest balance: ${h['warn_details']['min_balance']:.2f} on {h['warn_details']['min_balance_day']}</li>
                           </ul>
                         </div>"""
        return f"<span class='health-badge {cls}'>{h['status'].upper()}</span>{extra}"

    rows_html = "".join(f"""
      <tr>
        <td>{r['item']}</td>
        <td>{r['category']}</td>
        <td>${float(r['price']):.2f}</td>
        <td>{r['target_date']}</td>
        <td>{badge(r['health'])}</td>
        <td style="max-width:380px">
          <div><em>{r['health']['message']}</em></div>
          <div style="opacity:.8;margin-top:.2rem">{r['health']['alternative']}</div>
        </td>
        <td>
          <a href="/wishlist/edit/{r['id']}">✏️ Edit</a>
          &nbsp;|&nbsp;
          <form method="post" action="/wishlist/delete/{r['id']}" style="display:inline" onsubmit="return confirm('Delete this wish?');">
            <button type="submit" style="background:#c00;color:#fff;border:none;border-radius:6px;padding:.25rem .5rem">🗑</button>
          </form>
          <br>
          <form method="post" action="/wishlist/import/{r['id']}" style="margin-top:.4rem"
                {"onsubmit=\"return confirm('This purchase is "+r['health']['status'].upper()+" — are you sure you want to schedule it?');\"" if r['health']['status'] in ('yellow','red') else ""}>
            <button type="submit">📆 Import to Schedule</button>
          </form>
        </td>
      </tr>
    """ for r in enriched)

    return HTMLResponse(
    "<!doctype html><html><head>" 
    + STYLE
    + """
    <style>
      .health-badge {
        display:inline-block; padding:.1rem .5rem; border-radius:8px; font-weight:800; letter-spacing:.02em;
      }
      .health-green  { background:#eaffea; color:#0b6115; box-shadow:0 0 10px rgba(0,255,0,.35); }
      .health-yellow { background:#fff8d9; color:#7a6400; box-shadow:0 0 10px rgba(255,215,0,.35); }
      .health-red    { background:#ffe8e8; color:#7d0a0a; box-shadow:0 0 10px rgba(255,0,0,.35); }
      .warn-btn {
        margin-left:.35rem; border:none; background:transparent; cursor:pointer; font-size:1rem;
      }
      .warn-panel {
        background:#0a1f44; color:#FFD700; border:1px solid #FFD700; border-radius:8px; padding:.4rem .6rem; margin-top:.35rem;
      }
      .wl-form label { display:inline-block; margin:.25rem .4rem .25rem 0; }
      .wl-form input, .wl-form select { padding:.4rem; border-radius:6px; border:1px solid #888; }
    </style>
    <link href="https://cdn.jsdelivr.net/npm/intro.js/minified/introjs.min.css" rel="stylesheet">
    </head><body>
    """
    + NAVBAR
    + """
    <div class="card">
      <h2>📝 Wishlist</h2>
      <form class="wl-form" method="post" action="/wishlist/new">
        <label>Item <input name="item" required></label>
        <label>Category <input name="category" placeholder="e.g., entertainment" required></label>
        <label>Price $ <input type="number" step="0.01" name="price" required></label>
        <label>Target Date <input type="date" name="target_date" value=""" + "\"" + date.today().isoformat() + "\"" + """ required></label>
        <button type="submit">Add</button>
      </form>
    </div>
    <div class="card">
      <table>
        <tr><th>Item</th><th>Category</th><th>Price</th><th>Target Date</th><th>Health</th><th>Why / Alternatives</th><th>Actions</th></tr>
        """
    + (rows_html if rows_html else "<tr><td colspan='7' style='opacity:.7'>No wishes yet — add one above.</td></tr>")
    + """
      </table>
    </div>
    <script>
      function toggleWarn(btn){
        const panel = btn.nextElementSibling;
        panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
      }
    </script>
    """
    + '<script src="https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js"></script>'
    + "<script>"
    + "function startTutorial() {"
    + "  introJs().setOptions({"
    + "    steps: ["
    + "      { element: document.querySelector('#nav-home'), intro: 'Your Dashboard — see today’s balance and budgets.' },"
    + "      { element: document.querySelector('#nav-daily'), intro: 'Daily View — charts your income, expenses, and running balance.' },"
    + "      { element: document.querySelector('#nav-calendar'), intro: 'Calendar — month-by-month view of income, expenses, and balances.' },"
    + "      { element: document.querySelector('#nav-wishlist'), intro: 'Wishlist — track things you want to buy and see if they fit your budget.' },"
    + "      { element: document.querySelector('#add-transaction'), intro: 'Add Transaction — record income or expenses here.' },"
    + "      { element: document.querySelector('#nav-advisor'), intro: 'AI Advisor — get insights and feedback on your financial picture.' }"
    + "    ],"
    + "    showProgress: true,"
    + "    nextLabel: 'Next →',"
    + "    prevLabel: '← Back',"
    + "    doneLabel: 'Got it!'"
    + "  }).start();"
    + "}"
    + "</script>"
    + "</body></html>"
)

@app.post("/wishlist/new")
async def wishlist_new(item: str = Form(...), category: str = Form(...), price: float = Form(...), target_date: str = Form(...)):
    # normalize date
    td = datetime.fromisoformat(parse_date(target_date)).date().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO wishlist(item,category,price,target_date) VALUES(?,?,?,?)",
                (item.strip(), category.strip().lower(), float(price), td))
    conn.commit(); conn.close()
    return RedirectResponse("/wishlist", status_code=303)

@app.get("/wishlist/edit/{wid}", response_class=HTMLResponse)
async def wishlist_edit(wid: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM wishlist WHERE id=?", (wid,))
    r = cur.fetchone(); conn.close()
    if not r:
        return PlainTextResponse("Not found", status_code=404)

    return HTMLResponse("<!doctype html><html><head>" + STYLE 
        + '<link rel="manifest" href="/static/manifest.json">'
        + '<meta name="theme-color" content="#0a1f44">'
        + '<link rel="apple-touch-icon" href="/static/icons/icon-192.png">'
        + '<meta name="apple-mobile-web-app-capable" content="yes">'
        + '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">'
        + '<meta name="apple-mobile-web-app-title" content="E.A.S.I.E.">'
     + '<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/icon-180.png">'
    + '<link rel="apple-touch-icon" sizes="167x167" href="/static/icons/icon-167.png">'
    + '<link rel="apple-touch-icon" sizes="152x152" href="/static/icons/icon-152.png">'
    + '<link rel="apple-touch-icon" sizes="120x120" href="/static/icons/icon-120.png">'

    + "</head><body>"
    + NAVBAR
    + "<div class='card'>"
      + "<h2>Edit Wish</h2>"
      + "<form method='post' action='/wishlist/update/" + str(wid) + "'>"
        + "<label>Item <input name='item' value='" + r['item'] + "' required></label><br>"
        + "<label>Category <input name='category' value='" + r['category'] + "' required></label><br>"
        + "<label>Price $ <input type='number' step='0.01' name='price' value='" + str(r['price']) + "' required></label><br>"
        + "<label>Target Date <input type='date' name='target_date' value='" + r['target_date'] + "' required></label><br>"
        + "<button type='submit'>Save</button>"
        + "<a href='/wishlist' style='margin-left:.5rem'>Cancel</a>"
      + "</form>"
    + "</div>"
    + "<script src='https://cdn.jsdelivr.net/npm/intro.js/minified/intro.min.js'></script>"
    + "<script>"
      "function startTutorial() {"
      "  introJs().setOptions({"
      "    steps: ["
      "      { element: document.querySelector('#nav-home'), intro: \"Your Dashboard — see today’s balance and budgets.\" },"
      "      { element: document.querySelector('#nav-daily'), intro: \"Daily View — charts your income, expenses, and running balance.\" },"
      "      { element: document.querySelector('#nav-calendar'), intro: \"Calendar — month-by-month view of income, expenses, and balances.\" },"
      "      { element: document.querySelector('#nav-wishlist'), intro: \"Wishlist — track things you want to buy and see if they fit your budget.\" },"
      "      { element: document.querySelector('#add-transaction'), intro: \"Add Transaction — record income or expenses here.\" },"
      "      { element: document.querySelector('#nav-advisor'), intro: \"AI Advisor — get insights and feedback on your financial picture.\" }"
      "    ],"
      "    showProgress: true,"
      "    nextLabel: 'Next →',"
      "    prevLabel: '← Back',"
      "    doneLabel: 'Got it!'"
      "  }).start();"
      "}"
    + "</script>"
    + "</body></html>"
)

@app.post("/wishlist/update/{wid}")
async def wishlist_update(wid: int, item: str = Form(...), category: str = Form(...), price: float = Form(...), target_date: str = Form(...)):
    td = datetime.fromisoformat(parse_date(target_date)).date().isoformat()
    conn = get_db(); cur = conn.cursor()
    cur.execute("UPDATE wishlist SET item=?, category=?, price=?, target_date=? WHERE id=?",
                (item.strip(), category.strip().lower(), float(price), td, wid))
    conn.commit(); conn.close()
    return RedirectResponse("/wishlist", status_code=303)

@app.post("/wishlist/delete/{wid}")
async def wishlist_delete(wid: int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("DELETE FROM wishlist WHERE id=?", (wid,))
    conn.commit(); conn.close()
    return RedirectResponse("/wishlist", status_code=303)

@app.post("/wishlist/import/{wid}")
async def wishlist_import_schedule(wid: int):
    """Create a one-time schedule from this wish (expense on its target_date)."""
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM wishlist WHERE id=?", (wid,))
    r = cur.fetchone()
    if not r:
        conn.close(); return PlainTextResponse("Not found", status_code=404)

    # Insert as a one-time schedule (frequency can be any non-weekly/biweekly/monthly string)
    start_d = datetime.fromisoformat(r["target_date"]).date().isoformat()
    cur.execute("""INSERT INTO schedules(
                     kind, name, category, amount, frequency, dow, dom, start_date, end_date, anchor_date, color, notes, active
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                ("expense", r["item"], r["category"], float(r["price"]),
                 "one-time", None, None, start_d, start_d, None, "", "from wishlist",))
    conn.commit(); conn.close()
    return RedirectResponse("/schedules", status_code=303)
    
@app.get("/api/today_balance")
async def api_today_balance():
    return {"balance": running_balance_through(date.today())}
    
@app.post("/api/ai_chat")
async def ai_chat(request: Request):
    data = await request.json()
    user_msg = data.get("message", "")

    # Pull context from DB
    conn = get_db(); cur = conn.cursor()

    # Example: monthly category spend
    cur.execute("""
        SELECT category,
               SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) as spent,
               SUM(CASE WHEN kind='income'  THEN amount ELSE 0 END) as earned
        FROM transactions
        WHERE strftime('%Y-%m', ts) = strftime('%Y-%m', 'now')
        GROUP BY category
    """)
    cat_rows = cur.fetchall()

    cur.execute("SELECT SUM(amount) FROM transactions WHERE kind='expense'")
    total_exp = float(cur.fetchone()[0] or 0)

    cur.execute("SELECT SUM(amount) FROM transactions WHERE kind='income'")
    total_inc = float(cur.fetchone()[0] or 0)

    conn.close()

    # Build context summary
    category_summary = ", ".join([f"{r['category']}: ${r['spent']:.2f}" for r in cat_rows])
    context = f"""
    Current month income: ${total_inc:.2f}, expenses: ${total_exp:.2f}.
    Breakdown by category: {category_summary}.
    User message: {user_msg}
    """

    # Restrictive system prompt
    system_msg = """You are EASIE’s built-in AI financial insights assistant.
    You ONLY analyze the user’s spending, budgets, and balances based on data provided.
    You NEVER give investment advice, tax advice, or specific financial directives.
    You only provide INSIGHT, TRENDS, and CONTEXT (e.g., overspending in categories, balance projections, savings opportunities).
    Stay concise, practical, and easy to read."""

    
@app.get("/advisor", response_class=HTMLResponse)
async def advisor_placeholder():
    return HTMLResponse(
        "<!doctype html><html><head>"
        + STYLE
        + """
        <style>
          #interestBtn { 
            margin-top:1rem; 
            background:#FFD700; 
            color:#0a1f44; 
            font-weight:bold; 
            padding:.6rem 1.2rem; 
            border-radius:6px; 
            cursor:pointer; 
          }
          #interestBtn:hover { background:#B8860B; color:#fff; }
        </style>
        </head><body>
        """
        + NAVBAR
        + """
        <div class='card' style='text-align:center; padding:2rem;'>
          <h2>🤖 AI Finance Advisor</h2>
          <p style='font-size:1.2rem; margin-top:1rem;'>
            This feature is <strong>under construction</strong>.
          </p>
          <p style='margin-top:1rem; font-size:1rem; opacity:.85;'>
            Upgrade to <span style='color:#FFD700; font-weight:bold;'>E.A.S.I.E. Pro</span>
            to unlock AI-powered financial insights.
          </p>

          <button id="interestBtn" onclick="submitInterest()">✅ I’m Interested</button>
          <p style="margin-top:1rem; font-size:1.1rem;">
            <span id="interestCount">0</span> people have signed up!
          </p>
        </div>

        <script>
        async function fetchCount() {
          try {
            let r = await fetch("/api/advisor_interest_count");
            let j = await r.json();
            document.getElementById("interestCount").textContent = j.count;
          } catch(e) {
            console.error("Failed to fetch count", e);
          }
        }

        async function submitInterest() {
          try {
            let r = await fetch("/advisor/interest", {method:"POST"});
            if(r.ok){
              fetchCount(); // refresh counter
              alert("✅ Thanks for your interest! You’ve been counted.");
            } else {
              alert("⚠️ Couldn’t record your interest. Try again later.");
            }
          } catch(e) {
            alert("⚠️ Network error.");
          }
        }

        document.addEventListener("DOMContentLoaded", fetchCount);
        </script>
        </body></html>
        """
    )

@app.post("/advisor/interest")
async def advisor_interest():
    conn = get_db(); cur = conn.cursor()
    cur.execute("INSERT INTO advisor_interest DEFAULT VALUES")
    conn.commit(); conn.close()
    return {"ok": True}  # keep it simple for JS

@app.get("/api/advisor_interest_count")
async def advisor_interest_count():
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM advisor_interest")
    count = cur.fetchone()[0]
    conn.close()
    return {"count": count}


# -------- Health --------
@app.get("/health")
async def health(): return {"ok":True}

# -------- Entrypoint --------
@app.on_event("startup")
def _startup(): init_db()

if __name__=="__main__":
    init_db(); import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=8000,reload=True)
