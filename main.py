# main.py — FastAPI dashboard + admin using your UI sheet design
import os
import sqlite3
from datetime import date, timedelta
from typing import Optional, Literal, Any

from fastapi import FastAPI, Query, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from pydantic import BaseModel, field_validator

DB_PATH = os.environ.get("DB_PATH", "ppm_local.db")

# --------------------- DB helpers ---------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_views_and_columns():
    conn = db()
    cur = conn.cursor()
    # lifecycle columns (safe if already exist)
    for col, ddl in [
        ("is_active", "INTEGER DEFAULT 1"),
        ("retired_at", "TEXT"),
        ("retired_reason", "TEXT"),
        ("suspended_until", "TEXT"),
    ]:
        try:
            cur.execute(f"ALTER TABLE ppm_plan ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # already exists

    # helpful view: active + not suspended (or suspension ended)
    cur.execute("""
    CREATE VIEW IF NOT EXISTS view_active_ppm AS
    SELECT p.*
    FROM ppm_plan p
    WHERE p.is_active = 1
      AND (p.suspended_until IS NULL OR p.suspended_until <= date('now'));
    """)

    # indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ppm_due ON ppm_plan(next_due_date)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ppm_site ON ppm_plan(site_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_site_status ON site(status)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_site_type ON site(site_type_code)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ppm_cat ON ppm_plan(category_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ppm_pri ON ppm_plan(priority)")
    conn.commit()
    conn.close()

ensure_views_and_columns()

# --------------------- FastAPI app ---------------------
app = FastAPI(title="Compliance Dashboard API")

# CORS (so you can call from other tools/ports if needed)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten later if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------- Models ---------------------
class PpmUpdate(BaseModel):
    finished_date: Optional[str] = None       # 'YYYY-MM-DD' or None
    next_due_date: Optional[str] = None
    is_active: Optional[bool] = None
    retired_reason: Optional[str] = None
    retired_at: Optional[str] = None
    suspended_until: Optional[str] = None

    # pydantic v2: use field_validator
    @field_validator("finished_date", "next_due_date", "retired_at", "suspended_until", mode="before")
    @classmethod
    def date_like(cls, v):
        if v in (None, ""):
            return None
        if isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-":
            return v
        raise ValueError("Dates must be YYYY-MM-DD")

class SiteUpdate(BaseModel):
    status: Literal["Active", "Closed", "Suspended", "Under-Construction", "Unknown"]

# --------------------- Utils ---------------------
def classify_site_status(min_next_due: Optional[str], has_overdue: bool) -> str:
    """
    Color/logic per your UI sheet:
    - DUE (red): any overdue
    - DUE_SOON (amber): next due within 30 days
    - OK (green): else
    """
    if has_overdue:
        return "DUE"
    if not min_next_due:
        return "OK"
    today = date.today()
    nd = date.fromisoformat(min_next_due)
    if nd < today:
        return "DUE"
    if nd <= today + timedelta(days=30):
        return "DUE_SOON"
    return "OK"

def build_where(filters: dict[str, Any], params: list[Any]) -> str:
    """
    Compose WHERE additions for /api/sites with filter toolbar options.
    Filters supported: q, status, site_type, category_id, priority, due_window
    due_window: 'overdue' | 'soon' | 'quarter' | 'all' (default 'all')
    """
    where = " WHERE 1=1 "
    if filters.get("status"):
        where += " AND s.status = ?"
        params.append(filters["status"])
    if filters.get("site_type"):
        where += " AND s.site_type_code = ?"
        params.append(filters["site_type"])
    if filters.get("q"):
        like = f"%{filters['q'].lower()}%"
        where += " AND (LOWER(s.name) LIKE ? OR LOWER(s.site_code) LIKE ? OR LOWER(s.uprn) LIKE ?)"
        params += [like, like, like]
    # join-level filters need EXISTS against active ppm
    if filters.get("category_id") or filters.get("priority") or filters.get("due_window"):
        # base exists on view_active_ppm (already lifecycle aware)
        exists = " EXISTS (SELECT 1 FROM view_active_ppm ap WHERE ap.site_id=s.site_id "
        if filters.get("category_id"):
            exists += " AND ap.category_id = ?"
            params.append(filters["category_id"])
        if filters.get("priority"):
            exists += " AND ap.priority = ?"
            params.append(filters["priority"])
        dw = filters.get("due_window", "all")
        if dw == "overdue":
            exists += " AND ap.next_due_date IS NOT NULL AND ap.next_due_date < date('now')"
        elif dw == "soon":
            exists += " AND ap.next_due_date BETWEEN date('now') AND date('now','+30 days')"
        elif dw == "quarter":
            exists += " AND ap.next_due_date BETWEEN date('now') AND date('now','+90 days')"
        exists += " ) "
        where += " AND " + exists
    return where

# --------------------- Routes ---------------------
@app.get("/api/summary")
def api_summary():
    conn = db(); cur = conn.cursor()
    total_sites = cur.execute("SELECT COUNT(*) FROM site").fetchone()[0]
    active_sites = cur.execute("SELECT COUNT(*) FROM site WHERE status='Active'").fetchone()[0]
    total_ppm_active = cur.execute("SELECT COUNT(*) FROM view_active_ppm").fetchone()[0]
    overdue = cur.execute("""
        SELECT COUNT(*) FROM view_active_ppm
        WHERE next_due_date IS NOT NULL AND next_due_date < date('now')
    """).fetchone()[0]
    due_30 = cur.execute("""
        SELECT COUNT(*) FROM view_active_ppm
        WHERE next_due_date BETWEEN date('now') AND date('now','+30 days')
    """).fetchone()[0]
    non_compliant_sites = cur.execute("""
        SELECT COUNT(DISTINCT s.site_id)
        FROM site s
        JOIN view_active_ppm p ON p.site_id = s.site_id
        WHERE p.next_due_date IS NOT NULL AND p.next_due_date < date('now')
    """).fetchone()[0]
    compliant_sites = active_sites - non_compliant_sites
    conn.close()
    return {
        "total_sites": total_sites,
        "active_sites": active_sites,
        "total_ppm_active": total_ppm_active,
        "overdue": overdue,
        "due_next_30": due_30,
        "compliant_sites": compliant_sites,
        "non_compliant_sites": non_compliant_sites,
        "today": date.today().isoformat(),
    }

@app.get("/api/filters")
def api_filters():
    """Options for filter toolbar: site types, categories, priorities seen in DB."""
    conn = db(); cur = conn.cursor()
    site_types = [dict(r) for r in cur.execute("SELECT site_type_code AS code, site_type_name AS name FROM site_type ORDER BY name NULLS LAST, code")]
    categories = [dict(r) for r in cur.execute("SELECT category_id, name FROM category ORDER BY name")]
    priorities = sorted({(r[0] or "").strip() for r in cur.execute("SELECT DISTINCT priority FROM ppm_plan") if (r[0] or "").strip()})
    conn.close()
    return {"site_types": site_types, "categories": categories, "priorities": priorities}

@app.get("/api/sites")
def api_sites(q: Optional[str] = None,
              status: Optional[str] = None,
              site_type: Optional[str] = None,
              category_id: Optional[int] = None,
              priority: Optional[str] = None,
              due_window: Literal["all", "overdue", "soon", "quarter"] = "all",
              limit: int = 50):
    filters = {
        "q": (q or "").strip(),
        "status": (status or "").strip(),
        "site_type": (site_type or "").strip(),
        "category_id": category_id,
        "priority": (priority or "").strip(),
        "due_window": due_window,
    }

    conn = db(); cur = conn.cursor()

    params: list[Any] = []
    where = build_where(filters, params)

    sql = f"""
      SELECT s.site_id, s.name, s.uprn, s.site_code, s.status, s.site_type_code,
             (SELECT MIN(p2.next_due_date) FROM view_active_ppm p2 WHERE p2.site_id=s.site_id) AS min_next_due,
             (SELECT SUM(CASE WHEN p3.next_due_date IS NOT NULL AND p3.next_due_date < date('now') THEN 1 ELSE 0 END)
              FROM view_active_ppm p3 WHERE p3.site_id=s.site_id) AS overdue_count,
             (SELECT COUNT(*) FROM view_active_ppm p4 WHERE p4.site_id=s.site_id) AS active_ppm
      FROM site s
      {where}
      ORDER BY s.name
      LIMIT ?
    """
    params.append(limit)

    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    for r in rows:
        r["min_next_due"] = r["min_next_due"] or None
        r["overdue_count"] = int(r["overdue_count"] or 0)
        r["active_ppm"] = int(r["active_ppm"] or 0)
        r["ui_status"] = classify_site_status(r["min_next_due"], r["overdue_count"] > 0)
    conn.close()
    return rows

@app.get("/api/ppm")
def api_ppm_by_site(site_id: str = Query(..., description="site.site_id"),
                    include_inactive: int = 0):
    conn = db(); cur = conn.cursor()
    if include_inactive:
        q = """
        SELECT p.*, c.name AS category_name
        FROM ppm_plan p
        LEFT JOIN category c ON c.category_id = p.category_id
        WHERE p.site_id = ?
        ORDER BY COALESCE(p.next_due_date, '9999-12-31') ASC
        """
        rows = [dict(r) for r in cur.execute(q, (site_id,)).fetchall()]
    else:
        q = """
        SELECT p.*, c.name AS category_name
        FROM view_active_ppm p
        LEFT JOIN category c ON c.category_id = p.category_id
        WHERE p.site_id = ?
        ORDER BY COALESCE(p.next_due_date, '9999-12-31') ASC
        """
        rows = [dict(r) for r in cur.execute(q, (site_id,)).fetchall()]
    conn.close()
    return rows

@app.patch("/api/ppm/{ppm_plan_id}")
def api_update_ppm(ppm_plan_id: int, body: PpmUpdate):
    fields = []
    vals: list[Any] = []

    for k in ("finished_date", "next_due_date", "retired_reason", "suspended_until"):
        v = getattr(body, k)
        if v is not None:
            fields.append(f"{k} = ?")
            vals.append(v or None)

    if body.is_active is not None:
        fields.append("is_active = ?")
        vals.append(1 if body.is_active else 0)
        if not body.is_active:
            if body.retired_at is not None:
                fields.append("retired_at = ?"); vals.append(body.retired_at)
            else:
                fields.append("retired_at = date('now')")
        else:
            fields.append("retired_at = NULL")
            fields.append("retired_reason = NULL")

    if not fields:
        raise HTTPException(status_code=400, detail="No updatable fields provided")

    vals.append(ppm_plan_id)
    conn = db(); cur = conn.cursor()
    cur.execute(f"UPDATE ppm_plan SET {', '.join(fields)} WHERE ppm_plan_id = ?", vals)
    conn.commit()
    conn.close()
    return {"ok": True}

@app.patch("/api/site/{site_id}")
def api_update_site(site_id: str, body: SiteUpdate):
    conn = db(); cur = conn.cursor()
    cur.execute("UPDATE site SET status=?, updated_at=CURRENT_TIMESTAMP WHERE site_id=?", (body.status, site_id))
    if cur.rowcount == 0:
        conn.close()
        raise HTTPException(status_code=404, detail="site not found")
    conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/export/sites.csv")
def api_export_sites_csv(q: Optional[str] = None,
                         status: Optional[str] = None,
                         site_type: Optional[str] = None,
                         category_id: Optional[int] = None,
                         priority: Optional[str] = None,
                         due_window: Literal["all", "overdue", "soon", "quarter"] = "all",
                         limit: int = 5000):
    # reuse same filter logic as /api/sites
    filters = {
        "q": (q or "").strip(),
        "status": (status or "").strip(),
        "site_type": (site_type or "").strip(),
        "category_id": category_id,
        "priority": (priority or "").strip(),
        "due_window": due_window,
    }
    conn = db(); cur = conn.cursor()
    params: list[Any] = []
    where = build_where(filters, params)

    sql = f"""
      SELECT s.site_id, s.name, s.uprn, s.site_code, s.status, s.site_type_code,
             (SELECT MIN(p2.next_due_date) FROM view_active_ppm p2 WHERE p2.site_id=s.site_id) AS min_next_due,
             (SELECT SUM(CASE WHEN p3.next_due_date IS NOT NULL AND p3.next_due_date < date('now') THEN 1 ELSE 0 END)
              FROM view_active_ppm p3 WHERE p3.site_id=s.site_id) AS overdue_count,
             (SELECT COUNT(*) FROM view_active_ppm p4 WHERE p4.site_id=s.site_id) AS active_ppm
      FROM site s
      {where}
      ORDER BY s.name
      LIMIT ?
    """
    params.append(limit)
    rows = [dict(r) for r in cur.execute(sql, params).fetchall()]
    conn.close()

    # build CSV
    import io, csv
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else
                       ["site_id","name","uprn","site_code","status","site_type_code","min_next_due","overdue_count","active_ppm"])
    w.writeheader()
    for r in rows:
        w.writerow(r)
    csv_bytes = buf.getvalue().encode("utf-8")
    headers = {"Content-Disposition": "attachment; filename=sites_export.csv"}
    return Response(content=csv_bytes, media_type="text/csv; charset=utf-8", headers=headers)

# --------------------- Minimal UI: Dashboard ---------------------
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Compliance Dashboard</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root{
      --blue:#0f4c81; --green:#27ae60; --amber:#f39c12; --red:#e74c3c; --bg:#f7f9fc; --ink:#2c3e50; --card:#fff; --muted:#8aa0b4;
    }
    body{margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; color:var(--ink); background:var(--bg);}
    header{background:var(--blue); color:#fff; padding:14px 18px; display:flex; align-items:center; justify-content:space-between;}
    header h1{margin:0; font-size:18px; letter-spacing:.5px;}
    .summary{display:flex; gap:12px; flex-wrap:wrap;}
    .tile{background:#ffffff1a; padding:8px 12px; border-radius:10px; font-size:13px}
    .wrap{display:grid; grid-template-columns: 1fr 420px; gap:14px; padding:14px;}
    .toolbar{background:var(--card); padding:10px; border-radius:12px; border:1px solid #e3eaf2; display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:12px 0;}
    input,select,button{font:inherit; padding:8px 10px; border:1px solid #dfe7ef; border-radius:10px; outline:none}
    button{background:var(--blue); color:#fff; border:none; cursor:pointer}
    button.ghost{background:#fff; color:var(--blue); border:1px solid var(--blue)}
    .board{display:grid; grid-template-columns: repeat(auto-fill,minmax(280px,1fr)); gap:12px;}
    .card{background:var(--card); border-radius:14px; padding:12px; border:2px solid #e8eef5; box-shadow:0 1px 2px rgba(0,0,0,.04)}
    .card.ok{border-color:var(--green)}
    .card.due_soon{border-color:var(--amber)}
    .card.due{border-color:var(--red)}
    .card h3{margin:0 0 4px; font-size:16px}
    .meta{font-size:12px; color:var(--muted)}
    .side{background:var(--card); border-radius:14px; padding:12px; border:2px solid #e8eef5; height:calc(100vh - 140px); overflow:auto}
    .ppm{border-bottom:1px solid #eef3f8; padding:8px 0}
    .ppm:last-child{border-bottom:none}
    .ppm .title{font-weight:600}
    .status-pill{display:inline-block; padding:2px 8px; font-size:11px; border-radius:999px; margin-left:8px;}
    .pill-ok{background:var(--green); color:#fff}
    .pill-soon{background:var(--amber); color:#fff}
    .pill-due{background:var(--red); color:#fff}
    .row{display:flex; gap:8px; align-items:center; flex-wrap:wrap}
    .row label{font-size:12px; color:var(--muted)}
    .ppm small{color:var(--muted)}
    .edit{display:flex; gap:6px; margin-top:6px}
    .admin-link{color:#fff; text-decoration:none; border:1px solid #ffffff55; padding:6px 10px; border-radius:10px}
  </style>
</head>
<body>
  <header>
    <h1>Compliance Dashboard</h1>
    <div class="summary" id="summary"></div>
    <a class="admin-link" href="/app/admin.html">Admin</a>
  </header>

  <div class="wrap">
    <div>
      <div class="toolbar">
        <input id="q" placeholder="Search site name / code / UPRN" />
        <select id="status">
          <option value="">All Statuses</option>
          <option>Active</option><option>Closed</option><option>Suspended</option><option>Under-Construction</option><option>Unknown</option>
        </select>
        <select id="siteType"><option value="">All Site Types</option></select>
        <select id="category"><option value="">All Categories</option></select>
        <select id="priority"><option value="">All Priorities</option></select>
        <select id="dueWindow">
          <option value="all">All</option>
          <option value="overdue">Overdue</option>
          <option value="soon">Next 30 days</option>
          <option value="quarter">This quarter (90d)</option>
        </select>
        <button onclick="loadSites()">Apply</button>
        <button class="ghost" onclick="exportCSV()">Export CSV</button>
      </div>
      <div class="board" id="board"></div>
    </div>

    <aside class="side">
      <h3 id="sideTitle">Select a site</h3>
      <div class="row" style="margin-bottom:8px">
        <label>Site status:</label>
        <select id="siteStatus" disabled onchange="saveSiteStatus()">
          <option>Active</option><option>Closed</option><option>Suspended</option><option>Under-Construction</option><option>Unknown</option>
        </select>
        <button class="ghost" onclick="toggleInactive()" id="toggleInactive" style="display:none">Show inactive PPM</button>
      </div>
      <div id="ppmList"></div>
    </aside>
  </div>

<script>
let currentSite = null;
let includeInactive = 0;
let cacheFilters = null;

function pill(label){
  if(label==='DUE') return '<span class="status-pill pill-due">DUE</span>';
  if(label==='DUE_SOON') return '<span class="status-pill pill-soon">DUE SOON</span>';
  return '<span class="status-pill pill-ok">OK</span>';
}

async function loadFilters(){
  const r = await fetch('/api/filters'); const d = await r.json();
  const siteType = document.getElementById('siteType');
  const category = document.getElementById('category');
  const priority = document.getElementById('priority');
  siteType.innerHTML = '<option value="">All Site Types</option>' + d.site_types.map(x=>`<option value="${x.code||''}">${x.name||x.code}</option>`).join('');
  category.innerHTML = '<option value="">All Categories</option>' + d.categories.map(x=>`<option value="${x.category_id}">${x.name}</option>`).join('');
  priority.innerHTML = '<option value="">All Priorities</option>' + d.priorities.map(x=>`<option value="${x}">${x}</option>`).join('');
  cacheFilters = d;
}

async function loadSummary(){
  const r = await fetch('/api/summary'); const d = await r.json();
  const el = document.getElementById('summary');
  el.innerHTML = `
    <div class="tile">Sites: <b>${d.total_sites}</b></div>
    <div class="tile">Active Sites: <b>${d.active_sites}</b></div>
    <div class="tile">Active PPM: <b>${d.total_ppm_active}</b></div>
    <div class="tile">Overdue: <b>${d.overdue}</b></div>
    <div class="tile">Due next 30d: <b>${d.due_next_30}</b></div>
    <div class="tile">Compliant Sites: <b>${d.compliant_sites}</b></div>
    <div class="tile">Non-Compliant Sites: <b>${d.non_compliant_sites}</b></div>
  `;
}

function buildParams(){
  const params = new URLSearchParams({limit: 50});
  const q = document.getElementById('q').value.trim();
  const s = document.getElementById('status').value;
  const st = document.getElementById('siteType').value;
  const cat = document.getElementById('category').value;
  const pri = document.getElementById('priority').value;
  const dw = document.getElementById('dueWindow').value;
  if(q) params.set('q', q);
  if(s) params.set('status', s);
  if(st) params.set('site_type', st);
  if(cat) params.set('category_id', cat);
  if(pri) params.set('priority', pri);
  if(dw && dw!=='all') params.set('due_window', dw);
  return params;
}

async function loadSites(){
  const params = buildParams();
  const r = await fetch('/api/sites?'+params.toString());
  const rows = await r.json();
  const board = document.getElementById('board');
  board.innerHTML = rows.map(x=>{
    const cls = x.ui_status==='DUE' ? 'due' : (x.ui_status==='DUE_SOON' ? 'due_soon' : 'ok');
    return `
      <div class="card ${cls}" onclick='selectSite(${JSON.stringify(x)})'>
        <h3>${x.name || '(no name)'} ${pill(x.ui_status)}</h3>
        <div class="meta">UPRN: ${x.uprn || '-'} • Code: ${x.site_code || '-'} • Active PPM: ${x.active_ppm}</div>
        <div class="meta">Min Next Due: ${x.min_next_due || '—'} • Status: ${x.status}</div>
      </div>
    `;
  }).join('') || '<div class="meta">No results</div>';
}

async function exportCSV(){
  const params = buildParams();
  const url = '/api/export/sites.csv?'+params.toString();
  window.open(url, '_blank');
}

async function selectSite(site){
  currentSite = site;
  includeInactive = 0;
  document.getElementById('sideTitle').textContent = site.name || '(no name)';
  const sel = document.getElementById('siteStatus');
  sel.value = site.status; sel.disabled = false;
  document.getElementById('toggleInactive').style.display = 'inline-block';
  await loadPPM();
}

async function toggleInactive(){
  includeInactive = includeInactive ? 0 : 1;
  await loadPPM();
}

function fmtRow(p){
  return `
    <div class="ppm">
      <div class="title">${p.category_name || '-'} — ${p.instruction || '(no instruction)'}</div>
      <small>Next due: ${p.next_due_date || '—'} • Freq: ${p.frequency_months || '—'} • Active: ${p.is_active ? 'Yes' : 'No'}</small>
      <div class="edit">
        <input type="date" value="${p.next_due_date || ''}" onchange="queueUpdate(${p.ppm_plan_id}, 'next_due_date', this.value)" />
        <input type="date" placeholder="suspend until" value="${p.suspended_until || ''}" onchange="queueUpdate(${p.ppm_plan_id}, 'suspended_until', this.value)" />
        <select onchange="queueUpdate(${p.ppm_plan_id}, 'is_active', this.value)">
          <option value="1" ${p.is_active ? 'selected':''}>Active</option>
          <option value="0" ${!p.is_active ? 'selected':''}>Inactive</option>
        </select>
        <input placeholder="retired reason" value="${p.retired_reason || ''}" onchange="queueUpdate(${p.ppm_plan_id}, 'retired_reason', this.value)" />
        <button onclick="saveUpdate(${p.ppm_plan_id})">Save</button>
      </div>
    </div>
  `;
}

const pending = new Map();
function queueUpdate(id, key, val){
  const obj = pending.get(id) || {};
  if(key==='is_active') obj[key] = (val==='1');
  else obj[key] = val || null;
  pending.set(id, obj);
}

async function saveUpdate(id){
  const body = pending.get(id);
  if(!body){ alert('Nothing to save'); return; }
  const r = await fetch('/api/ppm/'+id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  if(!r.ok){ alert('Save failed'); return; }
  pending.delete(id);
  await loadPPM();
  await loadSummary();
}

async function loadPPM(){
  if(!currentSite) return;
  const params = new URLSearchParams({site_id: currentSite.site_id});
  if(includeInactive) params.set('include_inactive','1');
  const r = await fetch('/api/ppm?'+params.toString());
  const rows = await r.json();
  document.getElementById('ppmList').innerHTML = rows.map(fmtRow).join('') || '<div class="meta">No PPM lines</div>';
}

async function saveSiteStatus(){
  if(!currentSite) return;
  const newStatus = document.getElementById('siteStatus').value;
  await fetch('/api/site/'+currentSite.site_id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({status:newStatus})});
  await loadSummary();
  await loadSites();
}

(async function init(){
  await loadFilters();
  await loadSummary();
  await loadSites();
})();
</script>
</body>
</html>
"""

# --------------------- Minimal UI: Admin ---------------------
ADMIN_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Admin — Sites</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root{ --blue:#0f4c81; --bg:#f7f9fc; --ink:#2c3e50; --card:#fff; --muted:#8aa0b4; }
    body{margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; color:var(--ink); background:var(--bg);}
    header{background:var(--blue); color:#fff; padding:14px 18px; display:flex; align-items:center; justify-content:space-between;}
    header a{color:#fff; text-decoration:none; border:1px solid #ffffff55; padding:6px 10px; border-radius:10px}
    .wrap{padding:14px; max-width:1100px; margin:0 auto}
    .bar{display:flex; gap:8px; flex-wrap:wrap; margin-bottom:12px}
    input,select,button{font:inherit; padding:8px 10px; border:1px solid #dfe7ef; border-radius:10px; outline:none}
    button{background:var(--blue); color:#fff; border:none; cursor:pointer}
    table{width:100%; border-collapse:collapse; background:var(--card); border-radius:12px; overflow:hidden; box-shadow:0 1px 2px rgba(0,0,0,.04)}
    th,td{padding:8px 10px; border-bottom:1px solid #eef3f8; font-size:14px}
    th{text-align:left; background:#f3f6fa; color:#506579}
  </style>
</head>
<body>
<header><h1>Admin — Sites</h1><a href="/">Back to Dashboard</a></header>
<div class="wrap">
  <div class="bar">
    <input id="q" placeholder="Search sites"/>
    <select id="status"><option value="">Any status</option><option>Active</option><option>Closed</option><option>Suspended</option><option>Under-Construction</option><option>Unknown</option></select>
    <select id="siteType"><option value="">Any site type</option></select>
    <button onclick="loadSites()">Search</button>
  </div>
  <table>
    <thead><tr><th>Name</th><th>UPRN</th><th>Code</th><th>Status</th><th>Type</th><th>Actions</th></tr></thead>
    <tbody id="rows"></tbody>
  </table>
</div>
<script>
async function loadFilters(){
  const r = await fetch('/api/filters'); const d = await r.json();
  const siteType = document.getElementById('siteType');
  siteType.innerHTML = '<option value=\"\">Any site type</option>' + d.site_types.map(x=>`<option value=\"${x.code||''}\">${x.name||x.code}</option>`).join('');
}
async function loadSites(){
  const q=document.getElementById('q').value.trim();
  const s=document.getElementById('status').value;
  const st=document.getElementById('siteType').value;
  const params=new URLSearchParams({limit:100});
  if(q) params.set('q', q);
  if(s) params.set('status', s);
  if(st) params.set('site_type', st);
  const r=await fetch('/api/sites?'+params.toString()); const rows=await r.json();
  const tb=document.getElementById('rows');
  tb.innerHTML = rows.map(x => `
    <tr>
      <td>${x.name || '(no name)'}</td>
      <td>${x.uprn || '-'}</td>
      <td>${x.site_code || '-'}</td>
      <td>
        <select onchange="updateSite('${x.site_id}', this.value)">
          <option ${x.status==='Active'?'selected':''}>Active</option>
          <option ${x.status==='Closed'?'selected':''}>Closed</option>
          <option ${x.status==='Suspended'?'selected':''}>Suspended</option>
          <option ${x.status==='Under-Construction'?'selected':''}>Under-Construction</option>
          <option ${x.status==='Unknown'?'selected':''}>Unknown</option>
        </select>
      </td>
      <td>${x.site_type_code || '-'}</td>
      <td><a href="/" target="_blank">Open</a></td>
    </tr>
  `).join('');
}
async function updateSite(site_id, status){
  await fetch('/api/site/'+site_id, {method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({status})});
  alert('Updated');
}
(async function(){ await loadFilters(); await loadSites(); })();
</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index_html():
    # Colors + layout per your UI reference sheet (blue header, green/amber/red, toolbar, board, sidebar).  # :contentReference[oaicite:1]{index=1}
    return HTMLResponse(INDEX_HTML)

@app.get("/app/admin.html", response_class=HTMLResponse)
def admin_html():
    # Admin page for editing site status & viewing basic site info.  # :contentReference[oaicite:2]{index=2}
    return HTMLResponse(ADMIN_HTML)
