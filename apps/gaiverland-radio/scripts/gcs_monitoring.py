"""
GCS Monitoring — Priority 1.
Dashboard temps réel de l'état de production Gaiverland.
Agrège les données de tous les services GCS + DB.
Logs structurés : TRACK_EVENT, STATE_UPDATE, REBEXIS_GENERATED, TTS_CREATED, AUDIO_INJECTED
"""
import os, sys, subprocess, time

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    _install()
    import fastapi, uvicorn, psycopg2, httpx

import json
import psycopg2.extras
from fastapi import FastAPI
from typing import Optional

DB_URL        = os.environ["DATABASE_URL"]
STATE_URL     = os.environ.get("GCS_STATE_ENGINE_URL",  "http://gcs-state-engine:8091")
REBEXIS_URL   = os.environ.get("GCS_REBEXIS_URL",       "http://gcs-rebexis:8092")
TTS_URL       = os.environ.get("GCS_TTS_URL",           "http://gcs-tts:8093")
TRACK_URL     = os.environ.get("GCS_TRACK_URL",         "http://gcs-track-service:8090")
INJECTOR_URL  = os.environ.get("GCS_INJECTOR_URL",      "http://gcs-audio-injector:8094")
VOTE_URL      = os.environ.get("GCS_VOTE_URL",          "http://gcs-vote-service:8095")
LORE_URL      = os.environ.get("GCS_LORE_SERVICE_URL",  "http://gcs-lore-service:8096")
WEATHER_URL   = os.environ.get("GCS_WEATHER_URL",       "http://gcs-weather:8098")

app = FastAPI(title="GCS Monitoring")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def ping(url: str, name: str) -> dict:
    try:
        t0 = time.time()
        r = httpx.get(f"{url}/health", timeout=2)
        ms = round((time.time() - t0) * 1000)
        return {"name": name, "status": "ok" if r.status_code == 200 else "error",
                "latency_ms": ms, "data": r.json() if r.status_code == 200 else {}}
    except Exception as e:
        return {"name": name, "status": "unreachable", "error": str(e)[:60]}


def get_db_stats(conn) -> dict:
    stats = {}
    try:
        with conn.cursor() as cur:
            # Last rebexis
            cur.execute("""
                SELECT intervention, created_at FROM rebexis_sessions
                ORDER BY created_at DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                stats["rebexis_last"] = {
                    "text": row["intervention"][:80],
                    "at": str(row["created_at"]),
                }
            # TTS cache stats
            cur.execute("""
                SELECT COUNT(*) as total,
                       COUNT(CASE WHEN created_at > NOW() - INTERVAL '1 hour' THEN 1 END) as last_hour
                FROM gcs_tts_cache
            """)
            r = cur.fetchone()
            stats["tts_cache"] = dict(r) if r else {}

            # Injector stats from lore_events
            cur.execute("""
                SELECT COUNT(*) as injections_today
                FROM lore_events
                WHERE type='rebexis_intervention'
                  AND created_at > NOW() - INTERVAL '24 hours'
            """)
            r = cur.fetchone()
            stats["injections_today"] = r["injections_today"] if r else 0

            # Vote summary
            cur.execute("""
                SELECT vote, COUNT(*) as n FROM votes
                WHERE created_at > NOW() - INTERVAL '24 hours'
                GROUP BY vote
            """)
            votes = {r["vote"]: r["n"] for r in cur.fetchall()}
            stats["votes_today"] = votes

            # Top scored track today
            cur.execute("""
                SELECT ts.song_id, ts.score, ts.vote_count
                FROM track_scores ts ORDER BY score DESC LIMIT 3
            """)
            stats["top_tracks"] = [dict(r) for r in cur.fetchall()]

            # Festival state
            cur.execute("SELECT * FROM gcs_state WHERE id=1")
            row = cur.fetchone()
            if row:
                state = dict(row)
                for k in ("last_track", "special_events", "weather_data"):
                    if isinstance(state.get(k), str):
                        state[k] = json.loads(state[k])
                stats["festival_state"] = state

            # Phrase memory
            cur.execute("""
                SELECT COUNT(*) as n FROM rebexis_phrases
                WHERE used_at > NOW() - INTERVAL '24 hours'
            """)
            r = cur.fetchone()
            stats["phrase_memory_24h"] = r["n"] if r else 0

    except Exception as e:
        stats["db_error"] = str(e)[:100]
    return stats


@app.get("/health")
def health():
    return {"status": "ok", "service": "gcs-monitoring"}


@app.get("/status")
def full_status():
    """Complete production dashboard."""
    services = [
        ping(TRACK_URL,    "gcs-track"),
        ping(STATE_URL,    "gcs-state"),
        ping(REBEXIS_URL,  "gcs-rebexis"),
        ping(TTS_URL,      "gcs-tts"),
        ping(INJECTOR_URL, "gcs-injector"),
        ping(VOTE_URL,     "gcs-vote"),
        ping(LORE_URL,     "gcs-lore"),
        ping(WEATHER_URL,  "gcs-weather"),
    ]

    ok_count  = sum(1 for s in services if s.get("status") == "ok")
    err_count = len(services) - ok_count

    conn = get_conn()
    db_stats = get_db_stats(conn)
    conn.close()

    # Current track
    current_track = {}
    try:
        r = httpx.get(f"{TRACK_URL}/track/current", timeout=2)
        if r.status_code == 200:
            current_track = r.json()
    except Exception:
        pass

    # Injector stats
    injector_health = next((s for s in services if s["name"] == "gcs-injector"), {})
    injector_data   = injector_health.get("data", {})

    # Weather
    weather = {}
    try:
        r = httpx.get(f"{WEATHER_URL}/weather", timeout=2)
        if r.status_code == 200:
            weather = r.json()
    except Exception:
        pass

    state = db_stats.get("festival_state", {})
    last_track_state = state.get("last_track", {})
    music_profile    = last_track_state.get("music_profile", {}) if isinstance(last_track_state, dict) else {}

    return {
        "summary": {
            "services_ok":    ok_count,
            "services_error": err_count,
            "overall":        "ok" if err_count == 0 else "degraded",
        },
        "current_track": {
            "title":   current_track.get("title", ""),
            "artist":  current_track.get("artist", ""),
            "elapsed": current_track.get("elapsed", 0),
            "duration": current_track.get("duration", 0),
        },
        "festival": {
            "energy":            state.get("energy_level"),
            "target_energy":     state.get("target_energy"),
            "festival_direction": state.get("festival_direction"),
            "stage":             state.get("stage_active"),
            "tod":               state.get("time_of_day"),
            "city":              state.get("city"),
            "weather_mood":      state.get("weather_mood"),
        },
        "music_profile": music_profile,
        "weather":       weather,
        "rebexis_last":  db_stats.get("rebexis_last", {}),
        "injection": {
            "inject_active": injector_data.get("inject_active"),
            "generated":     injector_data.get("generated"),
            "injected":      injector_data.get("injected"),
            "errors":        injector_data.get("errors"),
            "injections_today": db_stats.get("injections_today"),
        },
        "tts_cache":       db_stats.get("tts_cache", {}),
        "votes_today":     db_stats.get("votes_today", {}),
        "top_tracks":      db_stats.get("top_tracks", []),
        "phrase_memory_24h": db_stats.get("phrase_memory_24h"),
        "services":        services,
    }


@app.get("/events")
def recent_events(limit: int = 20, type: Optional[str] = None):
    """Recent structured lore events (REBEXIS_GENERATED, etc.)."""
    conn = get_conn()
    with conn.cursor() as cur:
        if type:
            cur.execute("""
                SELECT id, type, description, city, created_at FROM lore_events
                WHERE type=%s ORDER BY created_at DESC LIMIT %s
            """, (type, limit))
        else:
            cur.execute("""
                SELECT id, type, description, city, created_at FROM lore_events
                ORDER BY created_at DESC LIMIT %s
            """, (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows]}


@app.get("/rebexis/phrases")
def recent_phrases(hours: int = 6):
    """Recently used Rebexis phrases for debugging."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT phrase_text, mode, used_at FROM rebexis_phrases
            WHERE used_at > NOW() - INTERVAL '%s hours'
            ORDER BY used_at DESC LIMIT 30
        """, (hours,))
        rows = cur.fetchall()
    conn.close()
    return {"phrases": [dict(r) for r in rows]}


@app.get("/tracks/scores")
def track_scores(limit: int = 20):
    """gaiverland_score for analyzed tracks."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT title, artist, mood, bpm, energy, gaiverland_score
            FROM tracks WHERE gaiverland_score IS NOT NULL
            ORDER BY gaiverland_score DESC LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"tracks": [dict(r) for r in rows]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8097)
