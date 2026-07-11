"""
TTS Worker — ElevenLabs API + bibliothèque de phrases cachées
Stratégie :
  - Chaque phrase générée est stockée en DB (tts_library) → jamais régénérée
  - Quota mensuel : 10 000 chars/mois (paramétrable EL_CHARS_LIMIT)
  - [playful] tag auto sur toutes les phrases pour expressivité radio
  - Pré-génération des phrases statiques Cat3 au démarrage
  - Dégradation gracieuse si quota épuisé : pioche dans la même catégorie en cache
"""
import os, sys, subprocess, hashlib, pathlib, threading, queue, time, re, json

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

# Nombre de jingles contextuels ('custom') conservés dans la playlist Rebexis.
# Au-delà, les plus anciens sont retirés de la playlist (pas supprimés de la
# bibliothèque) pour éviter l'accumulation : répétition de vieux jingles et pool
# de rotation surchargé. Les phrases statiques (cat3_bloc, templates) restent.
REBEXIS_PLAYLIST_KEEP = int(os.environ.get("REBEXIS_PLAYLIST_KEEP", "20"))

# Paramètres voix ElevenLabs — validés : jeune, motivée, ton radio
# TAMED 11/07 (chef : accent/prononciation/débit inégaux) — stability HAUTE + style BAS
# = prononciation FR fiable + débit stable (l'ancien 0.35/0.55 partait encore en vrille).
# Modèle réglable via env ELEVENLABS_MODEL : eleven_multilingual_v2 = meilleur accent FR ;
# eleven_v3 = plus expressif mais instable. Vitesse via ELEVENLABS_SPEED (<1 = plus lent).
EL_VOICE_SETTINGS = {
    "stability":         float(os.environ.get("ELEVENLABS_STABILITY", "0.50")),
    "similarity_boost":  0.80,
    "style":             float(os.environ.get("ELEVENLABS_STYLE", "0.35")),
    "use_speaker_boost": True,
    "speed":             float(os.environ.get("ELEVENLABS_SPEED", "0.92")),
}

# ── Phrases statiques pré-générées au démarrage (coût fixe une seule fois) ───
# Cat3 : blocs musicaux (aucune variable, priorité absolue)
STATIC_PHRASES = [
    # --- Blocs musicaux — charte Scott Taylor : collectif, spectacle, jamais "tu" ---
    ("cat3_bloc", "Et maintenant, un maximum d'électro."),
    ("cat3_bloc", "Retour à la musique. Pour tout le monde."),
    ("cat3_bloc", "On continue sans ralentir."),
    ("cat3_bloc", "On garde cette énergie. Tous ensemble."),
    ("cat3_bloc", "La nuit avance, la musique aussi."),
    ("cat3_bloc", "On enchaîne, et on ne ralentit pas."),
    ("cat3_bloc", "Gaiverland Radio — en mode plein gaz pour vous tous."),
    ("cat3_bloc", "Et voilà, on repart."),
    ("cat3_bloc", "Le set continue. Restez avec nous."),
    ("cat3_bloc", "Peu importe où vous êtes... la musique est là."),
    # --- Nouveauté sans artiste ---
    ("cat4_nouveaute", "Place à une nouveauté sur Gaiverland Radio."),
    ("cat4_nouveaute", "Un nouveau titre à partager avec vous tous ce soir."),
    ("cat4_nouveaute", "Découverte en cours. Pour tout le monde ici."),
]

# Templates avec variables — générés à la demande, cachés à vie
# {artist} ou {title} sont résolus dynamiquement
# Charte : jamais "tu", toujours collectif
TEMPLATES = {
    # Catégorie 1 — lancement artiste
    "cat1_artiste_1": "Et maintenant, {artist} sur Gaiverland Radio.",
    "cat1_artiste_2": "Place à {artist}.",
    "cat1_artiste_3": "On retrouve {artist}. Ce soir, pour vous tous.",
    "cat1_artiste_4": "{artist} prend le relais sur Gaiverland Radio.",
    # Catégorie 2 — lancement morceau
    "cat2_morceau_1": "Et maintenant, {title}.",
    "cat2_morceau_2": "Voici {title}. Pour tout le monde.",
    "cat2_morceau_3": "On s'écoute {title}.",
    # Catégorie 4 — nouveauté avec artiste
    "cat4_nouveaute_1": "Et maintenant une découverte signée {artist}.",
    "cat4_nouveaute_2": "Nouveauté sur Gaiverland Radio — {artist} est là.",
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
    "afade=t=in:st=0:d=0.05:curve=tri",
    "apad=pad_dur=3.5",
])
FFMPEG_CHAIN_HASH = __import__("hashlib").sha256(FFMPEG_RADIO.encode()).hexdigest()[:8]


AZ_URL     = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY     = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))


def get_audio_duration(path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=10
        )
        return float(json.loads(r.stdout).get("format", {}).get("duration", 0))
    except Exception:
        return 0


def update_az_cue(az_file_id: int, duration: float):
    if not az_file_id or not AZ_KEY: return
    try:
        r = httpx.put(
            f"{AZ_URL}/api/station/{AZ_STATION}/file/{az_file_id}",
            headers={"X-API-Key": AZ_KEY, "Content-Type": "application/json"},
            json={"extra_metadata": {"cue_in": 0.0, "cue_out": round(duration, 3), "fade_in": 0.0, "fade_out": 0.0, "cross_start_next": None}}, timeout=10)
        if r.status_code < 400: print(f"  cue in=0 out={duration:.1f}s")
        else: print(f"  warning cue HTTP {r.status_code}")
    except Exception as e: print(f"  warning cue: {e}")


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
    """Phrase aléatoire de secours quand le quota EL est épuisé.
    D'abord la même catégorie, puis la réserve de samples pré-enregistrés
    ('sample', chargée par seed_rebexis_samples.py) → Rebexis n'est jamais muette.
    """
    for cat in (category, "sample"):
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM tts_library
                WHERE category=%s AND audio_file IS NOT NULL
                ORDER BY RANDOM() LIMIT 1
            """, (cat,))
            row = cur.fetchone()
        if row:
            return row
    return None


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
    """Balise [playful] = audio tag interprété UNIQUEMENT par eleven_v3. Sur les autres
    modèles (multilingual_v2…) elle serait LUE à voix haute → on la retire."""
    is_v3 = "v3" in EL_MODEL
    if text.startswith("["):
        return text if is_v3 else text.split("]", 1)[-1].strip()
    return f"[playful] {text}" if is_v3 else text


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
    h = hashlib.sha256(f"{label}|{FFMPEG_CHAIN_HASH}".encode()).hexdigest()[:16]
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
        # Verify the cached file uses the current FFMPEG chain
        expected_h = __import__("hashlib").sha256(f"{text}|{FFMPEG_CHAIN_HASH}".encode()).hexdigest()[:16]
        expected_path = TTS_CACHE / f"rebexis_{expected_h}.mp3"
        if pathlib.Path(cached["audio_file"]) == expected_path:
            print(f"  ✓ cache hit [{category}] → {pathlib.Path(cached['audio_file']).name}")
            conn.close()
            return pathlib.Path(cached["audio_file"])
        else:
            print(f"  ↻ chain mismatch, regenerating [{category}] : {text[:40]}")

    # 2. Quota check
    el_text = el_add_playful(text)
    needed  = len(el_text)
    remaining = quota_remaining(conn)

    if remaining < needed:
        print(f"  ⚠ quota insuffisant ({remaining}/{EL_CHARS_LIMIT} restant, besoin {needed})")
        fallback = library_random_fallback(conn, category)
        conn.close()
        if fallback:
            print(f"  ↩ fallback bibliothèque [{category}] → {fallback['id']}")
            return pathlib.Path(fallback["audio_file"])
        raise RuntimeError(
            f"Quota EL épuisé ({remaining} chars restants) et aucun fallback en cache pour [{category}]"
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
        if az_file_id:
            dur = get_audio_duration(str(mp3_path))
            if dur > 0: update_az_cue(az_file_id, dur)
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


def _prune_rebexis_playlist(conn, rb_pl_id: int):
    """Ne garde dans la playlist Rebexis que les REBEXIS_PLAYLIST_KEEP jingles
    contextuels ('custom') les plus récents. Les phrases statiques réutilisables
    (cat3_bloc, cat*_*) ne sont jamais élaguées. Empêche l'accumulation qui
    provoque répétitions et clustering de voix."""
    if not rb_pl_id:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT az_file_id FROM tts_library
                WHERE az_file_id IS NOT NULL AND category = 'custom'
                ORDER BY created_at DESC
                OFFSET %s
            """, (REBEXIS_PLAYLIST_KEEP,))
            stale = [r["az_file_id"] for r in cur.fetchall()]
        if not stale:
            return
        from az_utils import remove_files_from_playlist
        n = remove_files_from_playlist(stale[:20], rb_pl_id)  # cap 20/synthèse
        if n:
            print(f"  🧹 playlist Rebexis élaguée — {n} ancien(s) jingle(s) contextuel(s) retiré(s)")
    except Exception as e:
        print(f"  ⚠ prune Rebexis: {e}")


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
                ok = batch_assign_playlist([az_file_id], [rb_pl_id])
                if ok:
                    print(f"  ✓ az_id={az_file_id} assigné → playlist Rebexis (id={rb_pl_id})")
                    _prune_rebexis_playlist(conn, rb_pl_id)
                else:
                    print(f"  ⚠ assignation playlist ÉCHOUÉE pour az_id={az_file_id} (rb_pl_id={rb_pl_id})")
            elif not rb_pl_id:
                print(f"  ⚠ rb_pl_id manquant — az_id={az_file_id} non assigné à Rebexis")

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
@app.get("/pending")
def pending_sessions(limit: int = 5):
    """Retourne les sessions Rebexis en attente de synthèse TTS (audio_file IS NULL)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, intervention, mood_trigger, context_track
                FROM rebexis_sessions
                WHERE audio_file IS NULL
                ORDER BY generated_at ASC
                LIMIT %s
            """, (limit,))
            rows = cur.fetchall()
        conn.close()
        return {"sessions": [dict(r) for r in rows]}
    except Exception as e:
        print(f"⚠ /pending: {e}")
        return {"sessions": [], "error": str(e)}


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

