"""
GCS Track Service — Phase 1 GCS migration.
Polls AzuraCast Now Playing, normalizes to GCS format, pushes to gcs-state-engine on track change.
Dual-run safe: completely independent of gw-scheduler.
"""
import os, sys, subprocess, time, threading

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "httpx", "psycopg2-binary"], check=True)

try:
    import fastapi, uvicorn, httpx, psycopg2
except ImportError:
    install_deps()
    import fastapi, uvicorn, httpx, psycopg2

import psycopg2.extras
from fastapi import FastAPI

AZ_URL       = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY       = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION   = int(os.environ.get("AZURACAST_STATION_ID", "1"))
DB_URL       = os.environ["DATABASE_URL"]
STATE_URL    = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")
POLL_INTERVAL = int(os.environ.get("GCS_POLL_INTERVAL", "10"))

app = FastAPI(title="GCS Track Service")

_current_track: dict = {}
_last_song_id: str = ""
_poll_errors: int = 0


def fetch_now_playing() -> dict | None:
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=5)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def normalize(np: dict) -> dict:
    """AzuraCast Now Playing payload → GCS track format."""
    now = np.get("now_playing", {})
    song = now.get("song", {})
    elapsed = now.get("elapsed", 0)
    duration = now.get("duration", 0)
    return {
        "title":    song.get("title", ""),
        "artist":   song.get("artist", ""),
        "genre":    song.get("genre", ""),
        "art":      song.get("art", ""),
        "song_id":  song.get("id", ""),
        "elapsed":  elapsed,
        "duration": duration,
    }


def push_state(track: dict):
    try:
        r = httpx.post(f"{STATE_URL}/state/update", json={"track": track}, timeout=5)
        if r.status_code not in (200, 201):
            print(f"  ⚠ gcs-state-engine: HTTP {r.status_code}")
    except Exception as e:
        print(f"  ⚠ gcs-state-engine unreachable: {e}")


def poll_loop():
    global _current_track, _last_song_id, _poll_errors
    print(f"🎧 GCS Track Service — poll every {POLL_INTERVAL}s → {STATE_URL}")
    while True:
        try:
            np = fetch_now_playing()
            if np:
                track = normalize(np)
                song_id = track.get("song_id", "")
                if song_id and song_id != _last_song_id:
                    print(f"  → {track['artist']} — {track['title']}")
                    _current_track = track
                    _last_song_id = song_id
                    push_state(track)
                _poll_errors = 0
            else:
                _poll_errors += 1
        except Exception as e:
            _poll_errors += 1
            print(f"  ⚠ poll: {e}")
        time.sleep(POLL_INTERVAL)


@app.on_event("startup")
def startup():
    threading.Thread(target=poll_loop, daemon=True).start()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "song_id": _last_song_id,
        "poll_errors": _poll_errors,
    }


@app.get("/track/current")
def current_track():
    return _current_track or {"status": "no_track"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8090)
