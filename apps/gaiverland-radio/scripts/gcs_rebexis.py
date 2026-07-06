"""
GCS Rebexis Engine — Phase 2.
Input:  { track: {title,artist}, state: {energy_level,stage_active,...} }
Output: { emotion, segment_type, text, action }
100% template-based (RPE v1) — aucune génération libre.
"""
import os, sys, subprocess, json, random

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    _install()
    import fastapi, uvicorn, psycopg2, httpx

import psycopg2.extras
from fastapi import FastAPI

DB_URL    = os.environ["DATABASE_URL"]
STATE_URL = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")
LORE_URL  = os.environ.get("GCS_LORE_SERVICE_URL", "http://gcs-lore-service:8096")
INT_MIN   = int(os.environ.get("REBEXIS_INTERVAL_MIN", "15")) * 60
INT_MAX   = int(os.environ.get("REBEXIS_INTERVAL_MAX", "30")) * 60

app = FastAPI(title="GCS Rebexis Engine")
_tpl: dict = {}

# energy_level 1-5 → emotion RPE v1
ENERGY_EMOTION = {1: "calm", 2: "calm", 3: "playful", 4: "excited", 5: "energetic"}
# energy_level → template mode
ENERGY_MODE    = {1: "flow",  2: "flow",   3: "normal", 4: "hype",    5: "peak"}
# stage → segment_type
STAGE_SEGMENT  = {"mainstage": "announcement", "sunset": "transition",
                  "rush": "intro", "night": "outro"}


def load_tpl():
    global _tpl
    for path in ("/app/templates.json", "/app/rebexis-templates.json"):
        try:
            with open(path) as f:
                _tpl = json.load(f)
            return
        except Exception:
            pass
    _tpl = {"modes": {"normal": {"templates": ["La musique continue."]}}}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def fetch_state() -> dict:
    try:
        r = httpx.get(f"{STATE_URL}/state/current", timeout=3)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def pick(mode: str, track: dict) -> str:
    modes = _tpl.get("modes", {})
    pool  = modes.get(mode, {}).get("templates") or \
            modes.get("normal", {}).get("templates", ["La musique continue."])
    t = random.choice(pool)
    t = t.replace("{artist}", track.get("artist", "l'artiste"))
    t = t.replace("{title}",  track.get("title",  "ce morceau"))
    return t


def should_fire(conn, force: bool) -> bool:
    if force:
        return True
    import datetime
    with conn.cursor() as cur:
        cur.execute("SELECT last_rebexis FROM radio_state WHERE id=1")
        row = cur.fetchone()
    if not row or not row["last_rebexis"]:
        return True
    elapsed = (datetime.datetime.now(datetime.timezone.utc)
               - row["last_rebexis"]).total_seconds()
    return elapsed >= random.randint(INT_MIN, INT_MAX)


def log_lore(text: str, state: dict):
    try:
        httpx.post(f"{LORE_URL}/events", json={
            "type": "rebexis_intervention",
            "description": text,
            "city": state.get("city", ""),
        }, timeout=2)
    except Exception:
        pass


@app.on_event("startup")
def startup():
    load_tpl()


@app.get("/health")
def health():
    return {"status": "ok", "templates_loaded": bool(_tpl)}


@app.post("/generate")
def generate(body: dict = None, force: bool = False):
    body  = body or {}
    state = body.get("state") or fetch_state()
    track = body.get("track") or state.get("last_track") or {}

    conn = get_conn()
    if not should_fire(conn, force):
        conn.close()
        return {"intervention": None, "reason": "interval_not_reached"}

    energy       = int(state.get("energy_level", 3))
    stage        = str(state.get("stage_active", "mainstage"))
    tod          = str(state.get("time_of_day", "day"))
    mode         = "flow" if tod == "night" else ENERGY_MODE.get(energy, "normal")
    emotion      = ENERGY_EMOTION.get(energy, "playful")
    segment_type = STAGE_SEGMENT.get(stage, "announcement")
    text         = pick(mode, track)
    action       = "announce_track" if track.get("title") else "play_music"

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO rebexis_sessions (intervention, mood_trigger, context_track)
            VALUES (%s,%s,%s) RETURNING id
        """, (text, f"energy={energy} stage={stage} emotion={emotion}",
              track.get("title", "")))
        sid = cur.fetchone()["id"]
        cur.execute("UPDATE radio_state SET last_rebexis=NOW() WHERE id=1")
    conn.commit()
    conn.close()

    log_lore(text, state)
    print(f"  ✓ rebexis [{emotion}/{segment_type}]: {text[:60]}")

    return {
        "emotion":      emotion,
        "segment_type": segment_type,
        "text":         text,
        "action":       action,
        "session_id":   sid,
    }


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8092)
