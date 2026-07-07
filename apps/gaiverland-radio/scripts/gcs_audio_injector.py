"""
GCS Audio Injector — Phase 4.
Orchestre le pipeline : gcs-rebexis → gcs-tts → AzuraCast.
Shadow mode par défaut (GCS_INJECT_ACTIVE=false) : génère tout, n'injecte pas.
Ducking = injection en fin de morceau (elapsed proche de duration) — silence naturel.
"""
import os, sys, subprocess, time, threading

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

DB_URL         = os.environ["DATABASE_URL"]
REBEXIS_URL    = os.environ.get("GCS_REBEXIS_URL",    "http://gcs-rebexis:8092")
TTS_URL        = os.environ.get("GCS_TTS_URL",        "http://gcs-tts:8093")
TRACK_URL      = os.environ.get("GCS_TRACK_URL",      "http://gcs-track-service:8090")
AZ_URL         = os.environ.get("AZURACAST_URL",       "http://azuracast:80")
AZ_KEY         = os.environ.get("AZURACAST_API_KEY",   "")
AZ_STATION     = int(os.environ.get("AZURACAST_STATION_ID", "1"))
INJECT_ACTIVE  = os.environ.get("GCS_INJECT_ACTIVE",  "false").lower() == "true"
POLL_INTERVAL  = int(os.environ.get("GCS_POLL_INTERVAL", "10"))

app = FastAPI(title="GCS Audio Injector")

_last_song_id: str      = ""
_last_inject_ts: float  = 0.0
_inject_interval: float = float(os.environ.get("REBEXIS_INTERVAL_MIN", "15")) * 60
_stats: dict            = {"generated": 0, "injected": 0, "errors": 0}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def az_headers():
    return {"X-API-Key": AZ_KEY, "Content-Type": "application/json"}


def get_rebexis_playlist_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT az_rb_playlist FROM radio_state WHERE id=1")
        row = cur.fetchone()
    return (row["az_rb_playlist"] or 0) if row else 0


def purge_old_jingles(keep_latest: int = 0):
    """Delete previous gcs-rebexis jingles from AzuraCast so they never
    accumulate in the Rebexis playlist (accumulation = chained voices bug)."""
    try:
        r = httpx.get(
            f"{AZ_URL}/api/station/{AZ_STATION}/files",
            headers=az_headers(),
            params={"searchPhrase": "rebexis", "rowsPerPage": 200},
            timeout=15,
        )
        if r.status_code != 200:
            return
        data = r.json()
        rows = data.get("rows", data) if isinstance(data, dict) else data
        # Both jingle generations: gcs-rebexis/ (new stack) + rebexis_*.mp3 (legacy gw-tts)
        jingles = [f for f in rows
                   if "gcs-rebexis/" in (f.get("path") or "")
                   or (f.get("path") or "").split("/")[-1].startswith("rebexis_")]
        # Sort newest first, keep keep_latest, delete the rest
        jingles.sort(key=lambda f: f.get("uploaded_at") or 0, reverse=True)
        for f in jingles[keep_latest:]:
            try:
                httpx.delete(
                    f"{AZ_URL}/api/station/{AZ_STATION}/file/{f['id']}",
                    headers=az_headers(), timeout=10,
                )
            except Exception:
                pass
        if len(jingles) > keep_latest:
            print(f"  🧹 injector: purged {len(jingles) - keep_latest} old jingle(s)")
    except Exception as e:
        print(f"  ⚠ injector purge: {e}")


def upload_and_queue(audio_file: str, text: str, conn) -> bool:
    """Upload MP3 to AzuraCast, add to rebexis playlist.
    Purges previous jingles first — the playlist holds at most ONE pending
    intervention so voices can never chain back-to-back."""
    rb_pl = get_rebexis_playlist_id(conn)
    if not rb_pl:
        print("  ⚠ injector: no rebexis playlist ID in DB — not injecting")
        return False
    purge_old_jingles(keep_latest=0)
    try:
        import base64
        with open(audio_file, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        import os as _os
        fname = _os.path.basename(audio_file)
        r = httpx.post(
            f"{AZ_URL}/api/station/{AZ_STATION}/files",
            headers=az_headers(),
            json={"path": f"gcs-rebexis/{fname}", "file": b64},
            timeout=60,
        )
        if r.status_code not in (200, 201):
            print(f"  ⚠ injector: AZ upload HTTP {r.status_code}")
            return False
        az_id = r.json().get("id")
        if not az_id:
            return False
        # Assign to rebexis playlist
        r2 = httpx.put(
            f"{AZ_URL}/api/station/{AZ_STATION}/files/batch",
            headers=az_headers(),
            json={"do": "playlist", "files": [r.json().get("path", "")],
                  "playlists": [str(rb_pl)]},
            timeout=15,
        )
        return r2.status_code in (200, 201)
    except Exception as e:
        print(f"  ⚠ injector upload: {e}")
        return False


def should_inject(elapsed: int, duration: int) -> bool:
    """Inject near end of track (last 30s) and interval elapsed."""
    global _last_inject_ts
    interval_ok = (time.time() - _last_inject_ts) >= _inject_interval
    near_end    = duration > 0 and (duration - elapsed) <= 30
    return interval_ok and near_end


def run_pipeline(track: dict, force: bool = False):
    global _last_inject_ts, _stats
    _last_inject_ts = time.time()  # Pessimistic lock — prevent re-entry on pipeline failure

    # 1. Generate Rebexis text
    try:
        url = f"{REBEXIS_URL}/generate" + ("?force=true" if force else "")
        r = httpx.post(url, json={"track": track}, timeout=10)
        if r.status_code != 200:
            _stats["errors"] += 1
            return
        result = r.json()
        if not result.get("text"):
            return
    except Exception as e:
        print(f"  ⚠ injector → rebexis: {e}")
        _stats["errors"] += 1
        return

    emotion = result.get("emotion", "playful")
    text    = result.get("text", "")
    if not text:
        return

    _stats["generated"] += 1
    print(f"  🎙 injector generated [{emotion}]: {text[:60]}")

    # 2. Synthesize TTS
    try:
        r2 = httpx.post(f"{TTS_URL}/synthesize",
                        json={"text": text, "emotion": emotion}, timeout=40)
        if r2.status_code != 200:
            print(f"  ⚠ injector → tts: HTTP {r2.status_code}")
            return
        audio_file = r2.json().get("audio_file", "")
    except Exception as e:
        print(f"  ⚠ injector → tts: {e}")
        return

    if not audio_file:
        return

    # 3. Inject (shadow or active)
    if INJECT_ACTIVE:
        conn = get_conn()
        ok = upload_and_queue(audio_file, text, conn)
        conn.close()
        if ok:
            _stats["injected"] += 1
            _last_inject_ts = time.time()
            print(f"  ✓ injector: INJECTED [{emotion}]")
        else:
            _stats["errors"] += 1
    else:
        _last_inject_ts = time.time()
        print(f"  👁 injector SHADOW: would inject [{emotion}] → {audio_file}")


def poll_loop():
    global _last_song_id
    print(f"🔊 GCS Audio Injector — shadow={not INJECT_ACTIVE} poll={POLL_INTERVAL}s")
    while True:
        try:
            r = httpx.get(f"{TRACK_URL}/track/current", timeout=5)
            if r.status_code == 200:
                track = r.json()
                song_id  = track.get("song_id", "")
                elapsed  = int(track.get("elapsed", 0))
                duration = int(track.get("duration", 0))

                if song_id and song_id != _last_song_id:
                    _last_song_id = song_id

                if song_id and should_inject(elapsed, duration):
                    run_pipeline(track)
        except Exception as e:
            print(f"  ⚠ injector poll: {e}")
        time.sleep(POLL_INTERVAL)


@app.on_event("startup")
def startup():
    threading.Thread(target=poll_loop, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ok", "inject_active": INJECT_ACTIVE, **_stats}


@app.post("/inject/force")
def force_inject():
    """Force immediate injection for testing."""
    try:
        r = httpx.get(f"{TRACK_URL}/track/current", timeout=3)
        track = r.json() if r.status_code == 200 else {}
    except Exception:
        track = {}
    threading.Thread(target=run_pipeline, args=(track, True), daemon=True).start()
    return {"ok": True, "shadow": not INJECT_ACTIVE}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8094)
