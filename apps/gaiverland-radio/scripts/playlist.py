"""
Moteur de playlist Gaiverland — API FastAPI.
Cohérence énergétique, transitions de mood, anti-répétition artiste,
danceability, découverte.
"""
import os, sys, subprocess, random, json

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    install_deps()
    import fastapi, uvicorn, psycopg2, httpx

from fastapi import FastAPI, Body
from fastapi.middleware.cors import CORSMiddleware
from typing import Optional
import psycopg2.extras
import datetime

DB_URL         = os.environ["DATABASE_URL"]
AZ_URL         = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY         = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION_ID  = int(os.environ.get("AZURACAST_STATION_ID", "1"))
DISCOVERY_RATIO = float(os.environ.get("DISCOVERY_RATIO", "20")) / 100

# ── Config UI par défaut ───────────────────────────────────────────────────────
DEFAULT_UI_CONFIG = {
    "work_start":        "08:20",
    "work_end":          "17:00",
    "work_days":         [1, 2, 3, 4, 5],
    "work_offset_min":   0,
    "paused_moods":      [],   # [{"name": "intense", "until": "2026-07-01"}]
    "paused_genres":     [],   # [{"name": "hardstyle", "until": "2026-07-05"}]
    "playlist_weights":  {"bien_francais": 2, "decouverte": 3, "gaiverland_ia": 1},
}

# ── Dayparting — genres avec plages horaires restreintes ──────────────────────
# Format env GENRE_HOURS : "Genre1,Genre2:HH-HH;Genre3:HH-HH"
# Exemple : "Hardstyle,Hardcore:22-06" = seulement entre 22h et 6h
# Genres sans restriction jouent à toute heure.
_DEFAULT_GENRE_HOURS = "Hardstyle,Hardcore,Happy Hardcore,Hard Techno,Hard Trance,Makina,Donk,Hands Up,Dubstep:22-06"

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

# ── Whitelist de genres — seuls ces genres électroniques sont autorisés ────────
# Les tracks avec genre_top1 non-électronique sont filtrées (French Pop, Rap, etc.)
# Les tracks avec genre_top1=NULL sont autorisées (Essentia n'a pas classifié → électro probable)
# Surcharger via env GENRE_WHITELIST="Techno,House,..." pour ajuster
_DEFAULT_GENRE_WHITELIST = (
    "Techno,House,Trance,Progressive House,Melodic Techno,Melodic Dubstep,"
    "Electronic,Dance,Hardstyle,Hardcore,Happy Hardcore,Hard Techno,Hard Trance,"
    "Makina,Donk,Hands Up,Dubstep,Drum and Bass,Drum n Bass,DnB,"
    "Electro House,Big Room,Progressive,Synthwave,Industrial,Industrial Techno,"
    "Electronica,Ambient Electronic,Future Bass,Bass House,Tech House,"
    "Deep House,Tribal,Trance,Psy-Trance,Goa,Breakbeat,UK Garage,Speed Garage"
)

GENRE_WHITELIST: list[str] = [
    g.strip()
    for g in os.environ.get("GENRE_WHITELIST", _DEFAULT_GENRE_WHITELIST).split(",")
    if g.strip()
]


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
    # Jour : festival <-> energique uniquement, jamais d'intense
    "festival":   ["festival", "energique"],
    "energique":  ["energique", "festival"],
    # Nuit : intense en priorité, festival comme buffer si stock insuffisant
    "intense":    ["intense", "festival"],
    # Secondaires (peu utilisés en auto)
    "melodique":  ["melodique", "energique"],
    "nocturne":   ["nocturne", "melodique"],
}

app = FastAPI(title="Gaiverland Playlist Engine")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def ensure_ui_config_table():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS ui_config (
                key VARCHAR PRIMARY KEY,
                value JSONB NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """)
    conn.commit()
    conn.close()


def load_ui_config() -> dict:
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM ui_config WHERE key='main'")
            row = cur.fetchone()
        conn.close()
        if row:
            cfg = DEFAULT_UI_CONFIG.copy()
            cfg.update(row["value"])
            return cfg
    except Exception:
        pass
    return DEFAULT_UI_CONFIG.copy()


def save_ui_config(cfg: dict):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO ui_config (key, value, updated_at)
            VALUES ('main', %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()
        """, (json.dumps(cfg),))
    conn.commit()
    conn.close()


def active_pauses(items: list) -> list:
    """Filtre les pauses expirées."""
    today = datetime.date.today().isoformat()
    return [p for p in items if p.get("until", "9999") >= today]


try:
    ensure_ui_config_table()
except Exception:
    pass


@app.get("/health")
def health():
    return {"status": "ok"}


# ── UI Config ─────────────────────────────────────────────────────────────────

@app.get("/config")
def get_config():
    cfg = load_ui_config()
    cfg["paused_moods"]  = active_pauses(cfg.get("paused_moods", []))
    cfg["paused_genres"] = active_pauses(cfg.get("paused_genres", []))
    return cfg


@app.post("/config")
def update_config(body: dict = Body(...)):
    cfg = load_ui_config()
    allowed = {"work_start", "work_end", "work_days", "work_offset_min",
               "paused_moods", "paused_genres", "playlist_weights"}
    for k, v in body.items():
        if k in allowed:
            cfg[k] = v
    # Nettoyer pauses expirées
    cfg["paused_moods"]  = active_pauses(cfg.get("paused_moods", []))
    cfg["paused_genres"] = active_pauses(cfg.get("paused_genres", []))
    save_ui_config(cfg)
    return cfg


@app.post("/config/pause")
def add_pause(type: str, name: str, days: int = 7):
    """Suspend un mood ou genre pour N jours."""
    if type not in ("mood", "genre"):
        return {"error": "type doit être 'mood' ou 'genre'"}
    cfg = load_ui_config()
    key = "paused_moods" if type == "mood" else "paused_genres"
    until = (datetime.date.today() + datetime.timedelta(days=days)).isoformat()
    pauses = [p for p in cfg.get(key, []) if p["name"] != name]
    pauses.append({"name": name, "until": until})
    cfg[key] = pauses
    save_ui_config(cfg)
    return {"ok": True, "name": name, "until": until}


@app.delete("/config/pause")
def remove_pause(type: str, name: str):
    """Réactive un mood ou genre mis en pause."""
    if type not in ("mood", "genre"):
        return {"error": "type doit être 'mood' ou 'genre'"}
    cfg = load_ui_config()
    key = "paused_moods" if type == "mood" else "paused_genres"
    cfg[key] = [p for p in cfg.get(key, []) if p["name"] != name]
    save_ui_config(cfg)
    return {"ok": True}


# ── Now Playing ───────────────────────────────────────────────────────────────

@app.get("/nowplaying")
def get_nowplaying():
    result = {}
    # AzuraCast nowplaying
    try:
        headers = {"X-API-Key": AZ_KEY}
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION_ID}/nowplaying", headers=headers, timeout=5)
        if r.status_code == 200:
            np = r.json()
            result["now_playing"] = {
                "title":    np.get("now_playing", {}).get("song", {}).get("title", ""),
                "artist":   np.get("now_playing", {}).get("song", {}).get("artist", ""),
                "art":      np.get("now_playing", {}).get("song", {}).get("art", ""),
                "duration": np.get("now_playing", {}).get("duration", 0),
                "elapsed":  np.get("now_playing", {}).get("elapsed", 0),
            }
            result["listeners"]  = np.get("listeners", {}).get("current", 0)
            result["is_online"]  = np.get("is_online", False)
            result["station"]    = np.get("station", {}).get("name", "")
            # Playlists actives
            result["current_playlist"] = np.get("now_playing", {}).get("playlist", "")
    except Exception as e:
        result["az_error"] = str(e)

    # État scheduler (mood, énergie)
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT mood, energy_avg, updated_at FROM radio_state WHERE id=1")
            state = cur.fetchone()
        conn.close()
        if state:
            result["mood"]       = state["mood"]
            result["energy_avg"] = float(state["energy_avg"] or 0)
            result["state_at"]   = state["updated_at"].isoformat() if state["updated_at"] else None
    except Exception:
        pass

    # Tracks en librairie
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) as total, SUM(CASE WHEN analyzed THEN 1 ELSE 0 END) as analyzed FROM tracks")
            counts = cur.fetchone()
        conn.close()
        if counts:
            result["library"] = {"total": counts["total"], "analyzed": counts["analyzed"]}
    except Exception:
        pass

    return result


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

    # Appliquer les pauses UI
    ui_cfg = load_ui_config()
    paused_mood_names  = {p["name"] for p in active_pauses(ui_cfg.get("paused_moods", []))}
    paused_genre_names = {p["name"] for p in active_pauses(ui_cfg.get("paused_genres", []))}
    candidate_moods = [m for m in candidate_moods if m not in paused_mood_names]
    if not candidate_moods:
        candidate_moods = [current_mood]  # fallback si tout est pausé
    excluded_now = list(set(excluded_now) | paused_genre_names)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id
            FROM tracks
            WHERE analyzed=TRUE AND mood = ANY(%s) AND id != ALL(%s)
              AND file_path NOT LIKE %s
              AND (genre_top1 IS NULL OR genre_top1 != ALL(%s))
              AND (genre_top1 IS NULL OR genre_top1 = ANY(%s))
            ORDER BY RANDOM() LIMIT %s
        """, (candidate_moods, recent_ids, '%rebexis_%', excluded_now or [''], GENRE_WHITELIST, count * 4))
        candidates = list(cur.fetchall())

    if not candidates:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id
                FROM tracks WHERE analyzed=TRUE AND file_path NOT LIKE %s
                  AND (genre_top1 IS NULL OR genre_top1 != ALL(%s))
                  AND (genre_top1 IS NULL OR genre_top1 = ANY(%s))
                ORDER BY RANDOM() LIMIT %s
            """, ('%rebexis_%', excluded_now or [''], GENRE_WHITELIST, count * 2))
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
