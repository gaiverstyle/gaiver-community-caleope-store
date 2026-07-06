"""
GCS State Engine — Phase 1 GCS migration.
Maintains full Gaiverland festival state (GSE v1).
Separate gcs_state table — never touches the legacy radio_state.
City is configured via GCS_CITY env var (changes ~every 3 years).
"""
import os, sys, subprocess, datetime

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2
except ImportError:
    install_deps()
    import fastapi, uvicorn, psycopg2

import json
import psycopg2.extras
from fastapi import FastAPI

DB_URL   = os.environ["DATABASE_URL"]
GCS_CITY = os.environ.get("GCS_CITY", "Toulon")

MOOD_TO_STAGE: dict[str, str] = {
    "drift":    "sunset",
    "pulse":    "mainstage",
    "festival": "mainstage",
    "rush":     "rush",
    "night":    "night",
    "intense":  "mainstage",
    "energique":"mainstage",
    "melodique":"sunset",
    "nocturne": "night",
}

ENERGY_UP   = {"intense", "festival", "energique"}
ENERGY_DOWN = {"nocturne", "melodique"}

app = FastAPI(title="GCS State Engine")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gcs_state (
                id              INTEGER PRIMARY KEY DEFAULT 1,
                city            VARCHAR(100) DEFAULT 'Toulon',
                festival_phase  VARCHAR(20)  DEFAULT 'live',
                stage_active    VARCHAR(20)  DEFAULT 'mainstage',
                energy_level    INTEGER      DEFAULT 3,
                time_of_day     VARCHAR(20)  DEFAULT 'day',
                weather_mood    VARCHAR(20)  DEFAULT 'calm',
                last_track      JSONB        DEFAULT '{}',
                special_events  JSONB        DEFAULT '[]',
                updated_at      TIMESTAMPTZ  DEFAULT NOW()
            )
        """)
        cur.execute("INSERT INTO gcs_state (id, city) VALUES (1, %s) ON CONFLICT DO NOTHING", (GCS_CITY,))
    conn.commit()
    conn.close()
    print(f"✓ gcs_state table ready (city={GCS_CITY})")


def time_of_day() -> str:
    hour = datetime.datetime.now().hour
    if 6 <= hour < 18:
        return "day"
    elif 18 <= hour < 22:
        return "sunset"
    return "night"


def resolve_mood_from_db(conn, artist: str, title: str) -> str | None:
    """Look up the track in the existing tracks table for its analyzed mood."""
    if not artist and not title:
        return None
    with conn.cursor() as cur:
        cur.execute("""
            SELECT mood FROM tracks
            WHERE (artist ILIKE %s OR title ILIKE %s) AND analyzed=TRUE
            LIMIT 1
        """, (artist, title))
        row = cur.fetchone()
    return row["mood"] if row else None


def compute_new_energy(current: int, mood: str) -> int:
    if mood in ENERGY_UP:
        return min(5, current + 1)
    if mood in ENERGY_DOWN:
        return max(1, current - 1)
    return current


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok", "city": GCS_CITY}


@app.get("/state/current")
def get_state():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM gcs_state WHERE id=1")
        row = cur.fetchone()
    conn.close()
    if not row:
        return {}
    state = dict(row)
    if isinstance(state.get("last_track"), str):
        state["last_track"] = json.loads(state["last_track"])
    if isinstance(state.get("special_events"), str):
        state["special_events"] = json.loads(state["special_events"])
    return state


@app.post("/state/update")
def update_state(body: dict):
    """Receive GCS track event, update festival state."""
    track = body.get("track", {})
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT energy_level FROM gcs_state WHERE id=1")
        row = cur.fetchone()
    current_energy = row["energy_level"] if row else 3

    # Resolve mood — from analyzed DB if available
    mood = resolve_mood_from_db(conn, track.get("artist", ""), track.get("title", ""))
    if not mood:
        mood = "energique"  # safe default — festival always runs

    stage = MOOD_TO_STAGE.get(mood, "mainstage")
    energy = compute_new_energy(current_energy, mood)
    tod = time_of_day()

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE gcs_state SET
                city           = %s,
                festival_phase = 'live',
                stage_active   = %s,
                energy_level   = %s,
                time_of_day    = %s,
                last_track     = %s::jsonb,
                updated_at     = NOW()
            WHERE id = 1
        """, (GCS_CITY, stage, energy, tod, json.dumps(track)))
    conn.commit()
    conn.close()

    print(f"  ✓ state: mood={mood} energy={energy} stage={stage} tod={tod}")
    return {
        "mood": mood,
        "energy_level": energy,
        "stage_active": stage,
        "time_of_day": tod,
        "city": GCS_CITY,
    }


@app.post("/state/weather")
def set_weather(weather_mood: str):
    """Manual override for weather_mood (calm|windy|storm|warm)."""
    valid = {"calm", "windy", "storm", "warm"}
    if weather_mood not in valid:
        from fastapi import HTTPException
        raise HTTPException(400, f"weather_mood must be one of {valid}")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE gcs_state SET weather_mood=%s, updated_at=NOW() WHERE id=1", (weather_mood,))
    conn.commit()
    conn.close()
    return {"ok": True, "weather_mood": weather_mood}


@app.post("/state/phase")
def set_phase(festival_phase: str):
    """Manual override for festival_phase (live|transit|setup)."""
    valid = {"live", "transit", "setup"}
    if festival_phase not in valid:
        from fastapi import HTTPException
        raise HTTPException(400, f"festival_phase must be one of {valid}")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE gcs_state SET festival_phase=%s, updated_at=NOW() WHERE id=1", (festival_phase,))
    conn.commit()
    conn.close()
    return {"ok": True, "festival_phase": festival_phase}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8091)
