"""
YouTube Live Streamer — Gaiverland Radio
Diffuse le stream Icecast d'AzuraCast vers YouTube Live.

Visuel :
  - Fond    : photos presse officielles (Tomorrowland ou autre portail Prezly)
              → rotation à chaque changement de titre
  - Centre  : cover art du titre en cours (avec ombre)
  - Texte   : titre + artiste + badge station
  - CPU     : 1 frame/sec côté Python (statique entre changements),
              libx264 ultrafast+stillimage → B-frames quasi-nulles

Variables d'environnement :
  YOUTUBE_RTMP_URL         rtmp://a.rtmp.youtube.com/live2/<clé>  (requis)
  ICECAST_STREAM_URL       URL du stream Icecast (défaut: interne Docker)
  AZURACAST_URL            URL AzuraCast (défaut: http://azuracast:80)
  AZURACAST_STATION_ID     ID station (défaut: 1)
  BACKGROUND_SOURCE_URLS   URLs portails presse séparées par virgule
                           (défaut: Tomorrowland Winter + Tomorrowland)
  BG_CACHE_DIR             Dossier cache photos (défaut: /bg-cache)
"""
import os, sys, subprocess, time, threading, io, hashlib, random, re
from pathlib import Path

# ── Bootstrap dépendances ─────────────────────────────────────────────────────
def install_deps():
    subprocess.run(["apt-get", "update", "-qq"], check=True)
    subprocess.run(["apt-get", "install", "-y", "-qq",
                    "ffmpeg", "fonts-dejavu-core"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "httpx", "Pillow"], check=True)

try:
    import httpx
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
except ImportError:
    print("→ Installation dépendances streamer (ffmpeg + Pillow)...")
    install_deps()
    import httpx
    from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ── Config ────────────────────────────────────────────────────────────────────
AZ_URL      = os.environ.get("AZURACAST_URL",     "http://azuracast:80")
AZ_STATION  = int(os.environ.get("AZURACAST_STATION_ID", "1"))
ICECAST_URL = os.environ.get("ICECAST_STREAM_URL",
                              f"{AZ_URL}/listen/gaiverlandradio/radio.mp3")
YT_RTMP     = os.environ.get("YOUTUBE_RTMP_URL", "")

# Portails presse (pages Prezly publiques) — séparés par virgule
_DEFAULT_BG_URLS = (
    "https://tomorrowlandwinter.press.tomorrowland.com/media,"
    "https://press.tomorrowland.com/media"
)
BACKGROUND_SOURCE_URLS = [
    u.strip()
    for u in os.environ.get("BACKGROUND_SOURCE_URLS", _DEFAULT_BG_URLS).split(",")
    if u.strip()
]
BG_CACHE_DIR = Path(os.environ.get("BG_CACHE_DIR", "/bg-cache"))

W, H         = 1280, 720
INPUT_FPS    = 1        # fps envoyés à FFmpeg (image statique → ultra-faible CPU)
OUTPUT_FPS   = 30       # fps sortie YouTube
POLL_SEC     = 5        # intervalle vérif nowplaying

# ── État partagé ──────────────────────────────────────────────────────────────
_frame      : bytes | None = None
_frame_lock = threading.Lock()
_song_id    = None


# ── BackgroundManager — photos presse ─────────────────────────────────────────
class BackgroundManager:
    """
    Scrape des portails presse Prezly, télécharge et met en cache les photos.
    Fournit une photo aléatoire à chaque appel get_random().
    """
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/120.0.0.0 Safari/537.36")
    # Pattern UUID Prezly CDN
    UUID_RE = re.compile(
        r"cdn\.assets\.prezly\.com/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
        r"-[0-9a-f]{4}-[0-9a-f]{12})/"
    )

    def __init__(self, source_urls: list[str], cache_dir: Path):
        self.source_urls = source_urls
        self.cache_dir   = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.photos: list[Path] = []

    def _scrape_urls(self, page_url: str) -> list[str]:
        """Retourne les URLs 1920x de toutes les photos Prezly sur la page."""
        try:
            r = httpx.get(page_url, timeout=15, follow_redirects=True,
                          headers={"User-Agent": self.UA})
            if r.status_code != 200:
                print(f"  ⚠ BG scrape {page_url}: HTTP {r.status_code}")
                return []
            html = r.text
            uuids = set(self.UUID_RE.findall(html))
            urls  = []
            for uuid in uuids:
                # Récupérer le nom de fichier depuis le HTML pour l'URL
                m = re.search(
                    rf"cdn\.assets\.prezly\.com/{uuid}/-/[^\"]+?/([^/\"]+\.(?:jpg|jpeg|png))",
                    html
                )
                fname = m.group(1) if m else "photo.jpg"
                url = (f"https://cdn.assets.prezly.com/{uuid}"
                       f"/-/format/jpeg/-/stretch/off/-/resize/1920x"
                       f"/-/quality/smart/{fname}")
                urls.append((uuid[:8], url))
            return urls
        except Exception as e:
            print(f"  ⚠ BG scrape {page_url}: {e}")
            return []

    def load(self):
        """Télécharge (ou recharge depuis cache) toutes les photos de fond."""
        all_urls: list[tuple[str, str]] = []
        for src in self.source_urls:
            found = self._scrape_urls(src)
            all_urls.extend(found)
            print(f"  → BG {src}: {len(found)} photos")

        if not all_urls:
            print("  ⚠ Aucune photo de fond trouvée — fond noir utilisé")
            return

        downloaded = 0
        for uuid_short, url in all_urls:
            dest = self.cache_dir / f"bg_{uuid_short}.jpg"
            if dest.exists():
                self.photos.append(dest)
                continue
            try:
                r = httpx.get(url, timeout=30, follow_redirects=True,
                              headers={"User-Agent": self.UA})
                if r.status_code == 200:
                    dest.write_bytes(r.content)
                    self.photos.append(dest)
                    downloaded += 1
            except Exception as e:
                print(f"    ⚠ DL {uuid_short}: {e}")

        # Dédoublonner (mêmes photos présentes sur les deux portails)
        seen = set()
        unique = []
        for p in self.photos:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        self.photos = unique

        print(f"  ✓ BG: {len(self.photos)} photos ({downloaded} nouvelles)")

    def get_random(self) -> "Image.Image | None":
        if not self.photos:
            return None
        path = random.choice(self.photos)
        try:
            return Image.open(path).convert("RGB")
        except Exception:
            self.photos.remove(path)
            return None


_bg_manager: BackgroundManager | None = None


# ── AzuraCast ─────────────────────────────────────────────────────────────────
def get_nowplaying() -> dict:
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=5)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def fetch_cover(url: str) -> "Image.Image | None":
    if not url:
        return None
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True)
        if r.status_code == 200:
            return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as e:
        print(f"  ⚠ cover fetch: {e}")
    return None


# ── Génération de frame ───────────────────────────────────────────────────────
_fonts = None

def _load_fonts():
    global _fonts
    bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
    reg  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    try:
        _fonts = (
            ImageFont.truetype(bold, 40),   # titre
            ImageFont.truetype(reg,  28),   # artiste
            ImageFont.truetype(bold, 20),   # badge station
        )
    except Exception:
        f = ImageFont.load_default()
        _fonts = (f, f, f)


def _tw(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    try:
        return int(draw.textlength(text, font=font))
    except AttributeError:
        return font.getsize(text)[0]


def make_frame(bg_photo: "Image.Image | None",
               cover:    "Image.Image | None",
               title:    str,
               artist:   str) -> bytes:
    if _fonts is None:
        _load_fonts()
    font_title, font_artist, font_station = _fonts

    # ── Fond : photo presse plein-écran + légère obscurcissement ─────────────
    if bg_photo:
        # Recadrer en crop centré (ratio fill) puis resize à 1280x720
        img_w, img_h = bg_photo.size
        target_ratio = W / H
        img_ratio    = img_w / img_h
        if img_ratio > target_ratio:
            new_h = img_h
            new_w = int(img_h * target_ratio)
            left  = (img_w - new_w) // 2
            bg_crop = bg_photo.crop((left, 0, left + new_w, new_h))
        else:
            new_w = img_w
            new_h = int(img_w / target_ratio)
            top   = (img_h - new_h) // 2
            bg_crop = bg_photo.crop((0, top, new_w, top + new_h))
        bg = bg_crop.resize((W, H), Image.LANCZOS)
        # Assombrir légèrement pour lisibilité du texte
        dark = Image.new("RGB", (W, H), (0, 0, 0))
        bg   = Image.blend(bg, dark, 0.38)
    else:
        bg = Image.new("RGB", (W, H), (12, 10, 32))

    # ── Cover art centrée ─────────────────────────────────────────────────────
    cover_sz = min(H - 200, 380)
    cx = (W - cover_sz) // 2
    cy = (H - cover_sz) // 2 - 42

    if cover:
        c = cover.resize((cover_sz, cover_sz), Image.LANCZOS)
        # Ombre portée (rectangle décalé + flouté)
        shadow_img = Image.new("RGB", (W, H), (0, 0, 0))
        shadow_img.paste(Image.new("RGB", (cover_sz + 20, cover_sz + 20),
                                   (20, 20, 20)), (cx - 10 + 8, cy - 10 + 8))
        shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(8))
        # Fusionner ombre dans le fond
        bg = Image.blend(bg, shadow_img, 0.55)
        bg.paste(c, (cx, cy))

    draw = ImageDraw.Draw(bg)

    # ── Titre ─────────────────────────────────────────────────────────────────
    title_s = (title[:44] + "…") if len(title) > 44 else title
    if title_s:
        tw = _tw(draw, title_s, font_title)
        tx, ty = (W - tw) // 2, cy + cover_sz + 18
        draw.text((tx + 1, ty + 1), title_s, font=font_title, fill=(0, 0, 0))
        draw.text((tx, ty),         title_s, font=font_title, fill=(255, 255, 255))

    # ── Artiste ───────────────────────────────────────────────────────────────
    artist_s = (artist[:50] + "…") if len(artist) > 50 else artist
    if artist_s:
        aw = _tw(draw, artist_s, font_artist)
        draw.text(((W - aw) // 2, cy + cover_sz + 68),
                  artist_s, font=font_artist, fill=(210, 200, 240))

    # ── Badge station (bas gauche) ────────────────────────────────────────────
    bg.paste(Image.new("RGB", (270, 42), (150, 20, 50)), (0, H - 42))
    draw = ImageDraw.Draw(bg)  # rafraîchir après paste
    draw.text((12, H - 34), "● GAIVERLAND RADIO",
              font=font_station, fill=(255, 255, 255))

    return bg.tobytes()


def make_default_frame() -> bytes:
    bg = _bg_manager.get_random() if _bg_manager else None
    return make_frame(bg, None, "Gaiverland Radio", "Electronic Music 24/7")


# ── Cover + BG updater (thread) ───────────────────────────────────────────────
def cover_updater():
    global _song_id, _frame
    print("→ Cover updater démarré")
    while True:
        try:
            np     = get_nowplaying()
            song   = np.get("now_playing", {}).get("song", {})
            title  = song.get("title",  "")
            artist = song.get("artist", "")
            art    = song.get("art",    "")
            sid    = song.get("id") or hashlib.md5(
                f"{title}{artist}".encode()).hexdigest()

            if sid != _song_id:
                print(f"  🎵 {artist} — {title}")
                cover    = fetch_cover(art)
                print(f"    cover={'ok '+str(cover.size) if cover else 'None (pas de cover)'}")
                bg_photo = _bg_manager.get_random() if _bg_manager else None
                print(f"    bg={'ok' if bg_photo else 'None'}")
                try:
                    frame = make_frame(bg_photo, cover, title, artist)
                    frame_sz = len(frame)
                    with _frame_lock:
                        _frame   = frame
                        _song_id = sid
                    print(f"    ✓ _frame mis à jour ({frame_sz} bytes, cover={'oui' if cover else 'non'})")
                except Exception as ef:
                    print(f"    ✗ make_frame ERREUR: {ef}")
        except Exception as e:
            print(f"  ⚠ cover_updater: {e}")
        time.sleep(POLL_SEC)


# ── FFmpeg ────────────────────────────────────────────────────────────────────
def build_cmd() -> list:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        # Entrée vidéo : pipe rawvideo RGB24 @ 1fps
        "-f",            "rawvideo",
        "-pixel_format", "rgb24",
        "-video_size",   f"{W}x{H}",
        "-framerate",    str(INPUT_FPS),
        "-i",            "pipe:0",
        # Entrée audio : Icecast avec reconnexion auto
        "-reconnect",            "1",
        "-reconnect_streamed",   "1",
        "-reconnect_delay_max",  "30",
        "-i",            ICECAST_URL,
        # Vidéo : ultrafast + stillimage → frames répétées = B-frames quasi-vides
        "-c:v",    "libx264",
        "-preset", "ultrafast",
        "-tune",   "stillimage",
        "-b:v",    "1500k",
        "-maxrate","2500k",
        "-bufsize", "5000k",
        "-vf",     f"fps={OUTPUT_FPS}",   # 1fps → 30fps (duplication frame)
        "-g",      "60",                  # keyframe toutes les 2 s
        "-pix_fmt","yuv420p",
        # Audio : MP3 → AAC (requis YouTube)
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar",  "44100",
        # Sortie
        "-f",   "flv",
        YT_RTMP,
    ]


def start_ffmpeg() -> subprocess.Popen:
    cmd = build_cmd()
    print(f"  → FFmpeg pipe→rtmp (1fps→{OUTPUT_FPS}fps, ultrafast+stillimage)")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def watch_stderr(proc: subprocess.Popen):
    def _r():
        for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line and "frame=" not in line:
                print(f"  [ffmpeg] {line}")
    threading.Thread(target=_r, daemon=True).start()


# ── Dry-run (test sans RTMP) ──────────────────────────────────────────────────
def dry_run():
    print("→ DRY RUN (YOUTUBE_RTMP_URL vide) — génération 3 frames JPEG de test")
    for i in range(3):
        np   = get_nowplaying()
        song = np.get("now_playing", {}).get("song", {})
        cover    = fetch_cover(song.get("art", ""))
        bg_photo = _bg_manager.get_random() if _bg_manager else None
        frame    = make_frame(bg_photo, cover, song.get("title",""), song.get("artist",""))
        img  = Image.frombytes("RGB", (W, H), frame)
        path = f"/tmp/yt_frame_{i}.jpg"
        img.save(path, "JPEG", quality=90)
        print(f"  ✓ {path}  [{song.get('title','?')}]")
        time.sleep(4)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _bg_manager
    print("📺 YouTube Streamer — Gaiverland Radio")
    print(f"   Icecast : {ICECAST_URL}")
    print(f"   YouTube : {(YT_RTMP[:50]+'…') if YT_RTMP else '⚠ NON CONFIGURÉ (dry-run)'}")

    # Charger les photos de fond
    _bg_manager = BackgroundManager(BACKGROUND_SOURCE_URLS, BG_CACHE_DIR)
    print(f"→ Chargement photos de fond ({len(BACKGROUND_SOURCE_URLS)} sources)...")
    _bg_manager.load()

    if not YT_RTMP:
        dry_run()
        return

    # Frame par défaut avant le premier nowplaying
    with _frame_lock:
        _frame = make_default_frame()

    # Lancer le cover updater
    threading.Thread(target=cover_updater, daemon=True).start()

    # Attendre le premier nowplaying (max 15s)
    for _ in range(15):
        with _frame_lock:
            if _song_id:
                break
        time.sleep(1)

    # Lancer FFmpeg
    proc = start_ffmpeg()
    watch_stderr(proc)
    print(f"✅ Stream actif — {W}x{H} @ {INPUT_FPS}fps→{OUTPUT_FPS}fps")

    try:
        while True:
            with _frame_lock:
                frame = _frame
            try:
                proc.stdin.write(frame)
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                rc = proc.wait(timeout=3)
                print(f"⚠ FFmpeg terminé (rc={rc}), redémarrage dans 5s…")
                time.sleep(5)
                proc = start_ffmpeg()
                watch_stderr(proc)
            if proc.poll() is not None:
                print(f"⚠ FFmpeg crash (rc={proc.returncode}), redémarrage dans 5s…")
                time.sleep(5)
                proc = start_ffmpeg()
                watch_stderr(proc)
            time.sleep(1.0 / INPUT_FPS)
    except KeyboardInterrupt:
        print("\n⏹ Arrêt streamer")
    finally:
        try:
            proc.stdin.close()
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
