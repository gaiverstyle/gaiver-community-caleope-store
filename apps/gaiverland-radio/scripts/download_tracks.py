"""
Auto-downloader Gaiverland (Yann).

Boucle qui télécharge automatiquement les titres **ACCEPTÉS** par le validateur de
propositions (`proposal_decisions`, verdict='accept', pas encore téléchargés) via
`yt-dlp` AVEC COOKIES (compte système dédié, cf `procedure-cookies-downloader.md`),
en MP3 dans le dossier média AzuraCast → l'analyzer les capte → rotation.

Garde-fous (sûr par défaut) :
  - **Sans cookies valides → en pause** (aucun download). Le service peut donc tourner
    en permanence sans rien faire tant que le chef n'a pas posé le cookies.txt.
  - **Limite quotidienne** (défaut 20) pour ne pas marteler YouTube ni remplir le disque.
  - **Uniquement des titres déjà validés** (électro/dance) par proposal_validator → jamais
    de contenu arbitraire.
  - Marque chaque titre traité (ok/failed) → pas de boucle infinie sur un échec.
  - Détecte l'expiration des cookies (« sign in ») et le loggue clairement pour le chef.
"""
import os
import time
import subprocess
import psycopg2
import psycopg2.extras

DB_URL       = os.environ["DATABASE_URL"]
COOKIES      = os.environ.get("YT_COOKIES", "/cookies/youtube-cookies.txt")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR",
                              "/var/azuracast/stations/gaiverlandradio/media/music/community")
INTERVAL_S   = int(os.environ.get("DOWNLOAD_INTERVAL_S", "600"))    # 10 min entre passes
DAILY_LIMIT  = int(os.environ.get("DOWNLOAD_DAILY_LIMIT", "20"))
BATCH        = int(os.environ.get("DOWNLOAD_BATCH", "3"))           # par passe


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _ensure_schema(cur):
    cur.execute("ALTER TABLE proposal_decisions ADD COLUMN IF NOT EXISTS downloaded_at TIMESTAMP")
    cur.execute("ALTER TABLE proposal_decisions ADD COLUMN IF NOT EXISTS download_status TEXT")


def _downloaded_today(cur) -> int:
    cur.execute("SELECT count(*) AS n FROM proposal_decisions WHERE downloaded_at::date = CURRENT_DATE")
    return cur.fetchone()["n"]


def _cookies_ok() -> bool:
    try:
        return os.path.exists(COOKIES) and os.path.getsize(COOKIES) > 0
    except Exception:
        return False


def download_one(query: str) -> bool:
    """Cherche + télécharge le meilleur audio en MP3 dans DOWNLOAD_DIR. True si OK."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    out = os.path.join(DOWNLOAD_DIR, "%(artist,uploader)s - %(title)s.%(ext)s")
    cmd = ["yt-dlp", "--cookies", COOKIES, "-f", "bestaudio",
           "-x", "--audio-format", "mp3", "--audio-quality", "0",
           "--no-playlist", "--embed-metadata", "--no-progress",
           "-o", out, f"ytsearch1:{query} audio"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        blob = (r.stdout or "") + (r.stderr or "")
        if r.returncode == 0 or "has already been downloaded" in blob:
            return True
        tail = blob[-250:]
        if "sign in" in tail.lower() or "cookies" in tail.lower() or "not a bot" in tail.lower():
            print("  🔴 COOKIES YouTube invalides/expirés — refaire la procédure "
                  "(procedure-cookies-downloader.md)", flush=True)
        print(f"  ⚠ échec '{query}': {tail}", flush=True)
        return False
    except Exception as e:
        print(f"  ⚠ download '{query}': {e}", flush=True)
        return False


def loop():
    print("⬇  Auto-downloader Gaiverland démarré "
          f"(cookies={COOKIES}, limite/j={DAILY_LIMIT}, dir={DOWNLOAD_DIR})", flush=True)
    idle_logged = False
    while True:
        try:
            conn = get_conn()
            conn.autocommit = True
            with conn.cursor() as cur:
                _ensure_schema(cur)
                if not _cookies_ok():
                    if not idle_logged:
                        print("  ⏸ pas de cookies YouTube — downloader en pause "
                              "(cf procedure-cookies-downloader.md)", flush=True)
                        idle_logged = True
                elif _downloaded_today(cur) >= DAILY_LIMIT:
                    print(f"  ⏸ limite quotidienne atteinte ({DAILY_LIMIT})", flush=True)
                else:
                    idle_logged = False
                    cur.execute("""SELECT title, artist, canon_title FROM proposal_decisions
                                   WHERE verdict='accept' AND downloaded_at IS NULL
                                   ORDER BY votes DESC NULLS LAST LIMIT %s""", (BATCH,))
                    for p in cur.fetchall():
                        q = f"{p['artist']} {p['canon_title'] or p['title']}".strip() or p["title"]
                        ok = download_one(q)
                        cur.execute("""UPDATE proposal_decisions
                                       SET downloaded_at=NOW(), download_status=%s WHERE title=%s""",
                                    ("ok" if ok else "failed", p["title"]))
                        print(f"  {'✓' if ok else '✗'} {q}", flush=True)
                        if _downloaded_today(cur) >= DAILY_LIMIT:
                            break
            conn.close()
        except Exception as e:
            print(f"  ⚠ downloader loop: {e}", flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    loop()
