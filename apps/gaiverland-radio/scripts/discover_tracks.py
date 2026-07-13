"""
Découverte auto de musique Gaiverland (Yann) — nocturne 00h.

Pour chaque artiste de watchlist.json (par scène), cherche ses NOUVEAUX titres
(SoundCloud primaire, YouTube fallback), applique une QC stricte, télécharge dans
le bac de la scène → l'analyzer + multi_rotation les captent.

QC (exigence chef) :
  - DURÉE ≈ ORIGINAL : durée officielle via iTunes/Deezer → rejette extraits (trop
    court), mixes/sets (trop long), sped-up/slowed. Tolérance ±25 % (ou ±30 s).
  - PAS D'IA/STOCK : STOCK_RE (repris du proposal_validator de Régis).
  - STYLE RESPECTÉ : genre officiel ∈ genres électro admis + routage par scène.
Apprentissage :
  - BLACKLIST (compte chef + titres très SKIP) → artistes/titres évités.
  - LIKES (ENCORE) → artistes boostés (plus de budget de découverte).

Sûr par défaut : budget/scène/nuit borné, dédup par titre, ce que YouTube bloque
(PO-token/cookie) part dans la file `mac_download_queue` (traité par l'agent Mac).
"""
import os, re, json, time, subprocess, unicodedata
import psycopg2, psycopg2.extras
try:
    import httpx
except ImportError:
    httpx = None

DB_URL   = os.environ["DATABASE_URL"]
WATCHLIST = os.environ.get("WATCHLIST", "/app/watchlist.json")
STATIONS_ROOT = os.environ.get("STATIONS_ROOT", "/var/azuracast/stations")
# ⚠ La rotation (multi_rotation.select_tracks) sélectionne par `file_path LIKE %/media/music/<theme>/%`
# (filtre theme) OU genre_top1 (chill/hard). Les titres de scène qui JOUENT vivent tous dans
# `gaiverlandradio/media/music/<theme>/` (storage partagé loc 3, scanné par l'analyzer WATCH_DIR
# = /var/azuracast/stations). → on télécharge LÀ (même dossier que les bacs existants qui tournent),
# PAS dans gaiverland_<scene>/media/ (qui ne matche pas le filtre → orphelins). Mainstage = music/discover.
_MEDIA = f"{STATIONS_ROOT}/gaiverlandradio/media/music"
SCENE_DIR = {
    "main":      f"{_MEDIA}/discover",
    "hard":      f"{_MEDIA}/hard",
    "phonk":     f"{_MEDIA}/phonk",
    "lofi":      f"{_MEDIA}/lofi",
    "synthwave": f"{_MEDIA}/synthwave",
    "chill":     f"{_MEDIA}/chill",
}
PER_ARTIST      = int(os.environ.get("DISCOVER_PER_ARTIST", "2"))    # nouveaux titres max / artiste / passe
PER_SCENE       = int(os.environ.get("DISCOVER_PER_SCENE", "8"))     # plafond / scène / passe
CANDIDATES      = int(os.environ.get("DISCOVER_CANDIDATES", "6"))    # combien de résultats SC on inspecte / artiste
DUR_SHORT_FLOOR = float(os.environ.get("DISCOVER_DUR_SHORT_FLOOR", "0.6"))  # < 60 % de l'original = extrait → rejet
JS_RUNTIME      = os.environ.get("DISCOVER_JS_RUNTIME", "")          # ex "node" pour YT
COOKIES         = os.environ.get("YT_COOKIES", "/cookies/youtube-cookies.txt")

# Empreinte IA / stock / library (repris de proposal_validator.py — garde NCS/free-download)
STOCK_RE = re.compile(
    r"(no copyright|royalty[- ]free|background music|stock music|library music|"
    r"progressive house anthem|corporate|ableton template|flp\b|template|type beat|"
    r"\bai\b|suno|udio|generated|sped up|slowed( \+? ?reverb)?|nightcore|8d audio|"
    r"\d+ ?hours?|\bmix\b|full set|live set|dj set|mashup|megamix|compilation)",
    re.I)
# ⚠ iTunes/Deezer mis-taguent l'underground (phonk→« Pop », lofi→« Hip-Hop »). L'ARTISTE curé
# par scène EST la garantie de style (leçon Régis : le dossier/curation prime sur le tag genre).
# → on ne rejette QUE les genres clairement hors-sujet pour une radio dance/électro.
REJECT_GENRES = {"classical","country","spoken word","comedy","audiobook","children's music",
                 "holiday","christian & gospel","opera","podcast","musical","fitness & workout"}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def ensure_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS discovery_seen (
        url TEXT PRIMARY KEY, artist TEXT, scene TEXT, title TEXT,
        verdict TEXT, reason TEXT, seen_at TIMESTAMPTZ DEFAULT NOW())""")
    cur.execute("""CREATE TABLE IF NOT EXISTS mac_download_queue (
        id SERIAL PRIMARY KEY, query TEXT, scene TEXT, artist TEXT,
        status TEXT DEFAULT 'pending', created_at TIMESTAMPTZ DEFAULT NOW(),
        done_at TIMESTAMPTZ)""")


def _downloaded_today(cur) -> int:
    cur.execute("SELECT count(*) AS n FROM discovery_seen WHERE verdict='ok' AND seen_at::date=CURRENT_DATE")
    return cur.fetchone()["n"]


def blacklisted_norms(cur) -> set:
    """Titres/artistes à éviter : blacklist compte chef (si table) + titres très SKIP."""
    out = set()
    for q in ("SELECT title FROM track_blacklist",
              "SELECT DISTINCT t.artist FROM track_scores ts JOIN tracks t ON t.song_id=ts.song_id WHERE ts.score <= -0.6"):
        try:
            cur.execute(q)
            for r in cur.fetchall():
                v = list(r.values())[0]
                if v: out.add(_norm(v))
        except Exception:
            cur.connection.rollback()
    return out


def encore_artist_norms(cur) -> set:
    """Artistes dont un titre cartonne aux ENCORE (score>0.3) → priorité découverte."""
    try:
        cur.execute("""SELECT DISTINCT t.artist FROM track_scores ts JOIN tracks t ON t.song_id=ts.song_id
                       WHERE ts.score > 0.3 AND t.artist IS NOT NULL""")
        return {_norm(r["artist"]) for r in cur.fetchall()}
    except Exception:
        cur.connection.rollback(); return set()


def official_meta(title: str, artist_hint: str = ""):
    """Retourne (genre, artist, canon_title, duration_s) via iTunes puis Deezer. None si rien."""
    if not httpx:
        return None
    term = f"{artist_hint} {title}".strip()
    try:
        r = httpx.get("https://itunes.apple.com/search",
                      params={"term": term, "media": "music", "limit": 1}, timeout=8)
        res = r.json().get("results", [])
        if res:
            x = res[0]
            dur = (x.get("trackTimeMillis") or 0) / 1000.0
            return (x.get("primaryGenreName"), x.get("artistName"), x.get("trackName"), dur)
    except Exception:
        pass
    try:
        r = httpx.get("https://api.deezer.com/search", params={"q": term, "limit": 1}, timeout=8)
        data = r.json().get("data", [])
        if data:
            x = data[0]
            genre = None
            try:
                alb = httpx.get(f"https://api.deezer.com/album/{x['album']['id']}", timeout=8).json()
                gs = [g["name"] for g in alb.get("genres", {}).get("data", [])]
                genre = gs[0] if gs else None
            except Exception:
                pass
            return (genre, x.get("artist", {}).get("name"), x.get("title"), float(x.get("duration") or 0))
    except Exception:
        pass
    return None


def sc_candidates(artist: str, n: int) -> list:
    """Liste les résultats SoundCloud pour un artiste (titre, url, uploader, durée)."""
    cmd = ["yt-dlp", f"scsearch{n}:{artist}", "--flat-playlist", "--no-warnings", "-q",
           "--print", "%(title)s\t%(webpage_url)s\t%(uploader)s\t%(duration)s"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90).stdout
    except Exception:
        return []
    rows = []
    for line in out.splitlines():
        p = line.split("\t")
        if len(p) >= 4:
            try: dur = float(p[3]) if p[3] not in ("NA", "None", "") else 0
            except ValueError: dur = 0
            rows.append({"title": p[0], "url": p[1], "uploader": p[2], "duration": dur})
    return rows


def dur_ok(got: float, original: float) -> bool:
    """Anti-EXTRAIT : le but (consigne du chef) est d'éviter les clips/previews de 60-90 s,
    pas d'imposer une version. Deux versions complètes du même titre (radio 3:12 vs extended
    5:04) sont TOUTES DEUX valides — l'API officielle liste souvent l'extended alors que la
    source a la radio edit, donc on ne rejette PAS un écart 'plus long/plus court raisonnable'.
    On rejette seulement : nettement plus court que l'original (= extrait), ou démesurément
    long (= compil/loop 1 h avalée par erreur)."""
    if got <= 0:
        return False
    if original <= 0:
        return 60 <= got <= 600            # pas de réf officielle → durée de morceau plausible
    if got < original * DUR_SHORT_FLOOR:   # nettement plus court = extrait/preview
        return False
    if got > original * 3 and got > 900:   # démesurément long = compil/loop, pas un titre
        return False
    return True


def download(url: str, outdir: str, use_yt_runtime: bool = False) -> str:
    """Télécharge en mp3. Retourne le chemin, ou '' si échec, ou 'COOKIE' si YT bloque."""
    os.makedirs(outdir, exist_ok=True)
    cmd = ["yt-dlp", "-f", "bestaudio", "--extract-audio", "--audio-format", "mp3",
           "--audio-quality", "5", "--no-playlist", "--no-warnings", "-q",
           "-o", os.path.join(outdir, "%(uploader)s - %(title)s.%(ext)s"), url]
    if "youtube" in url and JS_RUNTIME:
        cmd[1:1] = ["--js-runtimes", JS_RUNTIME]
        if os.path.exists(COOKIES):
            cmd[1:1] = ["--cookies", COOKIES]
    before = set(os.listdir(outdir)) if os.path.isdir(outdir) else set()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
    if r.returncode != 0:
        err = (r.stderr or "").lower()
        if "sign in" in err or "cookie" in err or "po token" in err or "403" in err:
            return "COOKIE"
        return ""
    new = [f for f in os.listdir(outdir) if f.endswith(".mp3") and f not in before]
    return os.path.join(outdir, new[0]) if new else ""


def ffdur(path: str) -> float:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", path], capture_output=True, text=True, timeout=20).stdout
        return float(out.strip())
    except Exception:
        return 0.0


def already_have(title: str, artist: str, outdir: str, cur) -> bool:
    key = _norm(f"{artist} {title}")
    # déjà vu en découverte
    cur.execute("SELECT 1 FROM discovery_seen WHERE url IS NOT NULL AND lower(title)=lower(%s) LIMIT 1", (title,))
    if cur.fetchone():
        return True
    # déjà dans la biblio (tracks)
    try:
        cur.execute("SELECT 1 FROM tracks WHERE lower(regexp_replace(artist||' '||title,'[^a-zA-Z0-9]+',' ','g')) LIKE %s LIMIT 1",
                    (f"%{key}%",))
        if cur.fetchone():
            return True
    except Exception:
        cur.connection.rollback()
    return False


def discover_artist(scene: str, artist: str, budget: int, blk: set, cur, conn) -> int:
    got = 0
    outdir = SCENE_DIR.get(scene)
    if not outdir:
        return 0
    if _norm(artist) in blk:
        return 0
    for c in sc_candidates(artist, CANDIDATES):
        if got >= budget:
            break
        url, title, up, cdur = c["url"], c["title"], c["uploader"], c["duration"]
        cur.execute("SELECT 1 FROM discovery_seen WHERE url=%s", (url,))
        if cur.fetchone():
            continue
        def mark(v, r):
            cur.execute("""INSERT INTO discovery_seen (url,artist,scene,title,verdict,reason)
                           VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (url) DO NOTHING""",
                        (url, artist, scene, title, v, r)); conn.commit()
        # l'uploader ou le titre doit contenir l'artiste (évite les résultats hors-sujet)
        na, nt, nu = _norm(artist), _norm(title), _norm(up)
        if na not in nt and na not in nu:
            mark("reject", "artiste absent"); continue
        if STOCK_RE.search(f"{title} {up}"):
            mark("reject", "IA/stock/extrait/mix"); continue
        if cdur and (cdur < 60 or cdur > 900):
            mark("reject", f"durée SC absurde {int(cdur)}s"); continue
        if _norm(title) in blk or _norm(f"{artist} {title}") in blk:
            mark("reject", "blacklist"); continue
        if already_have(title, artist, outdir, cur):
            mark("skip", "déjà en biblio"); continue
        # QC officielle (genre + durée de l'original)
        meta = official_meta(title, artist)
        if meta:
            genre, oart, ocanon, odur = meta
            if genre and genre.lower() in REJECT_GENRES:
                mark("reject", f"style clairement hors-scène ({genre})"); continue
        else:
            odur = 0
        # download
        path = download(url, outdir)
        if path == "COOKIE":
            cur.execute("INSERT INTO mac_download_queue (query,scene,artist) VALUES (%s,%s,%s)",
                        (f"{artist} {title}", scene, artist)); conn.commit()
            mark("mac_queue", "YouTube bloqué → file Mac"); continue
        if not path:
            mark("fail", "download KO"); continue
        # QC durée réelle vs original
        gd = ffdur(path)
        if not dur_ok(gd, odur):
            try: os.remove(path)
            except OSError: pass
            mark("reject", f"durée {int(gd)}s vs original {int(odur)}s (extrait/mix)"); continue
        mark("ok", f"OK {int(gd)}s"); got += 1
        print(f"  ✅ [{scene}] {artist} — {title} ({int(gd)}s)", flush=True)
    return got


def main():
    conn = get_conn(); cur = conn.cursor()
    ensure_schema(cur); conn.commit()
    try:
        wl = json.load(open(WATCHLIST))
    except Exception as e:
        print(f"  ⚠ watchlist illisible: {e}", flush=True); return
    blk = blacklisted_norms(cur)
    encore = encore_artist_norms(cur)
    total = 0
    for scene, sc in wl.get("scenes", {}).items():
        if scene not in SCENE_DIR:
            continue
        artists = sc.get("artists", [])
        # priorité aux artistes qui cartonnent (ENCORE) : on les met en tête
        artists = sorted(artists, key=lambda a: 0 if _norm(a) in encore else 1)
        scene_got = 0
        for a in artists:
            if scene_got >= PER_SCENE:
                break
            n = discover_artist(scene, a, min(PER_ARTIST, PER_SCENE - scene_got), blk, cur, conn)
            scene_got += n; total += n
            time.sleep(1)
        print(f"[{scene}] {scene_got} nouveaux titres", flush=True)
    print(f"\n=== DÉCOUVERTE : {total} nouveaux titres, {_downloaded_today(cur)} aujourd'hui ===", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
