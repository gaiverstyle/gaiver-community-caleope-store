#!/bin/bash
set -euo pipefail

trap 'echo "❌ setup.sh : erreur ligne ${LINENO} — ${BASH_COMMAND}" >&2' ERR

APP_ID="gaiverland-radio"
CONFIG_DIR="${CALEOPE_BASE_DIR}/app-config/${APP_ID}"
DATA_DIR="${CALEOPE_BASE_DIR}/app-data/${APP_ID}"
SCRIPTS_DIR="${CONFIG_DIR}/scripts"

mkdir -p "${CONFIG_DIR}" "${SCRIPTS_DIR}"

# ── Nettoyage containers défaillants ──────────────────────────────────
for _ct in gaiverland-db gaiverland-analyzer gaiverland-playlist \
           gaiverland-rebexis gaiverland-tts gaiverland-scheduler gaiverland-ollama; do
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${_ct}$"; then
        echo "→ Nettoyage container '${_ct}'..."
        docker stop "${_ct}" 2>/dev/null || true
        docker rm   "${_ct}" 2>/dev/null || true
    fi
done

# ── Vérifier qu'AzuraCast tourne déjà ────────────────────────────────
echo "→ Vérification d'AzuraCast..."
if ! docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^azuracast$"; then
    echo ""
    echo "  ⚠  ATTENTION : le container 'azuracast' n'est pas en cours d'exécution."
    echo "     Ce package nécessite que le package 'azuracast' Caleope soit déjà installé."
    echo "     Lance d'abord : caleope install azuracast"
    echo ""
    # On continue quand même (l'utilisateur pourrait le démarrer après)
fi

# Vérifier que le réseau azuracast-internal existe
if ! docker network ls --format '{{.Name}}' 2>/dev/null | grep -q "^azuracast-internal$"; then
    echo "  ⚠  Réseau 'azuracast-internal' introuvable — il sera créé par AzuraCast au démarrage."
fi
echo "  ✓ Vérification terminée"

# ── Dossiers de données ───────────────────────────────────────────────
echo "→ Création des dossiers..."
mkdir -p "${DATA_DIR}/db" \
         "${DATA_DIR}/tts-cache" \
         "${DATA_DIR}/ollama"
chmod -R 755 "${DATA_DIR}/tts-cache"
echo "  ✓ Dossiers créés"

# ── Lecture des paramètres ────────────────────────────────────────────
AZURACAST_URL="${CALEOPE_PARAM_AZURACAST_URL:-http://azuracast:80}"
AZURACAST_API_KEY="${CALEOPE_PARAM_AZURACAST_API_KEY:-__CONFIGURE__}"
AZURACAST_STATION_ID="${CALEOPE_PARAM_AZURACAST_STATION_ID:-1}"
AZURACAST_STATIONS_PATH="${CALEOPE_PARAM_AZURACAST_STATIONS_PATH:-${CALEOPE_BASE_DIR}/app-data/azuracast/stations}"

REBEXIS_MODE="${CALEOPE_PARAM_REBEXIS_MODE:-template}"
REBEXIS_LLM_MODEL="${CALEOPE_PARAM_REBEXIS_LLM_MODEL:-llama3.2:3b}"
REBEXIS_API_KEY="${CALEOPE_PARAM_REBEXIS_API_KEY:-}"
REBEXIS_API_BASE="${CALEOPE_PARAM_REBEXIS_API_BASE:-https://api.openai.com/v1}"
TTS_VOICE="${CALEOPE_PARAM_TTS_VOICE:-fr_FR-upmc-medium}"
DISCOVERY_RATIO="${CALEOPE_PARAM_DISCOVERY_RATIO:-20}"
REBEXIS_INTERVAL_MIN="${CALEOPE_PARAM_REBEXIS_INTERVAL_MIN:-15}"
REBEXIS_INTERVAL_MAX="${CALEOPE_PARAM_REBEXIS_INTERVAL_MAX:-30}"

API_PORT="${CALEOPE_PORT_API:-8080}"

# ── Secrets DB ────────────────────────────────────────────────────────
echo "→ Génération des secrets..."
DB_PASSWORD=$(openssl rand -hex 20)
DB_USER="gaiverland"
DB_NAME="gaiverland"

# ── db.env ────────────────────────────────────────────────────────────
cat > "${CONFIG_DIR}/db.env" <<EOF
POSTGRES_USER=${DB_USER}
POSTGRES_PASSWORD=${DB_PASSWORD}
POSTGRES_DB=${DB_NAME}
EOF
chmod 600 "${CONFIG_DIR}/db.env"

# ── services.env ─────────────────────────────────────────────────────
cat > "${CONFIG_DIR}/services.env" <<EOF
# Base de données
DATABASE_URL=postgresql://${DB_USER}:${DB_PASSWORD}@gw-db:5432/${DB_NAME}

# AzuraCast existant
AZURACAST_URL=${AZURACAST_URL}
AZURACAST_API_KEY=${AZURACAST_API_KEY}
AZURACAST_STATION_ID=${AZURACAST_STATION_ID}

# Chemin stations sur l'hôte (pour l'analyseur)
AZURACAST_STATIONS_PATH=${AZURACAST_STATIONS_PATH}

# Rebexis
REBEXIS_MODE=${REBEXIS_MODE}
REBEXIS_INTERVAL_MIN=${REBEXIS_INTERVAL_MIN}
REBEXIS_INTERVAL_MAX=${REBEXIS_INTERVAL_MAX}

# Playlist
DISCOVERY_RATIO=${DISCOVERY_RATIO}

# TTS
TTS_VOICE=${TTS_VOICE}
TTS_CACHE_DIR=/tts-cache

# Ollama (si mode=ollama)
OLLAMA_URL=http://gaiverland-ollama:11434
OLLAMA_MODEL=${REBEXIS_LLM_MODEL}
EOF
chmod 600 "${CONFIG_DIR}/services.env"

# ── rebexis.env ───────────────────────────────────────────────────────
cat > "${CONFIG_DIR}/rebexis.env" <<EOF
REBEXIS_API_KEY=${REBEXIS_API_KEY}
REBEXIS_API_BASE=${REBEXIS_API_BASE}
EOF
chmod 600 "${CONFIG_DIR}/rebexis.env"
echo "  ✓ Fichiers de config créés"

# ── Schéma base de données ────────────────────────────────────────────
cat > "${CONFIG_DIR}/db-init.sql" <<'SQL'
CREATE TABLE IF NOT EXISTS tracks (
    id          SERIAL PRIMARY KEY,
    az_id       INTEGER UNIQUE,
    file_path   TEXT NOT NULL,
    title       TEXT,
    artist      TEXT,
    album       TEXT,
    duration    FLOAT,
    bpm         FLOAT,
    energy      FLOAT,
    has_vocals  BOOLEAN DEFAULT FALSE,
    mood        TEXT,
    genre_tags  TEXT[],
    analyzed    BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS play_history (
    id          SERIAL PRIMARY KEY,
    track_id    INTEGER REFERENCES tracks(id),
    played_at   TIMESTAMPTZ DEFAULT NOW(),
    mood_state  TEXT
);

CREATE TABLE IF NOT EXISTS rebexis_sessions (
    id              SERIAL PRIMARY KEY,
    intervention    TEXT NOT NULL,
    mood_trigger    TEXT,
    context_track   TEXT,
    audio_file      TEXT,
    played_at       TIMESTAMPTZ,
    generated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS radio_state (
    id          INTEGER PRIMARY KEY DEFAULT 1,
    mood        TEXT DEFAULT 'energique',
    energy_avg  FLOAT DEFAULT 0.6,
    last_rebexis TIMESTAMPTZ,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO radio_state (id, mood) VALUES (1, 'energique') ON CONFLICT DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_tracks_mood ON tracks(mood);
CREATE INDEX IF NOT EXISTS idx_tracks_bpm ON tracks(bpm);
CREATE INDEX IF NOT EXISTS idx_play_history_time ON play_history(played_at DESC);
SQL
echo "  ✓ Schéma DB préparé"

# ── Templates Rebexis ─────────────────────────────────────────────────
cat > "${CONFIG_DIR}/rebexis-templates.json" <<'JSON'
{
  "modes": {
    "normal": {
      "templates": [
        "Ce morceau est exactement ce qu'il fallait là.",
        "Je vais rester discrète et laisser ça tourner.",
        "C'est bien. Continuons.",
        "Aucune raison d'interrompre ça."
      ]
    },
    "hype": {
      "triggers": ["festival", "edm", "big_room"],
      "templates": [
        "Ok… là ça commence sérieusement à accélérer.",
        "On est clairement dans la montée. Bonne chance aux voisins.",
        "On reste dans l'énergie festival et franchement, ça me va très bien.",
        "Quelqu'un a commandé de l'énergie ? Livraison en cours.",
        "Je crois que les enceintes viennent de demander une augmentation."
      ]
    },
    "peak": {
      "triggers": ["hardstyle", "rawstyle"],
      "templates": [
        "Bon… celui-là, il n'est clairement pas venu pour être discret.",
        "Je vais faire semblant d'être surprise par ce drop.",
        "C'est loud. C'est voulu. C'est bien.",
        "On est au sommet. Profitez-en.",
        "Ça c'est le genre de track qui fait changer d'avis sur la vie."
      ]
    },
    "flow": {
      "triggers": ["melodique", "nocturne", "progressive"],
      "templates": [
        "On glisse vers quelque chose de plus profond. C'est agréable.",
        "Ce changement de rythme était exactement ce qu'il fallait.",
        "Melodic techno à cette heure. Les choix sont défendables.",
        "On descend en douceur. Sans perdre l'essentiel.",
        "Miss Monique approuverait probablement."
      ]
    }
  }
}
JSON
echo "  ✓ Templates Rebexis créés"

# ── Scripts Python ────────────────────────────────────────────────────
# (identiques à la version précédente — copiés depuis le package)

cat > "${SCRIPTS_DIR}/analyzer.py" <<'PYEOF'
"""
Analyseur musical — surveille les nouveaux fichiers AzuraCast
et extrait BPM, énergie, présence vocale via librosa/mutagen.
"""
import os, sys, subprocess, time

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "librosa", "mutagen", "psycopg2-binary", "inotify-simple"], check=True)

try:
    import librosa, mutagen, psycopg2, inotify_simple
except ImportError:
    print("→ Installation des dépendances analyzer...")
    install_deps()
    import librosa, mutagen, psycopg2, inotify_simple

import numpy as np

DB_URL = os.environ["DATABASE_URL"]
WATCH_DIR = "/var/azuracast/stations"
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".wav", ".aac", ".m4a"}


def get_conn():
    return psycopg2.connect(DB_URL)


def analyze_file(path: str) -> dict:
    try:
        y, sr = librosa.load(path, sr=22050, mono=True, duration=120)
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        energy = float(np.mean(librosa.feature.rms(y=y)))
        energy_norm = min(1.0, energy * 100)

        spec = np.abs(librosa.stft(y))
        freq_bins = librosa.fft_frequencies(sr=sr)
        vocal_mask = (freq_bins > 300) & (freq_bins < 3400)
        low_mask = freq_bins <= 300
        vocal_energy = float(np.mean(spec[vocal_mask]))
        low_energy = float(np.mean(spec[low_mask])) + 1e-6
        has_vocals = (vocal_energy / low_energy) > 0.3

        bpm = float(tempo[0]) if hasattr(tempo, "__len__") else float(tempo)
        if bpm > 155:
            mood = "festival" if energy_norm > 0.7 else "intense"
        elif bpm > 128:
            mood = "energique" if energy_norm > 0.5 else "melodique"
        elif bpm > 110:
            mood = "melodique"
        else:
            mood = "nocturne"

        meta = mutagen.File(path, easy=True) or {}
        title = str(meta.get("title", [""])[0]) if meta.get("title") else os.path.basename(path)
        artist = str(meta.get("artist", [""])[0]) if meta.get("artist") else "Inconnu"
        album = str(meta.get("album", [""])[0]) if meta.get("album") else ""
        duration = librosa.get_duration(y=y, sr=sr)

        return {
            "file_path": path, "title": title, "artist": artist, "album": album,
            "duration": duration, "bpm": round(bpm, 1), "energy": round(energy_norm, 3),
            "has_vocals": has_vocals, "mood": mood, "analyzed": True,
        }
    except Exception as exc:
        print(f"  ⚠ Analyse échouée pour {path}: {exc}")
        return {"file_path": path, "analyzed": False}


def save_track(conn, data: dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tracks (file_path, title, artist, album, duration, bpm, energy,
                                has_vocals, mood, analyzed)
            VALUES (%(file_path)s, %(title)s, %(artist)s, %(album)s, %(duration)s,
                    %(bpm)s, %(energy)s, %(has_vocals)s, %(mood)s, %(analyzed)s)
            ON CONFLICT (file_path) DO UPDATE SET
                bpm=EXCLUDED.bpm, energy=EXCLUDED.energy, has_vocals=EXCLUDED.has_vocals,
                mood=EXCLUDED.mood, analyzed=EXCLUDED.analyzed, updated_at=NOW()
            """, data)
    conn.commit()


def main():
    print("🎵 Analyzer démarré — surveillance de", WATCH_DIR)
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM tracks WHERE analyzed=TRUE")
        known = {r[0] for r in cur.fetchall()}

    for root, _, files in os.walk(WATCH_DIR):
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                fp = os.path.join(root, f)
                if fp not in known:
                    print(f"  → Analyse : {fp}")
                    save_track(conn, analyze_file(fp))

    inotify = inotify_simple.INotify()
    for root, dirs, _ in os.walk(WATCH_DIR):
        inotify.add_watch(root, inotify_simple.flags.CLOSE_WRITE | inotify_simple.flags.MOVED_TO)

    print("  ✓ Surveillance active")
    while True:
        events = inotify.read(timeout=5000)
        for event in events:
            name = event.name.decode() if isinstance(event.name, bytes) else event.name
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS:
                try:
                    conn.cursor().execute("SELECT 1")
                except Exception:
                    conn = get_conn()
                fp = os.path.join(WATCH_DIR, name)
                print(f"  → Nouveau fichier : {fp}")
                time.sleep(1)
                save_track(conn, analyze_file(fp))

if __name__ == "__main__":
    main()
PYEOF

cat > "${SCRIPTS_DIR}/playlist.py" <<'PYEOF'
"""
Moteur de playlist — API FastAPI générant des playlists pour AzuraCast.
"""
import os, sys, subprocess, random

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary"], check=True)

try:
    import fastapi, uvicorn, psycopg2
except ImportError:
    print("→ Installation des dépendances playlist...")
    install_deps()
    import fastapi, uvicorn, psycopg2

from fastapi import FastAPI
from typing import Optional
import psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]
DISCOVERY_RATIO = float(os.environ.get("DISCOVERY_RATIO", "20")) / 100

MOOD_TRANSITIONS = {
    "nocturne":   ["nocturne", "melodique"],
    "melodique":  ["melodique", "energique", "nocturne"],
    "energique":  ["energique", "festival", "melodique"],
    "festival":   ["festival", "intense", "energique"],
    "intense":    ["intense", "festival", "energique"],
    "euphorique": ["euphorique", "festival", "energique"],
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
        cur.execute("SELECT * FROM radio_state WHERE id=1")
        state = cur.fetchone()
    return dict(state) if state else {"mood": "energique", "energy_avg": 0.6}


@app.post("/state/mood")
def set_mood(mood: str):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE radio_state SET mood=%s, updated_at=NOW() WHERE id=1", (mood,))
    conn.commit()
    return {"ok": True, "mood": mood}


@app.get("/playlist/next")
def generate_playlist(count: int = 10, mood: Optional[str] = None):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT mood, energy_avg FROM radio_state WHERE id=1")
        state = cur.fetchone() or {"mood": "energique", "energy_avg": 0.6}

    current_mood = mood or state["mood"]
    current_energy = float(state.get("energy_avg") or 0.6)

    with conn.cursor() as cur:
        cur.execute("""
            SELECT track_id FROM play_history
            WHERE played_at > NOW() - INTERVAL '2 hours'
        """)
        recent_ids = {r["track_id"] for r in cur.fetchall()}

    candidate_moods = [current_mood] + MOOD_TRANSITIONS.get(current_mood, [current_mood])

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, artist, bpm, energy, mood, az_id
            FROM tracks WHERE analyzed=TRUE AND mood = ANY(%s) AND id != ALL(%s)
            ORDER BY RANDOM() LIMIT %s
        """, (candidate_moods, list(recent_ids) or [0], count * 3))
        candidates = list(cur.fetchall())

    if not candidates:
        with conn.cursor() as cur:
            cur.execute("SELECT id, title, artist, bpm, energy, mood, az_id FROM tracks WHERE analyzed=TRUE ORDER BY RANDOM() LIMIT %s", (count * 2,))
            candidates = list(cur.fetchall())

    selected = []
    target_energy = current_energy
    main_count = count - int(count * DISCOVERY_RATIO)

    for track in candidates:
        if len(selected) >= count:
            break
        if track["artist"] in {t["artist"] for t in selected[-3:]}:
            continue
        e = float(track.get("energy") or 0.5)
        if abs(e - target_energy) > 0.35 and len(selected) < main_count:
            continue
        selected.append(dict(track))
        target_energy = target_energy * 0.8 + e * 0.2

    remaining = [c for c in candidates if c not in selected]
    while len(selected) < count and remaining:
        selected.append(dict(remaining.pop()))

    if selected:
        avg_e = sum(float(t.get("energy") or 0.5) for t in selected) / len(selected)
        with conn.cursor() as cur:
            cur.execute("UPDATE radio_state SET energy_avg=%s, updated_at=NOW() WHERE id=1", (round(avg_e, 3),))
        conn.commit()

    return {"mood": current_mood, "tracks": selected, "count": len(selected)}


@app.post("/history/record")
def record_play(track_id: int, mood_state: Optional[str] = None):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO play_history (track_id, mood_state) VALUES (%s, %s)", (track_id, mood_state))
    conn.commit()
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8080)
PYEOF

cat > "${SCRIPTS_DIR}/rebexis.py" <<'PYEOF'
"""
Rebexis Engine — génère les textes d'intervention de l'animatrice.
Modes : template | ollama | api
"""
import os, sys, subprocess, json, random

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    print("→ Installation des dépendances rebexis...")
    install_deps()
    import fastapi, uvicorn, psycopg2, httpx

from fastapi import FastAPI
import psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]
REBEXIS_MODE = os.environ.get("REBEXIS_MODE", "template")
INTERVAL_MIN = int(os.environ.get("REBEXIS_INTERVAL_MIN", "15")) * 60
INTERVAL_MAX = int(os.environ.get("REBEXIS_INTERVAL_MAX", "30")) * 60
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://gaiverland-ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
API_KEY = os.environ.get("REBEXIS_API_KEY", "")
API_BASE = os.environ.get("REBEXIS_API_BASE", "https://api.openai.com/v1")

templates = {}
app = FastAPI(title="Rebexis Engine")

SYSTEM_PROMPT = """Tu es Rebexis, animatrice de Gaiverland Radio, radio électro.
Tu n'es pas une assistante IA. Tu es une animatrice passionnée d'électro.
Tu réagis à la musique comme une présence vivante. Jamais comme un robot.
Règles : 5 à 20 secondes max (≈ 15 à 60 mots). Tu NE décris PAS la musique.
Tu RÉAGIS à ce qu'elle fait ressentir. Humour léger OK. Pas de blagues longues.
Une ou deux phrases naturelles, radio-friendly, en français."""


def load_templates():
    global templates
    try:
        with open("/app/templates.json") as f:
            templates = json.load(f)
    except Exception as e:
        print(f"⚠ Templates: {e}")
        templates = {"modes": {"normal": {"templates": ["La musique continue."]}}}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def should_intervene(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT last_rebexis FROM radio_state WHERE id=1")
        row = cur.fetchone()
    if not row or not row["last_rebexis"]:
        return True
    import datetime
    elapsed = (datetime.datetime.now(datetime.timezone.utc) - row["last_rebexis"]).total_seconds()
    return elapsed >= random.randint(INTERVAL_MIN, INTERVAL_MAX)


def generate_template(mood: str) -> str:
    key = "hype" if mood in ("festival", "euphorique") else \
          "peak" if mood == "intense" else \
          "flow" if mood in ("nocturne", "melodique") else "normal"
    t = templates.get("modes", {}).get(key, {}).get("templates", ["La radio continue."])
    return random.choice(t)


def generate_ollama(mood: str, context: str, recent: list) -> str:
    prompt = f"Génère UNE intervention courte de Rebexis. Morceau: {context}. Ambiance: {mood}."
    if recent:
        prompt += f" Évite de répéter: {' / '.join(recent[:3])}"
    try:
        resp = httpx.post(f"{OLLAMA_URL}/api/generate",
                          json={"model": OLLAMA_MODEL, "prompt": f"{SYSTEM_PROMPT}\n\n{prompt}", "stream": False},
                          timeout=30)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        print(f"⚠ Ollama: {e}")
        return generate_template(mood)


def generate_api(mood: str, context: str, recent: list) -> str:
    user = f"Morceau: {context}. Ambiance: {mood}."
    if recent:
        user += f" Évite: {' / '.join(recent[:3])}"
    try:
        resp = httpx.post(f"{API_BASE}/chat/completions",
                          headers={"Authorization": f"Bearer {API_KEY}"},
                          json={"model": "gpt-4o-mini",
                                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                             {"role": "user", "content": user}],
                                "max_tokens": 80, "temperature": 0.9},
                          timeout=15)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"⚠ API: {e}")
        return generate_template(mood)


@app.get("/health")
def health():
    return {"status": "ok", "mode": REBEXIS_MODE}


@app.post("/generate")
def generate(mood: str = "energique", context_track: str = "", force: bool = False):
    conn = get_conn()
    if not force and not should_intervene(conn):
        return {"intervention": None, "reason": "intervalle_non_atteint"}

    with conn.cursor() as cur:
        cur.execute("SELECT intervention FROM rebexis_sessions ORDER BY generated_at DESC LIMIT 5")
        recent = [r["intervention"] for r in cur.fetchall()]

    if REBEXIS_MODE == "ollama":
        text = generate_ollama(mood, context_track, recent)
    elif REBEXIS_MODE == "api":
        text = generate_api(mood, context_track, recent)
    else:
        text = generate_template(mood)

    with conn.cursor() as cur:
        cur.execute("INSERT INTO rebexis_sessions (intervention, mood_trigger, context_track) VALUES (%s,%s,%s) RETURNING id",
                    (text, mood, context_track))
        session_id = cur.fetchone()["id"]
        cur.execute("UPDATE radio_state SET last_rebexis=NOW() WHERE id=1")
    conn.commit()

    return {"intervention": text, "session_id": session_id, "mode": REBEXIS_MODE}


if __name__ == "__main__":
    load_templates()
    print(f"🎙 Rebexis Engine — mode: {REBEXIS_MODE}")
    uvicorn.run(app, host="0.0.0.0", port=8081)
PYEOF

cat > "${SCRIPTS_DIR}/tts_worker.py" <<'PYEOF'
"""
TTS Worker — convertit les textes Rebexis en fichiers audio MP3 via Piper TTS,
puis les uploade dans AzuraCast via son API.
"""
import os, sys, subprocess, hashlib, pathlib

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "piper-tts", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    print("→ Installation des dépendances TTS...")
    install_deps()
    import fastapi, uvicorn, psycopg2, httpx

from fastapi import FastAPI
import psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]
TTS_VOICE = os.environ.get("TTS_VOICE", "fr_FR-upmc-medium")
TTS_CACHE = pathlib.Path(os.environ.get("TTS_CACHE_DIR", "/tts-cache"))
TTS_CACHE.mkdir(parents=True, exist_ok=True)
MODELS_DIR = TTS_CACHE / "models"
MODELS_DIR.mkdir(exist_ok=True)

AZ_URL = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))

app = FastAPI(title="TTS Worker")


def get_model_path():
    model_file = MODELS_DIR / f"{TTS_VOICE}.onnx"
    config_file = MODELS_DIR / f"{TTS_VOICE}.onnx.json"
    if not model_file.exists():
        print(f"  → Téléchargement modèle Piper {TTS_VOICE}...")
        lang = TTS_VOICE[:5].replace("_", "-")
        name = TTS_VOICE[6:]
        base = f"https://huggingface.co/rhasspy/piper-voices/resolve/main/{lang}/{TTS_VOICE}"
        for fname, fpath in [(f"{TTS_VOICE}.onnx", model_file), (f"{TTS_VOICE}.onnx.json", config_file)]:
            subprocess.run(["wget", "-q", "-O", str(fpath), f"{base}/{fname}"], check=True)
        print("  ✓ Modèle téléchargé")
    return str(model_file), str(config_file)


def synthesize(text: str) -> pathlib.Path:
    h = hashlib.sha256(f"{TTS_VOICE}:{text}".encode()).hexdigest()[:16]
    mp3_path = TTS_CACHE / f"rebexis_{h}.mp3"
    if mp3_path.exists():
        return mp3_path

    wav_path = TTS_CACHE / f"rebexis_{h}.wav"
    model_path, config_path = get_model_path()

    result = subprocess.run(
        ["python", "-m", "piper", "--model", model_path, "--config", config_path, "--output_file", str(wav_path)],
        input=text.encode(), capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Piper: {result.stderr.decode()}")

    subprocess.run(["ffmpeg", "-y", "-i", str(wav_path), "-b:a", "192k", str(mp3_path)],
                   capture_output=True, check=True)
    wav_path.unlink(missing_ok=True)
    return mp3_path


def upload_to_azuracast(mp3_path: pathlib.Path, title: str) -> dict:
    """Upload le fichier audio dans AzuraCast comme jingle de la station."""
    if not AZ_KEY or AZ_KEY == "__CONFIGURE__":
        return {"ok": False, "reason": "clé API non configurée"}
    try:
        with open(mp3_path, "rb") as f:
            resp = httpx.post(
                f"{AZ_URL}/api/station/{AZ_STATION}/files",
                headers={"X-API-Key": AZ_KEY},
                files={"file": (mp3_path.name, f, "audio/mpeg")},
                data={"title": title, "is_jingle": "true"},
                timeout=30,
            )
            resp.raise_for_status()
            return {"ok": True, "az_id": resp.json().get("id")}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


@app.get("/health")
def health():
    return {"status": "ok", "voice": TTS_VOICE}


@app.post("/synthesize")
def synthesize_endpoint(session_id: int, text: str):
    try:
        mp3_path = synthesize(text)
        upload_result = upload_to_azuracast(mp3_path, f"Rebexis #{session_id}")
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE rebexis_sessions SET audio_file=%s WHERE id=%s", (str(mp3_path), session_id))
        conn.commit()
        return {"ok": True, "audio_file": str(mp3_path), "upload": upload_result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/pending")
def list_pending():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, intervention, mood_trigger FROM rebexis_sessions
            WHERE audio_file IS NULL AND played_at IS NULL ORDER BY generated_at
        """)
        return {"sessions": list(cur.fetchall())}


if __name__ == "__main__":
    print(f"🔊 TTS Worker — voix: {TTS_VOICE} | AzuraCast: {AZ_URL}")
    uvicorn.run(app, host="0.0.0.0", port=8082)
PYEOF

cat > "${SCRIPTS_DIR}/scheduler.py" <<'PYEOF'
"""
Scheduler Gaiverland — orchestre playlist + Rebexis + TTS.
Se connecte à AzuraCast existant via API.
Charge CPU stable — aucun pic à la diffusion.
"""
import os, sys, subprocess, time, json

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "httpx", "psycopg2-binary"], check=True)

try:
    import httpx, psycopg2
except ImportError:
    print("→ Installation dépendances scheduler...")
    install_deps()
    import httpx, psycopg2

import psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]
AZ_URL = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))
PLAYLIST_URL = "http://gaiverland-playlist:8080"
REBEXIS_URL = "http://gaiverland-rebexis:8081"
TTS_URL = "http://gaiverland-tts:8082"
CYCLE_SECONDS = 300


def wait_for(url: str, name: str, retries: int = 30):
    for i in range(retries):
        try:
            if httpx.get(f"{url}/health", timeout=5).status_code == 200:
                print(f"  ✓ {name} prêt")
                return True
        except Exception:
            pass
        print(f"  ⏳ Attente {name}... ({i+1}/{retries})")
        time.sleep(10)
    print(f"  ⚠ {name} non disponible")
    return False


def check_azuracast():
    """Vérifie la connexion à l'AzuraCast existant."""
    try:
        resp = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}", headers={"X-API-Key": AZ_KEY}, timeout=5)
        if resp.status_code == 200:
            name = resp.json().get("name", "?")
            print(f"  ✓ AzuraCast connecté — station : {name}")
            return True
        else:
            print(f"  ⚠ AzuraCast répond {resp.status_code} — vérifier URL et clé API dans services.env")
            return False
    except Exception as e:
        print(f"  ⚠ AzuraCast inaccessible : {e}")
        return False


def process_pending_tts():
    try:
        sessions = httpx.get(f"{TTS_URL}/pending", timeout=10).json().get("sessions", [])
        for s in sessions[:2]:
            print(f"  → TTS session {s['id']}: {s['intervention'][:50]}...")
            httpx.post(f"{TTS_URL}/synthesize",
                       params={"session_id": s["id"], "text": s["intervention"]},
                       timeout=90)
    except Exception as e:
        print(f"  ⚠ TTS: {e}")


def maybe_rebexis():
    try:
        state = httpx.get(f"{PLAYLIST_URL}/state", timeout=5).json()
        mood = state.get("mood", "energique")
        resp = httpx.post(f"{REBEXIS_URL}/generate", params={"mood": mood}, timeout=30)
        data = resp.json()
        if data.get("intervention"):
            print(f"  🎙 Rebexis [{mood}]: {data['intervention'][:60]}...")
    except Exception as e:
        print(f"  ⚠ Rebexis: {e}")


def sync_playlist_with_azuracast():
    """Récupère la prochaine playlist et la soumet à AzuraCast."""
    if not AZ_KEY or AZ_KEY in ("__CONFIGURE__", ""):
        print("  ℹ Clé API AzuraCast non configurée — sync playlist désactivée")
        return
    try:
        state = httpx.get(f"{PLAYLIST_URL}/state", timeout=5).json()
        playlist = httpx.get(f"{PLAYLIST_URL}/playlist/next", params={"count": 5}, timeout=15).json()
        tracks = playlist.get("tracks", [])
        print(f"  📻 {len(tracks)} titres préparés [{state.get('mood', '?')}]")
        # Les titres avec az_id peuvent être mis en file dans AzuraCast
        # via /api/station/{id}/queue (AzuraCast ≥ 0.19)
        for t in tracks:
            if t.get("az_id"):
                try:
                    httpx.post(f"{AZ_URL}/api/station/{AZ_STATION}/queue",
                               headers={"X-API-Key": AZ_KEY},
                               json={"song_id": t["az_id"]}, timeout=5)
                except Exception:
                    pass
    except Exception as e:
        print(f"  ⚠ Sync playlist: {e}")


def main():
    print("⚙  Scheduler Gaiverland démarré")
    wait_for(PLAYLIST_URL, "Playlist Engine")
    wait_for(REBEXIS_URL, "Rebexis Engine")
    wait_for(TTS_URL, "TTS Worker")
    check_azuracast()

    print("\n✅ Boucle principale active.\n")
    cycle = 0
    while True:
        cycle += 1
        print(f"\n── Cycle #{cycle} ────────────────────────────────")
        process_pending_tts()
        maybe_rebexis()
        sync_playlist_with_azuracast()
        time.sleep(CYCLE_SECONDS)


if __name__ == "__main__":
    main()
PYEOF

echo "  ✓ Scripts Python créés"

# ── post-install.txt ──────────────────────────────────────────────────
cat > "${CONFIG_DIR}/post-install.txt" <<EOF
╔══════════════════════════════════════════════════════════════════╗
║           🎙 Gaiverland Radio IA — Post-installation             ║
╠══════════════════════════════════════════════════════════════════╣
║  Ce package est maintenant connecté à ton AzuraCast existant.   ║
║                                                                  ║
║  AzuraCast URL  : ${AZURACAST_URL}
║  Station ID     : ${AZURACAST_STATION_ID}
║  Playlist API   : http://<IP-serveur>:${API_PORT}
╠══════════════════════════════════════════════════════════════════╣
║  ⚙  CONFIGURATION OBLIGATOIRE (si clé API non fournie)          ║
║                                                                  ║
║  1. Dans AzuraCast → Administration → API Keys                  ║
║     → Créer une clé Read + Write                                 ║
║                                                                  ║
║  2. Éditer : ${CONFIG_DIR}/services.env                         ║
║     → AZURACAST_API_KEY=<ta-clé>                                 ║
║                                                                  ║
║  3. docker restart gaiverland-scheduler gaiverland-tts           ║
╠══════════════════════════════════════════════════════════════════╣
$([ "${REBEXIS_MODE}" == "ollama" ] && echo "║  🤖 MODE OLLAMA — télécharger le modèle :                        ║" || echo "")
$([ "${REBEXIS_MODE}" == "ollama" ] && echo "║  docker exec gaiverland-ollama ollama pull ${REBEXIS_LLM_MODEL}" || echo "")
║  🔊 Voix TTS    : ${TTS_VOICE}
║  🎙 Rebexis     : mode ${REBEXIS_MODE}
╠══════════════════════════════════════════════════════════════════╣
║  🎛 COMMANDES UTILES                                            ║
║                                                                  ║
║  État radio     : curl http://localhost:${API_PORT}/state        ║
║  Logs analyzer  : docker logs gaiverland-analyzer -f            ║
║  Logs rebexis   : docker logs gaiverland-rebexis -f             ║
║  Logs scheduler : docker logs gaiverland-scheduler -f           ║
║  Forcer Rebexis : curl -X POST http://localhost:8081/generate?force=true
╚══════════════════════════════════════════════════════════════════╝
EOF

echo ""
echo "✅ Gaiverland Radio IA configuré !"
echo ""
echo "   AzuraCast cible : ${AZURACAST_URL} (station ${AZURACAST_STATION_ID})"
echo "   Rebexis mode    : ${REBEXIS_MODE} | voix ${TTS_VOICE}"
echo "   Playlist API    : port ${API_PORT}"
echo ""
if [[ "${AZURACAST_API_KEY}" == "__CONFIGURE__" || -z "${AZURACAST_API_KEY}" ]]; then
    echo "   ⚠  Clé API AzuraCast manquante — configure services.env puis redémarre."
fi
if [[ "${REBEXIS_MODE}" == "ollama" ]]; then
    echo "   ℹ  Ollama : lance 'docker exec gaiverland-ollama ollama pull ${REBEXIS_LLM_MODEL}' après démarrage."
fi
