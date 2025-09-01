
"""E.A.S.I.E. — Expense & Savings–Income Engine (FastAPI Full)"""

import os, io, csv, calendar, sqlite3
from datetime import date, datetime, timedelta
from typing import Dict, Optional, Tuple, List

from fastapi import FastAPI, Form, UploadFile, File, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles   # ✅ add this
from pydantic import BaseModel, condecimal
from jinja2 import Environment, FileSystemLoader, select_autoescape

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
    conn.commit()

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
  color: #0a1f44;
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
/* ✅ Tooltip style */
.tooltip {
  position: relative;
  cursor: help;
  border-bottom: 1px dotted #FFD700; /* gold underline */
}

.tooltip .tooltiptext {
  visibility: hidden;
  width: 220px;
  background-color: #0a1f44; /* navy */
  color: #f5f5f0;           /* off-white */
  text-align: left;
  border-radius: 6px;
  padding: 0.5rem;
  position: absolute;
  z-index: 1001;
  bottom: 125%; /* show above word */
  left: 50%;
  margin-left: -110px;
  opacity: 0;
  transition: opacity 0.3s;
  border: 1px solid #FFD700;
}

.tooltip:hover .tooltiptext {
  visibility: visible;
  opacity: 1;
}
</style>
"""
# -------- Navbar (shared across all pages) --------
NAVBAR = """
<header>
  <h1>E.A.S.I.E</h1>
  <small>(Expense & Savings / Income Engine)</small><br>
  <a href="/home">🏠 Home</a>
  <a href="/budgets">Budgets</a>
  <a href="/daily">Daily</a>
  <a href="/calendar">Calendar</a>
  <a href="/schedules">Schedules</a>
  <a href="/settings">Settings</a>
  <a href="/glossary">Glossary</a>

</header>
"""
## -------- Landing animation --------
WELCOME_HTML = """<!doctype html>
<html>
<head>
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
    today = date.today(); ws, we = week_bounds(today)

    # 1. Running balance
    conn = get_db(); cur = conn.cursor()
    cur.execute("""SELECT 
                     SUM(CASE WHEN kind='income' THEN amount ELSE 0 END) -
                     SUM(CASE WHEN kind='expense' THEN amount ELSE 0 END) +
                     SUM(CASE WHEN category='savings' THEN amount ELSE 0 END)
                  FROM transactions""")
    total = float(cur.fetchone()[0] or 0)
    start_bal = float(get_param("starting_balance", "0") or 0)
    balance = start_bal + total

    # 2. Weekly budgets left
    cur.execute("SELECT category,weekly_amount FROM budgets ORDER BY category")
    rows = cur.fetchall()
    budgets = []
    for r in rows:
        wk = float(r[1])
        spent = spend_this_week(r[0], ws, we)
        budgets.append({
            "category": r[0],
            "weekly_amount": wk,
            "spent": spent,
            "remaining": max(wk - spent, 0)
        })
    conn.close()

    # HTML Dashboard
    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <h2>📊 Dashboard</h2>
    <div class="card">
      <h3>Today's Balance</h3>
      <p style="font-size:1.5rem; font-weight:bold; color:#0a1f44;">${balance:.2f}</p>
    </div>

    <div class="card">
      <h3>🎯 Wish List</h3>
      <table>
        <tr><th>Item</th><th>Target $</th><th>Status</th></tr>
        <tr><td>Example Item</td><td>$500</td><td>⏳</td></tr>
      </table>
    </div>

    <div class="card">
      <h3>📅 Week {today.isocalendar()[1]} Budgets Left</h3>
      <table>
        <tr><th>Category</th><th>Weekly Budget</th><th>Remaining</th></tr>
        {''.join(f"<tr><td>{b['category']}</td><td>${b['weekly_amount']:.2f}</td><td>${b['remaining']:.2f}</td></tr>" for b in budgets)}
      </table>
    </div>
    </body></html>""")


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

    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <div class="card">
      <h2>Transactions</h2>
      <table>
        <tr><th>Date</th><th>Kind</th><th>Category</th><th>Amount</th><th>Memo</th><th>Actions</th></tr>
        {table}
      </table>
      <form method="post" action="/transactions/clear" onsubmit="return confirm('Delete ALL transactions?');">
        <button type="submit" style="background:#c00; color:#fff; margin-top:1rem;">🗑 Clear All</button>
      </form>
    </div>
    </body></html>""")

@app.get("/transactions/edit/{tid}", response_class=HTMLResponse)
async def edit_transaction(tid:int):
    conn = get_db(); cur = conn.cursor()
    cur.execute("SELECT * FROM transactions WHERE id=?", (tid,))
    r = cur.fetchone(); conn.close()
    if not r: return PlainTextResponse("Not found", status_code=404)

    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <div class="card">
    <h2>Edit Transaction</h2>
    <form method="post" action="/transactions/update/{tid}">
      <label>Date: <input type="date" name="date" value="{r['ts'][:10]}"></label><br>
      <label>Kind: <select name="kind">
        <option value="income" {"selected" if r['kind']=="income" else ""}>Income</option>
        <option value="expense" {"selected" if r['kind']=="expense" else ""}>Expense</option>
      </select></label><br>
      <label>Category: <input name="category" value="{r['category']}"></label><br>
      <label>Amount: <input type="number" step="0.01" name="amount" value="{r['amount']}"></label><br>
      <label>Memo: <input name="memo" value="{r['memo'] or ''}"></label><br>
      <button type="submit">Save</button>
    </form>
    </div></body></html>""")

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
    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <div class="card">
      <form method="post" action="/settings">
        <label>Starting Balance: <input name="starting_balance" value="{sb}" type="number" step="0.01"></label>
        <button type="submit">Save</button>
      </form>
    </div>
    <div class="card" style="margin-top:1rem;">
      <form method="post" action="/clear_budgets" onsubmit="return confirm('Are you sure you want to reset ALL budgets to 0?');">
        <button type="submit" style="background:#c00; color:#fff;">🗑 Reset All Budgets</button>
      </form>
    </div>
    </body></html>""")

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

    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <div class="card"><form method="post" action="/budgets">
    <table><tr><th>Category</th><th>Weekly Amount</th></tr>{table}</table>
    <button type="submit">Save</button></form></div></body></html>""")

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
async def daily_page():
    days = daily_aggregates()
    running = 0.0   # ✅ start clean, no carry-over
    data = []

    for d in days:
        net = d["income"] - d["expense"]
        running += net if (d["income"] or d["expense"]) else 0  # ✅ only move if there’s activity
        data.append({
            "date": d["date"],
            "income": d["income"],
            "expense": d["expense"],
            "net": net,
            "running": running
        })

    labels = [d["date"] for d in data]
    inc = [d["income"] for d in data]
    exp = [d["expense"] for d in data]
    run = [d["running"] for d in data]

    table = "".join(
        f"<tr><td>{d['date']}</td><td>${d['income']:.2f}</td><td>${d['expense']:.2f}</td>"
        f"<td>${d['net']:.2f}</td><td>${d['running']:.2f}</td></tr>"
        for d in data
    )

    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    </head><body>
    {NAVBAR}
    <div class="card"><canvas id="lineChart"></canvas></div>
    <div class="card">
    <table><tr><th>Date</th><th>Income</th><th>Expense</th><th>Net</th><th>Running</th></tr>{table}</table>
    </div>
    <script>
    const ctx = document.getElementById('lineChart');
    new Chart(ctx, {{
      type:'line',
      data:{{labels:{labels},datasets:[
        {{label:'Income',data:{inc},borderColor:'green',fill:false}},
        {{label:'Expense',data:{exp},borderColor:'red',fill:false}},
        {{label:'Running',data:{run},borderColor:'blue',fill:false}}
      ]}},
      options:{{responsive:true,plugins:{{legend:{{position:'top'}}}}}}
    }});
    </script>
    </body></html>""")
    
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
            inc, exp = posted.get(o["date"], (0, 0))
            if o["kind"] == "income":
                inc += o["amount"]
            else:
                exp += o["amount"]
            posted[o["date"]] = (inc, exp)

    # --- build grid aligned to weekdays ---
    days = []
    running = float(get_param("starting_balance", "0") or 0)

    # figure out weekday of 1st of month (Mon=0 … Sun=6)
    first_weekday = start.weekday()

    # pad with blanks before the 1st
    for _ in range(first_weekday):
        days.append(None)

    # actual month days
    for d in daterange(start, end):
        ds = d.isoformat()
        inc, exp = posted.get(ds, (0, 0))
        if inc or exp:
            running += (inc - exp)
        days.append({"date": ds, "inc": inc, "exp": exp, "running": running})

    # --- render HTML grid ---
    dow_labels = "".join(f"<div class='dow'>{x}</div>" for x in ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"])
    grid = ""
    for d in days:
        if d is None:
            grid += "<div class='cell' style='background:#eee;'></div>"
        else:
            grid += (
                f"<div class='cell{' today' if d['date']==today.isoformat() else ''}' "
                f"onclick=\"openDayModal('{d['date']}')\">"
                f"<div class='date'>{int(d['date'][8:]):02d}</div>"
                f"<div class='income'>+${d['inc']:.2f}</div>"
                f"<div class='expense'>-${d['exp']:.2f}</div>"
                f"<div class='savings'></div>"
                f"<div class='balance'>${d['running']:.2f}</div>"
                f"</div>"
            )

    month_name = calendar.month_name[m]
    prev_month = (date(y, m, 1) - timedelta(days=1)).strftime("%Y-%m")
    next_month = (date(y, m, calendar.monthrange(y, m)[1]) + timedelta(days=1)).strftime("%Y-%m")

    html = """<!doctype html><html><head>{STYLE}
    <style>
    h2.calendar-header {{
      font-family:'Orbitron', sans-serif;
      font-size:1.8rem;
      color:#FFD700;
      text-align:center;
      margin:1rem 0;
    }}
    .nav {{ text-align:center; margin-bottom:1rem; }}
    .nav a {{ margin:0 1rem; color:#FFD700; font-weight:bold; text-decoration:none; }}
    .grid {{ display:grid; grid-template-columns: repeat(7,1fr); gap:.5rem; }}
    .dow {{ font-weight:bold; text-align:center; color:#f5f5f0; }}
    .cell {{ border:1px solid #ddd; padding:.5rem; border-radius:6px; background:#fff; cursor:pointer; min-height:80px; }}
    .date {{ font-weight:bold; color:#0a1f44; }}
    .income {{ color:green; font-weight:bold; font-size:0.9rem; }}
    .expense {{ color:red; font-weight:bold; font-size:0.9rem; }}
    .savings {{ color:blue; font-weight:bold; font-size:0.9rem; }}
    .balance {{ color:#0a1f44; font-weight:bold; font-size:1rem; margin-top:0.3rem; }}
    .modal {{ display:none; position:fixed; z-index:2000; padding-top:100px; left:0; top:0; width:100%; height:100%; overflow:auto; background-color:rgba(0,0,0,0.6); }}
    .modal-content {{ background:#0a1f44; color:#FFD700; padding:20px; border-radius:12px; border:2px solid #FFD700; width:400px; margin:auto; animation: fadein .3s; }}
    .modal-content h2 {{ color:#FFD700; font-family:'Orbitron', sans-serif; margin-top:0; }}
    .modal-content label {{ display:block; margin-top:.5rem; color:#FFD700; font-weight:bold; }}
    .modal-content input, .modal-content select {{ width:100%; margin:.2rem 0 .8rem; padding:.4rem; border-radius:6px; border:1px solid #FFD700; background:#fdfdf9; color:#0a1f44; }}
    .modal-content button {{ background:#FFD700; color:#0a1f44; font-weight:bold; padding:.6rem 1rem; border:none; border-radius:6px; cursor:pointer; width:100%; margin-top:1rem; }}
    .modal-content button:hover {{ background:#B8860B; color:#fff; }}
    .close {{ color:#FFD700; float:right; font-size:1.5rem; font-weight:bold; cursor:pointer; }}
    .close:hover {{ color:#fff; }}
    @keyframes fadein {{ from {{opacity:0;}} to {{opacity:1;}} }}
    </style>
    </head><body>
    {NAVBAR}
    <h2 class="calendar-header">{month_name} {y}</h2>
    <div class="nav">
      <a href="/calendar?month={prev_month}">⬅ Prev</a>
      <a href="/calendar?month={next_month}">Next ➡</a>
    </div>
    <div class="grid">{dow_labels}{grid}</div>
    <!-- Modal -->
    <div id="dayModal" class="modal" style="display:none;">
      <div class="modal-content">
        <span class="close" onclick="document.getElementById('dayModal').style.display='none'">&times;</span>
        <h2>Add Transaction</h2>
        <form method="post" action="/tx">
          <label>Date: <input type="date" name="date" required></label>
          <label>Category: <input type="text" name="category"></label>
          <label>Amount: <input type="number" step="0.01" name="amount"></label>
          <label>Kind:
            <select name="kind">
              <option value="income">Income</option>
              <option value="expense">Expense</option>
            </select>
          </label>
          <label>Memo: <input type="text" name="memo"></label>
          <button type="submit">Save</button>
        </form>
      </div>
    </div>
    <script>
    function openDayModal(dateStr) {{
      const modal = document.getElementById("dayModal");
      const dateInput = document.querySelector("#dayModal input[name='date']");
      dateInput.value = dateStr;
      modal.style.display = "block";
    }}
    window.onclick = function(event) {{
      const modal = document.getElementById("dayModal");
      if (event.target == modal) modal.style.display = "none";
    }}
    </script>
    </body></html>"""

    return HTMLResponse(html.format(
        STYLE=STYLE,
        NAVBAR=NAVBAR,
        month_name=month_name,
        y=y,
        prev_month=prev_month,
        next_month=next_month,
        dow_labels=dow_labels,
        grid=grid
    ))

   
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

    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <div class="card">
      <h2>Add Schedule</h2>
      <form method="post" action="/schedules/new">
        <label>Name: <input name="name" required></label><br>
        <label>Category: <input name="category" required></label><br>
        <label>Amount: <input type="number" step="0.01" name="amount" required></label><br>
        <label>Kind: 
          <select name="kind">
            <option value="income">Income</option>
            <option value="expense">Expense</option>
          </select>
        </label><br>
        <label>Frequency:
          <select name="frequency">
            <option value="weekly">Weekly</option>
            <option value="biweekly">Biweekly</option>
            <option value="monthly">Monthly</option>
          </select>
        </label><br>
        <label>Start Date: <input type="date" name="start_date" required></label><br>
        <label>Day of Week:
          <select name="dow">
            <option value="0">Monday</option>
            <option value="1">Tuesday</option>
            <option value="2">Wednesday</option>
            <option value="3">Thursday</option>
            <option value="4">Friday</option>
            <option value="5">Saturday</option>
            <option value="6">Sunday</option>
          </select>
        </label><br>
        <button type="submit">Add</button>
      </form>
    </div>

    <div class="card">
      <h2>Schedules</h2>
      <table>
        <tr><th>ID</th><th>Name</th><th>Category</th><th>Amount</th><th>Kind</th><th>Freq</th><th>Start</th><th>DOW</th><th>Actions</th></tr>
        {table}
      </table>
    </div>
    </body></html>""")


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

    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <div class="card">
      <h2>Edit Schedule</h2>
      <form method="post" action="/schedules/update/{sid}">
        <label>Name: <input name="name" value="{r['name']}"></label><br>
        <label>Category: <input name="category" value="{r['category']}"></label><br>
        <label>Amount: <input type="number" step="0.01" name="amount" value="{r['amount']}"></label><br>
        <label>Kind: 
          <select name="kind">
            <option value="income" {"selected" if r['kind']=="income" else ""}>Income</option>
            <option value="expense" {"selected" if r['kind']=="expense" else ""}>Expense</option>
          </select>
        </label><br>
        <label>Frequency:
          <select name="frequency">
            <option value="weekly" {"selected" if r['frequency']=="weekly" else ""}>Weekly</option>
            <option value="biweekly" {"selected" if r['frequency']=="biweekly" else ""}>Biweekly</option>
            <option value="monthly" {"selected" if r['frequency']=="monthly" else ""}>Monthly</option>
          </select>
        </label><br>
        <label>Start Date: <input type="date" name="start_date" value="{r['start_date']}"></label><br>
        <label>Day of Week:
          <select name="dow">
            {''.join(f"<option value='{i}' {'selected' if r['dow']==i else ''}>{day}</option>" 
                     for i,day in enumerate(['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']))}
          </select>
        </label><br>
        <button type="submit">Save</button>
      </form>
    </div></body></html>""")


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

    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <div class="card">
    <h2>{o['name']} on {o['date']}</h2>
    <p>Amount: ${o['amount']} | Category: {o['category']} | Status: {o['status']}</p>
    </div></body></html>""")

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

    return HTMLResponse(f"""<!doctype html><html><head>{STYLE}</head><body>
    {NAVBAR}
    <div class="card">
      <h2>📖 Glossary / Index</h2>
      <p>Quick reference for terms used in E.A.S.I.E.</p>
      <table>
        <tr><th>Term</th><th>Definition</th></tr>
        {rows}
      </table>
    </div>
    </body></html>""")
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


# -------- Health --------
@app.get("/health")
async def health(): return {"ok":True}

# -------- Entrypoint --------
@app.on_event("startup")
def _startup(): init_db()

if __name__=="__main__":
    init_db(); import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=8000,reload=True)
