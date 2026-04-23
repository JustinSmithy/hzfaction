# HZFaction Backend

A self-contained Python backend for the HZFaction activity tracker.
Receives data from the Lua client, stores it, and serves a live dashboard.

---

## Requirements

- Python 3.10 or newer
- That's it — no external database needed (uses SQLite, which is built into Python)

---

## Setup (run once)

```bash
# 1. Open a terminal in this folder, then create a virtual environment
python -m venv venv

# 2. Activate it
#    Windows:
venv\Scripts\activate
#    Mac / Linux:
source venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

## Running the server

```bash
# Make sure the venv is active first (see step 2 above), then:
uvicorn main:app --reload --port 8000
```

The server is now running at **http://localhost:8000**

| URL | What it does |
|-----|-------------|
| `http://localhost:8000/` | Live dashboard |
| `http://localhost:8000/api/latest` | Latest snapshot (JSON) |
| `http://localhost:8000/api/history` | Full history (JSON) |
| `POST /administrative/factionmanagement/activity/api/factions/log` | Lua client ingestion endpoint |

---

## Pointing the Lua client at your server

In `hzgfm-lua-client.lua`, change this line:

```lua
local serverUrl = 'https://api-705871852969.europe-west1.run.app/administrative/...'
```

to:

```lua
local serverUrl = 'http://YOUR_SERVER_IP:8000/administrative/factionmanagement/activity/api/factions/log'
```

If running locally for testing, use `http://127.0.0.1:8000/...`

---

## Deploying to Google Cloud Run (optional)

If you want it hosted like the original:

1. Install the [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)
2. Run:
```bash
gcloud run deploy hzfaction \
  --source . \
  --region europe-west1 \
  --allow-unauthenticated \
  --port 8000
```

---

## File structure

```
hzfaction/
├── main.py            ← FastAPI app (all the logic)
├── requirements.txt   ← Python dependencies
├── factions.db        ← SQLite database (created automatically on first run)
├── templates/
│   └── dashboard.html ← The live dashboard
└── static/            ← Put any CSS/JS assets here if needed later
```

---

## How it works

```
Lua Client (in-game SAMP)
    │
    │  POST plain text payload every ~20 minutes
    ▼
FastAPI (main.py)
    │
    ├── parse_payload()  →  extracts faction tags, online counts, colours
    ├── saves to factions.db (SQLite)
    └── broadcasts via WebSocket to all open browser tabs
    
Browser (dashboard.html)
    ├── loads history on page open  (GET /api/history)
    └── receives live updates via WebSocket → chart updates automatically
```
