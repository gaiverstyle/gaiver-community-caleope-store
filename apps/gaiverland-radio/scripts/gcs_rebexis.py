"""
GCS Rebexis Engine — Phase 2 v2.
Input:  { track: {title,artist,...}, state: {energy_level,stage,...} }
Output: { emotion, segment_type, text, action }
100% template-based (RPE v1) — aucune génération libre.
Améliorations v2:
- Mémoire des phrases utilisées (évite répétitions sur 24h)
- Contexte enrichi : météo, heure, ville, festival_direction, genre, bpm
- Sélection de mode contextuelle avec probabilités
- Templates : 12 modes (weather, lore, track_announcement, late_night, humor...)
"""
import os, sys, subprocess, json, random, hashlib

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
# Max phrases récentes à exclure pour éviter répétitions
PHRASE_MEMORY_HOURS = int(os.environ.get("REBEXIS_MEMORY_HOURS", "24"))
PHRASE_MEMORY_MAX   = int(os.environ.get("REBEXIS_MEMORY_MAX", "50"))

app = FastAPI(title="GCS Rebexis Engine v2")
_tpl: dict = {}

ENERGY_EMOTION = {1: "calm", 2: "calm", 3: "playful", 4: "excited", 5: "energetic"}
ENERGY_MODE    = {1: "flow",  2: "flow",   3: "normal", 4: "hype",    5: "peak"}
STAGE_SEGMENT  = {"mainstage": "announcement", "sunset": "transition",
                  "rush": "intro", "night": "outro"}

# Bible v1.1 emotion mapping — amused = running gags, calm = sunset/night.
# Overrides the energy-based default per mode.
MODE_EMOTION = {
    "lore_stagiaire": "amused",
    "lore_c15":       "amused",
    "lore_festival":  "playful",
    "humor":          "amused",
    "late_night":     "calm",
    "flow":           "calm",
    "transition_down":"calm",
    "peak":           "energetic",
    "transition_up":  "energetic",
    "hype":           "excited",
    "track_announcement": "excited",
    "reaction":       "excited",
}
# Bible segment_type per mode (fallback = stage-based)
MODE_SEGMENT = {
    "lore_stagiaire": "joke",
    "humor":          "joke",
    "track_announcement": "announcement",
    "transition_up":  "transition",
    "transition_down":"transition",
    "late_night":     "outro",
    "city":           "announcement",
}


def load_tpl():
    global _tpl
    for path in ("/app/templates.json", "/app/rebexis-templates.json"):
        try:
            with open(path) as f:
                _tpl = json.load(f)
            print(f"  ✓ templates chargés depuis {path} ({len(_tpl.get('modes',{}))} modes)")
            return
        except Exception:
            pass
    _tpl = {"modes": {"normal": {"templates": ["La musique continue."]}}}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_phrases_table(conn):
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rebexis_phrases (
                id          SERIAL PRIMARY KEY,
                phrase_hash VARCHAR(32) NOT NULL UNIQUE,
                phrase_text TEXT        NOT NULL,
                mode        VARCHAR(30) DEFAULT '',
                used_at     TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_phrases_used ON rebexis_phrases(used_at DESC)")
    conn.commit()


def fetch_state() -> dict:
    try:
        r = httpx.get(f"{STATE_URL}/state/current", timeout=3)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def get_recent_phrase_hashes(conn) -> set:
    """Return hashes of phrases used in the last PHRASE_MEMORY_HOURS hours."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT phrase_hash FROM rebexis_phrases
            WHERE used_at > NOW() - INTERVAL '%s hours'
            ORDER BY used_at DESC LIMIT %s
        """, (PHRASE_MEMORY_HOURS, PHRASE_MEMORY_MAX))
        return {r["phrase_hash"] for r in cur.fetchall()}


def record_phrase(conn, text: str, mode: str):
    h = hashlib.md5(text.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO rebexis_phrases (phrase_hash, phrase_text, mode)
            VALUES (%s, %s, %s) ON CONFLICT (phrase_hash) DO UPDATE SET used_at=NOW()
        """, (h, text, mode))
    conn.commit()
    # Cleanup old entries
    with conn.cursor() as cur:
        cur.execute("""
            DELETE FROM rebexis_phrases
            WHERE used_at < NOW() - INTERVAL '%s hours'
        """, (PHRASE_MEMORY_HOURS * 2,))
    conn.commit()


def phrase_hash(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def pick_avoiding_recent(mode: str, track: dict, recent_hashes: set,
                         state: dict = None) -> tuple[str, bool]:
    """Pick a phrase from mode pool, avoiding recently used ones.
    Uses templates_with_next when artist/title are known (RPE announce style),
    templates_no_next otherwise; falls back to the plain templates list.
    Returns (text, was_fresh). Falls back to any phrase if all used."""
    state = state or {}
    modes = _tpl.get("modes", {})
    entry = modes.get(mode, {})

    has_track = bool(track.get("artist") and track.get("title"))
    tod = (state.get("time_of_day") or "day")
    # Lore jour/nuit : pioche la sous-liste collant à l'heure courante (catalogue Rebexis)
    if entry.get("templates_day") or entry.get("templates_night"):
        pool = entry.get("templates_night") if tod == "night" else entry.get("templates_day")
        pool = pool or entry.get("templates_day") or entry.get("templates_night")
    elif has_track and entry.get("templates_with_next"):
        pool = entry["templates_with_next"]
    elif not has_track and entry.get("templates_no_next"):
        pool = entry["templates_no_next"]
    else:
        pool = entry.get("templates")
    if not pool:
        pool = modes.get("normal", {}).get("templates", ["La musique continue."])

    music_profile = track.get("music_profile", {}) if isinstance(track, dict) else {}
    genre = (music_profile.get("genre") or track.get("genre_top1") or "électro")

    # Expand all variables first to compute accurate hashes
    def expand(t: str) -> str:
        t = t.replace("{artist}", track.get("artist", "l'artiste") or "l'artiste")
        t = t.replace("{title}", track.get("title", "ce morceau") or "ce morceau")
        t = t.replace("{city}",  state.get("city", "Toulon") or "Toulon")
        t = t.replace("{ville}", state.get("city", "Toulon") or "Toulon")
        t = t.replace("{genre}", str(genre))
        return t

    fresh = [t for t in pool if phrase_hash(expand(t)) not in recent_hashes]

    if fresh:
        chosen = random.choice(fresh)
        return expand(chosen), True
    # All recently used — pick randomly anyway but signal repetition
    chosen = random.choice(pool)
    return expand(chosen), False


def select_mode(energy: int, stage: str, tod: str, weather_mood: str,
                festival_direction: str, track: dict) -> str:
    """Context-aware mode selection with probability modifiers."""
    has_artist = bool(track.get("artist") and track.get("title"))

    # Base mode from energy
    base = ENERGY_MODE.get(energy, "normal")
    if tod == "night" and energy <= 2:
        base = "late_night"

    # Context override candidates with (probability, mode)
    candidates = []

    # Track announcement — prioritize when we have artist info
    if has_artist and energy >= 3:
        candidates.append((0.35, "track_announcement"))

    # Weather injection
    if weather_mood == "warm":
        candidates.append((0.12, "weather_warm"))
    elif weather_mood in ("storm", "windy"):
        candidates.append((0.10, "weather_rain"))

    # Lore injection — random, low probability
    candidates.append((0.07, "lore_c15"))
    candidates.append((0.05, "lore_stagiaire"))
    candidates.append((0.06, "lore_festival"))
    candidates.append((0.06, "humor"))
    candidates.append((0.05, "city"))

    # Festival direction transitions
    if festival_direction == "build_up" and energy >= 3:
        candidates.append((0.12, "transition_up"))
    elif festival_direction == "wind_down" and energy <= 3:
        candidates.append((0.10, "transition_down"))

    # Crowd reaction
    candidates.append((0.08, "reaction"))

    # Late night boost
    if tod == "night":
        candidates.append((0.15, "late_night"))

    # Roll dice for each candidate
    for prob, cand_mode in candidates:
        if random.random() < prob:
            # Validate mode exists in templates
            if cand_mode in _tpl.get("modes", {}):
                return cand_mode

    return base


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
    conn = get_conn()
    init_phrases_table(conn)
    conn.close()


@app.get("/health")
def health():
    modes = list(_tpl.get("modes", {}).keys())
    return {"status": "ok", "templates_loaded": bool(_tpl), "modes": modes}


@app.post("/generate")
def generate(body: dict = None, force: bool = False):
    body  = body or {}
    state = body.get("state") or fetch_state()
    track = body.get("track") or state.get("last_track") or {}

    conn = get_conn()
    if not should_fire(conn, force):
        conn.close()
        return {"intervention": None, "reason": "interval_not_reached"}

    energy           = int(state.get("energy_level", 3))
    stage            = str(state.get("stage_active", "mainstage"))
    tod              = str(state.get("time_of_day", "day"))
    weather_mood     = str(state.get("weather_mood", "calm"))
    city             = str(state.get("city", "Toulon"))
    festival_dir     = str(state.get("festival_direction", "cruise"))

    # Extract music profile from last_track if available
    music_profile = track.get("music_profile", {}) if isinstance(track, dict) else {}
    genre = (music_profile.get("genre") or track.get("genre_top1", "")) if track else ""
    bpm   = music_profile.get("bpm") or track.get("bpm", 0)

    # Select mode contextually
    mode = select_mode(energy, stage, tod, weather_mood, festival_dir, track)

    # Bible mapping: mode-specific emotion/segment first, energy/stage as fallback
    emotion      = MODE_EMOTION.get(mode) or ENERGY_EMOTION.get(energy, "playful")
    segment_type = MODE_SEGMENT.get(mode) or STAGE_SEGMENT.get(stage, "announcement")

    # Get recent phrase hashes for dedup
    recent_hashes = get_recent_phrase_hashes(conn)

    text, was_fresh = pick_avoiding_recent(mode, track, recent_hashes, state)
    action       = "announce_track" if track.get("title") else "play_music"

    # Record this phrase in memory
    record_phrase(conn, text, mode)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO rebexis_sessions (intervention, mood_trigger, context_track)
            VALUES (%s,%s,%s) RETURNING id
        """, (text,
              f"energy={energy} stage={stage} emotion={emotion} mode={mode} weather={weather_mood}",
              track.get("title", "") if isinstance(track, dict) else ""))
        sid = cur.fetchone()["id"]
        cur.execute("UPDATE radio_state SET last_rebexis=NOW() WHERE id=1")
    conn.commit()
    conn.close()

    log_lore(text, state)
    fresh_marker = "" if was_fresh else " (repeat pool exhausted)"
    print(f"  ✓ rebexis [{emotion}/{mode}]{fresh_marker}: {text[:60]}")

    return {
        "emotion":      emotion,
        "segment_type": segment_type,
        "text":         text,
        "action":       action,
        "session_id":   sid,
        "mode":         mode,
        "context": {
            "energy": energy, "stage": stage, "tod": tod,
            "weather_mood": weather_mood, "city": city,
            "festival_direction": festival_dir,
            "genre": genre, "bpm": bpm,
        }
    }


@app.get("/phrases/recent")
def recent_phrases(hours: int = 6, limit: int = 20):
    """Debugging endpoint — see recently used phrases."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT phrase_text, mode, used_at FROM rebexis_phrases
            WHERE used_at > NOW() - INTERVAL '%s hours'
            ORDER BY used_at DESC LIMIT %s
        """, (hours, limit))
        rows = cur.fetchall()
    conn.close()
    return {"phrases": [dict(r) for r in rows]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8092)
