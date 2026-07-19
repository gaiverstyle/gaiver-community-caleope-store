#!/usr/bin/env python3
"""
Pochettes Gaiverland — récupère la cover officielle (API iTunes Search) pour les titres
qui n'en ont pas et l'EMBARQUE dans le mp3 (ffmpeg). AzuraCast relit l'art embarqué → la
pochette s'affiche sur le player. Filtre de correspondance strict : on ne pose une cover
que si le titre/artiste iTunes recoupe le nôtre (mieux vaut PAS de cover qu'une FAUSSE).

Usage (conteneur downloader — a ffmpeg + accès média en écriture) :
  python3 cover_art.py [max]     # max = nb de covers à poser ce run (défaut 150)
"""
import os, sys, re, json, time, subprocess, tempfile, urllib.request, urllib.parse
import psycopg2

DB = os.environ["DATABASE_URL"]
DAY = ("festival", "energique", "melodique")
THROTTLE = float(os.environ.get("COVER_THROTTLE_S", "0.5"))   # politesse envers l'API iTunes
NOISE = re.compile(r"\b(official|music|video|lyric|audio|visualizer|hd|extended|radio|edit|mix|remix|feat|ft|remaster(ed)?)\b", re.I)


def clean_query(artist, title):
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title or "")
    # Titres à préfixe uploader (« Uploader - Vrai Artiste - Chanson ») : le vrai couple
    # artiste+chanson = les 2 DERNIERS segments « - ». Sinon on prend l'artiste + le titre.
    segs = [s.strip() for s in re.split(r"\s+[-–]\s+", t) if s.strip()]
    core = (segs[-2] + " " + segs[-1]) if len(segs) >= 2 else ((artist or "") + " " + t)
    q = NOISE.sub(" ", core)
    return re.sub(r"\s+", " ", q).strip()


def words(s):
    return set(w for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if len(w) > 1)


def itunes(q):
    u = "https://itunes.apple.com/search?" + urllib.parse.urlencode(
        {"term": q, "media": "music", "entity": "song", "limit": 3})
    return json.load(urllib.request.urlopen(u, timeout=12)).get("results", [])


def deezer(q):
    """Second recours (base Deezer, gratuite, sans clé). Normalisé au format d'itunes()."""
    u = "https://api.deezer.com/search?" + urllib.parse.urlencode({"q": q, "limit": 3})
    d = json.load(urllib.request.urlopen(u, timeout=12))
    out = []
    for r in d.get("data", []):
        alb = r.get("album") or {}
        out.append({"trackName": r.get("title", ""),
                    "artistName": (r.get("artist") or {}).get("name", ""),
                    "_art": alb.get("cover_xl") or alb.get("cover_big")})
    return out


def pick(artist, title, results):
    """Retient un résultat seulement si le TITRE recoupe ET (l'artiste recoupe le nôtre)."""
    tw = words(re.sub(r"\([^)]*\)", "", title or ""))
    aw = words(artist)
    for r in results:
        itw, iaw = words(r.get("trackName", "")), words(r.get("artistName", ""))
        if (tw & itw) and (aw & iaw or tw & iaw):
            return r
    return None


def has_art(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                              "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=20)
        return "video" in out.stdout
    except Exception:
        return True   # dans le doute, on saute (ne casse rien)


def artwork_url(r):
    if r.get("_art"):                       # Deezer : URL directe haute résolution
        return r["_art"]
    u = r.get("artworkUrl100") or r.get("artworkUrl60") or ""
    return u.replace("100x100", "600x600").replace("60x60", "600x600") or None


def embed(path, img):
    d = os.path.dirname(path)
    cover = tempfile.mktemp(suffix=".jpg", dir=d)
    open(cover, "wb").write(img)
    out = path + ".art.mp3"
    cmd = ["ffmpeg", "-y", "-i", path, "-i", cover, "-map", "0:a", "-map", "1:0",
           "-c:a", "copy", "-c:v", "mjpeg", "-id3v2_version", "3",
           "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)",
           "-disposition:v:0", "attached_pic", out]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90)
        ok = r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > os.path.getsize(path) * 0.5
        if ok:
            os.replace(out, path)
        elif os.path.exists(out):
            os.remove(out)
        return ok
    finally:
        if os.path.exists(cover):
            os.remove(cover)


def _set_cover(cur, conn, path, val):
    """Maintient tracks.has_cover (signal de reconnaissance utilisé par le tri playlist)."""
    try:
        cur.execute("UPDATE tracks SET has_cover=%s WHERE file_path=%s", (val, path))
        conn.commit()
    except Exception:
        conn.rollback()


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    conn = psycopg2.connect(DB)
    cur = conn.cursor()
    cur.execute("SELECT file_path, artist, title FROM tracks WHERE analyzed AND mood = ANY(%s) "
                "AND file_path IS NOT NULL ORDER BY random()", (list(DAY),))
    rows = cur.fetchall()
    done = miss = skip = 0
    for path, artist, title in rows:
        if done >= limit:
            break
        if not os.path.isfile(path):
            skip += 1
            continue
        if has_art(path):
            _set_cover(cur, conn, path, True)      # déjà une pochette
            skip += 1
            continue
        q = clean_query(artist, title)
        if len(q) < 4:
            skip += 1
            continue
        try:
            time.sleep(THROTTLE)
            r = pick(artist, title, itunes(q))
            src = "it"
            if not r:                       # iTunes rate → on tente Deezer
                time.sleep(THROTTLE)
                try:
                    r = pick(artist, title, deezer(q))
                    src = "dz"
                except Exception:
                    r = None
            if not r:
                miss += 1
                _set_cover(cur, conn, path, False)   # introuvable → obscur → relégué
                print("MISS", title[:42], flush=True)
                continue
            img = urllib.request.urlopen(artwork_url(r), timeout=15).read()
            if embed(path, img):
                done += 1
                _set_cover(cur, conn, path, True)
                print(f"OK[{src}]", title[:32], "<-", r.get("artistName", "")[:16], "-", r.get("trackName", "")[:20], flush=True)
            else:
                miss += 1
                print("FAIL", title[:38], flush=True)
        except Exception as e:
            miss += 1
            print("ERR ", title[:28], str(e)[:40], flush=True)
    print(f"=== COVERS: {done} posées / {miss} sans-match ou échec / {skip} sautés ===", flush=True)


if __name__ == "__main__":
    main()
