"""
YouTube Live Streamer — Gaiverland Radio
Diffuse le stream Icecast d'AzuraCast vers YouTube Live avec cover art dynamique.

Stratégie CPU-minimale :
  - Python génère 1 frame/seconde (image statique pendant toute la durée d'un titre)
  - FFmpeg reçoit 1fps via pipe, upscale à 30fps en répétant la frame
  - libx264 -preset ultrafast -tune stillimage → frames répétées = B-frames quasi-vides
  - Mise à jour visuelle seulement au changement de titre (~toutes les 4-6 min)
"""
import os, sys, subprocess, time, threading, io, hashlib

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

W, H         = 1280, 720
INPUT_FPS    = 1        # frames/sec envoyés à FFmpeg (statique = ultra-faible CPU)
OUTPUT_FPS   = 30       # fps sortie YouTube (requis)
POLL_SEC     = 5        # intervalle vérif nowplaying

# ── État partagé ──────────────────────────────────────────────────────────────
_frame      : bytes | None = None
_frame_lock = threading.Lock()
_song_id    = None


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
            ImageFont.truetype(bold, 20),   # station
        )
    except Exception:
        f = ImageFont.load_default()
        _fonts = (f, f, f)


def _text_w(draw: ImageDraw.ImageDraw, text: str, font) -> int:
    """Largeur de texte compatible Pillow ≥ 9."""
    try:
        return int(draw.textlength(text, font=font))
    except AttributeError:
        return font.getsize(text)[0]


def make_frame(cover: "Image.Image | None", title: str, artist: str) -> bytes:
    if _fonts is None:
        _load_fonts()
    font_title, font_artist, font_station = _fonts

    # ── Fond : cover floutée + assombrie ─────────────────────────────────────
    if cover:
        bg = cover.resize((W, H), Image.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(radius=30))
        dark = Image.new("RGB", (W, H), (0, 0, 0))
        bg = Image.blend(bg, dark, 0.55)
    else:
        bg = Image.new("RGB", (W, H), (12, 10, 32))

    # ── Cover art centrée ─────────────────────────────────────────────────────
    cover_sz = min(H - 200, 400)
    cx = (W - cover_sz) // 2
    cy = (H - cover_sz) // 2 - 45
    if cover:
        c = cover.resize((cover_sz, cover_sz), Image.LANCZOS)
        # Ombre légère (rectangle décalé)
        shadow = Image.new("RGB", (cover_sz + 8, cover_sz + 8), (0, 0, 0))
        bg.paste(shadow, (cx - 4 + 4, cy - 4 + 4))
        bg.paste(c, (cx, cy))

    draw = ImageDraw.Draw(bg)

    # ── Titre ─────────────────────────────────────────────────────────────────
    title_s = (title[:44] + "…") if len(title) > 44 else title
    if title_s:
        tw = _text_w(draw, title_s, font_title)
        tx = (W - tw) // 2
        ty = cy + cover_sz + 20
        # Ombre sous le texte
        draw.text((tx + 1, ty + 1), title_s, font=font_title, fill=(0, 0, 0))
        draw.text((tx, ty), title_s, font=font_title, fill=(255, 255, 255))

    # ── Artiste ───────────────────────────────────────────────────────────────
    artist_s = (artist[:50] + "…") if len(artist) > 50 else artist
    if artist_s:
        aw = _text_w(draw, artist_s, font_artist)
        draw.text(((W - aw) // 2, cy + cover_sz + 72),
                  artist_s, font=font_artist, fill=(190, 185, 215))

    # ── Bandeau station (bas gauche) ──────────────────────────────────────────
    bg.paste(Image.new("RGB", (270, 42), (150, 20, 50)), (0, H - 42))
    draw.text((12, H - 34), "● GAIVERLAND RADIO", font=font_station,
              fill=(255, 255, 255))

    return bg.tobytes()  # raw RGB24


def make_default_frame() -> bytes:
    return make_frame(None, "Gaiverland Radio", "Electronic Music 24/7")


# ── Cover updater (thread) ────────────────────────────────────────────────────
def cover_updater():
    global _song_id, _frame
    print("→ Cover updater démarré")
    while True:
        try:
            np   = get_nowplaying()
            song = np.get("now_playing", {}).get("song", {})
            title  = song.get("title",  "")
            artist = song.get("artist", "")
            art    = song.get("art",    "")
            sid    = song.get("id") or hashlib.md5(
                f"{title}{artist}".encode()).hexdigest()

            if sid != _song_id:
                print(f"  🎵 {artist} — {title}")
                cover = fetch_cover(art)
                frame = make_frame(cover, title, artist)
                with _frame_lock:
                    _frame   = frame
                    _song_id = sid
        except Exception as e:
            print(f"  ⚠ cover_updater: {e}")
        time.sleep(POLL_SEC)


# ── FFmpeg ────────────────────────────────────────────────────────────────────
def build_cmd() -> list:
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        # ── Entrée vidéo : pipe rawvideo RGB24 @ 1fps ──
        "-f",           "rawvideo",
        "-pixel_format","rgb24",
        "-video_size",  f"{W}x{H}",
        "-framerate",   str(INPUT_FPS),
        "-i",           "pipe:0",
        # ── Entrée audio : Icecast avec reconnexion auto ──
        "-reconnect",             "1",
        "-reconnect_streamed",    "1",
        "-reconnect_delay_max",   "30",
        "-i",           ICECAST_URL,
        # ── Encodage vidéo : ultrafast + stillimage (frames répétées ≈ zéro CPU) ──
        "-c:v",     "libx264",
        "-preset",  "ultrafast",
        "-tune",    "stillimage",
        "-b:v",     "1500k",
        "-maxrate", "2500k",
        "-bufsize", "5000k",
        "-vf",      f"fps={OUTPUT_FPS}",  # 1fps → 30fps (duplication de frame)
        "-g",       "60",                 # keyframe toutes les 2 s
        "-pix_fmt", "yuv420p",
        # ── Encodage audio : MP3 → AAC (requis YouTube) ──
        "-c:a",  "aac",
        "-b:a",  "128k",
        "-ar",   "44100",
        # ── Sortie RTMP ──
        "-f",  "flv",
        YT_RTMP,
    ]


def start_ffmpeg() -> subprocess.Popen:
    cmd = build_cmd()
    print(f"  → FFmpeg pipe→rtmp (1fps→{OUTPUT_FPS}fps, ultrafast+stillimage)")
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)


def watch_stderr(proc: subprocess.Popen):
    """Relaie les logs FFmpeg importants (pas les stats frame)."""
    def _read():
        for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line and "frame=" not in line:
                print(f"  [ffmpeg] {line}")
    threading.Thread(target=_read, daemon=True).start()


# ── Dry-run ───────────────────────────────────────────────────────────────────
def dry_run():
    """Test sans YouTube : génère 3 frames JPEG et les sauvegarde dans /tmp."""
    print("→ DRY RUN (YOUTUBE_RTMP_URL non défini)")
    if _fonts is None:
        _load_fonts()
    for i in range(3):
        np   = get_nowplaying()
        song = np.get("now_playing", {}).get("song", {})
        cover  = fetch_cover(song.get("art", ""))
        frame  = make_frame(cover, song.get("title",""), song.get("artist",""))
        img    = Image.frombytes("RGB", (W, H), frame)
        path   = f"/tmp/yt_frame_{i}.jpg"
        img.save(path, "JPEG", quality=90)
        print(f"  ✓ frame {i} → {path} ({song.get('title','?')})")
        time.sleep(4)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"📺 YouTube Streamer — Gaiverland Radio")
    print(f"   Icecast : {ICECAST_URL}")
    print(f"   YouTube : {(YT_RTMP[:45] + '…') if YT_RTMP else '⚠ NON CONFIGURÉ'}")

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
