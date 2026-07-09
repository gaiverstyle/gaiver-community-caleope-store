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
         "${CONFIG_DIR}/dockerfiles/api" \
         "${CONFIG_DIR}/dockerfiles/downloader" \
         "${CONFIG_DIR}/dockerfiles/analyzer" \
         "${CONFIG_DIR}/dockerfiles/scheduler" \
         "${CONFIG_DIR}/dockerfiles/streamer" \
         "${CONFIG_DIR}/dockerfiles/gcs-track-service" \
         "${CONFIG_DIR}/dockerfiles/gcs-state-engine" \
         "${CONFIG_DIR}/dockerfiles/gcs-api" \
         "${DATA_DIR}/db" \
         "${STORAGE_PATH}/tts-cache" "${STORAGE_PATH}/ollama" \
         "${STORAGE_PATH}/essentia-models" "${STORAGE_PATH}/backups" \
         "${CALEOPE_BASE_DIR}/app-data/azuracast/stations"

# ── Scripts applicatifs : source → app-config (montés read-only dans les conteneurs) ──
# CAUSE RACINE des "mises à jour qui n'arrivent jamais" : sans cette copie, un reinstall
# ne rafraîchit PAS le code des services (app-config/scripts reste figé au 1er install).
# On copie depuis le dossier source de l'app, résolu via BASH_SOURCE (= cache du store,
# rafraîchi par `caleope update`). Restart des conteneurs ensuite pour charger le nouveau code.
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -d "${SRC_DIR}/scripts" ]; then
    cp -f "${SRC_DIR}/scripts/"*.py "${SCRIPTS_DIR}/" 2>/dev/null || true
    echo "  ✓ scripts .py synchronisés depuis le store → app-config"
fi

# ── Dockerfiles (pip baked in image — évite 700MB/container de writable layer) ──
cat > "${CONFIG_DIR}/dockerfiles/api/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN apt-get update -qq && \
    apt-get install -y -qq ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    fastapi "uvicorn[standard]" psycopg2-binary httpx
EOF

cat > "${CONFIG_DIR}/dockerfiles/downloader/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN apt-get update -qq && \
    apt-get install -y -qq ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    httpx yt-dlp tzdata psycopg2-binary
EOF

cat > "${CONFIG_DIR}/dockerfiles/analyzer/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir \
    essentia-tensorflow psycopg2-binary inotify-simple httpx mutagen
EOF

cat > "${CONFIG_DIR}/dockerfiles/scheduler/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir \
    httpx psycopg2-binary tzdata
EOF

cat > "${CONFIG_DIR}/dockerfiles/streamer/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN apt-get update -qq && \
    apt-get install -y -qq ffmpeg fonts-dejavu-core && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
    httpx Pillow
EOF

# ── GCS services Dockerfiles ─────────────────────────────────────────────
cat > "${CONFIG_DIR}/dockerfiles/gcs-track-service/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" psycopg2-binary httpx
EOF

cat > "${CONFIG_DIR}/dockerfiles/gcs-state-engine/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" psycopg2-binary httpx
EOF

# gcs-api : image partagée pour rebexis/tts/injector/vote/lore (+ ffmpeg pour tts)
cat > "${CONFIG_DIR}/dockerfiles/gcs-api/Dockerfile" <<'EOF'
FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq ffmpeg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" psycopg2-binary httpx
EOF
echo "  ✓ Dockerfiles créés (legacy + GCS Phase 1-6)"

# ── Nettoyage containers ───────────────────────────────────────────────
for _ct in gaiverland-db gaiverland-analyzer gaiverland-playlist \
           gaiverland-rebexis gaiverland-tts gaiverland-scheduler \
           gaiverland-ollama gaiverland-bootstrap \
           gaiverland-gcs-track gaiverland-gcs-state gaiverland-gcs-rebexis \
           gaiverland-gcs-tts gaiverland-gcs-injector gaiverland-gcs-vote \
           gaiverland-gcs-lore; do
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

# ── Profils composites (azuracast + GCS optionnel) ────────────────────
# v2 data/web (track, vote, state, lore, web, weather, monitoring) — activé par DÉFAUT
# désormais (géré par Caleope, persiste aux redéploiements). Ce sont eux qui font
# marcher votes + ville + journal.
GCS_ENABLED="${CALEOPE_PARAM_GCS_ENABLED:-true}"
if [[ "${GCS_ENABLED}" == "true" ]]; then
    [[ -n "${COMPOSE_PROFILES_VALUE}" ]] \
        && COMPOSE_PROFILES_VALUE="${COMPOSE_PROFILES_VALUE},gcs" \
        || COMPOSE_PROFILES_VALUE="gcs"
fi
# Pipeline AUDIO v2 (gcs-rebexis/tts/injector) — OPT-IN via --param gcs_audio=true.
# Remplace la v1 (gw-rebexis/tts) ; laissée OFF tant que la v2 audio n'est pas validée
# (sinon doublon d'annonces Rebexis). Migration audio = étape séparée.
GCS_AUDIO="${CALEOPE_PARAM_GCS_AUDIO:-false}"
if [[ "${GCS_AUDIO}" == "true" ]]; then
    COMPOSE_PROFILES_VALUE="${COMPOSE_PROFILES_VALUE},gcs-audio"
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
RADIO_DOMAIN=${CALEOPE_DOMAIN:+radio.${CALEOPE_DOMAIN}}
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
GENRE_HOURS="${CALEOPE_PARAM_GENRE_HOURS:-Hardstyle,Rawstyle,Hardcore,Happy Hardcore,Frenchcore,Uptempo,Gabber,Speedcore,Terrorcore,Mentalcore,Tribecore:22-06}"

WEB_PORT="${CALEOPE_PORT_WEB:-8099}"
SFTP_PORT="${CALEOPE_PORT_SFTP:-2022}"
ICECAST_PORT="${CALEOPE_PORT_ICECAST:-8500}"
API_PORT="${CALEOPE_PORT_API:-8080}"

# ── Secrets (IDEMPOTENT) ───────────────────────────────────────────────
# Un reinstall (caleope install --force) NE DOIT JAMAIS régénérer les mots de passe :
# les volumes Postgres/MariaDB existants gardent les anciens, une régénération casserait
# la connexion à la DB (nouvelle config ≠ ancienne base). On relit donc les valeurs déjà
# écrites (si l'app est déjà installée) AVANT de réécrire db.env / azuracast.env plus bas.
_existing() {  # _existing <fichier.env> <clé>  → valeur existante, ou vide
    [ -f "$1" ] || return 0
    awk -F= -v k="$2" '$1==k{print substr($0,length(k)+2);exit}' "$1"
}

# ── Secrets DB ─────────────────────────────────────────────────────────
DB_PASSWORD="$(_existing "${CONFIG_DIR}/db.env" POSTGRES_PASSWORD)"
[ -n "${DB_PASSWORD}" ] || DB_PASSWORD=$(openssl rand -hex 20)

# ── Secrets AzuraCast (mode autonome seulement) ────────────────────────
AZ_ADMIN_EMAIL="admin@azuracast.local"
AZ_ADMIN_PASSWORD="$(_existing "${CONFIG_DIR}/azuracast.env" AZURACAST_ADMIN_PASSWORD)"
[ -n "${AZ_ADMIN_PASSWORD}" ] || AZ_ADMIN_PASSWORD=$(openssl rand -base64 16 | tr -dc 'a-zA-Z0-9' | cut -c1-16)
AZ_MYSQL_ROOT="$(_existing "${CONFIG_DIR}/azuracast.env" MYSQL_ROOT_PASSWORD)"
[ -n "${AZ_MYSQL_ROOT}" ] || AZ_MYSQL_ROOT=$(openssl rand -hex 24)
AZ_MYSQL_PWD="$(_existing "${CONFIG_DIR}/azuracast.env" MYSQL_PASSWORD)"
[ -n "${AZ_MYSQL_PWD}" ] || AZ_MYSQL_PWD=$(openssl rand -hex 20)

# ── URL publique d'AzuraCast (réécrit les URLs stream/pochette servies au web) ──
# Idempotent : réutilise la valeur en place, sinon le param --param az_public_url=..., sinon vide.
# Sans ça, gcs-web renvoie des URLs en IP LAN → player muet et pas de pochette hors LAN.
AZ_PUBLIC_URL="$(_existing "${CONFIG_DIR}/services.env" GCS_AZ_PUBLIC_URL)"
[ -n "${AZ_PUBLIC_URL}" ] || AZ_PUBLIC_URL="${CALEOPE_PARAM_AZ_PUBLIC_URL:-}"

# ── Secrets OAuth votes (Google/Discord) — idempotent : param la 1re fois, préservé ensuite.
# JAMAIS dans git : uniquement dans services.env (app-config) sur le serveur.
GOOGLE_CLIENT_ID="$(_existing "${CONFIG_DIR}/services.env" GOOGLE_CLIENT_ID)";       [ -n "$GOOGLE_CLIENT_ID" ]     || GOOGLE_CLIENT_ID="${CALEOPE_PARAM_GOOGLE_CLIENT_ID:-}"
GOOGLE_CLIENT_SECRET="$(_existing "${CONFIG_DIR}/services.env" GOOGLE_CLIENT_SECRET)"; [ -n "$GOOGLE_CLIENT_SECRET" ] || GOOGLE_CLIENT_SECRET="${CALEOPE_PARAM_GOOGLE_CLIENT_SECRET:-}"
DISCORD_CLIENT_ID="$(_existing "${CONFIG_DIR}/services.env" DISCORD_CLIENT_ID)";       [ -n "$DISCORD_CLIENT_ID" ]     || DISCORD_CLIENT_ID="${CALEOPE_PARAM_DISCORD_CLIENT_ID:-}"
DISCORD_CLIENT_SECRET="$(_existing "${CONFIG_DIR}/services.env" DISCORD_CLIENT_SECRET)"; [ -n "$DISCORD_CLIENT_SECRET" ] || DISCORD_CLIENT_SECRET="${CALEOPE_PARAM_DISCORD_CLIENT_SECRET:-}"
GCS_PUBLIC_BASE="$(_existing "${CONFIG_DIR}/services.env" GCS_PUBLIC_BASE)";           [ -n "$GCS_PUBLIC_BASE" ]       || GCS_PUBLIC_BASE="${CALEOPE_PARAM_GCS_PUBLIC_BASE:-https://gaiverland.gaiver-it.fr}"
OAUTH_SESSION_SECRET="$(_existing "${CONFIG_DIR}/services.env" OAUTH_SESSION_SECRET)"; [ -n "$OAUTH_SESSION_SECRET" ] || OAUTH_SESSION_SECRET="$(openssl rand -hex 32)"

# ── Clé API AzuraCast (IDEMPOTENT) ─────────────────────────────────────
# La clé est ajoutée à la main après l'install (jamais fournie par le store).
# Un reinstall NE DOIT PAS la remettre à vide : on relit la clé déjà en place
# (si l'app est déjà installée), sinon le param --param azuracast_api_key=...,
# sinon vide. Écrase l'assignation param-seul faite plus haut (ligne ~179).
AZ_API_KEY="$(_existing "${CONFIG_DIR}/services.env" AZURACAST_API_KEY)"
[ -n "${AZ_API_KEY}" ] || AZ_API_KEY="${CALEOPE_PARAM_AZURACAST_API_KEY:-}"

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
GCS_AZ_PUBLIC_URL=${AZ_PUBLIC_URL}
GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}
GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}
DISCORD_CLIENT_ID=${DISCORD_CLIENT_ID}
DISCORD_CLIENT_SECRET=${DISCORD_CLIENT_SECRET}
GCS_PUBLIC_BASE=${GCS_PUBLIC_BASE}
OAUTH_SESSION_SECRET=${OAUTH_SESSION_SECRET}
AZURACAST_API_KEY=${AZ_API_KEY}
AZURACAST_STATION_ID=${AZ_STATION_ID}
AZURACAST_STATIONS_PATH=${CALEOPE_BASE_DIR}/app-data/azuracast/stations
AZURACAST_GW_PLAYLIST_ID=
AZURACAST_REBEXIS_PLAYLIST_ID=
REBEXIS_MODE=${REBEXIS_MODE}
REBEXIS_INTERVAL_MIN=${REBEXIS_INTERVAL_MIN}
REBEXIS_INTERVAL_MAX=${REBEXIS_INTERVAL_MAX}
DISCOVERY_RATIO=${DISCOVERY_RATIO}
# Dayparting : genres restreints à certaines plages horaires (format "Genre1,Genre2:HH-HH;...")
GENRE_HOURS=${GENRE_HOURS}
TTS_CACHE_DIR=/tts-cache
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

# ── services-preprod.env ───────────────────────────────────────────────
# Station AzuraCast id=2, DB gaiverland_preprod, cache TTS séparé
AZ_PREPROD_STATION_ID="${CALEOPE_PARAM_AZURACAST_PREPROD_STATION_ID:-2}"
cat > "${CONFIG_DIR}/services-preprod.env" <<EOF
DATABASE_URL=postgresql://gaiverland:${DB_PASSWORD}@gw-db:5432/gaiverland_preprod
AZURACAST_URL=${AZ_URL}
AZURACAST_API_KEY=${AZ_API_KEY}
AZURACAST_STATION_ID=${AZ_PREPROD_STATION_ID}
AZURACAST_STATIONS_PATH=${CALEOPE_BASE_DIR}/app-data/azuracast/stations
AZURACAST_GW_PLAYLIST_ID=
AZURACAST_REBEXIS_PLAYLIST_ID=
REBEXIS_MODE=${REBEXIS_MODE}
REBEXIS_INTERVAL_MIN=${REBEXIS_INTERVAL_MIN}
REBEXIS_INTERVAL_MAX=${REBEXIS_INTERVAL_MAX}
DISCOVERY_RATIO=${DISCOVERY_RATIO}
GENRE_HOURS=${GENRE_HOURS}
TTS_CACHE_DIR=/tts-cache
ESSENTIA_MODELS_DIR=/essentia-models
OLLAMA_URL=http://gaiverland-ollama:11434
OLLAMA_MODEL=${REBEXIS_LLM_MODEL}
EOF
chmod 600 "${CONFIG_DIR}/services-preprod.env"

mkdir -p "${STORAGE_PATH}/tts-cache-pp"

# ── gcs.env — configuration GCS Phase 1 ──────────────────────────────
GCS_CITY="${CALEOPE_PARAM_GCS_CITY:-Toulon}"
GCS_INJECT_ACTIVE="${CALEOPE_PARAM_GCS_INJECT_ACTIVE:-false}"
cat > "${CONFIG_DIR}/gcs.env" <<EOF
# Ville de Gaiverland (change environ tous les 3 ans)
GCS_CITY=${GCS_CITY}
# URLs internes GCS (Docker network gaiverland-internal)
GCS_STATE_ENGINE_URL=http://gcs-state-engine:8091
GCS_REBEXIS_URL=http://gcs-rebexis:8092
GCS_TTS_URL=http://gcs-tts:8093
GCS_TRACK_URL=http://gcs-track-service:8090
GCS_LORE_SERVICE_URL=http://gcs-lore-service:8096
GCS_VOTE_URL=http://gcs-vote-service:8095
GCS_INJECTOR_URL=http://gcs-audio-injector:8094
GCS_WEATHER_URL=http://gcs-weather:8098
# Intervalle de polling AzuraCast en secondes
GCS_POLL_INTERVAL=10
# Injection active : false=shadow/log, true=injecte dans AzuraCast
GCS_INJECT_ACTIVE=${GCS_INJECT_ACTIVE}
# Rebexis timing (minutes) et mémoire anti-répétition
REBEXIS_INTERVAL_MIN=12
REBEXIS_INTERVAL_MAX=25
REBEXIS_MEMORY_HOURS=24
REBEXIS_MEMORY_MAX=50
# Météo : intervalle en minutes
GCS_WEATHER_INTERVAL_MIN=30
EOF

echo "  ✓ Fichiers de config créés (legacy + GCS)"

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

-- GCS Phase 1 — état festival complet (séparé de radio_state)
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
);

INSERT INTO gcs_state (id) VALUES (1) ON CONFLICT DO NOTHING;

-- GCS Phase 3 — cache TTS avec emotion dans la clé
CREATE TABLE IF NOT EXISTS gcs_tts_cache (
    id          SERIAL PRIMARY KEY,
    cache_key   VARCHAR(64) UNIQUE NOT NULL,
    text        TEXT NOT NULL,
    emotion     VARCHAR(30) NOT NULL DEFAULT 'playful',
    voice_id    VARCHAR(100) NOT NULL,
    audio_file  TEXT,
    el_chars    INTEGER DEFAULT 0,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_gcs_tts_key ON gcs_tts_cache(cache_key);

-- GCS Phase 5 — votes ENCORE/REVIEW/SKIP
CREATE TABLE IF NOT EXISTS votes (
    id          SERIAL PRIMARY KEY,
    song_id     VARCHAR(100) NOT NULL,
    vote        VARCHAR(10)  NOT NULL CHECK (vote IN ('ENCORE','REVIEW','SKIP')),
    user_role   VARCHAR(20)  NOT NULL DEFAULT 'user',
    user_weight FLOAT        NOT NULL DEFAULT 0.3,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS track_scores (
    song_id     VARCHAR(100) PRIMARY KEY,
    score       FLOAT        NOT NULL DEFAULT 0.0,
    vote_count  INTEGER      NOT NULL DEFAULT 0,
    last_vote   TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_votes_song ON votes(song_id);

-- GCS Phase 6 — mémoire lore festival
CREATE TABLE IF NOT EXISTS lore_events (
    id          SERIAL PRIMARY KEY,
    type        VARCHAR(50)  NOT NULL,
    description TEXT         NOT NULL,
    city        VARCHAR(100) DEFAULT '',
    metadata    JSONB        DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_lore_type ON lore_events(type);
CREATE INDEX IF NOT EXISTS idx_lore_time ON lore_events(created_at DESC);

-- GCS v2 — mémoire phrases Rebexis (anti-répétition 24h)
CREATE TABLE IF NOT EXISTS rebexis_phrases (
    id          SERIAL PRIMARY KEY,
    phrase_hash VARCHAR(32) NOT NULL UNIQUE,
    phrase_text TEXT        NOT NULL,
    mode        VARCHAR(30) DEFAULT '',
    used_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_phrases_used ON rebexis_phrases(used_at DESC);

-- GCS v2 — score interne par morceau
ALTER TABLE tracks ADD COLUMN IF NOT EXISTS gaiverland_score FLOAT DEFAULT NULL;

-- Multi-radio prep — structure pour futures radios
CREATE TABLE IF NOT EXISTS radio_profiles (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(50)  NOT NULL UNIQUE,
    display_name VARCHAR(100) NOT NULL,
    stage       VARCHAR(20)  DEFAULT 'mainstage',
    mood_filter JSONB        DEFAULT '[]',
    energy_min  INTEGER      DEFAULT 1,
    energy_max  INTEGER      DEFAULT 5,
    active      BOOLEAN      DEFAULT FALSE,
    config      JSONB        DEFAULT '{}',
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

INSERT INTO radio_profiles (name, display_name, stage, mood_filter, energy_min, energy_max) VALUES
    ('mainstage', 'Mainstage', 'mainstage', '["festival","intense","energique"]', 3, 5),
    ('rush',      'Rush',      'rush',      '["intense","festival"]',             4, 5),
    ('sunset',    'Sunset',    'sunset',    '["melodique","energique"]',          2, 4),
    ('house',     'House',     'mainstage', '["energique","melodique"]',          3, 4),
    ('hardstyle', 'Hardstyle', 'mainstage', '["intense"]',                        4, 5)
ON CONFLICT (name) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_tracks_mood      ON tracks(mood);
CREATE INDEX IF NOT EXISTS idx_tracks_bpm       ON tracks(bpm);
CREATE INDEX IF NOT EXISTS idx_tracks_az        ON tracks(az_id);
CREATE INDEX IF NOT EXISTS idx_history_time     ON play_history(played_at DESC);
SQL
echo "  ✓ Schéma DB créé (legacy + gcs_state)"

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
cp "${CONFIG_DIR}/rebexis-templates.json" "${SCRIPTS_DIR}/templates.json"
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
    """Assigne des fichiers (az_id) à des playlists (AzuraCast 0.23+).
    Le batch endpoint attend des chemins de fichiers, pas des IDs."""
    if not _ok() or not file_ids:
        return False
    try:
        # 1. Récupérer les chemins correspondant aux IDs
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/files",
                      headers=_headers(), params={"rowsPerPage": 500}, timeout=30)
        if r.status_code != 200:
            return False
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("rows", [])
        id_to_path = {row["id"]: row["path"] for row in rows if "id" in row and "path" in row}
        paths = [id_to_path[fid] for fid in file_ids if fid in id_to_path]
        if not paths:
            return False
        # 2. Batch assign avec chemins + do=playlist
        r2 = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/files/batch",
                       headers=_headers(),
                       json={"do": "playlist", "files": paths,
                             "playlists": [str(pid) for pid in playlist_ids]},
                       timeout=30)
        d = r2.json() if r2.status_code in (200, 201) else {}
        return bool(d.get("success", False))
    except Exception as e:
        print(f"  ⚠ batch_assign_playlist: {e}")
        return False


def remove_files_from_playlist(file_ids: list, playlist_id: int) -> int:
    """Retire des fichiers (az_id) d'une playlist SANS toucher à leurs autres
    playlists. Le batch AzuraCast étant additif, le retrait se fait fichier par
    fichier via PUT (mêmes semantics que replace_playlist). Retourne le nombre
    de fichiers effectivement retirés."""
    if not _ok() or not file_ids:
        return 0
    removed = 0
    try:
        all_rows = _get_all_files()
        id_to_path = {row["id"]: row["path"] for row in all_rows if "id" in row and "path" in row}
        path_to_playlists = {row["path"]: [p["id"] for p in row.get("playlists", [])] for row in all_rows}
        for fid in file_ids:
            path = id_to_path.get(fid)
            if not path:
                continue
            current = path_to_playlists.get(path, [])
            if playlist_id not in current:
                continue  # déjà absent de la playlist
            keep = [p for p in current if p != playlist_id]
            try:
                r = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/file/{fid}",
                              headers=_headers(), json={"playlists": keep}, timeout=10)
                if r.status_code < 400:
                    removed += 1
            except Exception:
                pass
        return removed
    except Exception as e:
        print(f"  ⚠ remove_files_from_playlist: {e}")
        return 0


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
    """Upload un fichier audio dans AzuraCast (format JSON base64), retourne l'objet créé."""
    if not _ok():
        return None
    try:
        import base64
        with open(local_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode()
        dest_path = f"gaiverland/{os.path.basename(local_path)}"
        r = httpx.post(
            f"{AZ_URL}/api/station/{AZ_STATION}/files",
            headers={"X-API-Key": AZ_KEY, "Content-Type": "application/json"},
            json={"path": dest_path, "file": content_b64},
            timeout=120,
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


def get_queue(limit: int = 6) -> list:
    """Retourne les prochaines pistes planifiées dans la queue AzuraCast."""
    if not _ok():
        return []
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/queue",
                      headers=_headers(), timeout=5)
        items = r.json() if r.status_code == 200 else []
        return items[:limit] if isinstance(items, list) else []
    except Exception:
        return []


def update_playlist(playlist_id: int, **kwargs) -> bool:
    """Met à jour les propriétés d'une playlist AzuraCast (weight, is_enabled, etc.)."""
    if not _ok():
        return False
    try:
        r = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/playlist/{playlist_id}",
                      headers=_headers(), json=kwargs, timeout=_TIMEOUT)
        return r.status_code < 400
    except Exception as e:
        print(f"  warning update_playlist: {e}")
        return False


def _get_all_files() -> list:
    """Récupère tous les fichiers de la station (max 1000)."""
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/files",
                      headers=_headers(), params={"rowsPerPage": 1000}, timeout=30)
        rows = r.json() if r.status_code == 200 else []
        return rows.get("rows", rows) if isinstance(rows, dict) else rows
    except Exception:
        return []


def replace_playlist(file_ids: list, playlist_id: int,
                     prev_az_ids: list = None) -> bool:
    """
    Remplace le contenu de la playlist par file_ids.
    - Retire prev_az_ids de la playlist via PUT individuel (si fournis)
    - Ajoute les nouveaux via batch assign
    Note: le batch AzuraCast (do=playlist) est additif. Le retrait se fait fichier par fichier.
    """
    if not _ok() or not file_ids:
        return False
    try:
        all_rows = _get_all_files()
        id_to_path = {row["id"]: row["path"] for row in all_rows if "id" in row and "path" in row}
        path_to_playlists = {row["path"]: [p["id"] for p in row.get("playlists", [])] for row in all_rows}

        # Retirer prev_az_ids de la playlist si c'est une rotation
        to_remove = [fid for fid in (prev_az_ids or []) if fid not in file_ids and fid in id_to_path]
        for fid in to_remove[:20]:  # max 20 suppressions par cycle pour limiter la charge
            path = id_to_path[fid]
            current_pls = [p for p in path_to_playlists.get(path, []) if p != playlist_id]
            try:
                httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/file/{fid}",
                          headers=_headers(), json={"playlists": current_pls}, timeout=10)
            except Exception:
                pass

        # Ajouter les nouveaux
        new_paths = [id_to_path[fid] for fid in file_ids if fid in id_to_path]
        if not new_paths:
            return False
        r2 = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/files/batch",
                       headers=_headers(),
                       json={"do": "playlist", "files": new_paths,
                             "playlists": [str(playlist_id)]},
                       timeout=30)
        d = r2.json() if r2.status_code in (200, 201) else {}
        return bool(d.get("success", False))
    except Exception as e:
        print(f"  ⚠ replace_playlist: {e}")
        return False
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
    
    "genre_labels": "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.json",
}

# Mapping genres Discogs → moods Gaiverland
GENRE_TO_MOOD = {
    # Intense
    "Hardstyle": "intense", "Hardcore": "intense", "Gabber": "intense",
    "Hard Techno": "intense", "Industrial": "intense", "Speedcore": "intense",
    "UK Hardcore": "intense", "Frenchcore": "intense",
    "Hardbass": "intense", "Hard Bass": "intense", "Rawstyle": "intense",
    "Uptempo": "intense", "Terrorcore": "intense", "Hard Dance": "intense",
    "Hard House": "intense", "Schranz": "intense", "Makina": "intense",
    "Donk": "intense", "Jump Up": "intense",
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

# Préfixes de fichiers à exclure de l'analyse (jingles, TTS, etc.)
SKIP_PREFIXES = ("rebexis_",)


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
            import urllib.request; print(f"  → download {url}"); urllib.request.urlretrieve(url, str(dest))
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
    # Utiliser le même modèle effnet pour la classification directe (PartitionedCall:0)
    _effnet_model = es.TensorflowPredictEffnetDiscogs(
        graphFilename=str(effnet_path), output="PartitionedCall:0"
    )
    _genre_model = _effnet_model  # Alias — même modèle, output direct
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


def apply_bpm_guard(mood: str, bpm: float) -> str:
    """Corrige les incohérences flagrantes BPM/mood après classification genre.
    Évite ex: Happy Hardcore 161 BPM → festival, ou House 104 BPM → festival.
    """
    if mood in ("festival", "energique", "intense") and bpm < 112:
        return "melodique"
    if mood in ("festival", "energique") and bpm >= 150:
        return "intense"
    if mood == "intense" and bpm < 115:
        return "festival"
    if mood in ("nocturne", "melodique") and bpm > 165:
        return "intense"  # double-tempo probable mais safe fallback
    return mood


def apply_energy_guard(mood: str, bpm: float, energy: float) -> str:
    """Gros kick + loudness écrasée = hard music même sous 150 BPM
    (hardbass, rawstyle mid-tempo). Réservé à la nuit via mood=intense."""
    if mood in ("festival", "energique") and energy >= 0.82 and bpm >= 138:
        return "intense"
    return mood


# Mots-clés qui trahissent un style dur (nuit uniquement, 22h-06h)
_HARD_HINTS = (
    "frenchcore", "hardcore", "hardstyle", "uptempo", "gabber", "rawstyle",
    "terror", "speedcore", "tekstyle", "hardbass", "hard bass", "makina",
    "happy hardcore", "hard techno", "hard trance", "mainstream hardcore",
    "hard dance", "donk",
)


def apply_hard_genre_guard(mood: str, title: str, genre_top1: str, genre_top2: str) -> str:
    """Titre OU genre trahissant un style dur → nuit (intense), quel que soit le BPM.
    Indispensable car les frenchcore/hardstyle (~200 BPM) sont souvent détectés à
    demi-tempo (~100 BPM) et/ou mal classés (ex: 'Tribal') → l'inférence BPM/genre
    les laissait fuiter en journée. Le titre est le signal le plus fiable
    ('... Frenchcore Remix', 'Hardstyle Edit')."""
    hay = f"{title} {genre_top1} {genre_top2}".lower()
    if any(h in hay for h in _HARD_HINTS):
        return "intense"
    return mood


def apply_half_tempo_guard(mood: str, bpm: float, energy: float) -> str:
    """Hard music détecté à demi-tempo : énergie très haute avec un BPM anormalement
    bas pour de la journée (le vrai tempo est ~2x). Un vrai titre de jour à 90-108 BPM
    est downtempo/doux, pas à énergie ≥ 0.88 → on bascule en nuit."""
    if mood in ("festival", "energique", "melodique") and energy >= 0.88 and 90 <= bpm <= 108:
        return "intense"
    return mood


def analyze_file(path: str) -> dict:
    try:
        # ── Chargement audio ─────────────────────────────────────────
        loader = es.MonoLoader(filename=path, sampleRate=44100)
        audio_44k = loader()

        # Pour les modèles Essentia (16kHz)
        loader16 = es.MonoLoader(filename=path, sampleRate=16000)
        audio_16k = loader16()

        # ── Extracteur BPM, énergie, tonalité ────────────────────────
        extractor = es.MusicExtractor()
        features, _ = extractor(path)

        bpm          = float(features["rhythm.bpm"])
        energy       = float(features["lowlevel.average_loudness"])
        energy_norm  = min(1.0, max(0.0, (energy + 1.0) / 2.0))
        danceability = float(features["rhythm.danceability"])
        # tonal keys: variantes selon version Essentia
        try:
            key_note  = str(features["tonal.key_temperley.key"])
            key_scale = str(features["tonal.key_temperley.scale"])
        except Exception:
            try:
                key_note  = str(features["tonal.key_edma.key"])
                key_scale = str(features["tonal.key_edma.scale"])
            except Exception:
                key_note, key_scale = "", ""

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

        if _effnet_model and _genre_labels:
            try:
                predictions  = _effnet_model(audio_16k)
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

        # Garde BPM : corrige les incohérences flagrantes genre/BPM
        mood = apply_bpm_guard(mood, bpm)
        mood = apply_energy_guard(mood, bpm, energy_norm)

        # ── Métadonnées ID3/tags ──────────────────────────────────────
        meta = mutagen.File(path, easy=True) or {}
        title    = str(meta.get("title",  [""])[0]) or os.path.basename(path)
        artist   = str(meta.get("artist", [""])[0]) or "Inconnu"
        album    = str(meta.get("album",  [""])[0]) or ""
        duration = float(features["metadata.audio_properties.length"])

        # Gardes anti-fuite hard en journée (titre fiable + demi-tempo)
        mood = apply_hard_genre_guard(mood, title, genre_top1, genre_top2)
        mood = apply_half_tempo_guard(mood, bpm, energy_norm)

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
    if not data.get("analyzed"):
        return None
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
    conn.autocommit = True  # jamais de transaction ouverte (idle-in-transaction) → évite le lock sur 'tracks' qui bloquait le state-engine
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM tracks WHERE analyzed=TRUE")
        known = {r[0] for r in cur.fetchall()}

    def should_skip(filename: str) -> bool:
        return filename.startswith(SKIP_PREFIXES)

    # Scan initial
    count = 0
    for root, _, files in os.walk(WATCH_DIR):
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS and not should_skip(f):
                fp = os.path.join(root, f)
                if fp not in known:
                    print(f"  → {os.path.basename(fp)}")
                    save_track(conn, analyze_file(fp))
                    count += 1
    print(f"  ✓ {count} fichiers analysés au démarrage")

    # Surveillance inotify — dict wd→dossier pour reconstituer le chemin complet
    inotify = inotify_simple.INotify()
    wd_to_dir = {}
    for root, _, _ in os.walk(WATCH_DIR):
        wd = inotify.add_watch(root, inotify_simple.flags.CLOSE_WRITE | inotify_simple.flags.MOVED_TO)
        wd_to_dir[wd] = root
    print("  ✓ Surveillance temps réel active")

    while True:
        events = inotify.read(timeout=5000)
        for event in events:
            name = event.name.decode() if isinstance(event.name, bytes) else event.name
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS and not should_skip(name):
                try:
                    conn.cursor().execute("SELECT 1")
                except Exception:
                    conn = get_conn()
                    conn.autocommit = True
                # Reconstruire le chemin complet avec le dossier de l'event
                event_dir = wd_to_dir.get(event.wd, WATCH_DIR)
                fp = os.path.join(event_dir, name)
                print(f"  → Nouveau : {fp}")
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
NO_REPEAT_HOURS = float(os.environ.get("NO_REPEAT_HOURS", "11"))
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
    # Jour : festival / energique / melodique interchangeables (jamais d'intense).
    #  melodique = house mélodique énergie ~0.92 (identique à festival) : l'exclure
    #  du jour rétrécissait le pool de moitié (194 vs 417) → répétitions forcées.
    "festival":   ["festival", "energique", "melodique"],
    "energique":  ["energique", "festival", "melodique"],
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
            cur.execute("""
                SELECT t.id, t.artist, t.title,
                   round((0.6*COALESCE(avg(CASE WHEN v.user_role='founder'   THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 ELSE 0.0 END) END),0)
                        + 0.3*COALESCE(avg(CASE WHEN v.user_role='user'      THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 ELSE 0.0 END) END),0)
                        + 0.1*COALESCE(avg(CASE WHEN v.user_role='system_ai' THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 ELSE 0.0 END) END),0))::numeric, 3) AS score,
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

    # --- Effet des votes (spec Cassy) : score net pondéré Bible sur 14 j ---
    #  SKIP net ≤ -0.4 → quarantaine jour ; ENCORE ≥ +0.4 → boost fréquence.
    #  Reliés à tracks via song_id (hash AzuraCast capté par record_plays).
    vote_scores = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.id AS id,
                   0.6*COALESCE(avg(CASE WHEN v.user_role='founder'   THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 ELSE 0.0 END) END),0)
                 + 0.3*COALESCE(avg(CASE WHEN v.user_role='user'      THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 ELSE 0.0 END) END),0)
                 + 0.1*COALESCE(avg(CASE WHEN v.user_role='system_ai' THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 ELSE 0.0 END) END),0) AS score
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
    # SKIP : les titres rejetés sortent de la rotation jour (exclusion, comme l'anti-répétition)
    exclude_ids = list(set(recent_ids) | quarantined) or [0]

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
            WHERE analyzed=TRUE AND mood = ANY(%s) AND id != ALL(%s)
              AND file_path NOT LIKE %s
              AND genre_top1 IS NOT NULL
              AND genre_top1 != ALL(%s)
              AND genre_top1 = ANY(%s)
              AND (NOT %s OR title !~* %s)
            ORDER BY RANDOM() LIMIT %s
        """, (candidate_moods, exclude_ids, '%rebexis_%', excluded_now or ['__none__'], GENRE_WHITELIST, day_mode, HARD_TITLE_RE, count * 4))
        candidates = list(cur.fetchall())

    # ENCORE : les titres soutenus reviennent plus souvent — on les injecte dans le
    # vivier (priorité de fréquence) SANS forcer un rejeu le jour même (anti-répétition
    # respectée : on écarte ceux joués récemment). Ils restent bornés au thème jour.
    if encored:
        already = {c["id"] for c in candidates}
        boost_ids = [tid for tid in encored if tid not in already and tid not in set(recent_ids)]
        if boost_ids:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, title, artist, bpm, energy, danceability, mood, genre_top1, az_id,
                           key_note, key_scale
                    FROM tracks
                    WHERE id = ANY(%s) AND analyzed=TRUE AND mood = ANY(%s)
                      AND file_path NOT LIKE %s
                      AND genre_top1 = ANY(%s)
                      AND (NOT %s OR title !~* %s)
                """, (boost_ids, candidate_moods, '%rebexis_%', GENRE_WHITELIST, day_mode, HARD_TITLE_RE))
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
                  AND mood = ANY(%s)
                  AND genre_top1 IS NOT NULL
                  AND genre_top1 != ALL(%s)
                  AND genre_top1 = ANY(%s)
                  AND (NOT %s OR title !~* %s)
                ORDER BY RANDOM() LIMIT %s
            """, ('%rebexis_%', candidate_moods, excluded_now or ['__none__'], GENRE_WHITELIST, day_mode, HARD_TITLE_RE, count * 2))
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

# ── elevenlabs.env — variables API ElevenLabs (idempotent : préserve clé/voice existantes) ──
EL_KEY_VAL="$(_existing "${CONFIG_DIR}/elevenlabs.env" ELEVENLABS_API_KEY)"
{ [ -n "$EL_KEY_VAL" ] && [ "$EL_KEY_VAL" != "your_elevenlabs_api_key_here" ]; } || EL_KEY_VAL="${CALEOPE_PARAM_ELEVENLABS_API_KEY:-your_elevenlabs_api_key_here}"
EL_VOICE_VAL="$(_existing "${CONFIG_DIR}/elevenlabs.env" ELEVENLABS_VOICE_ID)"
{ [ -n "$EL_VOICE_VAL" ] && [ "$EL_VOICE_VAL" != "your_voice_id_here" ]; } || EL_VOICE_VAL="${CALEOPE_PARAM_ELEVENLABS_VOICE_ID:-your_voice_id_here}"
cat > "${CONFIG_DIR}/elevenlabs.env" <<ENVEOF
# Clé API ElevenLabs (https://elevenlabs.io → Profile → API Keys)
ELEVENLABS_API_KEY=${EL_KEY_VAL}
# ID de la voix Rebexis (ElevenLabs → Voices → ta voix → ID)
ELEVENLABS_VOICE_ID=${EL_VOICE_VAL}
# Modèle recommandé pour le français expressif
ELEVENLABS_MODEL=eleven_v3
# Limite mensuelle en caractères (plan gratuit = 10000)
EL_CHARS_LIMIT=10000
ENVEOF
chmod 600 "${CONFIG_DIR}/elevenlabs.env"
echo "  ✓ elevenlabs.env (clé/voice préservées si déjà posées)"

# ── tts_worker.py — ElevenLabs API + bibliothèque de phrases cachées ─────
cat > "${SCRIPTS_DIR}/tts_worker.py" <<'PYEOF'
"""
TTS Worker — ElevenLabs API + bibliothèque de phrases cachées
Stratégie :
  - Chaque phrase générée est stockée en DB (tts_library) → jamais régénérée
  - Quota mensuel : 10 000 chars/mois (paramétrable EL_CHARS_LIMIT)
  - [playful] tag auto sur toutes les phrases pour expressivité radio
  - Pré-génération des phrases statiques Cat3 au démarrage
  - Dégradation gracieuse si quota épuisé : pioche dans la même catégorie en cache
"""
import os, sys, subprocess, hashlib, pathlib, threading, queue, time, re

# ── Deps bootstrap ────────────────────────────────────────────────────────────
def install_deps():
    subprocess.run(["apt-get", "update", "-qq"], check=True)
    subprocess.run(["apt-get", "install", "-y", "-qq", "ffmpeg"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    install_deps()
    import fastapi, uvicorn, psycopg2, httpx

import psycopg2.extras
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

sys.path.insert(0, "/app")
from az_utils import upload_file

# ── Config ────────────────────────────────────────────────────────────────────
DB_URL          = os.environ["DATABASE_URL"]
EL_API_KEY      = os.environ["ELEVENLABS_API_KEY"]
EL_VOICE_ID     = os.environ["ELEVENLABS_VOICE_ID"]     # ID de la voix Rebexis sur EL
EL_MODEL        = os.environ.get("ELEVENLABS_MODEL", "eleven_v3")
EL_CHARS_LIMIT  = int(os.environ.get("EL_CHARS_LIMIT", "10000"))   # par mois
TTS_CACHE       = pathlib.Path(os.environ.get("TTS_CACHE_DIR", "/tts-cache"))
TTS_CACHE.mkdir(parents=True, exist_ok=True)

# Paramètres voix ElevenLabs — validés : jeune, motivée, ton radio
EL_VOICE_SETTINGS = {
    "stability":         0.30,   # dynamique, moins contrôlé = plus naturel/jeune
    "similarity_boost":  0.75,
    "style":             0.75,   # expressivité radio affirmée
    "use_speaker_boost": True,
}

# ── Phrases statiques pré-générées au démarrage (coût fixe une seule fois) ───
# Cat3 : blocs musicaux (aucune variable, priorité absolue)
STATIC_PHRASES = [
    # --- Blocs musicaux ---
    ("cat3_bloc", "Et maintenant, un maximum d'électro."),
    ("cat3_bloc", "Retour à la musique."),
    ("cat3_bloc", "On continue sans ralentir."),
    ("cat3_bloc", "On garde cette énergie."),
    ("cat3_bloc", "La nuit avance, la musique aussi."),
    ("cat3_bloc", "On enchaîne, et on ne ralentit pas."),
    ("cat3_bloc", "Gaiverland Radio — on reste en mode plein gaz."),
    ("cat3_bloc", "Et voilà, on repart."),
    # --- Nouveauté sans artiste ---
    ("cat4_nouveaute", "Place à une nouveauté qui a attiré mon attention."),
    ("cat4_nouveaute", "Un nouveau titre que je voulais vraiment partager avec vous ce soir."),
]

# Templates avec variables — générés à la demande, cachés à vie
# {artist} ou {title} sont résolus dynamiquement
TEMPLATES = {
    # Catégorie 1 — lancement artiste
    "cat1_artiste_1": "Et maintenant, {artist} sur Gaiverland Radio.",
    "cat1_artiste_2": "Place à {artist}.",
    "cat1_artiste_3": "On retrouve maintenant {artist}.",
    # Catégorie 2 — lancement morceau
    "cat2_morceau_1": "Et maintenant, {title}.",
    "cat2_morceau_2": "Voici {title}.",
    "cat2_morceau_3": "On s'écoute {title}.",
    # Catégorie 4 — nouveauté avec artiste
    "cat4_nouveaute_1": "Et maintenant une découverte signée {artist}.",
}

# ── Chaîne ffmpeg radio ───────────────────────────────────────────────────────
FFMPEG_RADIO = ",".join([
    "highpass=f=90",
    "equalizer=f=180:width_type=o:width=2:g=-3",
    "equalizer=f=2500:width_type=o:width=1.5:g=4",
    "equalizer=f=10000:width_type=o:width=2:g=3",
    "acompressor=threshold=-22dB:ratio=4:attack=3:release=150:makeup=6",
    "aexciter=level_in=1:level_out=1:amount=1.5:drive=2",
    "alimiter=limit=0.92:attack=0.5:release=3",
    "loudnorm=I=-13:LRA=5:TP=-1.0",
])

app = FastAPI(title="TTS Worker — ElevenLabs")
_synth_queue: queue.Queue = queue.Queue()
_current_job: dict | None = None
_queue_lock = threading.Lock()


# ── DB ────────────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    """Crée les tables tts_library et el_monthly_quota si elles n'existent pas."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tts_library (
                id           SERIAL PRIMARY KEY,
                text_hash    VARCHAR(64) UNIQUE NOT NULL,
                text         TEXT NOT NULL,
                category     VARCHAR(50) NOT NULL DEFAULT 'custom',
                audio_file   TEXT,
                az_file_id   INTEGER,
                el_chars     INTEGER NOT NULL DEFAULT 0,
                created_at   TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS el_monthly_quota (
                month       CHAR(7) PRIMARY KEY,
                chars_used  INTEGER NOT NULL DEFAULT 0,
                chars_limit INTEGER NOT NULL DEFAULT 10000,
                updated_at  TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tts_lib_hash ON tts_library(text_hash)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tts_lib_cat  ON tts_library(category)")
    conn.commit()
    conn.close()
    print("✓ DB tts_library + el_monthly_quota OK")


# ── Quota ElevenLabs ──────────────────────────────────────────────────────────
def current_month() -> str:
    import datetime
    return datetime.date.today().strftime("%Y-%m")


def quota_used(conn) -> int:
    month = current_month()
    with conn.cursor() as cur:
        cur.execute("SELECT chars_used FROM el_monthly_quota WHERE month=%s", (month,))
        row = cur.fetchone()
    return row["chars_used"] if row else 0


def quota_remaining(conn) -> int:
    return max(0, EL_CHARS_LIMIT - quota_used(conn))


def quota_add(conn, chars: int):
    month = current_month()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO el_monthly_quota (month, chars_used, chars_limit)
            VALUES (%s, %s, %s)
            ON CONFLICT (month) DO UPDATE
               SET chars_used  = el_monthly_quota.chars_used + EXCLUDED.chars_used,
                   chars_limit = EXCLUDED.chars_limit,
                   updated_at  = NOW()
        """, (month, chars, EL_CHARS_LIMIT))
    conn.commit()


# ── Cache bibliothèque ────────────────────────────────────────────────────────
def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def library_get(conn, text: str) -> dict | None:
    """Retourne l'entrée de bibliothèque si le texte existe déjà."""
    h = text_hash(text)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM tts_library WHERE text_hash=%s AND audio_file IS NOT NULL",
            (h,)
        )
        return cur.fetchone()


def library_random_fallback(conn, category: str) -> dict | None:
    """Phrase aléatoire de la même catégorie (fallback quota épuisé)."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT * FROM tts_library
            WHERE category=%s AND audio_file IS NOT NULL
            ORDER BY RANDOM() LIMIT 1
        """, (category,))
        return cur.fetchone()


def library_save(conn, text: str, category: str, audio_file: str,
                 az_file_id: int | None, el_chars: int):
    h = text_hash(text)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tts_library (text_hash, text, category, audio_file, az_file_id, el_chars)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (text_hash) DO UPDATE
               SET audio_file = EXCLUDED.audio_file,
                   az_file_id = EXCLUDED.az_file_id,
                   el_chars   = EXCLUDED.el_chars
        """, (h, text, category, audio_file, az_file_id, el_chars))
    conn.commit()


# ── ElevenLabs API ────────────────────────────────────────────────────────────
def el_add_playful(text: str) -> str:
    """Ajoute [playful] si pas déjà présent."""
    if text.startswith("["):
        return text
    return f"[playful] {text}"


def el_synthesize(text: str) -> bytes:
    """Appelle l'API ElevenLabs et retourne les bytes MP3."""
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{EL_VOICE_ID}"
    resp = httpx.post(
        url,
        headers={"xi-api-key": EL_API_KEY, "Content-Type": "application/json"},
        json={
            "text": text,
            "model_id": EL_MODEL,
            "voice_settings": EL_VOICE_SETTINGS,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"ElevenLabs HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.content


def apply_radio(mp3_bytes: bytes, label: str) -> pathlib.Path:
    """Applique la chaîne radio ffmpeg sur les bytes MP3 EL → fichier final."""
    h = hashlib.sha256(label.encode()).hexdigest()[:16]
    raw_path = TTS_CACHE / f"el_raw_{h}.mp3"
    out_path  = TTS_CACHE / f"rebexis_{h}.mp3"

    if out_path.exists():
        return out_path

    raw_path.write_bytes(mp3_bytes)
    r = subprocess.run([
        "ffmpeg", "-y", "-i", str(raw_path),
        "-af", FFMPEG_RADIO,
        "-b:a", "192k", "-ar", "44100", str(out_path)
    ], capture_output=True)
    raw_path.unlink(missing_ok=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg radio: {r.stderr.decode()[:200]}")
    return out_path


# ── Génération d'une phrase (avec cache + quota) ──────────────────────────────
_el_credits = {"pct": None, "at": 0.0}


def el_credits_pct():
    """% de crédits ElevenLabs restants via /v1/user/subscription (caché 5 min, None si indispo)."""
    now = time.time()
    if _el_credits["at"] and now - _el_credits["at"] < 300:
        return _el_credits["pct"]
    pct = None
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/user/subscription",
                      headers={"xi-api-key": EL_API_KEY}, timeout=6)
        if r.status_code == 200:
            d = r.json()
            limit = d.get("character_limit") or 0
            used = d.get("character_count") or 0
            # limit<=0 = compte sans allocation (crédits épuisés / free expiré) → 0% → archives
            pct = 0.0 if limit <= 0 else max(0.0, 100.0 * (limit - used) / limit)
    except Exception as e:
        print(f"  ⚠ crédits EL: check impossible ({e})")
    _el_credits.update(pct=pct, at=now)
    return pct


def generate_phrase(text: str, category: str = "custom") -> pathlib.Path:
    """
    Génère ou récupère du cache un fichier audio pour le texte donné.
    - Cache hit → retourne immédiatement
    - Quota OK  → appel EL API, sauvegarde bibliothèque
    - Quota KO  → fallback aléatoire même catégorie, ou RuntimeError
    """
    conn = get_conn()

    # 1. Cache check
    cached = library_get(conn, text)
    if cached and pathlib.Path(cached["audio_file"]).exists():
        print(f"  ✓ cache hit [{category}] → {pathlib.Path(cached['audio_file']).name}")
        conn.close()
        return pathlib.Path(cached["audio_file"])

    # 2. Garde-fou crédits EL réels (spec chef 09/07 : LOG à 15 %, bascule ARCHIVES à 2 %) + quota local
    el_text = el_add_playful(text)
    needed  = len(el_text)
    remaining = quota_remaining(conn)
    pct = el_credits_pct()  # % de crédits ElevenLabs réels (None si API indispo)
    if pct is not None and 2.0 <= pct < 15.0:
        print(f"  🟠 ALERTE crédits ElevenLabs : {pct:.1f}% restants (<15%) — penser à recharger")

    low_credits = (pct is not None and pct < 2.0)
    if low_credits or remaining < needed:
        why = f"crédits EL {pct:.1f}% (<2%)" if low_credits else f"quota local {remaining}/{EL_CHARS_LIMIT}"
        print(f"  🔴 bascule ARCHIVES ({why}) — jamais 0, jamais local")
        fallback = library_random_fallback(conn, category)
        conn.close()
        if fallback:
            print(f"  ↩ fallback archives [{category}] → {fallback['id']}")
            return pathlib.Path(fallback["audio_file"])
        raise RuntimeError(
            f"Bascule archives impossible ({why}) : aucun fallback en cache pour [{category}]"
        )

    # 3. Appel EL API
    print(f"  → EL API [{category}] {needed} chars : {text[:60]}")
    t0 = time.time()
    mp3_bytes = el_synthesize(el_text)
    elapsed   = time.time() - t0
    print(f"  ✓ EL répondu en {elapsed:.1f}s ({len(mp3_bytes)//1024}KB)")

    # 4. Traitement radio
    mp3_path = apply_radio(mp3_bytes, text)

    # 5. Upload AzuraCast
    conn2 = get_conn()
    try:
        az_result  = upload_file(str(mp3_path), f"Rebexis — {text[:40]}")
        az_file_id = az_result.get("id") if az_result else None
    except Exception as e:
        print(f"  ⚠ upload AZ: {e}")
        az_file_id = None

    # 6. Sauvegarder bibliothèque + quota
    library_save(conn2, text, category, str(mp3_path), az_file_id, needed)
    quota_add(conn2, needed)
    conn2.close()

    used = quota_used(get_conn())
    print(f"  ✓ bibliothèque sauvée | quota {used}/{EL_CHARS_LIMIT} chars ce mois")
    return mp3_path


# ── Pré-génération des phrases statiques ─────────────────────────────────────
def pregen_static():
    """Génère toutes les phrases Cat3 statiques si pas encore en cache."""
    print("→ Pré-génération phrases statiques...")
    conn = get_conn()
    done = skipped = 0
    for category, text in STATIC_PHRASES:
        cached = library_get(conn, text)
        if cached:
            skipped += 1
            continue
        remaining = quota_remaining(conn)
        if remaining < len(el_add_playful(text)):
            print(f"  ⚠ quota insuffisant pour pré-gen, arrêt")
            break
        conn.close()
        try:
            generate_phrase(text, category)
            done += 1
        except Exception as e:
            print(f"  ✗ pré-gen '{text[:40]}': {e}")
        conn = get_conn()
    conn.close()
    print(f"✓ Pré-génération : {done} générées, {skipped} déjà en cache")


# ── Worker queue ──────────────────────────────────────────────────────────────
def get_rebexis_playlist_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT az_rb_playlist FROM radio_state WHERE id=1")
        row = cur.fetchone()
    return (row["az_rb_playlist"] or 0) if row else 0


def _worker_loop():
    init_db()
    pregen_static()
    global _current_job
    while True:
        job = _synth_queue.get()
        session_id = job["session_id"]
        text       = job["text"]
        category   = job.get("category", "custom")
        started_at = time.time()
        with _queue_lock:
            _current_job = {**job, "started_at": started_at}
        try:
            print(f"🎙 EL session={session_id} [{category}] : {text[:60]}")
            mp3 = generate_phrase(text, category)
            elapsed = int(time.time() - started_at)
            print(f"✓ session={session_id} en {elapsed}s → {mp3.name}")

            conn = get_conn()
            rb_pl_id = get_rebexis_playlist_id(conn)

            # Récupérer l'az_file_id depuis la bibliothèque
            lib_entry = library_get(conn, text)
            az_file_id = lib_entry["az_file_id"] if lib_entry else None

            if rb_pl_id and az_file_id:
                from az_utils import batch_assign_playlist
                batch_assign_playlist([az_file_id], [rb_pl_id])

            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE rebexis_sessions SET audio_file=%s, az_file_id=%s WHERE id=%s",
                    (str(mp3), az_file_id, session_id)
                )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"✗ session={session_id}: {e}")
        finally:
            with _queue_lock:
                _current_job = None
            _synth_queue.task_done()


# ── API ───────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    with _queue_lock:
        cur = _current_job
    try:
        conn = get_conn()
        used = quota_used(conn)
        remaining = quota_remaining(conn)
        with conn.cursor() as cur2:
            cur2.execute("SELECT COUNT(*) AS n FROM tts_library WHERE audio_file IS NOT NULL")
            lib_count = cur2.fetchone()["n"]
        conn.close()
    except Exception:
        used = remaining = lib_count = -1
    return {
        "status":        "ok",
        "engine":        "elevenlabs",
        "voice_id":      EL_VOICE_ID,
        "model":         EL_MODEL,
        "quota_used":    used,
        "quota_remaining": remaining,
        "quota_limit":   EL_CHARS_LIMIT,
        "library_count": lib_count,
        "queue_size":    _synth_queue.qsize(),
        "current_job":   cur["session_id"] if cur else None,
        "elapsed_s":     int(time.time() - cur["started_at"]) if cur else None,
    }


@app.post("/synthesize")
def synthesize_endpoint(session_id: int, text: str,
                        mood: str = "default", category: str = "custom"):
    """Enfile la synthèse. Cache check d'abord, EL API si nécessaire."""
    _synth_queue.put({
        "session_id": session_id,
        "text":       text,
        "mood":       mood,
        "category":   category,
    })
    with _queue_lock:
        busy = _current_job is not None
    return {
        "ok":                True,
        "queued":            True,
        "position":          _synth_queue.qsize(),
        "estimated_seconds": (_synth_queue.qsize() + (1 if busy else 0)) * 3,
    }


@app.post("/synthesize_template")
def synthesize_template(session_id: int, template_key: str,
                        artist: str = "", title: str = ""):
    """
    Génère depuis un template prédéfini.
    Ex: template_key=cat1_artiste_1, artist=Daft Punk
    → "Et maintenant, Daft Punk sur Gaiverland Radio."
    """
    if template_key not in TEMPLATES:
        raise HTTPException(400, f"Template inconnu: {template_key}. Disponibles: {list(TEMPLATES.keys())}")
    text = TEMPLATES[template_key].format(artist=artist, title=title)
    cat  = template_key.split("_")[0] + "_" + template_key.split("_")[1]
    _synth_queue.put({
        "session_id": session_id,
        "text":       text,
        "mood":       "default",
        "category":   cat,
    })
    return {"ok": True, "queued": True, "text": text, "category": cat}


@app.get("/library")
def get_library(category: str = "", limit: int = 50):
    """Liste les phrases en cache."""
    conn = get_conn()
    with conn.cursor() as cur:
        if category:
            cur.execute(
                "SELECT id, category, text, el_chars, created_at FROM tts_library "
                "WHERE category=%s ORDER BY created_at DESC LIMIT %s",
                (category, limit)
            )
        else:
            cur.execute(
                "SELECT id, category, text, el_chars, created_at FROM tts_library "
                "ORDER BY created_at DESC LIMIT %s", (limit,)
            )
        rows = list(cur.fetchall())
    conn.close()
    return {"count": len(rows), "phrases": rows}


@app.get("/quota")
def get_quota():
    """Quota ElevenLabs du mois en cours."""
    conn = get_conn()
    used = quota_used(conn)
    conn.close()
    return {
        "month":     current_month(),
        "used":      used,
        "remaining": max(0, EL_CHARS_LIMIT - used),
        "limit":     EL_CHARS_LIMIT,
        "pct":       round(used / EL_CHARS_LIMIT * 100, 1),
    }


@app.post("/pregen")
def trigger_pregen():
    """Déclenche la pré-génération des phrases statiques manuellement."""
    threading.Thread(target=pregen_static, daemon=True).start()
    return {"ok": True, "message": "Pré-génération lancée en arrière-plan"}


@app.get("/templates")
def list_templates():
    return {"templates": TEMPLATES}


if __name__ == "__main__":
    month = current_month()
    print(f"TTS Worker — ElevenLabs | Voice: {EL_VOICE_ID} | Quota: {EL_CHARS_LIMIT} chars/{month}")
    t = threading.Thread(target=_worker_loop, daemon=True)
    t.start()
    uvicorn.run(app, host="0.0.0.0", port=8082)

PYEOF

# ── scheduler.py — playlists AzuraCast 0.23.4 ─────────────────────────
cat > "${SCRIPTS_DIR}/scheduler.py" <<'PYEOF'
"""
Scheduler Gaiverland — orchestre playlist + Rebexis + TTS.
Gère les playlists AzuraCast 0.23.4 directement via API.

Stratégie playlist :
  - "Gaiverland IA" (default, weight=3) : 20-30 titres mood-appropriés, mis à jour toutes les 3 min
  - "Rebexis"       (once_per_x_songs)  : jingles de Rebexis générés à l'avance
"""
import os, sys, subprocess, time, datetime, unicodedata
from zoneinfo import ZoneInfo
LOCAL_TZ = ZoneInfo("Europe/Paris")

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "httpx", "psycopg2-binary", "tzdata"], check=True)

try:
    import httpx, psycopg2
except ImportError:
    install_deps()
    import httpx, psycopg2

import psycopg2.extras
import sys
sys.path.insert(0, "/app")
from az_utils import (get_or_create_playlist, batch_assign_playlist, replace_playlist,
                      set_playlist_order, get_station, now_playing, get_queue, update_playlist,
                      _get_all_files)

DB_URL       = os.environ["DATABASE_URL"]
PLAYLIST_URL = "http://gaiverland-playlist:8080"
REBEXIS_URL  = "http://gaiverland-rebexis:8081"
TTS_URL      = "http://gaiverland-tts:8082"
CYCLE_SEC    = 180  # 3 minutes
AZ_KEY       = os.environ.get("AZURACAST_API_KEY", "")
PROPOSAL_INTERVAL_S = int(os.environ.get("PROPOSAL_CHECK_INTERVAL_S", str(6 * 3600)))  # 6h
_last_proposal_check = 0.0

# Intervalle Rebexis (en nombre de morceaux entre chaque jingle)
REBEXIS_SONGS_INTERVAL = 3
# Timer indépendant pour les phrases lore (en secondes)
LORE_INTERVAL_S = 45 * 60  # 1 phrase lore toutes les 45 min

# Créneaux horaires : jour = EDM/energique, nuit = nocturne
NIGHT_START = (22, 0)  # 22h00
NIGHT_END   = (7, 0)   # 7h00


_lore_last_time: float = 0.0
_current_slot: str = ""


def get_time_slot() -> str:
    now = datetime.datetime.now(LOCAL_TZ)
    h, m = now.hour, now.minute
    if h >= NIGHT_START[0] or (h, m) < NIGHT_END:
        return "night"
    return "day"


def maybe_update_slot_mood(gw_id: int, wd_id: int = 0, fr_id: int = 0):
    """Bascule mood selon le créneau : jour=energique, nuit=nocturne."""
    global _current_slot
    slot = get_time_slot()
    if slot == _current_slot:
        return
    _current_slot = slot
    try:
        target_mood = "intense" if slot == "night" else "festival"
        httpx.post(f"{PLAYLIST_URL}/state/mood", params={"mood": target_mood}, timeout=5)
        icon = "nuit" if slot == "night" else "soleil"
        print(f"  {icon} Créneau {slot} -> mood = {target_mood}")
    except Exception as e:
        print(f"  warning Slot mood: {e}")


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

    # get_or_create_playlist ne reconfigure PAS une playlist "Rebexis" déjà
    # existante (il renvoie son ID tel quel). Si elle a été créée un jour en
    # rotation générale, ses jingles jouent en série -> plusieurs voix d'affilée.
    # On ré-applique donc le type à chaque démarrage pour garantir 1 jingle/X titres.
    if rb_id:
        update_playlist(rb_id, type="once_per_x_songs",
                        play_per_songs=REBEXIS_SONGS_INTERVAL,
                        weight=1, is_enabled=True)

    # Lecture séquentielle pour la playlist musicale : l'ordre harmonique calculé
    # par le moteur doit être respecté à l'antenne, pas re-mélangé par AzuraCast.
    if gw_id:
        update_playlist(gw_id, order="sequential")

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

    # Playlists secondaires désactivées
    for pl_name in ("Travail Decouverte", "Bien Francais"):
        pl_id = get_or_create_playlist(pl_name, pl_type="default", weight=2)
        if pl_id:
            update_playlist(pl_id, is_enabled=False)

    return gw_id, rb_id, 0, 0


def update_gaiverland_playlist(conn, gw_playlist_id: int):
    """Met à jour la playlist 'Gaiverland IA' avec les titres mood-appropriés."""
    if not gw_playlist_id:
        return

    try:
        # 40 (pas 25) : playlist plus large → si le moteur cale entre 2 cycles,
        # AzuraCast boucle sur 40 titres au lieu de 20 (moins de répétition audible).
        resp = httpx.get(f"{PLAYLIST_URL}/playlist/next", params={"count": 40}, timeout=20)
        data = resp.json()
        tracks = data.get("tracks", [])
        mood   = data.get("mood", "?")

        az_ids = [t["az_id"] for t in tracks if t.get("az_id")]
        if not az_ids:
            print(f"  ℹ Playlist [{mood}] — aucun az_id disponible (analyzer pas encore synchro ?)")
            return

        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT az_id FROM tracks WHERE az_playlist_assigned=true AND az_id IS NOT NULL")
            tracked = [row["az_id"] for row in cur.fetchall()]
        # Retrait ROBUSTE : on part des membres RÉELS de la playlist côté AzuraCast,
        # pas seulement de ce qu'on a tracké en base. Un retrait échoué à un cycle
        # laissait sinon un orphelin collé pour toujours — typiquement un titre
        # intense/hardstyle d'un cycle de nuit qui polluait la rotation JOUR (Subzero
        # & co entendus à midi). On purge donc tout ce qui est actuellement dans la
        # playlist et absent de la nouvelle sélection. Auto-réparant cycle après cycle.
        try:
            files = _get_all_files()
            current_members = [f["id"] for f in files
                               if any((p.get("id") == gw_playlist_id or p.get("name") == "Gaiverland IA")
                                      for p in (f.get("playlists") or []))]
        except Exception:
            current_members = []
        prev_az_ids = list({*tracked, *current_members} - set(az_ids))

        ok = replace_playlist(az_ids, gw_playlist_id, prev_az_ids=prev_az_ids)

        if ok:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("UPDATE tracks SET az_playlist_assigned=false WHERE az_playlist_assigned=true")
                if az_ids:
                    cur.execute("UPDATE tracks SET az_playlist_assigned=true WHERE az_id = ANY(%s)", (az_ids,))
            conn.commit()
            print(f"  📻 Playlist [{mood}] mise à jour — {len(az_ids)} titres")
            # Imposer l'ordre d'enchaînement calculé (sinon AzuraCast rejoue dans
            # son ordre interne et la cohérence harmonique est perdue).
            if set_playlist_order(gw_playlist_id, az_ids):
                print(f"  🎚 Ordre d'enchaînement appliqué ({len(az_ids)} titres)")
        else:
            print(f"  ⚠ Mise à jour playlist échouée")
    except Exception as e:
        print(f"  ⚠ update_playlist: {e}")


def process_tts_queue():
    """Convertit en audio les interventions Rebexis en attente."""
    try:
        pending = httpx.get(f"{TTS_URL}/pending", timeout=10).json().get("sessions", [])
        for s in pending[:2]:
            print(f"  → TTS session {s['id']}: {s['intervention'][:50]}…")
            httpx.post(f"{TTS_URL}/synthesize",
                       params={"session_id": s["id"], "text": s["intervention"]},
                       timeout=120)
    except Exception as e:
        print(f"  ⚠ TTS queue: {e}")


def maybe_generate_rebexis():
    """Déclenche la génération d'une intervention Rebexis.

    Les phrases "lore" (identité de Rebexis) ont leur propre timer indépendant
    (LORE_INTERVAL_S). Les phrases mood-appropriées suivent l'interval check du
    moteur Rebexis (INT_MIN/INT_MAX). Les deux timers sont décorrélés pour éviter
    que le lore monopolise toutes les interventions.
    """
    global _lore_last_time
    try:
        now = time.time()
        force_lore = (now - _lore_last_time >= LORE_INTERVAL_S)

        if not force_lore:
            state = httpx.get(f"{PLAYLIST_URL}/state", timeout=5).json()
            mood  = state.get("mood", "energique")
        else:
            mood = "lore"

        # Titre en cours
        np_data = now_playing()
        context = ""
        if np_data:
            song = np_data.get("now_playing", {}).get("song", {})
            context = f"{song.get('artist', '')} — {song.get('title', '')}".strip(" —")

        # Prochain titre musical (skip les jingles Rebexis)
        next_music = ""
        try:
            queue = get_queue(limit=6)
            for entry in queue:
                song = entry.get("song", {})
                title = song.get("title", "")
                artist = song.get("artist", "")
                if title and "rebexis" not in title.lower() and "rebexis" not in artist.lower():
                    next_music = f"{artist} — {title}".strip(" —") if artist else title
                    break
        except Exception:
            pass

        resp = httpx.post(f"{REBEXIS_URL}/generate",
                          params={"mood": mood, "context_track": context,
                                  "next_track": next_music, "force": str(force_lore).lower()},
                          timeout=30)
        data = resp.json()
        if data.get("intervention"):
            if force_lore:
                _lore_last_time = now
            suffix = f" → {next_music[:35]}" if next_music else ""
            print(f"  🎙 Rebexis [{mood}]{suffix} : {data['intervention'][:60]}…")
    except Exception as e:
        print(f"  ⚠ Rebexis: {e}")


_recorded_sh: set = set()  # sh_id AzuraCast déjà enregistrés (dédup)


def _norm(s: str) -> str:
    """Normalise pour un matching robuste : retire accents (NFKD), ne garde que
    l'alphanumérique, minuscules. Indispensable car les titres accentués sont
    parfois corrompus en base ('Dernière' -> 'Dernie?re') → le match exact
    échouait → le morceau n'était pas enregistré → il repassait en boucle."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def record_plays():
    """Enregistre dans play_history les morceaux réellement diffusés (source de
    vérité = historique AzuraCast). Sans ça, l'anti-répétition du moteur de
    playlist n'a rien à exclure → les titres se répètent en journée.
    Dédup par sh_id ; matching titre/artiste NORMALISÉ (robuste accents/encodage).
    Tout est encapsulé : une erreur DB/réseau ne doit jamais tuer la boucle."""
    try:
        np = now_playing()
        if not np:
            return
        entries = []
        if np.get("now_playing"):
            entries.append(np["now_playing"])
        entries += (np.get("song_history") or [])
        pending = [(e.get("sh_id"), e.get("song") or {}) for e in entries
                   if e.get("sh_id") and e.get("sh_id") not in _recorded_sh]
        if not pending:
            return

        recorded = 0
        conn = get_conn()
        try:
            # Index : exact (titre,artiste) + par artiste pour le prefix-match
            by_ta = {}          # (nt, na) -> id
            by_artist = {}      # na -> [(nt, id)]
            with conn.cursor() as cur:
                cur.execute("SELECT id, title, artist FROM tracks WHERE analyzed=TRUE")
                for r in cur.fetchall():
                    nt = _norm(r["title"])
                    if not nt:
                        continue
                    na = _norm(r.get("artist") or "")
                    by_ta[(nt, na)] = r["id"]
                    by_artist.setdefault(na, []).append((nt, r["id"]))

            def _match(nt, na):
                if (nt, na) in by_ta:                  # match exact
                    return by_ta[(nt, na)]
                best = None                            # sinon préfixe (même artiste) :
                for cnt, cid in by_artist.get(na, []): # gère les suffixes AzuraCast
                    if len(cnt) < 8 or len(nt) < 8:    # (feat., [Clean], (Bass Boosted)…)
                        continue                       # trop court = risque de faux positif
                    if nt.startswith(cnt) or cnt.startswith(nt):
                        if best is None or len(cnt) > best[1]:
                            best = (cid, len(cnt))
                return best[0] if best else None

            with conn.cursor() as cur:
                for sh_id, song in pending:
                    nt = _norm(song.get("title") or "")
                    if nt:
                        tid = _match(nt, _norm(song.get("artist") or ""))
                        if tid:
                            cur.execute("INSERT INTO play_history (track_id) VALUES (%s)", (tid,))
                            recorded += 1
                            # Relie le titre au hash AzuraCast (song.id) → clé des votes.
                            # C'est ce chaînon qui permet à l'effet des votes de viser
                            # le bon track dans le moteur de rotation.
                            sid = song.get("id") or ""
                            if sid:
                                cur.execute(
                                    "UPDATE tracks SET song_id=%s WHERE id=%s "
                                    "AND (song_id IS NULL OR song_id<>%s)",
                                    (sid, tid, sid))
                    _recorded_sh.add(sh_id)
            conn.commit()
        finally:
            conn.close()

        if len(_recorded_sh) > 2000:      # borne mémoire
            _recorded_sh.clear()
        if recorded:
            print(f"  📝 {recorded} lecture(s) enregistrée(s) dans play_history")
    except Exception as e:
        print(f"  ⚠ record_plays: {e}")


def maybe_validate_proposals():
    """Classe périodiquement les titres proposés par la communauté (accept/reject
    par genre, via proposal_validator.py). Résultats dans proposal_decisions, à
    disposition de Régis. Ne télécharge rien : l'import musical reste sa main."""
    global _last_proposal_check
    now = time.time()
    if now - _last_proposal_check < PROPOSAL_INTERVAL_S:
        return
    _last_proposal_check = now
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proposal_validator.py")
    if not os.path.exists(script):
        return
    try:
        print("  🔎 validation des propositions communauté…")
        subprocess.run([sys.executable, script], timeout=300, check=False)
    except Exception as e:
        print(f"  ⚠ validation propositions : {e}")


def backfill_song_ids():
    """Backfill one-shot du song_id (hash AzuraCast) sur toute la librairie depuis
    l'API fichiers, en matchant titre/artiste normalisés. Rend l'effet des votes
    opérationnel IMMÉDIATEMENT après un (re)déploiement, sans attendre que chaque
    titre rejoue (record_plays entretient ensuite au fil de l'eau). No-op si la
    couverture est déjà bonne."""
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n, count(song_id) AS s FROM tracks WHERE analyzed=TRUE")
                row = cur.fetchone(); n = row["n"] or 0; s = row["s"] or 0
            if n == 0 or (s / n) >= 0.5:
                return  # déjà couvert → on ne rappelle pas l'API à chaque boot
            files = _get_all_files()
            if not files:
                return
            by_ta, by_art = {}, {}
            with conn.cursor() as cur:
                cur.execute("SELECT id, title, artist FROM tracks WHERE analyzed=TRUE")
                for r in cur.fetchall():
                    nt = _norm(r["title"])
                    if not nt:
                        continue
                    na = _norm(r.get("artist") or "")
                    by_ta[(nt, na)] = r["id"]
                    by_art.setdefault(na, []).append((nt, r["id"]))

            def _match(nt, na):
                if (nt, na) in by_ta:
                    return by_ta[(nt, na)]
                best = None
                for cnt, cid in by_art.get(na, []):
                    if len(cnt) < 8 or len(nt) < 8:
                        continue
                    if nt.startswith(cnt) or cnt.startswith(nt):
                        if best is None or len(cnt) > best[1]:
                            best = (cid, len(cnt))
                return best[0] if best else None

            upd = 0
            with conn.cursor() as cur:
                for f in files:
                    sid = f.get("song_id")
                    if not sid:
                        continue
                    tid = _match(_norm(f.get("title") or ""), _norm(f.get("artist") or ""))
                    if tid:
                        cur.execute(
                            "UPDATE tracks SET song_id=%s WHERE id=%s "
                            "AND (song_id IS NULL OR song_id<>%s)", (sid, tid, sid))
                        upd += cur.rowcount
            conn.commit()
            if upd:
                print(f"  🔗 backfill song_id : {upd} titre(s) reliés (effet votes prêt)")
        finally:
            conn.close()
    except Exception as e:
        print(f"  ⚠ backfill song_id: {e}")


def backfill_az_ids():
    """Relie tracks.az_id (id média AzuraCast) aux titres non liés. L'analyzer rate
    ce liage sur les métadonnées pourries (titres à rallonge SoundCloud/discovery)
    → ces titres restent INVISIBLES pour la rotation, ce qui rétrécit le pool jour
    et force des répétitions. On matche titre/artiste normalisés contre l'API
    fichiers, en respectant la contrainte UNIQUE (un média = un seul track ; les
    doublons non liés sont ignorés). No-op si tout est déjà lié."""
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS miss FROM tracks WHERE analyzed=TRUE AND az_id IS NULL")
                if (cur.fetchone()["miss"] or 0) == 0:
                    return
            files = _get_all_files()
            if not files:
                return
            by_ta, by_art = {}, {}
            for f in files:
                nt = _norm(f.get("title"))
                if not nt:
                    continue
                na = _norm(f.get("artist") or "")
                by_ta[(nt, na)] = f["id"]
                by_art.setdefault(na, []).append((nt, f["id"]))

            def _mfile(nt, na):
                if (nt, na) in by_ta:
                    return by_ta[(nt, na)]
                best = None
                for cnt, fid in by_art.get(na, []):
                    if len(cnt) < 8 or len(nt) < 8:
                        continue
                    if nt.startswith(cnt) or cnt.startswith(nt):
                        if best is None or len(cnt) > best[1]:
                            best = (fid, len(cnt))
                return best[0] if best else None

            with conn.cursor() as cur:
                cur.execute("SELECT az_id FROM tracks WHERE az_id IS NOT NULL")
                used = {r["az_id"] for r in cur.fetchall()}
                cur.execute("SELECT id, title, artist FROM tracks WHERE analyzed=TRUE AND az_id IS NULL")
                rows = cur.fetchall()
                upd = 0
                for r in rows:
                    fid = _mfile(_norm(r["title"]), _norm(r.get("artist") or ""))
                    if fid is None or fid in used:   # pas de média, ou média déjà pris (doublon)
                        continue
                    cur.execute("UPDATE tracks SET az_id=%s WHERE id=%s AND az_id IS NULL", (fid, r["id"]))
                    used.add(fid); upd += 1
            conn.commit()
            if upd:
                print(f"  🔗 backfill az_id : {upd} titre(s) rendus jouables (pool rotation élargi)")
        finally:
            conn.close()
    except Exception as e:
        print(f"  ⚠ backfill az_id: {e}")


def main():
    print("⚙  Scheduler Gaiverland démarré")
    wait_for(PLAYLIST_URL, "Playlist Engine")
    wait_for(REBEXIS_URL,  "Rebexis Engine")
    wait_for(TTS_URL,      "TTS Worker")

    station = get_station()
    if station:
        print(f"  ✓ AzuraCast connecté — station : {station.get('name', '?')}")
    else:
        print("  ⚠ AzuraCast non joignable — scheduler en mode dégradé (Rebexis + TTS actifs)")

    # Beatmatch niveau 2 : câbler le fondu « smart » de Liquidsoap (idempotent).
    # L'ordre par BPM proche est fait par playlist.py (Régis) ; ici on active le
    # crossfade intelligent qui beatmatche les tempos rapprochés. CPU négligeable.
    if station:
        try:
            from az_crossfade import ensure_smart_crossfade
            ensure_smart_crossfade()
        except Exception as e:
            print(f"  ⚠ crossfade non appliqué : {e}")

    conn = get_conn()
    # Colonne song_id (hash AzuraCast) : chaînon tracks ↔ votes pour l'effet des
    # votes du moteur de rotation. Idempotent — no-op si déjà présente.
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS song_id VARCHAR(64)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tracks_song_id ON tracks(song_id)")
        conn.commit()
    except Exception as e:
        print(f"  ⚠ migration song_id: {e}")
        conn.rollback()
    backfill_song_ids()  # rend l'effet des votes opérationnel dès le boot
    backfill_az_ids()    # relie les titres orphelins → élargit le pool (anti-répétition)
    gw_id, rb_id, wd_id, fr_id = setup_playlists(conn)

    print("\n✅ Boucle principale active.\n")
    cycle = 0
    while True:
        cycle += 1
        print(f"\n── Cycle #{cycle} ─────────────────────────────────")

        # 1. Traitement TTS en attente (priorité : audio prêt avant diffusion)
        process_tts_queue()

        # 2. Générer Rebexis si intervalle atteint (lore timer indépendant)
        maybe_generate_rebexis()

        # 3. Vérifier et switcher le créneau horaire (travail/standard)
        maybe_update_slot_mood(gw_id, wd_id, fr_id)

        # 3b. Enregistrer les morceaux diffusés (alimente l'anti-répétition)
        record_plays()

        # 4. Mettre à jour la playlist Gaiverland IA dans AzuraCast
        if gw_id:
            conn = get_conn()
            update_gaiverland_playlist(conn, gw_id)

        # 5. Classer les propositions de titres de la communauté (toutes les 6 h)
        maybe_validate_proposals()

        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    main()
PYEOF

cat > "${SCRIPTS_DIR}/proposal_validator.py" <<'PYEOF'
import os, re, json, urllib.request, urllib.parse, psycopg2, psycopg2.extras as E
ACCEPT_GENRES={'dance','electronic','house','techno','trance','dubstep','drum & bass','drum and bass',
  'edm','electro','dance/electronic','breakbeat','garage','ambient','downtempo','nu disco','disco',
  'hardstyle','hardcore','hard dance','future bass','bass','trap','electronica'}
def _get(url):
    return json.load(urllib.request.urlopen(url, timeout=10))
def genre_lookup(q):
    # 1. iTunes : genre direct (primaryGenreName)
    try:
        d=_get("https://itunes.apple.com/search?"+urllib.parse.urlencode({"term":q,"entity":"song","limit":1}))
        if d.get("results"):
            r=d["results"][0]; return r.get("primaryGenreName"), r["artistName"], r["trackName"]
    except Exception: pass
    # 2. Deezer fallback : search -> album -> genres
    try:
        d=_get("https://api.deezer.com/search?"+urllib.parse.urlencode({"q":q,"limit":1}))
        if d.get("data"):
            t=d["data"][0]; alb=_get(f"https://api.deezer.com/album/{t['album']['id']}")
            gs=[g['name'] for g in alb.get('genres',{}).get('data',[])]
            return (gs[0] if gs else None), t['artist']['name'], t['title']
    except Exception: pass
    return None, "", ""
def main():
    c=psycopg2.connect(os.environ['DATABASE_URL'], cursor_factory=E.RealDictCursor); cur=c.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS proposal_decisions (
        title TEXT PRIMARY KEY, verdict TEXT, genre TEXT, artist TEXT,
        canon_title TEXT, votes INT, decided_at TIMESTAMP DEFAULT NOW())""")
    # title_proposals est alimentée par gcs_web ; on la garantit ici pour pouvoir
    # tourner même avant la première proposition (sinon SELECT sur table absente).
    cur.execute("""CREATE TABLE IF NOT EXISTS title_proposals (
        id SERIAL PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
        title TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())""")
    c.commit()
    cur.execute("SELECT title, count(DISTINCT user_id) votes FROM title_proposals GROUP BY title ORDER BY votes DESC")
    props=cur.fetchall(); accepted=[]; na=nr=skip=0
    for p in props:
        cur.execute("SELECT 1 FROM proposal_decisions WHERE title=%s",(p['title'],))
        if cur.fetchone(): skip+=1; continue
        g,artist,canon=genre_lookup(p['title'])
        verdict='accept' if (g and g.lower() in ACCEPT_GENRES) else 'reject'
        cur.execute("""INSERT INTO proposal_decisions (title,verdict,genre,artist,canon_title,votes)
                       VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (title) DO NOTHING""",
                    (p['title'],verdict,g,artist,canon,p['votes']))
        if verdict=='accept': accepted.append(f"{artist} - {canon}"); na+=1
        else: nr+=1
        print(f"{'✅ ACCEPT' if verdict=='accept' else '🚫 reject'} [{g}] ({p['votes']} votes)  {p['title']}")
    c.commit()
    print(f"=== {na} acceptés, {nr} rejetés, {skip} déjà décidés ===")
    if accepted:
        print("--- À TÉLÉCHARGER ---")
        for a in accepted: print(a)
if __name__=="__main__": main()
PYEOF

echo "  ✓ Scripts Python créés (Essentia + XTTS v2 + playlists 0.23.4)"

# ── gcs_track_service.py — GCS Phase 1 : polling AzuraCast → normalize → push ─
cat > "${SCRIPTS_DIR}/gcs_track_service.py" <<'PYEOF'
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
PYEOF

# ── gcs_state_engine.py — GCS Phase 1 : GSE complet avec gcs_state table ─────
cat > "${SCRIPTS_DIR}/gcs_state_engine.py" <<'PYEOF'
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


@app.on_event("startup")
def startup():
    init_db()


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
        cur.execute("""
            UPDATE gcs_state SET
                city               = %s,
                festival_phase     = 'live',
                stage_active       = %s,
                energy_level       = %s,
                festival_direction = %s,
                time_of_day        = %s,
                last_track         = %s::jsonb,
                updated_at         = NOW()
            WHERE id = 1
        """, (GCS_CITY, stage, energy, direction, tod, json.dumps(enriched_track)))
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

PYEOF

echo "  ✓ Scripts GCS Phase 1 créés (gcs_track_service + gcs_state_engine)"

# ── gcs_rebexis.py — Phase 2 : output JSON structuré RPE v1 ──────────────────
cat > "${SCRIPTS_DIR}/gcs_rebexis.py" <<'PYEOF'
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


def pick_avoiding_recent(mode: str, track: dict, recent_hashes: set) -> tuple[str, bool]:
    """Pick a phrase from mode pool, avoiding recently used ones.
    Returns (text, was_fresh). Falls back to any phrase if all used."""
    modes = _tpl.get("modes", {})
    pool  = modes.get(mode, {}).get("templates") or \
            modes.get("normal", {}).get("templates", ["La musique continue."])

    # Expand {artist}/{title} first to compute accurate hashes
    def expand(t: str) -> str:
        t = t.replace("{artist}", track.get("artist", "l'artiste") or "l'artiste")
        t = t.replace("{title}", track.get("title", "ce morceau") or "ce morceau")
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
    candidates.append((0.06, "humor"))

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

    emotion      = ENERGY_EMOTION.get(energy, "playful")
    segment_type = STAGE_SEGMENT.get(stage, "announcement")

    # Get recent phrase hashes for dedup
    recent_hashes = get_recent_phrase_hashes(conn)

    text, was_fresh = pick_avoiding_recent(mode, track, recent_hashes)
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

PYEOF

# ── gcs_tts.py — Phase 3 : cache hash(text+emotion+voice_id) ──────────────────
cat > "${SCRIPTS_DIR}/gcs_tts.py" <<'PYEOF'
"""
GCS TTS Service — Phase 3.
Cache key: hash(text + emotion + voice_id).
Emotion influence sur les voice settings ElevenLabs.
"""
import os, sys, subprocess, hashlib, pathlib, time

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
               SET chars_used = el_monthly_quota.chars_used + EXCLUDED.chars_used,
                   updated_at = NOW()
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
    text     = body.get("text", "").strip()
    emotion  = body.get("emotion", "playful")
    voice_id = body.get("voice_id", EL_VOICE_ID)

    if not text:
        raise HTTPException(400, "text required")
    if not EL_API_KEY or not voice_id:
        raise HTTPException(503, "ElevenLabs not configured")

    key  = cache_key(text, emotion, voice_id)
    conn = get_conn()

    with conn.cursor() as cur:
        cur.execute("SELECT audio_file FROM gcs_tts_cache WHERE cache_key=%s", (key,))
        row = cur.fetchone()
    if row and row["audio_file"] and pathlib.Path(row["audio_file"]).exists():
        conn.close()
        return {"audio_file": row["audio_file"], "cache_hit": True, "el_chars_used": 0}

    el_text   = f"[playful] {text}" if not text.startswith("[") else text
    needed    = len(el_text)
    remaining = EL_CHARS_LIMIT - quota_used(conn)
    if remaining < needed:
        conn.close()
        raise HTTPException(429, f"EL quota insufficient ({remaining} chars remaining)")

    t0       = time.time()
    mp3      = synthesize_el(text, emotion)
    out_path = apply_radio(mp3, key)
    print(f"  ✓ gcs-tts [{emotion}] {needed}ch {time.time()-t0:.1f}s → {out_path.name}")

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
PYEOF

# ── gcs_audio_injector.py — Phase 4 : pipeline rebexis→tts→AzuraCast ─────────
cat > "${SCRIPTS_DIR}/gcs_audio_injector.py" <<'PYEOF'
"""
GCS Audio Injector — Phase 4.
Orchestre : gcs-rebexis → gcs-tts → AzuraCast.
Shadow mode par défaut (GCS_INJECT_ACTIVE=false) : génère tout, n'injecte pas.
Ducking = injection en fin de morceau (silence naturel).
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
_last_song_id: str     = ""
_last_inject_ts: float = 0.0
_inject_interval       = float(os.environ.get("REBEXIS_INTERVAL_MIN", "15")) * 60
_stats: dict           = {"generated": 0, "injected": 0, "errors": 0}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def az_headers():
    return {"X-API-Key": AZ_KEY, "Content-Type": "application/json"}


def get_rebexis_playlist_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT az_rb_playlist FROM radio_state WHERE id=1")
        row = cur.fetchone()
    return (row["az_rb_playlist"] or 0) if row else 0


def upload_and_queue(audio_file: str, conn) -> bool:
    rb_pl = get_rebexis_playlist_id(conn)
    if not rb_pl:
        print("  ⚠ injector: no rebexis playlist ID — not injecting")
        return False
    try:
        import base64, os as _os
        with open(audio_file, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        fname = _os.path.basename(audio_file)
        r = httpx.post(
            f"{AZ_URL}/api/station/{AZ_STATION}/files",
            headers=az_headers(),
            json={"path": f"gcs-rebexis/{fname}", "file": b64},
            timeout=60,
        )
        if r.status_code not in (200, 201):
            return False
        az_path = r.json().get("path", "")
        r2 = httpx.put(
            f"{AZ_URL}/api/station/{AZ_STATION}/files/batch",
            headers=az_headers(),
            json={"do": "playlist", "files": [az_path], "playlists": [str(rb_pl)]},
            timeout=15,
        )
        return r2.status_code in (200, 201)
    except Exception as e:
        print(f"  ⚠ injector upload: {e}")
        return False


def should_inject(elapsed: int, duration: int) -> bool:
    global _last_inject_ts
    return ((time.time() - _last_inject_ts) >= _inject_interval
            and duration > 0 and (duration - elapsed) <= 30)


def run_pipeline(track: dict):
    global _last_inject_ts, _stats
    try:
        r = httpx.post(f"{REBEXIS_URL}/generate", json={"track": track}, timeout=10)
        if r.status_code != 200:
            _stats["errors"] += 1
            return
        result = r.json()
        if result.get("intervention") is None:
            return
    except Exception as e:
        print(f"  ⚠ injector→rebexis: {e}")
        _stats["errors"] += 1
        return

    emotion = result.get("emotion", "playful")
    text    = result.get("text", "")
    if not text:
        return

    _stats["generated"] += 1
    print(f"  🎙 [{emotion}]: {text[:60]}")

    try:
        r2 = httpx.post(f"{TTS_URL}/synthesize",
                        json={"text": text, "emotion": emotion}, timeout=40)
        if r2.status_code != 200:
            return
        audio_file = r2.json().get("audio_file", "")
    except Exception as e:
        print(f"  ⚠ injector→tts: {e}")
        return

    if not audio_file:
        return

    if INJECT_ACTIVE:
        conn = get_conn()
        ok   = upload_and_queue(audio_file, conn)
        conn.close()
        if ok:
            _stats["injected"] += 1
            _last_inject_ts = time.time()
            print(f"  ✓ INJECTED [{emotion}]")
        else:
            _stats["errors"] += 1
    else:
        _last_inject_ts = time.time()
        print(f"  👁 SHADOW: would inject [{emotion}] → {audio_file}")


def poll_loop():
    global _last_song_id
    print(f"🔊 GCS Audio Injector — shadow={not INJECT_ACTIVE}")
    while True:
        try:
            r = httpx.get(f"{TRACK_URL}/track/current", timeout=5)
            if r.status_code == 200:
                track    = r.json()
                song_id  = track.get("song_id", "")
                elapsed  = int(track.get("elapsed", 0))
                duration = int(track.get("duration", 0))
                if song_id and song_id != _last_song_id:
                    _last_song_id = song_id
                if song_id and should_inject(elapsed, duration):
                    run_pipeline(track)
        except Exception as e:
            print(f"  ⚠ poll: {e}")
        time.sleep(POLL_INTERVAL)


@app.on_event("startup")
def startup():
    threading.Thread(target=poll_loop, daemon=True).start()


@app.get("/health")
def health():
    return {"status": "ok", "inject_active": INJECT_ACTIVE, **_stats}


@app.post("/inject/force")
def force_inject():
    try:
        r = httpx.get(f"{TRACK_URL}/track/current", timeout=3)
        track = r.json() if r.status_code == 200 else {}
    except Exception:
        track = {}
    threading.Thread(target=run_pipeline, args=(track,), daemon=True).start()
    return {"ok": True, "shadow": not INJECT_ACTIVE}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8094)
PYEOF

# ── gcs_vote_service.py — Phase 5 : ENCORE/REVIEW/SKIP ───────────────────────
cat > "${SCRIPTS_DIR}/gcs_vote_service.py" <<'PYEOF'
"""
GCS Vote Service — Phase 5.
ENCORE / REVIEW / SKIP — weighted scoring.
"""
import os, sys, subprocess

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    _install()
    import fastapi, uvicorn, psycopg2, httpx

import psycopg2.extras
from fastapi import FastAPI, HTTPException

DB_URL    = os.environ["DATABASE_URL"]
STATE_URL = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")

ROLE_WEIGHTS = {"founder": 0.6, "user": 0.3, "system_ai": 0.1}
VALID_VOTES  = {"ENCORE", "REVIEW", "SKIP"}

app = FastAPI(title="GCS Vote Service")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id          SERIAL PRIMARY KEY,
                song_id     VARCHAR(100) NOT NULL,
                vote        VARCHAR(10)  NOT NULL CHECK (vote IN ('ENCORE','REVIEW','SKIP')),
                user_role   VARCHAR(20)  NOT NULL DEFAULT 'user',
                user_weight FLOAT        NOT NULL DEFAULT 0.3,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS track_scores (
                song_id     VARCHAR(100) PRIMARY KEY,
                score       FLOAT        NOT NULL DEFAULT 0.0,
                vote_count  INTEGER      NOT NULL DEFAULT 0,
                last_vote   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_song ON votes(song_id)")
    conn.commit()
    conn.close()


def compute_score(song_id: str, conn) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT vote, user_weight FROM votes WHERE song_id=%s", (song_id,))
        rows = cur.fetchall()
    if not rows:
        return 0.0
    score = sum((1.0 if r["vote"]=="ENCORE" else -1.0 if r["vote"]=="SKIP" else 0.0)
                * r["user_weight"] for r in rows)
    return round(score / len(rows), 3)


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/vote")
def cast_vote(body: dict):
    song_id   = body.get("song_id", "").strip()
    vote      = body.get("vote", "").upper()
    user_role = body.get("user_role", "user")
    if not song_id:
        raise HTTPException(400, "song_id required")
    if vote not in VALID_VOTES:
        raise HTTPException(400, f"vote must be one of {VALID_VOTES}")
    if user_role not in ROLE_WEIGHTS:
        user_role = "user"
    weight = ROLE_WEIGHTS[user_role]
    conn   = get_conn()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO votes (song_id,vote,user_role,user_weight) VALUES (%s,%s,%s,%s)",
                    (song_id, vote, user_role, weight))
    conn.commit()
    score = compute_score(song_id, conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO track_scores (song_id,score,vote_count)
            VALUES (%s,%s,1)
            ON CONFLICT (song_id) DO UPDATE SET
                score=EXCLUDED.score, vote_count=track_scores.vote_count+1, last_vote=NOW()
        """, (song_id, score))
    conn.commit()
    conn.close()
    print(f"  ✓ vote [{user_role}] {vote} score={score}")
    return {"ok": True, "song_id": song_id, "vote": vote, "score": score}


@app.get("/track/{song_id}/score")
def track_score(song_id: str):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM track_scores WHERE song_id=%s", (song_id,))
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else {"song_id": song_id, "score": 0.0, "vote_count": 0}


@app.get("/leaderboard")
def leaderboard(limit: int = 10):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT song_id,score,vote_count FROM track_scores ORDER BY score DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"tracks": [dict(r) for r in rows]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8095)
PYEOF

# ── gcs_lore_service.py — Phase 6 : mémoire festival ─────────────────────────
cat > "${SCRIPTS_DIR}/gcs_lore_service.py" <<'PYEOF'
"""
GCS Lore Service — Phase 6.
Mémoire immuable : events C15, stagiaire, Rebexis, villes.
"""
import os, sys, subprocess, json, threading, random, time

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary"], check=True)

try:
    import fastapi, uvicorn, psycopg2
except ImportError:
    _install()
    import fastapi, uvicorn, psycopg2

import psycopg2.extras
from fastapi import FastAPI, HTTPException
from typing import Optional

DB_URL = os.environ["DATABASE_URL"]
VALID_TYPES = {"c15_event","stagiaire_event","city_transition",
               "rebexis_intervention","track_milestone","festival_moment"}

app = FastAPI(title="GCS Lore Service")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lore_events (
                id          SERIAL PRIMARY KEY,
                type        VARCHAR(50)  NOT NULL,
                description TEXT         NOT NULL,
                city        VARCHAR(100) DEFAULT '',
                metadata    JSONB        DEFAULT '{}',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lore_type ON lore_events(type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lore_time ON lore_events(created_at DESC)")
    conn.commit()
    conn.close()


# ── Générateur de lore (STARTER — catalogue à enrichir par Rebexis) ──────────
# Fait vivre le « Journal du festival » : insère périodiquement un event lié à
# l'état réel (ville). Contenu minimal de démarrage ; le vrai catalogue/ton = Rebexis.
GCS_CITY = os.environ.get("GCS_CITY", "Toulon")
LORE_INTERVAL_S = int(os.environ.get("LORE_GEN_INTERVAL_S", "1500"))  # ~25 min
LORE_STARTER = {
    "c15_event": [
        "Le C15 est garé à {city}. Le festival est chez lui.",
        "On a retrouvé le C15 exactement là où il devait être. Pour une fois.",
        "Le C15 ronronne dans la nuit de {city}. Il veille.",
        "Quelqu'un a lavé le C15. Personne n'avoue.",
        "Le C15 a démarré au quart de tour ce matin. À {city}, on savoure le miracle.",
        "Nouvel autocollant sur le pare-chocs du C15. On ne sait pas d'où il vient.",
        "Le C15 a fait le plein, vérifié les niveaux, et refuse de commenter la suite.",
        "Le C15 connaît la route de {city} mieux que le GPS. Il ne le dit pas, ça se sent.",
        "Quelqu'un a laissé un café tiède sur le capot du C15. Il fait avec.",
        "Le C15 a passé le contrôle technique. Le contrôleur n'en revient toujours pas.",
    ],
    "stagiaire_event": [
        "Le stagiaire a encore perdu la carte SD.",
        "Le stagiaire jure qu'il a tout sauvegardé. On vérifie... non.",
        "Le stagiaire a rebranché un câble à l'envers. Encore.",
        "On cherche le stagiaire. Le stagiaire cherche la sortie.",
        "Le stagiaire a appris ce qu'est un XLR aujourd'hui. À la dure.",
        "Le stagiaire a apporté des cafés. Trois sucres partout. Personne n'a demandé.",
        "Le stagiaire a rangé les câbles. En nœud marin. On salue l'intention.",
        "Le stagiaire a disparu pile au moment du montage. Comme un vrai pro.",
        "Le stagiaire a nommé un fichier « final_final_vrai2 ». On n'ose pas l'ouvrir.",
        "Le stagiaire a trouvé l'interrupteur. Toute la régie a clignoté. On respire.",
    ],
    "festival_moment": [
        "La foule de {city} ne veut pas que ça s'arrête.",
        "Un frisson a traversé {city} sur ce drop.",
        "Quelque part à {city}, quelqu'un danse seul et c'est parfait.",
        "L'air de {city} sent la basse et la nuit.",
        "Les mains se lèvent à {city} comme une seule vague.",
        "À {city}, deux inconnus viennent de devenir amis sur un refrain.",
        "Le sol de {city} vibre encore. On appelle ça la mémoire des enceintes.",
        "Il est tard à {city} et personne n'a envie de dormir.",
        "À {city}, même ceux qui ne dansent jamais tapent du pied.",
        "Une basse a fait trembler une vitre à {city}. On assume.",
    ],
}


def _lore_generator():
    time.sleep(20)  # laisser init_db finir
    last_type = None
    while True:
        try:
            types = [t for t in LORE_STARTER if t != last_type] or list(LORE_STARTER)
            etype = random.choice(types)
            last_type = etype
            desc = random.choice(LORE_STARTER[etype]).replace("{city}", GCS_CITY)
            conn = get_conn(); conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("INSERT INTO lore_events (type,description,city) VALUES (%s,%s,%s)",
                            (etype, desc, GCS_CITY))
            conn.close()
            print(f"  ✎ lore [{etype}] {desc}")
        except Exception as e:
            print(f"  ⚠ lore gen: {e}")
        time.sleep(LORE_INTERVAL_S)


@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=_lore_generator, daemon=True).start()


@app.get("/health")
def health():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM lore_events")
        n = cur.fetchone()["n"]
    conn.close()
    return {"status": "ok", "total_events": n}


@app.post("/events")
def log_event(body: dict):
    event_type  = body.get("type", "festival_moment")
    description = body.get("description", "").strip()
    city        = body.get("city", "")
    metadata    = body.get("metadata", {})
    if not description:
        raise HTTPException(400, "description required")
    if event_type not in VALID_TYPES:
        event_type = "festival_moment"
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO lore_events (type,description,city,metadata)
            VALUES (%s,%s,%s,%s::jsonb) RETURNING id, created_at
        """, (event_type, description, city, json.dumps(metadata)))
        row = cur.fetchone()
    conn.commit()
    conn.close()
    return {"ok": True, "id": row["id"], "created_at": str(row["created_at"])}


@app.get("/events")
def get_events(type: Optional[str] = None, limit: int = 20):
    conn = get_conn()
    with conn.cursor() as cur:
        if type:
            cur.execute("""SELECT * FROM lore_events WHERE type=%s
                           ORDER BY created_at DESC LIMIT %s""", (type, limit))
        else:
            cur.execute("SELECT * FROM lore_events ORDER BY created_at DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows]}


@app.get("/summary")
def summary():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT type, COUNT(*) AS n FROM lore_events GROUP BY type ORDER BY n DESC")
        counts = {r["type"]: r["n"] for r in cur.fetchall()}
        cur.execute("""SELECT description, created_at FROM lore_events
                       WHERE type IN ('c15_event','stagiaire_event')
                       ORDER BY created_at DESC LIMIT 3""")
        recent_lore = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT description FROM lore_events
                       WHERE type='rebexis_intervention'
                       ORDER BY created_at DESC LIMIT 5""")
        recent_rebexis = [r["description"] for r in cur.fetchall()]
    conn.close()
    return {
        "event_counts":  counts,
        "recent_lore":   recent_lore,
        "recent_rebexis": recent_rebexis,
        "lore_state": {"c15_status": "active", "stagiaire_status": "unknown",
                       "festival_is_permanent": True},
    }


@app.get("/rebexis-context")
def rebexis_context():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""SELECT description FROM lore_events
                       WHERE type='rebexis_intervention'
                       ORDER BY created_at DESC LIMIT 5""")
        recent = [r["description"] for r in cur.fetchall()]
    conn.close()
    return {"recent_interventions": recent}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8096)
PYEOF

echo "  ✓ Scripts GCS Phase 2-6 créés (rebexis/tts/injector/vote/lore)"

# ── GCS Weather & Monitoring (v2) ────────────────────────────────────────
cat > "${SCRIPTS_DIR}/gcs_weather.py" <<'PYEOF'
"""
GCS Weather Service — open-meteo.com (gratuit, sans clé).
Pousse la météo au gcs-state-engine toutes les 30 min.
Influence uniquement l'ambiance Rebexis — pas la sélection musicale.
"""
import os, sys, subprocess, time, threading

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "httpx"], check=True)
try:
    import fastapi, uvicorn, httpx
except ImportError:
    _install()
    import fastapi, uvicorn, httpx
from fastapi import FastAPI

STATE_URL     = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")
GCS_CITY      = os.environ.get("GCS_CITY", "Toulon")
POLL_INTERVAL = int(os.environ.get("GCS_WEATHER_INTERVAL_MIN", "30")) * 60

CITY_COORDS = {
    "toulon": (43.1242, 5.9280), "marseille": (43.2965, 5.3698),
    "paris": (48.8566, 2.3522), "lyon": (45.7640, 4.8357),
    "nice": (43.7102, 7.2620), "bordeaux": (44.8378, -0.5792),
}
WMO_CONDITION = {
    0: ("clear sky","sunny"), 1: ("mainly clear","sunny"), 2: ("partly cloudy","cloudy"), 3: ("overcast","cloudy"),
    45: ("fog","cloudy"), 48: ("fog","cloudy"),
    51: ("drizzle","rain"), 53: ("drizzle","rain"), 55: ("drizzle","rain"),
    61: ("rain","rain"), 63: ("rain","rain"), 65: ("heavy rain","rain"),
    80: ("showers","rain"), 81: ("showers","rain"), 82: ("violent rain","storm"),
    95: ("thunderstorm","storm"), 96: ("thunderstorm","storm"), 99: ("thunderstorm","storm"),
}

app = FastAPI(title="GCS Weather Service")
_last_weather: dict = {}
_last_fetch_ts: float = 0.0

def get_coords(city):
    return CITY_COORDS.get(city.lower(), CITY_COORDS["toulon"])

def condition_to_mood(raw, temp):
    if raw == "storm": return "storm"
    if raw == "rain": return "rain"
    if raw == "sunny" and temp >= 22: return "warm"
    if temp < 5: return "cold"
    return "calm"

def fetch_weather(city):
    lat, lon = get_coords(city)
    try:
        r = httpx.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "timezone": "Europe/Paris",
        }, timeout=10)
        if r.status_code != 200: return {}
        data = r.json().get("current", {})
        temp = float(data.get("temperature_2m", 20))
        wind = float(data.get("wind_speed_10m", 0))
        code = int(data.get("weather_code", 0))
        condition_str, raw = WMO_CONDITION.get(code, ("unknown","calm"))
        weather_mood = condition_to_mood(raw, temp)
        if wind > 40 and weather_mood not in ("storm","rain"): weather_mood = "windy"
        result = {"city": city, "temperature": round(temp,1), "wind_kmh": round(wind,1),
                  "condition": condition_str, "weather_mood": weather_mood}
        print(f"  🌤 weather [{city}]: {temp}°C {condition_str} → {weather_mood}")
        return result
    except Exception as e:
        print(f"  ⚠ weather: {e}"); return {}

def push_to_state(w):
    if not w: return
    try: httpx.post(f"{STATE_URL}/state/weather", json={"weather_mood": w["weather_mood"], "weather_data": w}, timeout=5)
    except Exception as e: print(f"  ⚠ weather→state: {e}")

def weather_loop():
    global _last_weather, _last_fetch_ts
    print(f"🌤 GCS Weather — city={GCS_CITY} poll={POLL_INTERVAL//60}min")
    while True:
        w = fetch_weather(GCS_CITY)
        if w: _last_weather = w; _last_fetch_ts = time.time(); push_to_state(w)
        time.sleep(POLL_INTERVAL)

@app.on_event("startup")
def startup(): threading.Thread(target=weather_loop, daemon=True).start()

@app.get("/health")
def health(): return {"status":"ok","city":GCS_CITY,"last_weather":_last_weather}

@app.get("/weather")
def get_weather(): return _last_weather or {"status":"not_yet_fetched","city":GCS_CITY}

@app.post("/weather/refresh")
def force_refresh():
    w = fetch_weather(GCS_CITY)
    if w:
        global _last_weather, _last_fetch_ts
        _last_weather = w; _last_fetch_ts = time.time(); push_to_state(w)
    return w or {"error":"fetch_failed"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8098)
PYEOF

cat > "${SCRIPTS_DIR}/gcs_monitoring.py" <<'PYEOF'
"""
GCS Monitoring — dashboard temps réel de production Gaiverland.
Agrège : état festival, track en cours, injections, météo, phrases, votes.
"""
import os, sys, subprocess, time, json

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
from typing import Optional

DB_URL       = os.environ["DATABASE_URL"]
STATE_URL    = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")
REBEXIS_URL  = os.environ.get("GCS_REBEXIS_URL",      "http://gcs-rebexis:8092")
TTS_URL      = os.environ.get("GCS_TTS_URL",          "http://gcs-tts:8093")
TRACK_URL    = os.environ.get("GCS_TRACK_URL",        "http://gcs-track-service:8090")
INJECTOR_URL = os.environ.get("GCS_INJECTOR_URL",     "http://gcs-audio-injector:8094")
VOTE_URL     = os.environ.get("GCS_VOTE_URL",         "http://gcs-vote-service:8095")
LORE_URL     = os.environ.get("GCS_LORE_SERVICE_URL", "http://gcs-lore-service:8096")
WEATHER_URL  = os.environ.get("GCS_WEATHER_URL",      "http://gcs-weather:8098")

app = FastAPI(title="GCS Monitoring")

def get_conn(): return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def ping(url, name):
    try:
        t0 = time.time(); r = httpx.get(f"{url}/health", timeout=2); ms = round((time.time()-t0)*1000)
        return {"name":name,"status":"ok" if r.status_code==200 else "error","latency_ms":ms,"data":r.json() if r.status_code==200 else {}}
    except Exception as e: return {"name":name,"status":"unreachable","error":str(e)[:60]}

def get_db_stats(conn):
    s = {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT intervention, created_at FROM rebexis_sessions ORDER BY created_at DESC LIMIT 1")
            row = cur.fetchone()
            if row: s["rebexis_last"] = {"text": row["intervention"][:80], "at": str(row["created_at"])}
            cur.execute("SELECT COUNT(*) as total, COUNT(CASE WHEN created_at > NOW()-INTERVAL '1 hour' THEN 1 END) as last_hour FROM gcs_tts_cache")
            r = cur.fetchone(); s["tts_cache"] = dict(r) if r else {}
            cur.execute("SELECT COUNT(*) as n FROM lore_events WHERE type='rebexis_intervention' AND created_at > NOW()-INTERVAL '24 hours'")
            r = cur.fetchone(); s["injections_today"] = r["n"] if r else 0
            cur.execute("SELECT vote, COUNT(*) as n FROM votes WHERE created_at > NOW()-INTERVAL '24 hours' GROUP BY vote")
            s["votes_today"] = {r["vote"]: r["n"] for r in cur.fetchall()}
            cur.execute("SELECT * FROM gcs_state WHERE id=1")
            row = cur.fetchone()
            if row:
                state = dict(row)
                for k in ("last_track","special_events","weather_data"):
                    if isinstance(state.get(k), str): state[k] = json.loads(state[k])
                s["festival_state"] = state
            cur.execute("SELECT COUNT(*) as n FROM rebexis_phrases WHERE used_at > NOW()-INTERVAL '24 hours'")
            r = cur.fetchone(); s["phrase_memory_24h"] = r["n"] if r else 0
    except Exception as e: s["db_error"] = str(e)[:100]
    return s

@app.get("/health")
def health(): return {"status":"ok","service":"gcs-monitoring"}

@app.get("/status")
def full_status():
    services = [
        ping(TRACK_URL,"gcs-track"), ping(STATE_URL,"gcs-state"),
        ping(REBEXIS_URL,"gcs-rebexis"), ping(TTS_URL,"gcs-tts"),
        ping(INJECTOR_URL,"gcs-injector"), ping(VOTE_URL,"gcs-vote"),
        ping(LORE_URL,"gcs-lore"), ping(WEATHER_URL,"gcs-weather"),
    ]
    ok = sum(1 for s in services if s.get("status")=="ok")
    conn = get_conn(); db = get_db_stats(conn); conn.close()
    try: ct = httpx.get(f"{TRACK_URL}/track/current", timeout=2).json()
    except: ct = {}
    try: weather = httpx.get(f"{WEATHER_URL}/weather", timeout=2).json()
    except: weather = {}
    inj = next((s for s in services if s["name"]=="gcs-injector"), {}).get("data", {})
    state = db.get("festival_state", {})
    lt = state.get("last_track", {})
    return {
        "summary": {"services_ok": ok, "services_error": len(services)-ok, "overall": "ok" if ok==len(services) else "degraded"},
        "current_track": {"title": ct.get("title",""), "artist": ct.get("artist",""), "elapsed": ct.get("elapsed",0), "duration": ct.get("duration",0)},
        "festival": {"energy": state.get("energy_level"), "target": state.get("target_energy"), "direction": state.get("festival_direction"), "stage": state.get("stage_active"), "tod": state.get("time_of_day"), "city": state.get("city"), "weather_mood": state.get("weather_mood")},
        "music_profile": lt.get("music_profile", {}) if isinstance(lt, dict) else {},
        "weather": weather,
        "rebexis_last": db.get("rebexis_last", {}),
        "injection": {"inject_active": inj.get("inject_active"), "generated": inj.get("generated"), "injected": inj.get("injected"), "errors": inj.get("errors"), "today": db.get("injections_today")},
        "tts_cache": db.get("tts_cache", {}),
        "votes_today": db.get("votes_today", {}),
        "phrase_memory_24h": db.get("phrase_memory_24h"),
        "services": services,
    }

@app.get("/events")
def recent_events(limit: int=20, type: Optional[str]=None):
    conn = get_conn()
    with conn.cursor() as cur:
        if type:
            cur.execute("SELECT id,type,description,city,created_at FROM lore_events WHERE type=%s ORDER BY created_at DESC LIMIT %s", (type, limit))
        else:
            cur.execute("SELECT id,type,description,city,created_at FROM lore_events ORDER BY created_at DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows]}

@app.get("/tracks/scores")
def track_scores(limit: int=20):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT title,artist,mood,bpm,energy,gaiverland_score FROM tracks WHERE gaiverland_score IS NOT NULL ORDER BY gaiverland_score DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"tracks": [dict(r) for r in rows]}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8097)
PYEOF

echo "  ✓ Scripts GCS v2 créés (monitoring :8097 + weather :8098)"

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

# Résoudre l'IP interne d'azuracast (musl libc Alpine peut avoir des problèmes DNS avec curl)
AZ_IP=$(getent hosts azuracast 2>/dev/null | awk '{print $1}' | head -1 || true)
if [[ -n "${AZ_IP}" ]]; then
    echo "  ℹ azuracast résolu → ${AZ_IP}"
    CURL_RESOLVE="--resolve azuracast:80:${AZ_IP}"
else
    CURL_RESOLVE=""
    echo "  ℹ Résolution DNS standard"
fi

echo "  ⏳ Attente AzuraCast (peut prendre 5 min)..."
STATUS="000"
for i in $(seq 1 60); do
    STATUS=$(curl -s ${CURL_RESOLVE} -o /dev/null -w "%{http_code}" "${AZ_URL}/" 2>/dev/null) || STATUS="000"
    [[ "${STATUS}" != "000" && "${STATUS}" != "502" && "${STATUS}" != "503" ]] && break
    echo "  ⏳ (${i}/60)..."
    sleep 10
done

[[ "${STATUS}" == "000" ]] && echo "  ⚠ AzuraCast non joignable — skip" && exit 0
echo "  ✓ AzuraCast joignable (status ${STATUS})"

sleep 3
REDIR=$(curl -sf ${CURL_RESOLVE} -o /dev/null -w "%{redirect_url}" "${AZ_URL}/" 2>/dev/null) || REDIR=""
if echo "${REDIR}" | grep -q "/setup"; then
    # AzuraCast 0.23.x: setup via web form avec CSRF
    echo "  ℹ AzuraCast en mode setup — création du compte admin..."
    rm -f /tmp/az_setup_cookies.txt

    # Récupérer la page de registration avec le cookie de session + CSRF
    CSRF=$(curl -s ${CURL_RESOLVE} -c /tmp/az_setup_cookies.txt \
        "${AZ_URL}/setup/register" 2>/dev/null | \
        grep -o '"csrf":"[^"]*"' | sed 's/"csrf":"//;s/"//')
    [[ -z "${CSRF}" ]] && CSRF=$(curl -s ${CURL_RESOLVE} -c /tmp/az_setup_cookies.txt \
        "${AZ_URL}/setup/register" 2>/dev/null | \
        grep -oP '"csrf":"[^"]+"' | cut -d'"' -f4)

    if [[ -n "${CSRF}" ]]; then
        HTTP_CODE=$(curl -s ${CURL_RESOLVE} -o /dev/null -w "%{http_code}" \
            -b /tmp/az_setup_cookies.txt -c /tmp/az_setup_cookies.txt \
            -X POST "${AZ_URL}/setup/register" \
            -H "Content-Type: application/x-www-form-urlencoded" \
            -d "username=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${EMAIL}'))")&password=${PASS}&csrf=${CSRF}" 2>/dev/null)
        [[ "${HTTP_CODE}" == "302" ]] && echo "  ✓ Compte admin créé" || echo "  ⚠ Création compte : HTTP ${HTTP_CODE}"
    else
        echo "  ⚠ Token CSRF non trouvé — configuration manuelle requise"
    fi
fi

sleep 2
# Login via web form pour obtenir une session authentifiée
rm -f /tmp/az_cookies.txt
LOGIN_CSRF=$(curl -s ${CURL_RESOLVE} -c /tmp/az_cookies.txt \
    "${AZ_URL}/login" 2>/dev/null | \
    grep -oP '"csrf":"[^"]+"' | cut -d'"' -f4)
LOGIN_STATUS=$(curl -s ${CURL_RESOLVE} -o /dev/null -w "%{http_code}" \
    -b /tmp/az_cookies.txt -c /tmp/az_cookies.txt \
    -X POST "${AZ_URL}/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=$(python3 -c "import urllib.parse; print(urllib.parse.quote('${EMAIL}'))")&password=${PASS}&csrf=${LOGIN_CSRF}" 2>/dev/null)

if [[ "${LOGIN_STATUS}" != "302" && "${LOGIN_STATUS}" != "200" ]]; then
    echo "  ⚠ Auth API échouée (HTTP ${LOGIN_STATUS}) — configuration manuelle requise"
    exit 0
fi
echo "  ✓ Auth OK (HTTP ${LOGIN_STATUS})"

# Récupérer le token CSRF API depuis la page principale
API_CSRF=$(curl -sL ${CURL_RESOLVE} -b /tmp/az_cookies.txt -c /tmp/az_cookies.txt \
    "${AZ_URL}/" 2>/dev/null | grep -oP '"apiCsrf":"[^"]+"' | cut -d'"' -f4)
echo "  ℹ apiCsrf: ${API_CSRF:-non trouvé}"

az_curl() {
    curl -sf ${CURL_RESOLVE} \
        -b /tmp/az_cookies.txt -c /tmp/az_cookies.txt \
        -H "X-API-CSRF: ${API_CSRF:-}" \
        -H "Content-Type: application/json" "$@" 2>/dev/null
}

echo "  ✓ Auth API OK"

# Créer la station si absente
EXISTING=$(az_curl "${AZ_URL}/api/stations" | jq 'length' 2>/dev/null) || EXISTING=0
if [[ "${EXISTING}" -eq 0 ]]; then
    RESP=$(az_curl -X POST "${AZ_URL}/api/admin/stations" \
        -d "{\"name\":\"${STATION}\",\"short_name\":\"${SHORT}\",\"frontend_type\":\"icecast\",\"backend_type\":\"liquidsoap\",\"frontend_config\":{\"port\":${ICECAST}},\"is_public\":false,\"enable_requests\":true}") || RESP=""
    [[ -n "${RESP}" ]] && echo "  ✓ Station '${STATION}' créée" || echo "  ⚠ Création station échouée"
fi

# Créer les playlists IA
for PL_NAME in "Gaiverland IA" "Rebexis"; do
    TYPE="default"; WEIGHT=3; PER_SONGS=0
    [[ "${PL_NAME}" == "Rebexis" ]] && TYPE="once_per_x_songs" && WEIGHT=1 && PER_SONGS=8

    BODY="{\"name\":\"${PL_NAME}\",\"type\":\"${TYPE}\",\"weight\":${WEIGHT},\"is_enabled\":true"
    [[ "${PER_SONGS}" -gt 0 ]] && BODY="${BODY},\"play_per_songs\":${PER_SONGS}"
    BODY="${BODY}}"

    PL_RESP=$(az_curl -X POST "${AZ_URL}/api/station/${AZ_STATION_ID}/playlists" -d "${BODY}") || PL_RESP=""
    [[ -n "${PL_RESP}" ]] && echo "  ✓ Playlist '${PL_NAME}' créée" || echo "  ℹ Playlist '${PL_NAME}' peut déjà exister"
done

# Créer une API key persistante pour les services
API_KEY_RESP=$(az_curl -X POST "${AZ_URL}/api/frontend/account/api-keys" \
    -d "{\"comment\":\"Gaiverland Radio — services internes\"}") || API_KEY_RESP=""
PERSISTENT_KEY=$(echo "${API_KEY_RESP}" | jq -r '.key // empty') || PERSISTENT_KEY=""

if [[ -z "${PERSISTENT_KEY}" ]]; then
    PERSISTENT_KEY="${TOKEN}"
    echo "  ℹ Utilisation du token de session comme clé API"
else
    echo "  ✓ Clé API persistante créée"
fi

# Écrire la clé dans services.env (volume monté en lecture seule → écrire dans /config-rw si dispo)
SERVICES_ENV="/bootstrap/services.env"
if [[ -w "${SERVICES_ENV}" ]]; then
    if grep -q "^AZURACAST_API_KEY=" "${SERVICES_ENV}"; then
        sed -i "s|^AZURACAST_API_KEY=.*|AZURACAST_API_KEY=${PERSISTENT_KEY}|" "${SERVICES_ENV}"
    else
        echo "AZURACAST_API_KEY=${PERSISTENT_KEY}" >> "${SERVICES_ENV}"
    fi
    echo "  ✓ AZURACAST_API_KEY écrite dans services.env"
else
    echo "  ℹ services.env en lecture seule — clé API : ${PERSISTENT_KEY}"
    echo "  ℹ Copier dans services.env : AZURACAST_API_KEY=${PERSISTENT_KEY}"
fi

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
