"""
HZFaction Backend  –  FastAPI + SQLite + WebSockets
Run with:  uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3, json, re, os, asyncio
from datetime import datetime, timezone
from typing import Optional
from contextlib import asynccontextmanager

# ── database path ────────────────────────────────────────────────────────────
DB_PATH = "/tmp/factions.db"

# ── websocket connection manager ─────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

manager = ConnectionManager()

# ── db init ───────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            captured_at TEXT    NOT NULL,
            raw_payload TEXT    NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS faction_counts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id INTEGER NOT NULL,
            tag         TEXT    NOT NULL,
            color_hex   TEXT,
            online      INTEGER NOT NULL DEFAULT 0,
            leader      TEXT,
            leader_id   INTEGER,
            FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
        )
    """)
    con.commit()
    con.close()

# ── payload parser ────────────────────────────────────────────────────────────
def parse_payload(raw: str) -> tuple[list[dict], dict[str, str]]:
    """
    Returns (factions_list, color_map)
    Each faction dict: {tag, online, leader, leader_id, color_hex}
    """
    lines = raw.splitlines()

    # split off the --COLORS-- block
    try:
        color_idx = next(i for i, l in enumerate(lines) if l.strip() == "--COLORS--")
        color_lines = lines[color_idx + 1:]
        faction_lines = lines[:color_idx]
    except StopIteration:
        color_lines = []
        faction_lines = lines

    # build color map  id -> hex
    color_map: dict[str, str] = {}
    for cl in color_lines:
        m = re.match(r"(\d+)=([0-9A-Fa-f]{6})", cl.strip())
        if m:
            color_map[m.group(1)] = m.group(2).upper()

    # parse /id lines → leader name → id
    leader_ids: dict[str, int] = {}
    for ln in lines:
        # "ID: 42 | Name: Firstname_Lastname | ... | Faction: TAG"
        id_m   = re.search(r"ID:\s*(\d+)", ln)
        name_m = re.search(r"Name:\s*([^|]+)", ln)
        if id_m and name_m:
            name = name_m.group(1).strip().lower().replace("_", " ")
            leader_ids[name] = int(id_m.group(1))

    # parse faction rows
    factions: list[dict] = []
    for ln in faction_lines:
        # must start with "* N."
        if not re.match(r"\s*\*\s+\d+\.", ln):
            continue

        # extract faction id
        faction_id = re.search(r"\*\s+(\d+)\.", ln).group(1)

        # strip colour codes for easy parsing
        plain = re.sub(r"\{[0-9A-Fa-f]{6}\}", "", ln)

        # extract tag (first word after "N. ")
        tag_m = re.search(r"\*\s+\d+\.\s+(\S+)", plain)
        if not tag_m:
            continue
        tag = tag_m.group(1).upper()

        # extract leader
        leader_m = re.search(r"Leader:\s*([^|]+)", plain)
        leader_raw = leader_m.group(1).strip() if leader_m else ""
        leader = None if leader_raw.lower() in ("secret", "closed", "") else leader_raw

        # extract online count
        online_m = re.search(r"Online:\s*(\d+)", plain)
        online = int(online_m.group(1)) if online_m else 0

        color = color_map.get(faction_id, "FFFFFF")
        lid = leader_ids.get(leader.lower().replace("_", " ")) if leader else None

        factions.append({
            "tag":       tag,
            "online":    online,
            "leader":    leader,
            "leader_id": lid,
            "color_hex": color,
        })

    return factions, color_map

# ── lifespan (replaces @app.on_event) ────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="HZFaction API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ═════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ═════════════════════════════════════════════════════════════════════════════

# ── dashboard ─────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

# ── ingest (called by Lua client) ─────────────────────────────────────────────
@app.post("/administrative/factionmanagement/activity/api/factions/log")
async def ingest(request: Request):
    raw = (await request.body()).decode("utf-8", errors="replace")
    if not raw.strip():
        return JSONResponse({"error": "empty body"}, status_code=400)

    now = datetime.now(timezone.utc).isoformat()
    factions, _ = parse_payload(raw)

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("INSERT INTO snapshots (captured_at, raw_payload) VALUES (?,?)", (now, raw))
    snap_id = cur.lastrowid

    for f in factions:
        cur.execute("""
            INSERT INTO faction_counts
              (snapshot_id, tag, color_hex, online, leader, leader_id)
            VALUES (?,?,?,?,?,?)
        """, (snap_id, f["tag"], f["color_hex"], f["online"], f["leader"], f["leader_id"]))

    con.commit()
    con.close()

    # broadcast live update to all connected dashboards
    await manager.broadcast({
        "event":     "snapshot",
        "timestamp": now,
        "factions":  factions,
    })

    return JSONResponse({"status": "ok", "factions_parsed": len(factions)})

# ── history API ────────────────────────────────────────────────────────────────
@app.get("/api/history")
async def history(limit: int = 200):
    """
    Returns the last `limit` snapshots as a time-series list.
    Shape: [ { timestamp, factions: [{tag, online, color_hex}] }, ... ]
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    cur.execute("""
        SELECT s.id, s.captured_at, fc.tag, fc.online, fc.color_hex
        FROM snapshots s
        JOIN faction_counts fc ON fc.snapshot_id = s.id
        ORDER BY s.id DESC
        LIMIT ?
    """, (limit * 20,))   # over-fetch then group

    rows = cur.fetchall()
    con.close()

    # group by snapshot
    from collections import defaultdict, OrderedDict
    snaps: dict = OrderedDict()
    for r in rows:
        sid = r["id"]
        if sid not in snaps:
            snaps[sid] = {"timestamp": r["captured_at"], "factions": []}
        snaps[sid]["factions"].append({
            "tag":       r["tag"],
            "online":    r["online"],
            "color_hex": r["color_hex"],
        })

    result = list(snaps.values())[:limit]
    result.reverse()   # oldest first for charting
    return JSONResponse(result)

# ── latest snapshot ────────────────────────────────────────────────────────────
@app.get("/api/latest")
async def latest():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT id, captured_at FROM snapshots ORDER BY id DESC LIMIT 1")
    snap = cur.fetchone()
    if not snap:
        con.close()
        return JSONResponse({"factions": [], "timestamp": None})

    cur.execute("""
        SELECT tag, online, color_hex, leader, leader_id
        FROM faction_counts WHERE snapshot_id = ?
    """, (snap["id"],))
    factions = [dict(r) for r in cur.fetchall()]
    con.close()
    return JSONResponse({"timestamp": snap["captured_at"], "factions": factions})

# ── debug: see last raw payload ───────────────────────────────────────────────
@app.get("/api/debug/last")
async def debug_last():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    cur.execute("SELECT id, captured_at, raw_payload FROM snapshots ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    con.close()
    if not row:
        return JSONResponse({"error": "no snapshots yet"})
    factions, _ = parse_payload(row["raw_payload"])
    return JSONResponse({
        "snapshot_id": row["id"],
        "captured_at": row["captured_at"],
        "raw_payload": row["raw_payload"],
        "parsed_factions": factions,
    })

# ── websocket ──────────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            await ws.receive_text()   # keep alive
    except WebSocketDisconnect:
        manager.disconnect(ws)
