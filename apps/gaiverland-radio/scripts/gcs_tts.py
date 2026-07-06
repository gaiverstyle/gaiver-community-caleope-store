"""
GCS TTS Service — Phase 3.
Cache key: hash(text + emotion + voice_id).
Emotion influence sur les voice settings ElevenLabs.
Shadow-safe: l'upload AzuraCast est optionnel.
"""
import os, sys, subprocess, hashlib, pathlib, json, time

def _install():
    subprocess.run(["apt-get", "install", "-y", "-qq", "ffmpeg"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    _install()
    import fastapi, uvicorn, psycopg2, httpx

import psycopg2.extras
from fastapi import FastAPI, HTTPException

DB_URL        = os.environ["DATABASE_URL"]
EL_API_KEY    = os.environ.get("ELEVENLABS_API_KEY", "")
EL_VOICE_ID   = os.environ.get("ELEVENLABS_VOICE_ID", "")
EL_MODEL      = os.environ.get("ELEVENLABS_MODEL", "eleven_v3")
EL_CHARS_LIMIT= int(os.environ.get("EL_CHARS_LIMIT", "10000"))
TTS_CACHE     = pathlib.Path(os.environ.get("TTS_CACHE_DIR", "/tts-cache"))
TTS_CACHE.mkdir(parents=True, exist_ok=True)

# emotion → voice settings ElevenLabs
EMOTION_SETTINGS = {
    "calm":      {"stability": 0.55, "similarity_boost": 0.75, "style": 0.35, "use_speaker_boost": True},
    "playful":   {"stability": 0.30, "similarity_boost": 0.75, "style": 0.75, "use_speaker_boost": True},
    "excited":   {"stability": 0.20, "similarity_boost": 0.80, "style": 0.90, "use_speaker_boost": True},
    "energetic": {"stability": 0.15, "similarity_boost": 0.85, "style": 0.95, "use_speaker_boost": True},
    "amused":    {"stability": 0.35, "similarity_boost": 0.75, "style": 0.70, "use_speaker_boost": True},
}

FFMPEG_RADIO = ",".join([
    "highpass=f=90",
    "equalizer=f=2500:width_type=o:width=1.5:g=4",
    "equalizer=f=10000:width_type=o:width=2:g=3",
    "acompressor=threshold=-22dB:ratio=4:attack=3:release=150:makeup=6",
    "alimiter=limit=0.92:attack=0.5:release=3",
    "loudnorm=I=-13:LRA=5:TP=-1.0",
])

app = FastAPI(title="GCS TTS Service")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gcs_tts_cache (
                id          SERIAL PRIMARY KEY,
                cache_key   VARCHAR(64) UNIQUE NOT NULL,
                text        TEXT NOT NULL,
                emotion     VARCHAR(30) NOT NULL DEFAULT 'playful',
                voice_id    VARCHAR(100) NOT NULL,
                audio_file  TEXT,
                el_chars    INTEGER DEFAULT 0,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gcs_tts_key ON gcs_tts_cache(cache_key)")
    conn.commit()
    conn.close()


def cache_key(text: str, emotion: str, voice_id: str) -> str:
    return hashlib.sha256(f"{text}|{emotion}|{voice_id}".encode()).hexdigest()[:32]


def current_month() -> str:
    import datetime
    return datetime.date.today().strftime("%Y-%m")


def quota_used(conn) -> int:
    month = current_month()
    with conn.cursor() as cur:
        cur.execute("SELECT chars_used FROM el_monthly_quota WHERE month=%s", (month,))
        row = cur.fetchone()
    return row["chars_used"] if row else 0


def quota_add(conn, chars: int):
    month = current_month()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO el_monthly_quota (month, chars_used, chars_limit)
            VALUES (%s,%s,%s)
            ON CONFLICT (month) DO UPDATE
               SET chars_used  = el_monthly_quota.chars_used + EXCLUDED.chars_used,
                   updated_at  = NOW()
        """, (month, chars, EL_CHARS_LIMIT))
    conn.commit()


def synthesize_el(text: str, emotion: str) -> bytes:
    settings = EMOTION_SETTINGS.get(emotion, EMOTION_SETTINGS["playful"])
    el_text  = f"[playful] {text}" if not text.startswith("[") else text
    r = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE_ID}",
        headers={"xi-api-key": EL_API_KEY, "Content-Type": "application/json"},
        json={"text": el_text, "model_id": EL_MODEL, "voice_settings": settings},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"EL HTTP {r.status_code}: {r.text[:200]}")
    return r.content


def apply_radio(mp3_bytes: bytes, key: str) -> pathlib.Path:
    raw = TTS_CACHE / f"raw_{key}.mp3"
    out = TTS_CACHE / f"gcs_{key}.mp3"
    if out.exists():
        return out
    raw.write_bytes(mp3_bytes)
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(raw), "-af", FFMPEG_RADIO,
         "-b:a", "192k", "-ar", "44100", str(out)],
        capture_output=True
    )
    raw.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg: {r.stderr.decode()[:200]}")
    return out


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    conn = get_conn()
    used = quota_used(conn)
    conn.close()
    return {"status": "ok", "quota_used": used, "quota_limit": EL_CHARS_LIMIT}


@app.post("/synthesize")
def synthesize(body: dict):
    """
    Body: { text, emotion, voice_id? }
    Returns: { audio_file, cache_hit, el_chars_used, quota_remaining }
    """
    text     = body.get("text", "").strip()
    emotion  = body.get("emotion", "playful")
    voice_id = body.get("voice_id", EL_VOICE_ID)

    if not text:
        raise HTTPException(400, "text required")
    if not EL_API_KEY or not voice_id:
        raise HTTPException(503, "ElevenLabs not configured")

    key  = cache_key(text, emotion, voice_id)
    conn = get_conn()

    # Cache hit
    with conn.cursor() as cur:
        cur.execute("SELECT audio_file FROM gcs_tts_cache WHERE cache_key=%s", (key,))
        row = cur.fetchone()
    if row and row["audio_file"] and pathlib.Path(row["audio_file"]).exists():
        conn.close()
        print(f"  ✓ gcs-tts cache hit [{emotion}]")
        return {"audio_file": row["audio_file"], "cache_hit": True,
                "el_chars_used": 0, "quota_remaining": EL_CHARS_LIMIT - quota_used(get_conn())}

    # Quota check
    el_text = f"[playful] {text}" if not text.startswith("[") else text
    needed  = len(el_text)
    remaining = EL_CHARS_LIMIT - quota_used(conn)
    if remaining < needed:
        conn.close()
        raise HTTPException(429, f"EL quota insufficient ({remaining} chars remaining)")

    # Synthesize
    t0       = time.time()
    mp3      = synthesize_el(text, emotion)
    out_path = apply_radio(mp3, key)
    print(f"  ✓ gcs-tts [{emotion}] {len(el_text)}ch {time.time()-t0:.1f}s → {out_path.name}")

    # Store cache + quota
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO gcs_tts_cache (cache_key, text, emotion, voice_id, audio_file, el_chars)
            VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (cache_key) DO UPDATE SET audio_file=EXCLUDED.audio_file
        """, (key, text, emotion, voice_id, str(out_path), needed))
    conn.commit()
    quota_add(conn, needed)
    conn.close()

    return {"audio_file": str(out_path), "cache_hit": False,
            "el_chars_used": needed, "quota_remaining": remaining - needed}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8093)
