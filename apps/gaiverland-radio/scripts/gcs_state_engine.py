"""
GCS State Engine — Phase 1 v2.
Gaiverland festival state (GSE v1).
Separate gcs_state table — never touches the legacy radio_state.

Améliorations v2:
- music_profile intégré dans last_track (bpm, energy, danceability, genre)
- target_energy + festival_direction (build_up / peak / wind_down / cruise)
- weather_data JSONB (meteorologie réelle via gcs-weather)
- gaiverland_score sur les tracks
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
from fastapi import FastAPI, HTTPException

DB_URL   = os.environ["DATABASE_URL"]
GCS_CITY = os.environ.get("GCS_CITY", "Toulon")

import random, threading, time as _time

# ── MINI-SCÈNES (tournée) ─────────────────────────────────────────────────────
# Le festival part occasionnellement (surtout le week-end) donner une « mini-scène »
# dans une ville proche de Toulon (~2-3 h de route max), puis rentre. Ça fait varier
# les photos de fond (récupérées par NOM de ville) et le lore (token {ville}), et le
# site affiche « Mini-scène de <ville> ». Ville-mère = GCS_CITY (Toulon).
# CPU-only, aucune API externe : liste de villes curée en dur.
MINISCENE_ENABLED = os.environ.get("MINISCENE_ENABLED", "true").lower() == "true"
MINISCENE_HOME    = os.environ.get("MINISCENE_HOME", GCS_CITY)
MINISCENE_EVAL_S  = int(os.environ.get("MINISCENE_EVAL_S", "1800"))   # ré-évalue /30 min
MINISCENE_MIN_H   = float(os.environ.get("MINISCENE_MIN_H", "4"))     # durée mini d'une sortie
MINISCENE_MAX_H   = float(os.environ.get("MINISCENE_MAX_H", "8"))
MINISCENE_COOLDOWN_H = float(os.environ.get("MINISCENE_COOLDOWN_H", "6"))  # repos maison après retour
MINISCENE_TZ      = os.environ.get("LORE_TZ", "Europe/Paris")
# Probabilité de DÉPART à chaque évaluation quand on est à la maison (hors cooldown).
MINISCENE_P_WEEKEND = float(os.environ.get("MINISCENE_P_WEEKEND", "0.06"))
MINISCENE_P_WEEKDAY = float(os.environ.get("MINISCENE_P_WEEKDAY", "0.005"))

# Villes proches de Toulon (Provence / Côte d'Azur), ~2-3 h de route max.
MINISCENE_CITIES = [
    "Marseille", "Aix-en-Provence", "Nice", "Cannes", "Antibes", "Saint-Tropez",
    "Hyères", "Fréjus", "Bandol", "Cassis", "La Ciotat", "Aubagne",
    "Avignon", "Nîmes", "Arles", "Montpellier", "Grasse", "Menton",
    "Manosque", "Gap", "Digne-les-Bains", "Sisteron",
]

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

app = FastAPI(title="GCS State Engine v2")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    with conn.cursor() as cur:
        # Base table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gcs_state (
                id                 INTEGER PRIMARY KEY DEFAULT 1,
                city               VARCHAR(100) DEFAULT 'Toulon',
                festival_phase     VARCHAR(20)  DEFAULT 'live',
                stage_active       VARCHAR(20)  DEFAULT 'mainstage',
                energy_level       INTEGER      DEFAULT 3,
                target_energy      INTEGER      DEFAULT 4,
                festival_direction VARCHAR(20)  DEFAULT 'cruise',
                time_of_day        VARCHAR(20)  DEFAULT 'day',
                weather_mood       VARCHAR(20)  DEFAULT 'calm',
                weather_data       JSONB        DEFAULT '{}',
                last_track         JSONB        DEFAULT '{}',
                special_events     JSONB        DEFAULT '[]',
                updated_at         TIMESTAMPTZ  DEFAULT NOW()
            )
        """)
        cur.execute("INSERT INTO gcs_state (id, city) VALUES (1, %s) ON CONFLICT DO NOTHING", (GCS_CITY,))
        # Add new columns if missing (safe migration)
        for col, definition in [
            ("target_energy",      "INTEGER DEFAULT 4"),
            ("festival_direction", "VARCHAR(20) DEFAULT 'cruise'"),
            ("weather_data",       "JSONB DEFAULT '{}'"),
            # Mini-scènes (tournée)
            ("home_city",       f"VARCHAR(100) DEFAULT '{MINISCENE_HOME}'"),
            ("is_miniscene",       "BOOLEAN DEFAULT FALSE"),
            ("miniscene_until",    "TIMESTAMPTZ"),
            ("miniscene_return_after", "TIMESTAMPTZ"),  # cooldown maison
        ]:
            cur.execute(f"""
                DO $$ BEGIN
                  ALTER TABLE gcs_state ADD COLUMN IF NOT EXISTS {col} {definition};
                END $$;
            """)
        # gaiverland_score on tracks table
        cur.execute("""
            DO $$ BEGIN
              ALTER TABLE tracks ADD COLUMN IF NOT EXISTS gaiverland_score FLOAT DEFAULT NULL;
            END $$;
        """)
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


def compute_direction(current: int, target: int) -> str:
    if current < target - 1:
        return "build_up"
    elif current > target + 1:
        return "wind_down"
    elif current == target:
        return "peak"
    return "cruise"


def resolve_mood_and_profile(conn, artist: str, title: str) -> tuple[str, dict]:
    """Look up track mood + full music profile from the analyzed tracks table."""
    if not artist and not title:
        return "energique", {}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT mood, bpm, energy, danceability, genre_top1, genre_top2,
                   has_vocals, key_note, gaiverland_score
            FROM tracks
            WHERE (artist ILIKE %s OR title ILIKE %s) AND analyzed=TRUE
            LIMIT 1
        """, (artist, title))
        row = cur.fetchone()
    if not row:
        return "energique", {}

    mood = row["mood"] or "energique"
    profile = {
        "genre":           row["genre_top1"] or row["genre_top2"] or "",
        "bpm":             round(float(row["bpm"] or 0), 1),
        "energy":          round(float(row["energy"] or 0), 3),
        "danceability":    round(float(row["danceability"] or 0), 3),
        "festival_fit":    mood,
        "has_vocals":      bool(row["has_vocals"]),
        "key":             row["key_note"] or "",
        "gaiverland_score": row["gaiverland_score"],
    }
    return mood, profile


def compute_new_energy(current: int, mood: str) -> int:
    if mood in ENERGY_UP:
        return min(5, current + 1)
    if mood in ENERGY_DOWN:
        return max(1, current - 1)
    return current


def compute_gaiverland_score(conn, artist: str, title: str) -> float | None:
    """Compute and store gaiverland_score for a track."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, bpm, energy, danceability, mood,
                   (SELECT COUNT(*) FROM play_history ph
                    JOIN tracks t2 ON ph.track_id=t2.id
                    WHERE t2.artist=tracks.artist AND t2.title=tracks.title) as play_count
            FROM tracks
            WHERE (artist ILIKE %s OR title ILIKE %s) AND analyzed=TRUE LIMIT 1
        """, (artist, title))
        row = cur.fetchone()
    if not row:
        return None

    bpm    = float(row["bpm"] or 0)
    energy = float(row["energy"] or 0)
    dance  = float(row["danceability"] or 0)
    mood   = row["mood"] or ""
    plays  = int(row["play_count"] or 0)

    # BPM fitness: ideal range 128-148 for Gaiverland
    bpm_fit = max(0.0, 1.0 - abs(bpm - 138) / 60) if bpm > 0 else 0.3
    # Mood quality
    mood_quality = {
        "intense": 1.0, "festival": 0.9, "energique": 0.85,
        "melodique": 0.6, "nocturne": 0.4,
    }.get(mood, 0.5)
    # Discovery: inverse log of plays (rarer = more valuable)
    import math
    discovery = max(0.0, 1.0 - math.log(plays + 1) / 10) if plays < 100 else 0.0

    score = (
        bpm_fit       * 0.20 +
        energy        * 0.25 +
        dance         * 0.20 +
        mood_quality  * 0.25 +
        discovery     * 0.10
    )
    score = round(min(1.0, max(0.0, score)), 3)

    with conn.cursor() as cur:
        cur.execute("""
            UPDATE tracks SET gaiverland_score=%s
            WHERE (artist ILIKE %s OR title ILIKE %s) AND analyzed=TRUE
        """, (score, artist, title))
    conn.commit()
    return score


# ── Gestionnaire de mini-scènes (tournée) ────────────────────────────────────

def _is_weekend() -> bool:
    try:
        from zoneinfo import ZoneInfo
        wd = datetime.datetime.now(ZoneInfo(MINISCENE_TZ)).weekday()
    except Exception:
        wd = datetime.datetime.now().weekday()
    return wd >= 5   # samedi(5) / dimanche(6)


def _lore_transition(conn, text: str, city: str):
    """Écrit une entrée 'city_transition' dans le journal du festival (table partagée)."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lore_events (type, description, city) VALUES ('city_transition', %s, %s)",
                (text, city))
        conn.commit()
    except Exception as e:
        print(f"  ⚠ lore transition: {e}")


def _depart(conn, city: str, hours: float):
    until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=hours)
    with conn.cursor() as cur:
        cur.execute("""UPDATE gcs_state
                       SET city=%s, is_miniscene=TRUE, miniscene_until=%s
                       WHERE id=1""", (city, until))
    conn.commit()
    _lore_transition(conn, f"Le festival plie les enceintes : direction {city}. Mini-scène en approche.", city)
    print(f"  🚐 mini-scène → {city} (retour dans ~{hours:.1f} h)")


def _go_home(conn):
    cd = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=MINISCENE_COOLDOWN_H)
    with conn.cursor() as cur:
        cur.execute("""UPDATE gcs_state
                       SET city=home_city, is_miniscene=FALSE,
                           miniscene_until=NULL, miniscene_return_after=%s
                       WHERE id=1""", (cd,))
    conn.commit()
    _lore_transition(conn, f"Retour à {MINISCENE_HOME}. Le c15 connaît le chemin par cœur.", MINISCENE_HOME)
    print(f"  🏠 retour à {MINISCENE_HOME}")


def _tour_manager():
    _time.sleep(30)  # laisser init_db finir
    while True:
        try:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("""SELECT city, home_city, is_miniscene, miniscene_until,
                                      miniscene_return_after FROM gcs_state WHERE id=1""")
                st = cur.fetchone() or {}
            now = datetime.datetime.now(datetime.timezone.utc)

            if st.get("is_miniscene"):
                until = st.get("miniscene_until")
                if not until or now >= until:
                    _go_home(conn)
            else:
                cd = st.get("miniscene_return_after")
                on_cooldown = cd and now < cd
                if not on_cooldown:
                    p = MINISCENE_P_WEEKEND if _is_weekend() else MINISCENE_P_WEEKDAY
                    if random.random() < p:
                        # ville différente de la précédente
                        last = st.get("city")
                        pool = [c for c in MINISCENE_CITIES if c != last] or MINISCENE_CITIES
                        city = random.choice(pool)
                        hours = random.uniform(MINISCENE_MIN_H, MINISCENE_MAX_H)
                        _depart(conn, city, hours)
            conn.close()
        except Exception as e:
            print(f"  ⚠ tour manager: {e}")
        _time.sleep(MINISCENE_EVAL_S)


@app.post("/tour/depart")
def tour_depart(city: str = "", hours: float = 0.0):
    """Déclenche une mini-scène à la demande (test / régie). Ville libre ou tirée au sort."""
    conn = get_conn()
    c = city or random.choice(MINISCENE_CITIES)
    h = hours if hours > 0 else random.uniform(MINISCENE_MIN_H, MINISCENE_MAX_H)
    _depart(conn, c, h)
    conn.close()
    return {"miniscene": True, "city": c, "hours": round(h, 1)}


@app.post("/tour/home")
def tour_home():
    """Rappelle immédiatement le festival à la ville-mère."""
    conn = get_conn()
    _go_home(conn)
    conn.close()
    return {"miniscene": False, "city": MINISCENE_HOME}


@app.on_event("startup")
def startup():
    init_db()
    if MINISCENE_ENABLED:
        threading.Thread(target=_tour_manager, daemon=True).start()
        print(f"✓ mini-scènes actives (home={MINISCENE_HOME}, {len(MINISCENE_CITIES)} villes)")


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
    for key in ("last_track", "special_events", "weather_data"):
        if isinstance(state.get(key), str):
            state[key] = json.loads(state[key])
    return state


@app.post("/state/update")
def update_state(body: dict):
    """Receive GCS track event, update festival state."""
    track = body.get("track", {})
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT energy_level, target_energy FROM gcs_state WHERE id=1")
        row = cur.fetchone()
    current_energy = row["energy_level"] if row else 3
    target_energy  = row["target_energy"] if row else 4

    artist = track.get("artist", "") if isinstance(track, dict) else ""
    title  = track.get("title", "")  if isinstance(track, dict) else ""

    mood, music_profile = resolve_mood_and_profile(conn, artist, title)

    # Update gaiverland_score in background
    compute_gaiverland_score(conn, artist, title)

    stage     = MOOD_TO_STAGE.get(mood, "mainstage")
    energy    = compute_new_energy(current_energy, mood)
    tod       = time_of_day()
    direction = compute_direction(energy, target_energy)

    # Enrich track with music profile
    enriched_track = dict(track) if isinstance(track, dict) else {}
    if music_profile:
        enriched_track["music_profile"] = music_profile

    with conn.cursor() as cur:
        # NB : on ne touche PLUS à `city` ici. La ville courante appartient au gestionnaire
        # de mini-scènes (_tour_manager) ; l'écraser à chaque morceau ramènerait le festival
        # à Toulon en pleine mini-scène.
        cur.execute("""
            UPDATE gcs_state SET
                festival_phase     = 'live',
                stage_active       = %s,
                energy_level       = %s,
                festival_direction = %s,
                time_of_day        = %s,
                last_track         = %s::jsonb,
                updated_at         = NOW()
            WHERE id = 1
        """, (stage, energy, direction, tod, json.dumps(enriched_track)))
    conn.commit()
    conn.close()

    print(f"  ✓ state: mood={mood} energy={energy}→{target_energy}({direction}) stage={stage}")
    return {
        "mood": mood,
        "energy_level": energy,
        "target_energy": target_energy,
        "festival_direction": direction,
        "stage_active": stage,
        "time_of_day": tod,
        "city": GCS_CITY,
        "music_profile": music_profile,
    }


@app.post("/state/weather")
def set_weather(body: dict = None, weather_mood: str = None):
    """Update weather — accepts body {weather_mood, weather_data?} or query param."""
    if body and isinstance(body, dict):
        wm   = body.get("weather_mood", weather_mood or "calm")
        data = body.get("weather_data", {})
    else:
        wm, data = weather_mood or "calm", {}

    valid = {"calm", "windy", "storm", "warm", "rain", "cold"}
    if wm not in valid:
        wm = "calm"
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE gcs_state SET weather_mood=%s, weather_data=%s::jsonb, updated_at=NOW()
            WHERE id=1
        """, (wm, json.dumps(data)))
    conn.commit()
    conn.close()
    print(f"  ✓ weather updated: {wm} {data.get('temperature','')}")
    return {"ok": True, "weather_mood": wm, "weather_data": data}


@app.post("/state/phase")
def set_phase(festival_phase: str):
    """Manual override for festival_phase (live|transit|setup)."""
    valid = {"live", "transit", "setup"}
    if festival_phase not in valid:
        raise HTTPException(400, f"festival_phase must be one of {valid}")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE gcs_state SET festival_phase=%s, updated_at=NOW() WHERE id=1", (festival_phase,))
    conn.commit()
    conn.close()
    return {"ok": True, "festival_phase": festival_phase}


@app.post("/state/target-energy")
def set_target_energy(body: dict):
    """Set target energy level (1-5). Direction auto-computed from current."""
    target = int(body.get("target", 4))
    if not 1 <= target <= 5:
        raise HTTPException(400, "target must be 1-5")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT energy_level FROM gcs_state WHERE id=1")
        row = cur.fetchone()
    current = row["energy_level"] if row else 3
    direction = compute_direction(current, target)
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE gcs_state SET target_energy=%s, festival_direction=%s, updated_at=NOW()
            WHERE id=1
        """, (target, direction))
    conn.commit()
    conn.close()
    print(f"  ✓ target_energy={target} direction={direction}")
    return {"ok": True, "target_energy": target, "festival_direction": direction}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8091)
