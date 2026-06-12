"""
Moteur de playlist Gaiverland — API FastAPI.
Cohérence énergétique, transitions de mood, anti-répétition artiste,
danceability, découverte.
"""
import os, sys, subprocess, random, json

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary"], check=True)

try:
    import fastapi, uvicorn, psycopg2
except ImportError:
    install_deps()
    import fastapi, uvicorn, psycopg2

from fastapi import FastAPI
from typing import Optional
import psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]
DISCOVERY_RATIO = float(os.environ.get("DISCOVERY_RATIO", "20")) / 100

# ── Dayparting — genres avec plages horaires restreintes ──────────────────────
# Format env GENRE_HOURS : "Genre1,Genre2:HH-HH;Genre3:HH-HH"
# Exemple : "Hardstyle,Hardcore:22-06" = seulement entre 22h et 6h
# Genres sans restriction jouent à toute heure.
_DEFAULT_GENRE_HOURS = "Hardstyle,Hardcore,Happy Hardcore,Hard Techno,Hard Trance,Makina,Donk:22-06"

def _parse_genre_hours(raw: str) -> dict:
    """Retourne {genre: (start_h, end_h)} pour chaque genre restreint."""
    result = {}
    for entry in raw.split(";"):
        entry = entry.strip()
        if ":" not in entry:
            continue
        genres_part, hours_part = entry.rsplit(":", 1)
        if "-" not in hours_part:
            continue
        try:
            start, end = [int(h.strip()) for h in hours_part.split("-", 1)]
        except ValueError:
            continue
        for g in genres_part.split(","):
            g = g.strip()
            if g:
                result[g] = (start, end)
    return result

GENRE_HOURS = _parse_genre_hours(
    os.environ.get("GENRE_HOURS", _DEFAULT_GENRE_HOURS)
)

def get_excluded_genres() -> list:
    """Retourne la liste des genres hors de leur créneau horaire actuel."""
    import datetime
    now_h = datetime.datetime.now().hour
    excluded = []
    for genre, (start, end) in GENRE_HOURS.items():
        if start < end:
            # Créneau simple ex. 08-18 : autorisé si start <= h < end
            allowed = start <= now_h < end
        else:
            # Créneau sur minuit ex. 22-06 : autorisé si h >= start OR h < end
            allowed = now_h >= start or now_h < end
        if not allowed:
            excluded.append(genre)
    return excluded

MOOD_TRANSITIONS = {
    "nocturne":   ["nocturne", "melodique"],
    "melodique":  ["melodique", "energique", "nocturne"],
    "energique":  ["energique", "festival", "melodique"],
    "festival":   ["festival", "intense", "energique"],
    "intense":    ["intense", "festival", "energique"],
}

app = FastAPI(title="Gaiverland Playlist Engine")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/state")
def get_state():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT mood, energy_avg, az_gw_playlist, az_rb_playlist FROM radio_state WHERE id=1")
        state = cur.fetchone()
    return dict(state) if state else {}


@app.post("/state/mood")
def set_mood(mood: str):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE radio_state SET mood=%s, updated_at=NOW() WHERE id=1", (mood,))
    conn.commit()
    return {"ok": True, "mood": mood}


@app.get("/playlist/next")
def generate_playlist(count: int = 20, mood: Optional[str] = None):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT mood, energy_avg FROM radio_state WHERE id=1")
        state = cur.fetchone() or {}

    current_mood   = mood or state.get("mood", "energique")
    current_energy = float(state.get("energy_avg") or 0.6)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT track_id FROM play_history
            WHERE played_at > NOW() - INTERVAL '2 hours'
        """)
        recent_ids = [r["track_id"] for r in cur.fetchall()] or [0]

    candidate_moods = list({current_mood} | set(MOOD_TRANSITIONS.get(current_mood, [])))
    excluded_now = get_excluded_genres()

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id
            FROM tracks
            WHERE analyzed=TRUE AND mood = ANY(%s) AND id != ALL(%s)
              AND file_path NOT LIKE %s
              AND (genre_top1 IS NULL OR genre_top1 != ALL(%s))
            ORDER BY RANDOM() LIMIT %s
        """, (candidate_moods, recent_ids, '%rebexis_%', excluded_now or [''], count * 4))
        candidates = list(cur.fetchall())

    if not candidates:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id
                FROM tracks WHERE analyzed=TRUE AND file_path NOT LIKE %s
                  AND (genre_top1 IS NULL OR genre_top1 != ALL(%s))
                ORDER BY RANDOM() LIMIT %s
            """, ('%rebexis_%', excluded_now or [''], count * 2))
            candidates = list(cur.fetchall())

    selected = []
    target_energy = current_energy
    main_count = count - int(count * DISCOVERY_RATIO)

    for track in candidates:
        if len(selected) >= count:
            break
        # Anti-répétition artiste sur les 3 derniers titres
        if track["artist"] in {t["artist"] for t in selected[-3:]}:
            continue
        e = float(track.get("energy") or 0.5)
        if abs(e - target_energy) > 0.35 and len(selected) < main_count:
            continue
        selected.append(dict(track))
        target_energy = target_energy * 0.75 + e * 0.25  # glissement progressif

    # Compléter
    rest = [c for c in candidates if c not in selected]
    while len(selected) < count and rest:
        selected.append(dict(rest.pop()))

    if selected:
        avg_e = sum(float(t.get("energy") or 0.5) for t in selected) / len(selected)
        with conn.cursor() as cur:
            cur.execute("UPDATE radio_state SET energy_avg=%s, updated_at=NOW() WHERE id=1",
                        (round(avg_e, 3),))
        conn.commit()

    return {"mood": current_mood, "tracks": selected, "count": len(selected)}


@app.post("/history/record")
def record_play(track_id: int, mood_state: Optional[str] = None):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO play_history (track_id, mood_state) VALUES (%s, %s)",
                    (track_id, mood_state))
    conn.commit()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
