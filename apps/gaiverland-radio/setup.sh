#!/bin/bash
set -euo pipefail
trap 'echo "❌ setup.sh erreur ligne ${LINENO} — ${BASH_COMMAND}" >&2' ERR

APP_ID="gaiverland-radio"
CONFIG_DIR="${CALEOPE_BASE_DIR}/app-config/${APP_ID}"
DATA_DIR="${CALEOPE_BASE_DIR}/app-data/${APP_ID}"
SCRIPTS_DIR="${CONFIG_DIR}/scripts"
COMPOSE_DIR="${CALEOPE_BASE_DIR}/apps-installed/${APP_ID}"

# Support NAS : CALEOPE_PARAM_STORAGE_PATH fourni via --storage <path>
STORAGE_PATH="${CALEOPE_PARAM_STORAGE_PATH:-${DATA_DIR}}"

mkdir -p "${CONFIG_DIR}" "${SCRIPTS_DIR}" \
         "${DATA_DIR}/db" \
         "${STORAGE_PATH}/tts-cache" "${STORAGE_PATH}/ollama" \
         "${STORAGE_PATH}/essentia-models" "${STORAGE_PATH}/backups" \
         "${CALEOPE_BASE_DIR}/app-data/azuracast/stations"

# ── Nettoyage containers ───────────────────────────────────────────────
for _ct in gaiverland-db gaiverland-analyzer gaiverland-playlist \
           gaiverland-rebexis gaiverland-tts gaiverland-scheduler \
           gaiverland-ollama gaiverland-bootstrap; do
    if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -q "^${_ct}$"; then
        docker stop "${_ct}" 2>/dev/null || true
        docker rm   "${_ct}" 2>/dev/null || true
    fi
done

# ════════════════════════════════════════════════════════════════════════
# AUTO-DÉTECTION AZURACAST
# ════════════════════════════════════════════════════════════════════════
echo ""
echo "┌─────────────────────────────────────────────────────────────────┐"
echo "│  🔍 Détection AzuraCast                                         │"
echo "└─────────────────────────────────────────────────────────────────┘"

AZ_EXISTING=false
AZ_NETWORK_EXTERNAL=true
AZ_NETWORK_NAME="azuracast-internal"

if docker ps --format '{{.Names}}' 2>/dev/null | grep -q "^azuracast$"; then
    AZ_EXISTING=true
    echo "  ✓ Container 'azuracast' détecté — mode addon (connexion à l'instance existante)"
    AZ_NETWORK_EXTERNAL=true
    COMPOSE_PROFILES_VALUE=""
    # Détecter le nom réel du réseau interne AzuraCast (peut être préfixé par Docker Compose)
    _DETECTED=$(docker inspect azuracast \
        --format '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' 2>/dev/null \
        | tr ' ' '\n' | grep -i "internal" | head -1 || true)
    if [[ -z "${_DETECTED}" ]]; then
        _DETECTED=$(docker network ls --format '{{.Name}}' 2>/dev/null \
            | grep -i "azuracast.*internal" | head -1 || true)
    fi
    [[ -n "${_DETECTED}" ]] && AZ_NETWORK_NAME="${_DETECTED}"
    echo "  ✓ Réseau AzuraCast : ${AZ_NETWORK_NAME}"
else
    echo "  ℹ AzuraCast absent — mode autonome (AzuraCast sera déployé avec ce package)"
    AZ_NETWORK_EXTERNAL=false
    AZ_NETWORK_NAME="azuracast-internal"
    COMPOSE_PROFILES_VALUE="with-azuracast"
    # Créer le réseau avant que compose le déclare en external
    docker network create azuracast-internal 2>/dev/null || true
    docker network create caleope-public 2>/dev/null || true
    docker stop azuracast 2>/dev/null || true
    docker rm   azuracast 2>/dev/null || true
fi

# Écrire le .env Docker Compose (lu depuis le répertoire du compose.yml)
cat > "${CONFIG_DIR}/.env" <<EOF
COMPOSE_PROFILES=${COMPOSE_PROFILES_VALUE}
AZURACAST_NETWORK_EXTERNAL=${AZ_NETWORK_EXTERNAL}
AZURACAST_NETWORK_NAME=${AZ_NETWORK_NAME}
AZURACAST_STATIONS_PATH=${CALEOPE_BASE_DIR}/app-data/azuracast/stations
WEB_PORT=${CALEOPE_PORT_WEB:-8099}
SFTP_PORT=${CALEOPE_PORT_SFTP:-2022}
ICECAST_PORT=${CALEOPE_PORT_ICECAST:-8000}
API_PORT=${CALEOPE_PORT_API:-8080}
EOF

# Copier .env dans le répertoire du compose (Docker Compose v2 le lit depuis là)
if [[ -d "${COMPOSE_DIR}" ]]; then
    cp "${CONFIG_DIR}/.env" "${COMPOSE_DIR}/.env"
fi

# ── Paramètres ────────────────────────────────────────────────────────
AZ_URL="${CALEOPE_PARAM_AZURACAST_URL:-http://azuracast:80}"
AZ_API_KEY="${CALEOPE_PARAM_AZURACAST_API_KEY:-}"
AZ_STATION_ID="${CALEOPE_PARAM_AZURACAST_STATION_ID:-1}"
STATION_NAME="${CALEOPE_PARAM_STATION_NAME:-Gaiverland Radio}"
STATION_SHORT=$(echo "${STATION_NAME}" | tr '[:upper:]' '[:lower:]' \
    | iconv -f utf-8 -t ascii//TRANSLIT 2>/dev/null \
    | sed 's/[^a-z0-9]//g' | cut -c1-16) || STATION_SHORT="gaiverland"
[[ -z "${STATION_SHORT}" ]] && STATION_SHORT="gaiverland"

REBEXIS_MODE="${CALEOPE_PARAM_REBEXIS_MODE:-template}"
REBEXIS_LLM_MODEL="${CALEOPE_PARAM_REBEXIS_LLM_MODEL:-llama3.2:3b}"
REBEXIS_API_KEY="${CALEOPE_PARAM_REBEXIS_API_KEY:-}"
REBEXIS_API_BASE="${CALEOPE_PARAM_REBEXIS_API_BASE:-https://api.openai.com/v1}"
REBEXIS_INTERVAL_MIN="${CALEOPE_PARAM_REBEXIS_INTERVAL_MIN:-15}"
REBEXIS_INTERVAL_MAX="${CALEOPE_PARAM_REBEXIS_INTERVAL_MAX:-30}"
DISCOVERY_RATIO="${CALEOPE_PARAM_DISCOVERY_RATIO:-20}"

WEB_PORT="${CALEOPE_PORT_WEB:-8099}"
SFTP_PORT="${CALEOPE_PORT_SFTP:-2022}"
ICECAST_PORT="${CALEOPE_PORT_ICECAST:-8500}"
API_PORT="${CALEOPE_PORT_API:-8080}"

# ── Secrets DB ─────────────────────────────────────────────────────────
DB_PASSWORD=$(openssl rand -hex 20)

# ── Secrets AzuraCast (mode autonome seulement) ────────────────────────
AZ_ADMIN_EMAIL="admin@azuracast.local"
AZ_ADMIN_PASSWORD=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | cut -c1-16)
AZ_MYSQL_ROOT=$(openssl rand -hex 24)
AZ_MYSQL_PWD=$(openssl rand -hex 20)

if [[ "${AZ_EXISTING}" == "true" ]]; then
    AZ_BASE_URL="${AZ_URL}"
else
    INTERACTIVE=false; [ -t 0 ] && INTERACTIVE=true
    if [[ -n "${CALEOPE_DOMAIN:-}" ]]; then
        AZ_BASE_URL="https://${CALEOPE_DOMAIN}"
    elif [[ "${INTERACTIVE}" == "true" ]]; then
        read -rp "  IP ou domaine du serveur : " _HOST || _HOST="localhost"
        AZ_BASE_URL="http://${_HOST}:${WEB_PORT}"
    else
        AZ_BASE_URL="http://localhost:${WEB_PORT}"
    fi
fi

# ── azuracast.env (mode autonome) ──────────────────────────────────────
cat > "${CONFIG_DIR}/azuracast.env" <<EOF
APPLICATION_ENV=production
AZURACAST_VERSION=0.23.4
AZURACAST_HTTP_PORT=80
AZURACAST_HTTPS_PORT=443
AZURACAST_SFTP_PORT=2022
CALEOPE_PORT_ICECAST=${ICECAST_PORT}
AZURACAST_BASE_URL=${AZ_BASE_URL}
MYSQL_ROOT_PASSWORD=${AZ_MYSQL_ROOT}
MYSQL_USER=azuracast
MYSQL_PASSWORD=${AZ_MYSQL_PWD}
MYSQL_DATABASE=azuracast
AZURACAST_ADMIN_EMAIL=${AZ_ADMIN_EMAIL}
AZURACAST_ADMIN_PASSWORD=${AZ_ADMIN_PASSWORD}
AZURACAST_STATION_NAME=${STATION_NAME}
AZURACAST_STATION_SHORT=${STATION_SHORT}
EOF
chmod 600 "${CONFIG_DIR}/azuracast.env"

# ── db.env ────────────────────────────────────────────────────────────
cat > "${CONFIG_DIR}/db.env" <<EOF
POSTGRES_USER=gaiverland
POSTGRES_PASSWORD=${DB_PASSWORD}
POSTGRES_DB=gaiverland
EOF
chmod 600 "${CONFIG_DIR}/db.env"

# ── services.env ──────────────────────────────────────────────────────
cat > "${CONFIG_DIR}/services.env" <<EOF
DATABASE_URL=postgresql://gaiverland:${DB_PASSWORD}@gw-db:5432/gaiverland
AZURACAST_URL=${AZ_URL}
AZURACAST_API_KEY=${AZ_API_KEY}
AZURACAST_STATION_ID=${AZ_STATION_ID}
AZURACAST_STATIONS_PATH=${CALEOPE_BASE_DIR}/app-data/azuracast/stations
AZURACAST_GW_PLAYLIST_ID=
AZURACAST_REBEXIS_PLAYLIST_ID=
REBEXIS_MODE=${REBEXIS_MODE}
REBEXIS_INTERVAL_MIN=${REBEXIS_INTERVAL_MIN}
REBEXIS_INTERVAL_MAX=${REBEXIS_INTERVAL_MAX}
DISCOVERY_RATIO=${DISCOVERY_RATIO}
TTS_CACHE_DIR=/tts-cache
TTS_HOME=/tts-cache
ESSENTIA_MODELS_DIR=/essentia-models
OLLAMA_URL=http://gaiverland-ollama:11434
OLLAMA_MODEL=${REBEXIS_LLM_MODEL}
EOF
chmod 600 "${CONFIG_DIR}/services.env"

# ── rebexis.env ────────────────────────────────────────────────────────
cat > "${CONFIG_DIR}/rebexis.env" <<EOF
REBEXIS_API_KEY=${REBEXIS_API_KEY}
REBEXIS_API_BASE=${REBEXIS_API_BASE}
EOF
chmod 600 "${CONFIG_DIR}/rebexis.env"
echo "  ✓ Fichiers de config créés"

# ════════════════════════════════════════════════════════════════════════
# SCHÉMA BASE DE DONNÉES
# ════════════════════════════════════════════════════════════════════════
cat > "${CONFIG_DIR}/db-init.sql" <<'SQL'
CREATE TABLE IF NOT EXISTS tracks (
    id          SERIAL PRIMARY KEY,
    az_id       INTEGER UNIQUE,
    file_path   TEXT NOT NULL UNIQUE,
    title       TEXT,
    artist      TEXT,
    album       TEXT,
    duration    FLOAT,
    bpm         FLOAT,
    energy      FLOAT,          -- 0.0 à 1.0 (loudness normalisé)
    danceability FLOAT,         -- 0.0 à 1.0 (Essentia)
    has_vocals  BOOLEAN DEFAULT FALSE,
    key_note    TEXT,           -- C, C#, D… (Essentia tonal)
    key_scale   TEXT,           -- major / minor
    mood        TEXT,           -- festival | intense | energique | melodique | nocturne
    genre_top1  TEXT,           -- genre Discogs #1
    genre_top2  TEXT,           -- genre Discogs #2
    genre_scores JSONB,         -- {genre: score, ...} top 10
    az_playlist_assigned BOOLEAN DEFAULT FALSE,
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
    az_file_id      INTEGER,
    played_at       TIMESTAMPTZ,
    generated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS radio_state (
    id              INTEGER PRIMARY KEY DEFAULT 1,
    mood            TEXT DEFAULT 'energique',
    energy_avg      FLOAT DEFAULT 0.6,
    last_rebexis    TIMESTAMPTZ,
    az_gw_playlist  INTEGER,        -- ID playlist "Gaiverland IA" dans AzuraCast
    az_rb_playlist  INTEGER,        -- ID playlist "Rebexis" dans AzuraCast
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO radio_state (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_tracks_mood      ON tracks(mood);
CREATE INDEX IF NOT EXISTS idx_tracks_bpm       ON tracks(bpm);
CREATE INDEX IF NOT EXISTS idx_tracks_az        ON tracks(az_id);
CREATE INDEX IF NOT EXISTS idx_history_time     ON play_history(played_at DESC);
SQL
echo "  ✓ Schéma DB créé"

# ════════════════════════════════════════════════════════════════════════
# TEMPLATES REBEXIS
# ════════════════════════════════════════════════════════════════════════
cat > "${CONFIG_DIR}/rebexis-templates.json" <<'JSON'
{
  "_note": "Formatage intentionnel : majuscules = emphase XTTS v2, '...' = pause, phrases courtes = punch",
  "modes": {
    "normal": {
      "templates": [
        "Ce morceau... exactement ce qu'il fallait.",
        "Je reste discrète. La musique, elle, ne l'est pas.",
        "C'est BIEN. Continuons.",
        "Aucune raison d'interrompre ça. Aucune.",
        "Parfait... pour ce moment précis."
      ]
    },
    "hype": {
      "triggers": ["festival", "energique"],
      "templates": [
        "Ok... là ça commence SÉRIEUSEMENT à accélérer.",
        "On est dans la montée. CLAIREMENT dans la montée. Bonne chance aux voisins.",
        "L'énergie festival... elle est là. Et franchement, ça me va TRÈS bien.",
        "Quelqu'un a commandé de l'énergie ? Livraison EXPRESS en cours.",
        "Je crois que les enceintes viennent de demander une augmentation.",
        "C'est l'heure où on arrête de faire semblant d'être raisonnables.",
        "Transition PARFAITE. Je ne dis rien. Je profite.",
        "On monte... on monte encore... et puis on monte UN PEU PLUS."
      ]
    },
    "peak": {
      "triggers": ["intense"],
      "templates": [
        "Celui-là... il n'est CLAIREMENT pas venu pour être discret.",
        "Je vais faire semblant d'être surprise par ce drop. Voilà. C'est fait.",
        "C'est LOUD. C'est voulu. C'est TRÈS bien.",
        "On est au sommet. PROFITEZ-EN.",
        "Le genre de track qui fait changer d'avis sur la vie.",
        "Hardstyle activé. Tout le reste... peut attendre.",
        "Ce drop méritait qu'on en parle. Voilà, c'est fait.",
        "Je ne sais pas ce que tu fais là... mais j'espère que c'est PHYSIQUE."
      ]
    },
    "flow": {
      "triggers": ["melodique", "nocturne"],
      "templates": [
        "On glisse... vers quelque chose de plus profond. C'est agréable.",
        "Ce changement de rythme... exactement ce qu'il fallait.",
        "Melodic techno à cette heure. Les choix sont DÉFENDABLES.",
        "On descend en douceur. Sans perdre l'essentiel.",
        "Miss Monique approuverait... probablement.",
        "Progressive. Le mot dit tout.",
        "C'est le moment où on réalise que la nuit commence VRAIMENT.",
        "Moins fort. Mais toujours là. C'est ÇA, la profondeur."
      ]
    }
  }
}
JSON
echo "  ✓ Templates Rebexis créés"

# ════════════════════════════════════════════════════════════════════════
# SCRIPTS PYTHON
# ════════════════════════════════════════════════════════════════════════

# ── az_utils.py — client AzuraCast 0.23.x partagé ────────────────────
cat > "${SCRIPTS_DIR}/az_utils.py" <<'PYEOF'
"""
Client AzuraCast 0.23.x — utilisé par analyzer, tts_worker, scheduler.
"""
import os, httpx
from typing import Optional

AZ_URL = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))
_TIMEOUT = 15


def _headers():
    return {"X-API-Key": AZ_KEY, "Content-Type": "application/json"}


def _ok():
    return bool(AZ_KEY) and AZ_KEY not in ("", "__CONFIGURE__")


def get_station() -> Optional[dict]:
    if not _ok():
        return None
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}", headers=_headers(), timeout=_TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def list_playlists() -> list:
    if not _ok():
        return []
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/playlists", headers=_headers(), timeout=_TIMEOUT)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def create_playlist(name: str, pl_type: str = "default", weight: int = 3,
                    play_per_songs: int = 0) -> Optional[dict]:
    """Crée une playlist AzuraCast et retourne son objet."""
    if not _ok():
        return None
    body = {"name": name, "type": pl_type, "weight": weight, "is_enabled": True}
    if pl_type == "once_per_x_songs":
        body["play_per_songs"] = play_per_songs
    try:
        r = httpx.post(f"{AZ_URL}/api/station/{AZ_STATION}/playlists",
                       headers=_headers(), json=body, timeout=_TIMEOUT)
        return r.json() if r.status_code in (200, 201) else None
    except Exception:
        return None


def get_or_create_playlist(name: str, **kwargs) -> Optional[int]:
    """Retourne l'ID d'une playlist existante ou la crée."""
    for pl in list_playlists():
        if pl.get("name") == name:
            return pl["id"]
    pl = create_playlist(name, **kwargs)
    return pl["id"] if pl else None


def set_playlist_order(playlist_id: int, file_ids: list) -> bool:
    """Définit l'ordre de lecture d'une playlist (AzuraCast 0.20+)."""
    if not _ok() or not file_ids:
        return False
    try:
        r = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/playlist/{playlist_id}/order",
                      headers=_headers(), json={"order": file_ids}, timeout=30)
        return r.status_code in (200, 204)
    except Exception:
        return False


def batch_assign_playlist(file_ids: list, playlist_ids: list) -> bool:
    """Assigne des fichiers à des playlists en batch (AzuraCast 0.20+)."""
    if not _ok() or not file_ids:
        return False
    try:
        r = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/files/batch",
                      headers=_headers(),
                      json={"files": file_ids, "playlists": playlist_ids},
                      timeout=30)
        return r.status_code in (200, 204)
    except Exception:
        return False


def find_file_by_path(path: str) -> Optional[dict]:
    """Cherche un fichier AzuraCast par son nom."""
    if not _ok():
        return None
    name = os.path.basename(path)
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/files",
                      headers=_headers(),
                      params={"searchPhrase": name, "pageSize": 5},
                      timeout=_TIMEOUT)
        data = r.json() if r.status_code == 200 else {}
        rows = data.get("rows", data) if isinstance(data, dict) else data
        if rows:
            return rows[0]
        return None
    except Exception:
        return None


def upload_file(local_path: str, title: str) -> Optional[dict]:
    """Upload un fichier audio dans AzuraCast, retourne l'objet créé."""
    if not _ok():
        return None
    try:
        with open(local_path, "rb") as f:
            r = httpx.post(
                f"{AZ_URL}/api/station/{AZ_STATION}/files",
                headers={"X-API-Key": AZ_KEY},
                files={"file": (os.path.basename(local_path), f, "audio/mpeg")},
                data={"title": title},
                timeout=60,
            )
        return r.json() if r.status_code in (200, 201) else None
    except Exception as e:
        print(f"  ⚠ Upload AzuraCast: {e}")
        return None


def now_playing() -> Optional[dict]:
    """Retourne les infos du morceau en cours."""
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=5)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None
PYEOF

# ── analyzer.py — Essentia + Discogs 400 genres ───────────────────────
cat > "${SCRIPTS_DIR}/analyzer.py" <<'PYEOF'
"""
Analyseur musical — Essentia + modèle Discogs EffNet (400 genres).
Extrait BPM précis, énergie, danceability, tonalité, genre électronique.
Mappe sur les moods Gaiverland et synchronise les az_id avec AzuraCast.
"""
import os, sys, subprocess, time, pathlib, json

MODELS_DIR = pathlib.Path(os.environ.get("ESSENTIA_MODELS_DIR", "/essentia-models"))

DISCOGS_MODELS = {
    "effnet_embed": "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.pb",
    "genre_multi":  "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs_multi_embeddings-effnet-1.pb",
    "genre_labels": "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs_multi_embeddings-effnet-1.json",
}

# Mapping genres Discogs → moods Gaiverland
GENRE_TO_MOOD = {
    # Intense
    "Hardstyle": "intense", "Hardcore": "intense", "Gabber": "intense",
    "Hard Techno": "intense", "Industrial": "intense", "Speedcore": "intense",
    "UK Hardcore": "intense", "Frenchcore": "intense",
    # Festival / Big energy
    "Trance": "festival", "Euro House": "festival", "Happy Hardcore": "festival",
    "Jumpstyle": "festival", "Electro House": "festival", "Big Room": "festival",
    "Dance": "festival", "Eurodance": "festival", "Hi NRG": "festival",
    # Energique
    "Techno": "energique", "Tech House": "energique", "Minimal": "energique",
    "Electroclash": "energique", "EBM": "energique", "Electro": "energique",
    "Acid Techno": "energique", "Detroit Techno": "energique",
    # Melodique
    "Progressive House": "melodique", "Progressive Trance": "melodique",
    "Melodic Techno": "melodique", "Deep House": "melodique",
    "Melodic House": "melodique", "Electronica": "melodique",
    "Italo House": "melodique", "Dream House": "melodique",
    # Nocturne
    "Ambient": "nocturne", "Downtempo": "nocturne", "Chillout": "nocturne",
    "Dark Ambient": "nocturne", "Drone": "nocturne", "New Age": "nocturne",
}

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".wav", ".aac", ".m4a"}


def install_deps():
    pkgs = ["essentia-tensorflow", "psycopg2-binary", "inotify-simple", "httpx", "mutagen"]
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet"] + pkgs, check=True)


try:
    import essentia, essentia.standard as es, psycopg2, inotify_simple, httpx, mutagen
except ImportError:
    print("→ Installation dépendances analyzer (essentia-tensorflow, ~500Mo)...")
    install_deps()
    import essentia, essentia.standard as es, psycopg2, inotify_simple, httpx, mutagen

import numpy as np
import sys
sys.path.insert(0, "/app")
from az_utils import find_file_by_path, batch_assign_playlist

DB_URL = os.environ["DATABASE_URL"]
AZ_KEY = os.environ.get("AZURACAST_API_KEY", "")
WATCH_DIR = "/var/azuracast/stations"

_genre_labels = []
_effnet_model = None
_genre_model = None


def download_models():
    global _genre_labels
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in DISCOGS_MODELS.items():
        dest = MODELS_DIR / pathlib.Path(url).name
        if not dest.exists():
            print(f"  → Téléchargement modèle Essentia : {dest.name} (~{40 if 'effnet' in name else 2}Mo)...")
            subprocess.run(["wget", "-q", "--show-progress", "-O", str(dest), url], check=True)
            print(f"  ✓ {dest.name}")
    # Charger les labels genre
    labels_path = MODELS_DIR / pathlib.Path(DISCOGS_MODELS["genre_labels"]).name
    if labels_path.exists():
        with open(labels_path) as f:
            meta = json.load(f)
        _genre_labels = meta.get("classes", [])
    print(f"  ✓ {len(_genre_labels)} classes de genres chargées")


def load_models():
    global _effnet_model, _genre_model
    effnet_path = MODELS_DIR / pathlib.Path(DISCOGS_MODELS["effnet_embed"]).name
    genre_path  = MODELS_DIR / pathlib.Path(DISCOGS_MODELS["genre_multi"]).name
    _effnet_model = es.TensorflowPredictEffnetDiscogs(
        graphFilename=str(effnet_path), output="PartitionedCall:1"
    )
    _genre_model = es.TensorflowPredict2D(
        graphFilename=str(genre_path),
        input="serving_default_model_Placeholder:0",
        output="PartitionedCall:0"
    )
    print("  ✓ Modèles Essentia chargés en mémoire")


def infer_mood_from_bpm_energy(bpm: float, energy: float) -> str:
    """Fallback si pas de genre détecté."""
    if bpm > 155 and energy > 0.65:
        return "intense"
    elif bpm > 145 and energy > 0.55:
        return "festival"
    elif bpm > 128:
        return "energique" if energy > 0.4 else "melodique"
    elif bpm > 110:
        return "melodique"
    return "nocturne"


def analyze_file(path: str) -> dict:
    try:
        # ── Chargement audio ─────────────────────────────────────────
        loader = es.MonoLoader(filename=path, sampleRate=44100)
        audio_44k = loader()

        # Pour les modèles Essentia (16kHz)
        loader16 = es.MonoLoader(filename=path, sampleRate=16000)
        audio_16k = loader16()

        # ── Extracteur BPM, énergie, tonalité ────────────────────────
        extractor = es.MusicExtractor(lowlevelSilenceRate60dBRMS=0.9,
                                       lowlevelSilenceRate30dBRMS=0.9,
                                       lowlevelFrameSize=2048,
                                       lowlevelHopSize=1024)
        features, _ = extractor(path)

        bpm          = float(features["rhythm.bpm"])
        energy       = float(features["lowlevel.average_loudness"])
        energy_norm  = min(1.0, max(0.0, (energy + 1.0) / 2.0))
        danceability = float(features["rhythm.danceability"])
        key_note     = str(features["tonal.key_key"])
        key_scale    = str(features["tonal.key_scale"])

        # Détection vocaux (énergie mid vs low)
        spec = np.abs(es.Spectrum()(audio_44k[:44100]))
        freqs = np.linspace(0, 22050, len(spec))
        vocal_e = float(np.mean(spec[(freqs > 300) & (freqs < 3400)])) + 1e-9
        low_e   = float(np.mean(spec[freqs <= 300])) + 1e-9
        has_vocals = (vocal_e / low_e) > 0.25

        # ── Genre Discogs (Essentia ML) ───────────────────────────────
        genre_top1 = genre_top2 = ""
        genre_scores = {}
        mood = "energique"  # default

        if _effnet_model and _genre_model and _genre_labels:
            try:
                embeddings   = _effnet_model(audio_16k)
                predictions  = _genre_model(embeddings)
                mean_scores  = np.mean(predictions, axis=0)

                # Top 10 genres par score
                top_idx = np.argsort(mean_scores)[::-1][:10]
                for i in top_idx:
                    if i < len(_genre_labels) and float(mean_scores[i]) > 0.05:
                        label = _genre_labels[i].split("---")[-1].strip()
                        genre_scores[label] = round(float(mean_scores[i]), 4)

                # Genres principaux
                sorted_genres = sorted(genre_scores.items(), key=lambda x: -x[1])
                if sorted_genres:
                    genre_top1 = sorted_genres[0][0]
                if len(sorted_genres) > 1:
                    genre_top2 = sorted_genres[1][0]

                # Mood : premier genre reconnu dans le mapping
                for label, _ in sorted_genres:
                    if label in GENRE_TO_MOOD:
                        mood = GENRE_TO_MOOD[label]
                        break
                else:
                    mood = infer_mood_from_bpm_energy(bpm, energy_norm)
            except Exception as eg:
                print(f"  ⚠ Genre detection: {eg} — fallback BPM")
                mood = infer_mood_from_bpm_energy(bpm, energy_norm)
        else:
            mood = infer_mood_from_bpm_energy(bpm, energy_norm)

        # ── Métadonnées ID3/tags ──────────────────────────────────────
        meta = mutagen.File(path, easy=True) or {}
        title    = str(meta.get("title",  [""])[0]) or os.path.basename(path)
        artist   = str(meta.get("artist", [""])[0]) or "Inconnu"
        album    = str(meta.get("album",  [""])[0]) or ""
        duration = float(features["metadata.audio_properties.length"])

        return {
            "file_path": path, "title": title, "artist": artist, "album": album,
            "duration": round(duration, 1), "bpm": round(bpm, 1),
            "energy": round(energy_norm, 3), "danceability": round(danceability, 3),
            "has_vocals": has_vocals, "key_note": key_note, "key_scale": key_scale,
            "mood": mood, "genre_top1": genre_top1, "genre_top2": genre_top2,
            "genre_scores": json.dumps(genre_scores), "analyzed": True,
        }
    except Exception as exc:
        print(f"  ⚠ Analyse échouée {os.path.basename(path)}: {exc}")
        return {"file_path": path, "analyzed": False}


def get_conn():
    return psycopg2.connect(DB_URL)


def save_track(conn, data: dict):
    cols = [k for k in data if k != "genre_scores" or True]
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tracks
              (file_path, title, artist, album, duration, bpm, energy, danceability,
               has_vocals, key_note, key_scale, mood, genre_top1, genre_top2, genre_scores, analyzed)
            VALUES
              (%(file_path)s, %(title)s, %(artist)s, %(album)s, %(duration)s, %(bpm)s,
               %(energy)s, %(danceability)s, %(has_vocals)s, %(key_note)s, %(key_scale)s,
               %(mood)s, %(genre_top1)s, %(genre_top2)s, %(genre_scores)s::jsonb, %(analyzed)s)
            ON CONFLICT (file_path) DO UPDATE SET
              bpm=EXCLUDED.bpm, energy=EXCLUDED.energy, danceability=EXCLUDED.danceability,
              has_vocals=EXCLUDED.has_vocals, mood=EXCLUDED.mood,
              genre_top1=EXCLUDED.genre_top1, genre_top2=EXCLUDED.genre_top2,
              genre_scores=EXCLUDED.genre_scores::jsonb, analyzed=EXCLUDED.analyzed,
              key_note=EXCLUDED.key_note, key_scale=EXCLUDED.key_scale, updated_at=NOW()
            RETURNING id
        """, {**data, "genre_scores": data.get("genre_scores", "{}")})
        track_id = cur.fetchone()[0]
    conn.commit()

    # Sync az_id depuis AzuraCast
    if data.get("analyzed") and AZ_KEY:
        az_file = find_file_by_path(data["file_path"])
        if az_file:
            az_id = az_file.get("id")
            with conn.cursor() as cur:
                cur.execute("UPDATE tracks SET az_id=%s WHERE id=%s", (az_id, track_id))
            conn.commit()

    return track_id


def main():
    print("🎵 Analyzer Gaiverland démarré")
    download_models()
    load_models()

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM tracks WHERE analyzed=TRUE")
        known = {r[0] for r in cur.fetchall()}

    # Scan initial
    count = 0
    for root, _, files in os.walk(WATCH_DIR):
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS:
                fp = os.path.join(root, f)
                if fp not in known:
                    print(f"  → {os.path.basename(fp)}")
                    save_track(conn, analyze_file(fp))
                    count += 1
    print(f"  ✓ {count} fichiers analysés au démarrage")

    # Surveillance inotify
    inotify = inotify_simple.INotify()
    for root, _, _ in os.walk(WATCH_DIR):
        inotify.add_watch(root, inotify_simple.flags.CLOSE_WRITE | inotify_simple.flags.MOVED_TO)
    print("  ✓ Surveillance temps réel active")

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
                print(f"  → Nouveau : {name}")
                time.sleep(1)
                save_track(conn, analyze_file(fp))
                print(f"  ✓ Analysé : {name}")


if __name__ == "__main__":
    main()
PYEOF

# ── playlist.py — identique mais avec danceability + genre_scores ──────
cat > "${SCRIPTS_DIR}/playlist.py" <<'PYEOF'
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

    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id
            FROM tracks
            WHERE analyzed=TRUE AND mood = ANY(%s) AND id != ALL(%s)
            ORDER BY RANDOM() LIMIT %s
        """, (candidate_moods, recent_ids, count * 4))
        candidates = list(cur.fetchall())

    if not candidates:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id
                FROM tracks WHERE analyzed=TRUE ORDER BY RANDOM() LIMIT %s
            """, (count * 2,))
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
PYEOF

# ── rebexis.py — inchangé (déjà bon) ─────────────────────────────────
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
    install_deps()
    import fastapi, uvicorn, psycopg2, httpx

from fastapi import FastAPI
import psycopg2.extras

DB_URL      = os.environ["DATABASE_URL"]
MODE        = os.environ.get("REBEXIS_MODE", "template")
INT_MIN     = int(os.environ.get("REBEXIS_INTERVAL_MIN", "15")) * 60
INT_MAX     = int(os.environ.get("REBEXIS_INTERVAL_MAX", "30")) * 60
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://gaiverland-ollama:11434")
OLLAMA_MDL  = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
API_KEY     = os.environ.get("REBEXIS_API_KEY", "")
API_BASE    = os.environ.get("REBEXIS_API_BASE", "https://api.openai.com/v1")

templates = {}
app = FastAPI(title="Rebexis Engine")

SYSTEM = """Tu es Rebexis, animatrice de Gaiverland Radio — une radio électro autonome.
Tu n'es pas une IA. Tu es une présence radio vivante et passionnée.
Style : énergie de festival, légèrement sarcastique, surprenante, jamais robotique.

Format OBLIGATOIRE :
- 1 à 2 phrases MAXIMUM. 15 à 60 mots.
- Radio-friendly. Français naturel et parlé.
- Tu NE décris JAMAIS la musique — tu RÉAGIS à ce qu'elle fait ressentir.
- Humour léger OK. Pas de blagues longues. Jamais répétitif.

Formatage de la prosodie (IMPORTANT — influence la synthèse vocale) :
- Utilise les MAJUSCULES pour les mots à accentuer fortement (ex: "c'est ÉNORME")
- Utilise "..." pour marquer des pauses dramatiques (ex: "Et là... ça commence.")
- Phrases COURTES pour l'énergie. Pas de subordonnées longues.
- Ponctuation expressive : points d'exclamation, mais avec parcimonie."""


def load_tpl():
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
    return elapsed >= random.randint(INT_MIN, INT_MAX)


def gen_template(mood: str) -> str:
    key = "hype" if mood in ("festival", "energique") else \
          "peak" if mood == "intense" else \
          "flow" if mood in ("nocturne", "melodique") else "normal"
    t = templates.get("modes", {}).get(key, {}).get("templates", ["La radio continue."])
    return random.choice(t)


def gen_ollama(mood: str, context: str, recent: list) -> str:
    prompt = f"Génère UNE intervention courte de Rebexis. Morceau: {context}. Ambiance: {mood}."
    if recent:
        prompt += f" Phrases à éviter: {' / '.join(recent[:2])}"
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/generate",
                       json={"model": OLLAMA_MDL, "prompt": f"{SYSTEM}\n\n{prompt}", "stream": False},
                       timeout=30)
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        print(f"⚠ Ollama: {e}")
        return gen_template(mood)


def gen_api(mood: str, context: str, recent: list) -> str:
    user = f"Morceau en cours : {context}. Ambiance : {mood}."
    if recent:
        user += f" Évite: {' / '.join(recent[:2])}"
    try:
        r = httpx.post(f"{API_BASE}/chat/completions",
                       headers={"Authorization": f"Bearer {API_KEY}"},
                       json={"model": "gpt-4o-mini",
                             "messages": [{"role": "system", "content": SYSTEM},
                                          {"role": "user", "content": user}],
                             "max_tokens": 70, "temperature": 1.0},
                       timeout=15)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"⚠ API LLM: {e}")
        return gen_template(mood)


@app.get("/health")
def health():
    return {"status": "ok", "mode": MODE}


@app.post("/generate")
def generate(mood: str = "energique", context_track: str = "", force: bool = False):
    conn = get_conn()
    if not force and not should_intervene(conn):
        return {"intervention": None, "reason": "intervalle_non_atteint"}

    with conn.cursor() as cur:
        cur.execute("SELECT intervention FROM rebexis_sessions ORDER BY generated_at DESC LIMIT 5")
        recent = [r["intervention"] for r in cur.fetchall()]

    text = gen_ollama(mood, context_track, recent) if MODE == "ollama" else \
           gen_api(mood, context_track, recent)    if MODE == "api"    else \
           gen_template(mood)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO rebexis_sessions (intervention, mood_trigger, context_track)
            VALUES (%s,%s,%s) RETURNING id
        """, (text, mood, context_track))
        sid = cur.fetchone()["id"]
        cur.execute("UPDATE radio_state SET last_rebexis=NOW() WHERE id=1")
    conn.commit()

    return {"intervention": text, "session_id": sid, "mode": MODE}


if __name__ == "__main__":
    load_tpl()
    print(f"🎙 Rebexis Engine — mode: {MODE}")
    uvicorn.run(app, host="0.0.0.0", port=8081)
PYEOF

# ── tts_worker.py — XTTS v2 (Coqui) + post-traitement radio ──────────
cat > "${SCRIPTS_DIR}/tts_worker.py" <<'PYEOF'
"""
TTS Worker — XTTS v2 (Coqui TTS) + chaîne de post-traitement radio.
Qualité quasi-humaine, support clonage vocal (WAV de référence optionnel).
Pré-génération asynchrone : 2-3 min/clip sur CPU, jamais en temps réel.
Upload automatique dans AzuraCast, assignation playlist Rebexis.
"""
import os, sys, subprocess, hashlib, pathlib

TTS_CACHE = pathlib.Path(os.environ.get("TTS_CACHE_DIR", "/tts-cache"))
TTS_CACHE.mkdir(parents=True, exist_ok=True)
# Coqui stocke le modèle XTTS v2 (~2.4 Go) dans TTS_HOME → volume NAS tts-cache
os.environ.setdefault("TTS_HOME", str(TTS_CACHE))


def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "TTS>=0.22.0", "numpy",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)
    subprocess.run(["apt-get", "update", "-qq"], check=True)
    subprocess.run(["apt-get", "install", "-y", "-qq", "ffmpeg"], check=True)


try:
    from TTS.api import TTS as CoquiTTS
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    print("→ Installation XTTS v2 (Coqui TTS) + ffmpeg...")
    install_deps()
    from TTS.api import TTS as CoquiTTS
    import fastapi, uvicorn, psycopg2, httpx

from fastapi import FastAPI
import psycopg2.extras
sys.path.insert(0, "/app")
from az_utils import upload_file

DB_URL     = os.environ["DATABASE_URL"]
AZ_KEY     = os.environ.get("AZURACAST_API_KEY", "")
AZ_URL     = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))

# Clonage vocal optionnel — mettre un WAV de 6–30s dans :
#   app-config/gaiverland-radio/rebexis-voice.wav
# Sans fichier → locuteur XTTS v2 intégré (Ana Florence, bonne diction en fr)
_VOICE_REF = pathlib.Path("/rebexis-config/rebexis-voice.wav")
USE_VOICE_CLONING = _VOICE_REF.exists() and _VOICE_REF.stat().st_size > 10_000
VOICE_REF_PATH    = str(_VOICE_REF) if USE_VOICE_CLONING else None
XTTS_DEFAULT_SPEAKER = "Ana Florence"

_tts = None
app = FastAPI(title="TTS Worker")


def get_tts():
    global _tts
    if _tts is None:
        print("→ Chargement XTTS v2 (téléchargement modèle ~2.4 Go si premier démarrage)...")
        _tts = CoquiTTS("tts_models/multilingual/multi-dataset/xtts_v2")
        print("  ✓ XTTS v2 prêt")
    return _tts


def synthesize_raw(text: str) -> pathlib.Path:
    """XTTS v2 → WAV 24kHz. 2-3 min sur Ryzen 5 3600 CPU-only (pré-génération)."""
    ref_key = "clone" if USE_VOICE_CLONING else XTTS_DEFAULT_SPEAKER
    h = hashlib.sha256(f"xtts2:{ref_key}:{text}".encode()).hexdigest()[:16]
    wav_path = TTS_CACHE / f"raw_{h}.wav"
    if wav_path.exists():
        return wav_path

    engine = get_tts()
    if USE_VOICE_CLONING:
        engine.tts_to_file(
            text=text,
            speaker_wav=VOICE_REF_PATH,
            language="fr",
            file_path=str(wav_path),
        )
    else:
        engine.tts_to_file(
            text=text,
            speaker=XTTS_DEFAULT_SPEAKER,
            language="fr",
            file_path=str(wav_path),
        )
    return wav_path


# Chaîne ffmpeg radio — style festival/broadcast
# Objectif : voix présente, punchy, qui "sort" des basses du mix électro
FFMPEG_RADIO_CHAIN = ",".join([
    # 1. Highpass 90Hz — nettoie les basses (inutiles, masquées par la musique)
    "highpass=f=90",
    # 2. Coupure basse-mid (180Hz) — évite l'aspect "nasillard" du TTS
    "equalizer=f=180:width_type=o:width=2:g=-3",
    # 3. Boost présence voix (2.5kHz) — intelligibilité et énergie perçue
    "equalizer=f=2500:width_type=o:width=1.5:g=5",
    # 4. Boost air (10kHz) — brillance, réduit l'aspect synthétique
    "equalizer=f=10000:width_type=o:width=2:g=3",
    # 5. Compresseur agressif style festival radio (ratio élevé, makeup fort)
    "acompressor=threshold=-22dB:ratio=6:attack=2:release=120:makeup=7",
    # 6. Saturation harmonique légère — chaleur et agressivité
    "aexciter=level_in=1:level_out=1:amount=2:drive=3",
    # 7. Limiteur brick-wall
    "alimiter=limit=0.92:attack=0.5:release=3",
    # 8. Loudnorm EBU R128 (-13 LUFS, légèrement plus fort que streaming std)
    "loudnorm=I=-13:LRA=5:TP=-1.0",
])


def apply_radio_processing(wav_path: pathlib.Path) -> pathlib.Path:
    """Applique la chaîne radio et exporte en MP3 192kbps."""
    h = wav_path.stem.replace("raw_", "")
    mp3_path = TTS_CACHE / f"rebexis_{h}.mp3"
    if mp3_path.exists():
        return mp3_path

    result = subprocess.run([
        "ffmpeg", "-y", "-i", str(wav_path),
        "-af", FFMPEG_RADIO_CHAIN,
        "-b:a", "192k", "-ar", "44100",
        str(mp3_path)
    ], capture_output=True)

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg: {result.stderr.decode()[:300]}")

    wav_path.unlink(missing_ok=True)
    return mp3_path


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def get_rebexis_playlist_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT az_rb_playlist FROM radio_state WHERE id=1")
        row = cur.fetchone()
    return (row["az_rb_playlist"] or 0) if row else 0


@app.get("/health")
def health():
    mode = "voice-cloning" if USE_VOICE_CLONING else f"speaker:{XTTS_DEFAULT_SPEAKER}"
    return {"status": "ok", "engine": "xtts-v2", "mode": mode}


@app.post("/synthesize")
def synthesize_endpoint(session_id: int, text: str):
    try:
        wav = synthesize_raw(text)
        mp3 = apply_radio_processing(wav)

        az_file_id = None
        rb_pl_id   = get_rebexis_playlist_id(get_conn())

        az_result = upload_file(str(mp3), f"Rebexis #{session_id}")
        if az_result:
            az_file_id = az_result.get("id")
            if rb_pl_id and az_file_id:
                from az_utils import batch_assign_playlist
                batch_assign_playlist([az_file_id], [rb_pl_id])

        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE rebexis_sessions
                SET audio_file=%s, az_file_id=%s
                WHERE id=%s
            """, (str(mp3), az_file_id, session_id))
        conn.commit()

        return {"ok": True, "audio_file": str(mp3), "az_file_id": az_file_id}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/pending")
def list_pending():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, intervention, mood_trigger FROM rebexis_sessions
            WHERE audio_file IS NULL ORDER BY generated_at LIMIT 10
        """)
        return {"sessions": list(cur.fetchall())}


if __name__ == "__main__":
    get_tts()  # téléchargement modèle + préchauffage au démarrage
    mode = "clonage vocal" if USE_VOICE_CLONING else f"locuteur: {XTTS_DEFAULT_SPEAKER}"
    print(f"🔊 TTS Worker — XTTS v2 | Mode: {mode} | Cache: {TTS_CACHE}")
    uvicorn.run(app, host="0.0.0.0", port=8082)
PYEOF

# ── scheduler.py — playlists AzuraCast 0.23.4 ─────────────────────────
cat > "${SCRIPTS_DIR}/scheduler.py" <<'PYEOF'
"""
Scheduler Gaiverland — orchestre playlist + Rebexis + TTS.
Gère les playlists AzuraCast 0.23.4 directement via API.

Stratégie playlist :
  - "Gaiverland IA" (default, weight=3) : 20-30 titres mood-appropriés, mis à jour toutes les 5 min
  - "Rebexis"       (once_per_x_songs)  : jingles de Rebexis générés à l'avance
"""
import os, sys, subprocess, time

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "httpx", "psycopg2-binary"], check=True)

try:
    import httpx, psycopg2
except ImportError:
    install_deps()
    import httpx, psycopg2

import psycopg2.extras
import sys
sys.path.insert(0, "/app")
from az_utils import (get_or_create_playlist, batch_assign_playlist,
                      set_playlist_order, get_station, now_playing)

DB_URL       = os.environ["DATABASE_URL"]
PLAYLIST_URL = "http://gaiverland-playlist:8080"
REBEXIS_URL  = "http://gaiverland-rebexis:8081"
TTS_URL      = "http://gaiverland-tts:8082"
CYCLE_SEC    = 300  # 5 minutes
AZ_KEY       = os.environ.get("AZURACAST_API_KEY", "")

# Intervalle Rebexis (en nombre de morceaux entre chaque jingle)
REBEXIS_SONGS_INTERVAL = 8


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def wait_for(url: str, name: str, retries: int = 30):
    for i in range(retries):
        try:
            if httpx.get(f"{url}/health", timeout=5).status_code == 200:
                print(f"  ✓ {name} prêt")
                return True
        except Exception:
            pass
        print(f"  ⏳ {name}... ({i+1}/{retries})")
        time.sleep(10)
    print(f"  ⚠ {name} non disponible")
    return False


def setup_playlists(conn):
    """Crée ou retrouve les playlists Gaiverland dans AzuraCast."""
    print("  → Vérification playlists AzuraCast...")

    gw_id = get_or_create_playlist("Gaiverland IA", pl_type="default", weight=3)
    rb_id = get_or_create_playlist("Rebexis",
                                    pl_type="once_per_x_songs",
                                    weight=1,
                                    play_per_songs=REBEXIS_SONGS_INTERVAL)

    if gw_id or rb_id:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE radio_state
                SET az_gw_playlist=COALESCE(%s, az_gw_playlist),
                    az_rb_playlist=COALESCE(%s, az_rb_playlist),
                    updated_at=NOW()
                WHERE id=1
            """, (gw_id, rb_id))
        conn.commit()
        print(f"  ✓ Playlist 'Gaiverland IA' : ID {gw_id}")
        print(f"  ✓ Playlist 'Rebexis'       : ID {rb_id} (1 jingle/{REBEXIS_SONGS_INTERVAL} morceaux)")
    else:
        print("  ⚠ Playlists AzuraCast non configurées (clé API manquante ?)")

    return gw_id, rb_id


def update_gaiverland_playlist(conn, gw_playlist_id: int):
    """Met à jour la playlist 'Gaiverland IA' avec les titres mood-appropriés."""
    if not gw_playlist_id:
        return

    try:
        # Récupérer la playlist suggérée par le moteur IA
        resp = httpx.get(f"{PLAYLIST_URL}/playlist/next", params={"count": 25}, timeout=15)
        data = resp.json()
        tracks = data.get("tracks", [])
        mood   = data.get("mood", "?")

        # Extraire les az_id valides
        az_ids = [t["az_id"] for t in tracks if t.get("az_id")]
        if not az_ids:
            print(f"  ℹ Playlist [{mood}] — aucun az_id disponible (analyzer pas encore synchro ?)")
            return

        # Assigner les titres à la playlist en batch
        # D'abord vider les anciens (batch avec playlist vide), puis ajouter les nouveaux
        # Note : batch_assign remplace les playlists du fichier, pas juste pour cette playlist.
        # On utilise set_playlist_order qui réordonne sans modifier les autres playlists.
        ok = set_playlist_order(gw_playlist_id, az_ids)
        if ok:
            print(f"  📻 Playlist [{mood}] mise à jour — {len(az_ids)} titres")
        else:
            print(f"  ⚠ Mise à jour playlist échouée")
    except Exception as e:
        print(f"  ⚠ update_playlist: {e}")


def process_tts_queue():
    """Convertit en audio les interventions Rebexis en attente."""
    try:
        pending = httpx.get(f"{TTS_URL}/pending", timeout=10).json().get("sessions", [])
        for s in pending[:2]:  # max 2 par cycle — charge CPU stable
            print(f"  → TTS session {s['id']}: {s['intervention'][:50]}…")
            httpx.post(f"{TTS_URL}/synthesize",
                       params={"session_id": s["id"], "text": s["intervention"]},
                       timeout=120)
    except Exception as e:
        print(f"  ⚠ TTS queue: {e}")


def maybe_generate_rebexis():
    """Déclenche la génération d'une intervention Rebexis si le timing le permet."""
    try:
        state = httpx.get(f"{PLAYLIST_URL}/state", timeout=5).json()
        mood  = state.get("mood", "energique")

        # Récupérer le titre en cours depuis AzuraCast
        np_data = now_playing()
        context = ""
        if np_data:
            song = np_data.get("now_playing", {}).get("song", {})
            context = f"{song.get('artist', '')} — {song.get('title', '')}".strip(" —")

        resp = httpx.post(f"{REBEXIS_URL}/generate",
                          params={"mood": mood, "context_track": context},
                          timeout=30)
        data = resp.json()
        if data.get("intervention"):
            print(f"  🎙 Rebexis [{mood}] : {data['intervention'][:60]}…")
    except Exception as e:
        print(f"  ⚠ Rebexis: {e}")


def main():
    print("⚙  Scheduler Gaiverland démarré")
    wait_for(PLAYLIST_URL, "Playlist Engine")
    wait_for(REBEXIS_URL,  "Rebexis Engine")
    wait_for(TTS_URL,      "TTS Worker")

    # Vérifier la connexion AzuraCast
    station = get_station()
    if station:
        print(f"  ✓ AzuraCast connecté — station : {station.get('name', '?')}")
    else:
        print("  ⚠ AzuraCast non joignable — scheduler en mode dégradé (Rebexis + TTS actifs)")

    conn = get_conn()
    gw_id, rb_id = setup_playlists(conn)

    print("\n✅ Boucle principale active.\n")
    cycle = 0
    while True:
        cycle += 1
        print(f"\n── Cycle #{cycle} ─────────────────────────────────")

        # 1. Traitement TTS en attente (priorité : audio prêt avant diffusion)
        process_tts_queue()

        # 2. Générer Rebexis si intervalle atteint
        maybe_generate_rebexis()

        # 3. Mettre à jour la playlist Gaiverland IA dans AzuraCast
        if gw_id:
            conn = get_conn()
            update_gaiverland_playlist(conn, gw_id)

        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    main()
PYEOF

echo "  ✓ Scripts Python créés (Essentia + XTTS v2 + playlists 0.23.4)"

# ════════════════════════════════════════════════════════════════════════
# BOOTSTRAP AZURACAST (mode autonome uniquement)
# ════════════════════════════════════════════════════════════════════════
cat > "${CONFIG_DIR}/bootstrap.sh" <<'BOOTSTRAP'
#!/bin/bash
set -euo pipefail

AZ_URL="http://azuracast:80"
EMAIL="${AZURACAST_ADMIN_EMAIL:-admin@azuracast.local}"
PASS="${AZURACAST_ADMIN_PASSWORD:-changeme}"
STATION="${AZURACAST_STATION_NAME:-Gaiverland Radio}"
SHORT="${AZURACAST_STATION_SHORT:-gaiverland}"
ICECAST="${CALEOPE_PORT_ICECAST:-8500}"
AZ_STATION_ID="${AZURACAST_STATION_ID:-1}"

echo ""
echo "╔═══════════════════════════════════════════════════════╗"
echo "║  🎙 Gaiverland Radio — Bootstrap AzuraCast            ║"
echo "╚═══════════════════════════════════════════════════════╝"

echo "  ⏳ Attente AzuraCast (peut prendre 5 min)..."
STATUS="000"
for i in $(seq 1 60); do
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${AZ_URL}/" 2>/dev/null) || STATUS="000"
    [[ "${STATUS}" != "000" && "${STATUS}" != "502" && "${STATUS}" != "503" ]] && break
    echo "  ⏳ (${i}/60)..."
    sleep 10
done

[[ "${STATUS}" == "000" ]] && echo "  ⚠ AzuraCast non joignable — skip" && exit 0

sleep 3
REDIR=$(curl -sf -o /dev/null -w "%{redirect_url}" "${AZ_URL}/" 2>/dev/null) || REDIR=""
if echo "${REDIR}" | grep -q "/setup"; then
    RESP=$(curl -sf -X POST "${AZ_URL}/api/frontend/setup/registration" \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"Admin\",\"email\":\"${EMAIL}\",\"password\":\"${PASS}\",\"password_confirm\":\"${PASS}\"}" \
        2>/dev/null) || RESP=""
    [[ -n "${RESP}" ]] && echo "  ✓ Compte admin créé" || echo "  ⚠ Compte à créer manuellement"
fi

sleep 2
TOKEN=$(curl -sf -X POST "${AZ_URL}/api/user/login" \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"${EMAIL}\",\"password\":\"${PASS}\"}" 2>/dev/null \
    | jq -r '.api_key // empty') || TOKEN=""

[[ -z "${TOKEN}" ]] && echo "  ⚠ Auth API échouée — configuration manuelle requise" && exit 0
echo "  ✓ Auth API OK"

# Créer la station si absente
EXISTING=$(curl -sf "${AZ_URL}/api/stations" -H "X-API-Key: ${TOKEN}" 2>/dev/null | jq 'length') || EXISTING=0
if [[ "${EXISTING}" -eq 0 ]]; then
    RESP=$(curl -sf -X POST "${AZ_URL}/api/admin/stations" \
        -H "Content-Type: application/json" -H "X-API-Key: ${TOKEN}" \
        -d "{\"name\":\"${STATION}\",\"short_name\":\"${SHORT}\",\"frontend_type\":\"icecast\",\"backend_type\":\"liquidsoap\",\"frontend_config\":{\"port\":${ICECAST}},\"is_public\":false,\"enable_requests\":true}" \
        2>/dev/null) || RESP=""
    [[ -n "${RESP}" ]] && echo "  ✓ Station '${STATION}' créée" || echo "  ⚠ Création station échouée"
fi

# Créer les playlists IA
for PL_NAME in "Gaiverland IA" "Rebexis"; do
    TYPE="default"; WEIGHT=3; PER_SONGS=0
    [[ "${PL_NAME}" == "Rebexis" ]] && TYPE="once_per_x_songs" && WEIGHT=1 && PER_SONGS=8

    BODY="{\"name\":\"${PL_NAME}\",\"type\":\"${TYPE}\",\"weight\":${WEIGHT},\"is_enabled\":true"
    [[ "${PER_SONGS}" -gt 0 ]] && BODY="${BODY},\"play_per_songs\":${PER_SONGS}"
    BODY="${BODY}}"

    PL_RESP=$(curl -sf -X POST "${AZ_URL}/api/station/${AZ_STATION_ID}/playlists" \
        -H "Content-Type: application/json" -H "X-API-Key: ${TOKEN}" \
        -d "${BODY}" 2>/dev/null) || PL_RESP=""
    [[ -n "${PL_RESP}" ]] && echo "  ✓ Playlist '${PL_NAME}' créée" || echo "  ℹ Playlist '${PL_NAME}' peut déjà exister"
done

echo ""
echo "╔═══════════════════════════════════════════════════════╗"
echo "║  ✅ Bootstrap terminé                                  ║"
echo "╚═══════════════════════════════════════════════════════╝"
BOOTSTRAP
chmod +x "${CONFIG_DIR}/bootstrap.sh"

# ── post-install.txt ──────────────────────────────────────────────────
MODE_LINE=""
if [[ "${AZ_EXISTING}" == "true" ]]; then
    MODE_LINE="  Mode         : addon (AzuraCast existant connecté)"
else
    MODE_LINE="  Mode         : autonome (AzuraCast déployé ici)"
fi

cat > "${CONFIG_DIR}/post-install.txt" <<EOF
╔══════════════════════════════════════════════════════════════════╗
║           🎙 Gaiverland Radio IA — Post-installation             ║
╠══════════════════════════════════════════════════════════════════╣
║  ${MODE_LINE}
╠══════════════════════════════════════════════════════════════════╣
$([ "${AZ_EXISTING}" == "false" ] && echo "║  AzuraCast URL  : ${AZ_BASE_URL}")
$([ "${AZ_EXISTING}" == "false" ] && echo "║  Login admin    : ${AZ_ADMIN_EMAIL}")
$([ "${AZ_EXISTING}" == "false" ] && echo "║  Mot de passe   : ${AZ_ADMIN_PASSWORD}")
╠══════════════════════════════════════════════════════════════════╣
║  ⚙  CONFIGURATION REQUISE
║
$([ -z "${AZ_API_KEY}" ] && echo "║  1. Créer une clé API dans AzuraCast → Admin → API Keys")
$([ -z "${AZ_API_KEY}" ] && echo "║     Écrire dans : ${CONFIG_DIR}/services.env")
$([ -z "${AZ_API_KEY}" ] && echo "║     → AZURACAST_API_KEY=<ta-clé>")
$([ -z "${AZ_API_KEY}" ] && echo "║     Puis : docker restart gaiverland-scheduler gaiverland-tts gaiverland-analyzer")
╠══════════════════════════════════════════════════════════════════╣
║  🎵 ANALYSE MUSICALE
║     Essentia + Discogs 400 genres (téléchargement ~50Mo au 1er démarrage)
║     → logs : docker logs gaiverland-analyzer -f
║
║  🎙 REBEXIS : mode ${REBEXIS_MODE} | voix XTTS v2 (Coqui)
$([ "${REBEXIS_MODE}" == "ollama" ] && echo "║     → docker exec gaiverland-ollama ollama pull ${REBEXIS_LLM_MODEL}")
╠══════════════════════════════════════════════════════════════════╣
║  🎛 COMMANDES
║
║  État radio   : curl http://localhost:${API_PORT}/state
║  Forcer mood  : curl -X POST http://localhost:${API_PORT}/state/mood?mood=festival
║  Forcer Reb.  : curl -X POST http://localhost:8081/generate?force=true
║  Logs sched.  : docker logs gaiverland-scheduler -f
╚══════════════════════════════════════════════════════════════════╝
EOF

echo ""
echo "✅ Gaiverland Radio IA — setup terminé"
echo ""
echo "   Mode       : $( [[ "${AZ_EXISTING}" == "true" ]] && echo "addon (AzuraCast existant)" || echo "autonome (AzuraCast inclus)" )"
echo "   Rebexis    : ${REBEXIS_MODE} | XTTS v2 (Coqui)"
echo "   Analyse    : Essentia + Discogs 400 genres"
echo "   Playlist API : port ${API_PORT}"
if [[ -z "${AZ_API_KEY}" ]]; then
    echo ""
    echo "   ⚠  Clé API AzuraCast manquante — à configurer dans services.env"
fi
