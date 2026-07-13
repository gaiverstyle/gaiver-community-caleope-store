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
# Fenêtre anti-répétition : un titre joué dans les N dernières heures est exclu
# de la sélection. 11h couvre toute la journée de travail (8h-18h) avec marge →
# aucun titre ne repasse deux fois entre 8h et 18h, tant que le stock éligible
# est assez grand pour remplir la journée (~150 titres pour 10h).
NO_REPEAT_HOURS = float(os.environ.get("NO_REPEAT_HOURS", "6"))
# Fenêtre anti-répétition RACCOURCIE pour les titres BOOSTÉS (ENCORE ≥ seuil) : les
# pépites que le chef/auditeurs soutiennent reviennent plus souvent (toutes les ~2h
# au lieu de 6h) → l'antenne penche vers les bangers votés au lieu du random dilué.
BOOST_NO_REPEAT_HOURS = float(os.environ.get("BOOST_NO_REPEAT_HOURS", "2"))
# Préférence mainstage du chef : « ça se chante » — les vocal anthems (vocalness haut,
# modèle Essentia) remontent, la techno/électro INSTRUMENTALE (vocalness bas + mood
# 'energique' = Techno/Tech House/Minimal) est reléguée aux moments creux (pas bannie).
# Tri random pondéré (Efraimidis-Spirakis) : poids = 1 + VOCAL_BIAS·vocalness − pénalité.
# vocalness NULL (pas encore backfillé) = neutre 0.5. Mettre VOCAL_BIAS=0 pour désactiver.
VOCAL_BIAS            = float(os.environ.get("VOCAL_BIAS", "2.5"))
INSTR_TECHNO_PENALTY  = float(os.environ.get("INSTR_TECHNO_PENALTY", "1.5"))
INSTR_VOCAL_THRESHOLD = float(os.environ.get("INSTR_VOCAL_THRESHOLD", "0.4"))
# Fragment SQL de poids réutilisé dans les requêtes candidats (3 params : bias, pénalité, seuil).
_VOCAL_WEIGHT_SQL = ("GREATEST(0.05, 1.0 + %s * COALESCE(vocalness, 0.5) "
                     "- %s * (CASE WHEN COALESCE(vocalness, 0.5) < %s AND mood = 'energique' "
                     "THEN 1 ELSE 0 END))")
# Beatmatch : nombre de morceaux entre deux interventions Rebexis (= play_per_songs
# de la playlist Rebexis). Les sauts de tempo/style sont calés sur ces frontières.
REBEXIS_EVERY = int(os.environ.get("REBEXIS_SONGS_INTERVAL", "3"))
# Effet des votes (spec Cassy) : score net pondéré Bible (0.6 chef/0.3 users/0.1 IA)
# sur 14 j. ≤ -0.4 → quarantaine jour (SKIP) ; ≥ +0.4 → boosté (ENCORE).
# ±0.25 (fondateur 09/07) : laisse les votes auditeurs seuls (plafond ±0.3, poids
# Bible 0.3) basculer un titre — un ENCORE/SKIP auditeur marqué mord désormais.
VOTE_SKIP_THRESHOLD   = float(os.environ.get("VOTE_SKIP_THRESHOLD", "-0.25"))
VOTE_ENCORE_THRESHOLD = float(os.environ.get("VOTE_ENCORE_THRESHOLD", "0.25"))
VOTE_WINDOW_DAYS      = int(os.environ.get("VOTE_WINDOW_DAYS", "14"))
# REVIEW n'est plus une pile manuelle (le chef ne l'arbitre plus) : il compte comme
# soft-négatif dans le score. Gentil (un REVIEW seul ne quarantaine pas), mais REVIEW +
# SKIP fait basculer. Neutralisable (=0.0) ou ajustable par env.
VOTE_REVIEW_VALUE     = float(os.environ.get("VOTE_REVIEW_VALUE", "-0.5"))
# Garde-fou anti-dur en JOURNÉE par mots-clés de TITRE : rattrape les titres durs
# mal tagués (ex. bootleg hardstyle classé « Electro House/festival », « Bass Boosted »)
# que le filtre par genre laisse passer. Appliqué seulement aux moods jour.
DAY_MOODS = ("festival", "energique", "melodique")
#  ⚠️ Uniquement des marqueurs de genre DURS sans ambiguïté (pas « hands up » ni
#  « bass boost » : ça attrape des titres house légitimes — ex. « Put Your Hands Up
#  for Detroit »). Les genres durs bien tagués restent gérés par GENRE_HOURS.
HARD_TITLE_RE = os.environ.get("HARD_TITLE_RE",
    r"(hardstyle|hardcore|happy hardcore|frenchcore|rawstyle|raw hard|gabber|uptempo|"
    r"speedcore|terrorcore|hardstyle bootleg|hard bootleg)")
# Dossiers des stations thématiques (chill/phonk/synthwave/hard/lofi) : leur contenu
# appartient à CES scènes, jamais à la Mainstage — même si l'analyzer les tague
# festival/energique. Sinon phonk/synthwave bavent sur la Mainstage.
SCENE_PATH_RE = os.environ.get("SCENE_PATH_RE", r"(/music/(chill|phonk|synthwave|hard|lofi|lofi2|phonk2)/|/bien-francais/|/gaiverland_[a-z0-9]+/media/)")

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
# On ne cantonne à la nuit que le TIER RAW/EXTRÊME (celui de Sub Zero Project) :
# hardstyle/rawstyle/hardcore/frenchcore/uptempo/gabber/speedcore/terrorcore. Le
# fondateur (09/07) : « les niveaux du dessous ça passe » → psytrance, DnB, dubstep,
# hands up, hard techno/trance, tekkno restent AUTORISÉS en journée (électro qui hype).
_DEFAULT_GENRE_HOURS = "Hardstyle,Rawstyle,Hardcore,Happy Hardcore,Frenchcore,Uptempo,Gabber,Speedcore,Terrorcore,Mentalcore,Tribecore:22-06"

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
    "Deep House,Tribal,Trance,Psy-Trance,Goa,Breakbeat,UK Garage,Speed Garage,"
    "Bassline,Nu-Disco,Electro,Future House,Bass,Euro House,Italodance,Bounce,Disco"
)

GENRE_WHITELIST: list[str] = [
    g.strip()
    for g in os.environ.get("GENRE_WHITELIST", _DEFAULT_GENRE_WHITELIST).split(",")
    if g.strip()
]

# Promotion Forza : au JOUR, on ré-inclut les titres tagués 'melodique' qui sont en
# réalité de la house/electro qui PÈTE (grosse énergie + genre dansant), et PAS le
# deep/progressive/melodic-techno mou qu'on a volontairement exclu. Élargit le vivier
# jour (~x1,6) sans importer et sans ramener le mou. Genres = sous-ensemble punchy.
FORZA_PROMOTE_GENRES: list[str] = [
    g.strip()
    for g in os.environ.get("FORZA_PROMOTE_GENRES",
        "House,Electro House,Tech House,Big Room,Future House,Bass House,Bass,"
        "Electro,Bassline,Nu-Disco,Bounce,Donk,Hands Up,Euro House,Italodance,Makina"
    ).split(",")
    if g.strip()
]
FORZA_PROMOTE_ENERGY = float(os.environ.get("FORZA_PROMOTE_ENERGY", "0.95"))


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
    # Jour : festival / energique / melodique interchangeables (jamais d'intense).
    #  melodique = house mélodique énergie ~0.92 (identique à festival) : l'exclure
    #  du jour rétrécissait le pool de moitié (194 vs 417) → répétitions forcées.
    "festival":   ["festival", "energique"],
    "energique":  ["energique", "festival"],
    "melodique":  ["melodique", "festival", "energique"],
    # Nuit : intense en priorité, festival comme buffer si stock insuffisant
    "intense":    ["intense", "festival"],
    "nocturne":   ["nocturne", "melodique"],
}

# ── Cohérence d'enchaînement — roue de Camelot (mixage harmonique) ────────────
# Essentia fournit key_note (C, C#, D…) + key_scale (major/minor). On les
# convertit en code Camelot pour évaluer la compatibilité harmonique entre deux
# morceaux consécutifs : transitions douces = clés voisines sur la roue.
_CAMELOT = {
    ("C", "major"): "8B", ("G", "major"): "9B", ("D", "major"): "10B",
    ("A", "major"): "11B", ("E", "major"): "12B", ("B", "major"): "1B",
    ("F#", "major"): "2B", ("C#", "major"): "3B", ("G#", "major"): "4B",
    ("D#", "major"): "5B", ("A#", "major"): "6B", ("F", "major"): "7B",
    ("A", "minor"): "8A", ("E", "minor"): "9A", ("B", "minor"): "10A",
    ("F#", "minor"): "11A", ("C#", "minor"): "12A", ("G#", "minor"): "1A",
    ("D#", "minor"): "2A", ("A#", "minor"): "3A", ("F", "minor"): "4A",
    ("C", "minor"): "5A", ("G", "minor"): "6A", ("D", "minor"): "7A",
}
# Normalisation enharmonique des notes renvoyées par Essentia
_ENHARM = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}


def _camelot(note: str, scale: str):
    """(numéro 1-12, lettre 'A'/'B') ou None si tonalité inconnue."""
    if not note or not scale:
        return None
    note = _ENHARM.get(note, note)
    code = _CAMELOT.get((note, scale.lower()))
    return (int(code[:-1]), code[-1]) if code else None


def _key_cost(a: dict, b: dict) -> float:
    """Coût harmonique 0 (parfait) → 1 (dissonant) entre deux morceaux."""
    ka, kb = _camelot(a.get("key_note", ""), a.get("key_scale", "")), \
             _camelot(b.get("key_note", ""), b.get("key_scale", ""))
    if not ka or not kb:
        return 0.5  # tonalité inconnue → neutre
    (na, la_), (nb, lb) = ka, kb
    if ka == kb:
        return 0.0                                        # même clé
    step = min((na - nb) % 12, (nb - na) % 12)
    if la_ == lb and step == 1:
        return 0.15                                       # voisin sur la roue (±1)
    if na == nb and la_ != lb:
        return 0.2                                        # relatif majeur/mineur
    if la_ == lb and step == 2:
        return 0.45                                       # saut de 2 (énergisant)
    return 1.0                                            # dissonant


def _transition_cost(a: dict, b: dict, covered: bool = False) -> float:
    """Coût de passer du morceau a au morceau b (0 = transition idéale).

    Beatmatch (niveau 2) : hors intervention de Rebexis, le BPM PRIME (transition
    directe beatmatchée par le smart_cross Liquidsoap → il faut des tempos proches).
    Si `covered=True`, la transition est couverte par une intervention de Rebexis
    → on relâche le BPM et on autorise le changement de tempo/style à cet endroit.
    """
    ba, bb = float(a.get("bpm") or 0), float(b.get("bpm") or 0)
    if ba and bb:
        # tolère le mixage moitié/double tempo (128 <-> 174 en DnB, etc.)
        d = min(abs(ba - bb), abs(ba - 2 * bb), abs(2 * ba - bb))
        # serré en enchaînement direct (~8 BPM), large sous la voix (~16 BPM)
        bpm_cost = min(1.0, d / (16.0 if covered else 8.0))
    else:
        bpm_cost = 0.5
    ea, eb = float(a.get("energy") or 0.5), float(b.get("energy") or 0.5)
    energy_cost = min(1.0, abs(ea - eb) / 0.35)
    kc = _key_cost(a, b)
    if covered:
        # sous l'intervention Rebexis : place ici les changements de style/tempo
        return 0.30 * kc + 0.35 * energy_cost + 0.35 * bpm_cost
    # enchaînement direct beatmatché : BPM prioritaire, puis tonalité, puis énergie
    return 0.55 * bpm_cost + 0.30 * kc + 0.15 * energy_cost


def _bpm_gap(a: dict, b: dict) -> float:
    """Écart de tempo beatmatchable (tolère moitié/double). 99 si BPM inconnu."""
    ba, bb = float(a.get("bpm") or 0), float(b.get("bpm") or 0)
    if not (ba and bb):
        return 99.0
    return min(abs(ba - bb), abs(ba - 2 * bb), abs(2 * ba - bb))


def order_for_coherence(candidates: list, count: int, start_energy: float,
                        rebexis_every: int = REBEXIS_EVERY) -> list:
    """Ordonne les candidats en un chemin BEATMATCHÉ (glouton plus proche voisin) :
    en enchaînement direct, on ne prend QUE des tempos proches (≤10 BPM → le
    smart_cross beatmatche) ; les sauts de tempo/style sont réservés aux frontières
    de bloc (tous les `rebexis_every` morceaux), là où Rebexis couvre la transition
    — ou forcés seulement si aucun tempo proche n'est disponible.
    Anti-répétition artiste sur 3 titres."""
    pool = [dict(c) for c in candidates]
    if not pool:
        return []
    # Départ : morceau le plus proche de l'énergie courante → transition douce.
    pool.sort(key=lambda t: abs(float(t.get("energy") or 0.5) - start_energy))
    ordered = [pool.pop(0)]
    while pool and len(ordered) < count:
        last = ordered[-1]
        # Rebexis joue après chaque bloc de rebexis_every morceaux : la transition
        # vers le prochain titre est "couverte" pile sur ces frontières.
        covered = rebexis_every > 0 and (len(ordered) % rebexis_every == 0)
        recent_artists = {t["artist"] for t in ordered[-3:]}
        allowed = [t for t in pool if t.get("artist") not in recent_artists] or pool
        if not covered:
            # enchaînement direct : rester en tempo proche (beatmatch) si possible
            close = [t for t in allowed if _bpm_gap(last, t) <= 10.0]
            search = close or allowed   # forcé de sauter seulement si aucun proche
        else:
            search = allowed            # sous Rebexis : saut de tempo/style permis
        nxt = min(search, key=lambda t: _transition_cost(last, t, covered))
        pool.remove(nxt)
        ordered.append(nxt)
    return ordered


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


@app.get("/votes/review")
def review_pile(limit: int = 50):
    """Pile « à arbitrer » : titres ayant reçu des votes REVIEW (14 j), présentés
    au chef en lot (pas d'effet auto sur la rotation — spec Cassy)."""
    conn = get_conn()
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id, t.artist, t.title, t.mood, t.genre_top1,
                       count(*) AS reviews, max(v.created_at) AS last_review
                FROM tracks t JOIN votes v ON v.song_id = t.song_id
                WHERE v.vote = 'REVIEW'
                  AND v.created_at > NOW() - (%s * INTERVAL '1 day')
                GROUP BY t.id, t.artist, t.title, t.mood, t.genre_top1
                ORDER BY reviews DESC, last_review DESC
                LIMIT %s
            """, (VOTE_WINDOW_DAYS, limit))
            rows = [dict(r) for r in cur.fetchall()]
    except Exception as e:
        conn.rollback()
        return {"review_pile": [], "count": 0, "error": str(e)}
    finally:
        conn.close()
    for r in rows:
        if r.get("last_review"):
            r["last_review"] = r["last_review"].isoformat()
    return {"review_pile": rows, "count": len(rows)}


@app.get("/votes/scores")
def vote_scores_debug(limit: int = 100):
    """Diagnostic : score voté pondéré (14 j) par titre, + statut jour/quarantaine."""
    conn = get_conn()
    rows = []
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id, t.artist, t.title,
                   round((0.6*COALESCE(avg(CASE WHEN v.user_role='founder'   THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0)
                        + 0.3*COALESCE(avg(CASE WHEN v.user_role='user'      THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0)
                        + 0.1*COALESCE(avg(CASE WHEN v.user_role='system_ai' THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0))::numeric, 3) AS score,
                   count(*) AS votes
                FROM tracks t JOIN votes v ON v.song_id = t.song_id
                WHERE t.song_id IS NOT NULL
                  AND v.created_at > NOW() - (%s * INTERVAL '1 day')
                GROUP BY t.id, t.artist, t.title
                ORDER BY score ASC
                LIMIT %s
            """, (VOTE_WINDOW_DAYS, limit))
            for r in cur.fetchall():
                d = dict(r); s = float(d["score"])
                d["statut"] = ("quarantaine" if s <= VOTE_SKIP_THRESHOLD
                               else "boost" if s >= VOTE_ENCORE_THRESHOLD else "normal")
                rows.append(d)
    except Exception as e:
        conn.rollback()
        return {"scores": [], "count": 0, "error": str(e)}
    finally:
        conn.close()
    return {"scores": rows, "count": len(rows),
            "seuils": {"skip": VOTE_SKIP_THRESHOLD, "encore": VOTE_ENCORE_THRESHOLD,
                       "fenetre_jours": VOTE_WINDOW_DAYS}}


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
            WHERE played_at > NOW() - (%s * INTERVAL '1 hour')
        """, (NO_REPEAT_HOURS,))
        recent_ids = [r["track_id"] for r in cur.fetchall()] or [0]
        # Fenêtre courte pour les boostés : un titre ENCORE peut revenir après 2h.
        cur.execute("""
            SELECT track_id FROM play_history
            WHERE played_at > NOW() - (%s * INTERVAL '1 hour')
        """, (BOOST_NO_REPEAT_HOURS,))
        recent_ids_boost = {r["track_id"] for r in cur.fetchall()}

    # --- Effet des votes (spec Cassy) : score net pondéré Bible sur 14 j ---
    #  SKIP net ≤ -0.4 → quarantaine jour ; ENCORE ≥ +0.4 → boost fréquence.
    #  Reliés à tracks via song_id (hash AzuraCast capté par record_plays).
    vote_scores = {}
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT t.id AS id,
                   0.6*COALESCE(avg(CASE WHEN v.user_role='founder'   THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0)
                 + 0.3*COALESCE(avg(CASE WHEN v.user_role='user'      THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0)
                 + 0.1*COALESCE(avg(CASE WHEN v.user_role='system_ai' THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0) AS score
                FROM tracks t JOIN votes v ON v.song_id = t.song_id
                WHERE t.song_id IS NOT NULL
                  AND v.created_at > NOW() - (%s * INTERVAL '1 day')
                GROUP BY t.id
            """, (VOTE_WINDOW_DAYS,))
            vote_scores = {r["id"]: float(r["score"]) for r in cur.fetchall()}
    except Exception as e:
        # table votes / colonne song_id pas encore là → aucun effet (dégradé propre)
        print(f"  ⚠ effet votes ignoré: {e}")
        conn.rollback()
    quarantined = {tid for tid, s in vote_scores.items() if s <= VOTE_SKIP_THRESHOLD}
    encored     = [tid for tid, s in vote_scores.items() if s >= VOTE_ENCORE_THRESHOLD]
    # Denylist mainstage (bouton blacklist fondateur) : bannis DÉFINITIFS, résolus song_id → track id.
    denylisted = set()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT t.id FROM tracks t JOIN mainstage_denylist d ON d.song_id = t.song_id")
            denylisted = {r["id"] for r in cur.fetchall()}
    except Exception as e:
        print(f"  ⚠ denylist ignorée: {e}")
        conn.rollback()
    # SKIP : les titres rejetés sortent de la rotation jour (exclusion, comme l'anti-répétition)
    exclude_ids = list(set(recent_ids) | quarantined | denylisted) or [0]

    candidate_moods = list({current_mood} | set(MOOD_TRANSITIONS.get(current_mood, [])))
    # En mood jour, on écarte aussi les titres dont le TITRE trahit un genre dur
    # (rattrape les mistags que le filtre genre rate). Inactif la nuit.
    day_mode = current_mood in DAY_MOODS
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
            SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id,
                   key_note, key_scale
            FROM tracks
            WHERE analyzed=TRUE
              AND ( mood = ANY(%s)
                    OR (%s AND mood = 'melodique' AND energy >= %s AND genre_top1 = ANY(%s)) )
              AND id != ALL(%s)
              AND file_path NOT LIKE %s
              AND file_path !~ %s
              AND genre_top1 IS NOT NULL
              AND genre_top1 != ALL(%s)
              AND genre_top1 = ANY(%s)
              AND (NOT %s OR title !~* %s)
            ORDER BY POWER(RANDOM(), 1.0 / """ + _VOCAL_WEIGHT_SQL + """) DESC LIMIT %s
        """, (candidate_moods, day_mode, FORZA_PROMOTE_ENERGY, FORZA_PROMOTE_GENRES, exclude_ids, '%rebexis_%', SCENE_PATH_RE, excluded_now or ['__none__'], GENRE_WHITELIST, day_mode, HARD_TITLE_RE, VOCAL_BIAS, INSTR_TECHNO_PENALTY, INSTR_VOCAL_THRESHOLD, count * 4))
        candidates = list(cur.fetchall())

    # ENCORE : les titres soutenus reviennent plus souvent — on les injecte dans le
    # vivier (priorité de fréquence) SANS forcer un rejeu le jour même (anti-répétition
    # respectée : on écarte ceux joués récemment). Ils restent bornés au thème jour.
    if encored:
        already = {c["id"] for c in candidates}
        boost_ids = [tid for tid in encored if tid not in already and tid not in recent_ids_boost and tid not in denylisted]
        if boost_ids:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id,
                           key_note, key_scale
                    FROM tracks
                    WHERE id = ANY(%s) AND analyzed=TRUE
                      AND ( mood = ANY(%s)
                            OR (%s AND mood = 'melodique' AND energy >= %s AND genre_top1 = ANY(%s)) )
                      AND file_path NOT LIKE %s
                      AND file_path !~ %s
                      AND genre_top1 = ANY(%s)
                      AND (NOT %s OR title !~* %s)
                """, (boost_ids, candidate_moods, day_mode, FORZA_PROMOTE_ENERGY, FORZA_PROMOTE_GENRES, '%rebexis_%', SCENE_PATH_RE, GENRE_WHITELIST, day_mode, HARD_TITLE_RE))
                candidates = list(cur.fetchall()) + candidates

    if not candidates:
        # Filet de secours quand le vivier est trop maigre : on relâche l'anti-
        # répétition et la quarantaine votes, mais on GARDE le mood (jamais d'intense
        # en journée) et le garde-fou titre — c'était l'ancien vecteur de fuite.
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id,
                       key_note, key_scale
                FROM tracks WHERE analyzed=TRUE AND file_path NOT LIKE %s
                  AND file_path !~ %s
                  AND id != ALL(%s)
                  AND ( mood = ANY(%s)
                        OR (%s AND mood = 'melodique' AND energy >= %s AND genre_top1 = ANY(%s)) )
                  AND genre_top1 IS NOT NULL
                  AND genre_top1 != ALL(%s)
                  AND genre_top1 = ANY(%s)
                  AND (NOT %s OR title !~* %s)
                ORDER BY POWER(RANDOM(), 1.0 / """ + _VOCAL_WEIGHT_SQL + """) DESC LIMIT %s
            """, ('%rebexis_%', SCENE_PATH_RE, list(denylisted) or [0], candidate_moods, day_mode, FORZA_PROMOTE_ENERGY, FORZA_PROMOTE_GENRES, excluded_now or ['__none__'], GENRE_WHITELIST, day_mode, HARD_TITLE_RE, VOCAL_BIAS, INSTR_TECHNO_PENALTY, INSTR_VOCAL_THRESHOLD, count * 2))
            candidates = list(cur.fetchall())

    # Ordonner en chemin harmonique fluide (clé Camelot + BPM + énergie),
    # au lieu de garder l'ordre aléatoire du tirage SQL.
    selected = order_for_coherence(candidates, count, current_energy)

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
