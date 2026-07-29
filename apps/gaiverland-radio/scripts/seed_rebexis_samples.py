"""
Seed Rebexis — charge une réserve de voix "en boîte" (samples pré-enregistrés)
pour que Rebexis ne soit JAMAIS muette, même quand le quota ElevenLabs est épuisé.

Ce que fait le script, pour chaque sample de samples/rebexis/ :
  1. copie le fichier dans TTS_CACHE (/tts-cache) → lisible par le worker TTS
  2. l'upload dans AzuraCast + pose les cue points
  3. l'enregistre dans tts_library (category='sample', el_chars=0 → coût quota nul)
  4. l'assigne à la playlist "Rebexis" (rotation immédiate à l'antenne)

À lancer DANS le conteneur gaiverland-tts (il a /tts-cache, DATABASE_URL et les
identifiants AzuraCast) :
    docker cp apps/gaiverland-radio/samples/. gaiverland-tts:/app/samples/
    docker exec gaiverland-tts python /app/seed_rebexis_samples.py

Idempotent : relançable sans créer de doublons (upsert sur tts_library, assign
additif côté AzuraCast).
"""
import os, sys, glob, shutil, pathlib, subprocess, json

sys.path.insert(0, "/app")
try:
    import psycopg2, psycopg2.extras, httpx
except ImportError:
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "psycopg2-binary", "httpx"], check=True)
    import psycopg2, psycopg2.extras, httpx

from az_utils import upload_file, get_or_create_playlist, batch_assign_playlist

DB_URL     = os.environ["DATABASE_URL"]
TTS_CACHE  = pathlib.Path(os.environ.get("TTS_CACHE_DIR", "/tts-cache"))
SAMPLES_DIR = pathlib.Path(os.environ.get("REBEXIS_SAMPLES_DIR", "/app/samples/rebexis"))
AZ_URL     = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY     = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))
REBEXIS_SONGS_INTERVAL = int(os.environ.get("REBEXIS_SONGS_INTERVAL", "3"))


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def get_audio_duration(path) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            capture_output=True, text=True, timeout=10)
        return float(json.loads(r.stdout).get("format", {}).get("duration", 0))
    except Exception:
        return 0


def update_az_cue(az_file_id: int, duration: float):
    if not az_file_id or not AZ_KEY or duration <= 0:
        return
    try:
        httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/file/{az_file_id}",
                  headers={"X-API-Key": AZ_KEY, "Content-Type": "application/json"},
                  json={"extra_metadata": {"cue_in": 0.0, "cue_out": round(duration, 3),
                                           "fade_in": 0.0, "fade_out": 0.0,
                                           "cross_start_next": None}}, timeout=10)
    except Exception as e:
        print(f"  ⚠ cue: {e}")


def ensure_tts_library(conn):
    """La table peut ne pas exister si le worker n'a jamais démarré."""
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tts_library (
                id SERIAL PRIMARY KEY,
                text_hash VARCHAR(64) UNIQUE NOT NULL,
                text TEXT NOT NULL,
                category VARCHAR(50) NOT NULL DEFAULT 'custom',
                audio_file TEXT, az_file_id INTEGER,
                el_chars INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
    conn.commit()


def get_rebexis_playlist_id(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT az_rb_playlist FROM radio_state WHERE id=1")
        row = cur.fetchone()
    return (row["az_rb_playlist"] or 0) if row else 0


def library_upsert(conn, name: str, audio_file: str, az_file_id, category="sample"):
    import hashlib
    text = f"[sample:{name}]"
    h = hashlib.sha256(text.encode()).hexdigest()[:32]
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tts_library (text_hash, text, category, audio_file, az_file_id, el_chars)
            VALUES (%s,%s,%s,%s,%s,0)
            ON CONFLICT (text_hash) DO UPDATE
               SET audio_file=EXCLUDED.audio_file, az_file_id=EXCLUDED.az_file_id
        """, (h, text, category, audio_file, az_file_id))
    conn.commit()


def main():
    files = sorted(glob.glob(str(SAMPLES_DIR / "*.mp3")))
    if not files:
        print(f"✗ Aucun sample dans {SAMPLES_DIR}")
        sys.exit(1)
    print(f"→ {len(files)} samples à charger depuis {SAMPLES_DIR}")

    TTS_CACHE.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    ensure_tts_library(conn)

    rb_pl_id = get_rebexis_playlist_id(conn)
    if not rb_pl_id:
        rb_pl_id = get_or_create_playlist("Rebexis", pl_type="once_per_x_songs",
                                          weight=1, play_per_songs=REBEXIS_SONGS_INTERVAL)
        print(f"  ℹ Playlist Rebexis (créée) : id={rb_pl_id}")

    az_ids, done = [], 0
    for src in files:
        name = pathlib.Path(src).stem
        dest = TTS_CACHE / f"sample_{name}.mp3"
        if str(pathlib.Path(src).resolve()) != str(dest.resolve()):
            shutil.copy(src, dest)

        az = upload_file(str(dest), f"Rebexis — {name}")
        az_id = az.get("id") if az else None
        if az_id:
            update_az_cue(az_id, get_audio_duration(dest))
            az_ids.append(az_id)
        library_upsert(conn, name, str(dest), az_id)
        print(f"  ✓ {name}  (az_id={az_id})")
        done += 1

    if rb_pl_id and az_ids:
        ok = batch_assign_playlist(az_ids, [rb_pl_id])
        print(f"  {'✓' if ok else '⚠'} {len(az_ids)} samples assignés → playlist Rebexis (id={rb_pl_id})")

    conn.close()
    print(f"✅ Seed terminé : {done} samples en réserve. Rebexis ne sera plus muette.")


if __name__ == "__main__":
    main()
