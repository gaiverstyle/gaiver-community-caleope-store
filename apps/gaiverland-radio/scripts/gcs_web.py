"""
GCS Web — Site public Gaiverland (bible v1.1 site_layers).
Une page : Mainstage live + player, scènes secondaires (coming soon),
journal de lore, vote ENCORE/REVIEW/SKIP, ville du festival, galerie (soon).
Design festival (affiche sunset), pas dashboard technique.
Port 8099 — derrière NPM plus tard, accessible en LAN d'ici là.
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

import json, time, re
from urllib.parse import urlsplit
import psycopg2.extras
from fastapi import FastAPI, Body, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
import secrets, hmac, hashlib, base64

# Page « L'équipe » (maquette Cassy, avatars embarqués). Import guardé : si le
# module manque, le site reste debout, seul /equipe est indisponible.
try:
    from team_page import HTML as TEAM_HTML
except Exception:
    TEAM_HTML = None

DB_URL      = os.environ["DATABASE_URL"]
TRACK_URL   = os.environ.get("GCS_TRACK_URL",        "http://gcs-track-service:8090")
STATE_URL   = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")
VOTE_URL    = os.environ.get("GCS_VOTE_URL",         "http://gcs-vote-service:8095")
AZ_URL      = os.environ.get("AZURACAST_URL",        "http://azuracast:80")
AZ_STATION  = int(os.environ.get("AZURACAST_STATION_ID", "1"))
# Sélecteur de station du site : clé (data-st côté front) → id AzuraCast.
# Ajouter une station = 1 entrée ici + 1 scène dans le HTML (data-st + onclick).
STATIONS = {
    "main":  AZ_STATION,
    "chill": int(os.environ.get("GCS_CHILL_STATION", "3")),
    "hard":  int(os.environ.get("GCS_HARD_STATION",  "4")),
    "phonk": int(os.environ.get("GCS_PHONK_STATION", "5")),
    "lofi":  int(os.environ.get("GCS_LOFI_STATION",  "6")),
    "synthwave": int(os.environ.get("GCS_SYNTHWAVE_STATION", "7")),
}
STREAM_URL  = os.environ.get("GCS_STREAM_URL", "")  # override manuel Mainstage si besoin
# Base publique d'AzuraCast pour réécrire les URLs internes (stream, pochettes)
# que l'API nowplaying renvoie en http://azuracast. Mettre le domaine NPM ici
# quand il existe ; sinon l'IP LAN. Vide = pas de réécriture.
AZ_PUBLIC   = os.environ.get("GCS_AZ_PUBLIC_URL", "").rstrip("/")

# ── OAuth votes — Google + Discord (PAS Authentik : privé fondateur/famille) ──
GOOGLE_ID      = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_SECRET  = os.environ.get("GOOGLE_CLIENT_SECRET", "")
DISCORD_ID     = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_SECRET = os.environ.get("DISCORD_CLIENT_SECRET", "")
SESSION_SECRET = os.environ.get("OAUTH_SESSION_SECRET", "change-me")
PUBLIC_BASE    = os.environ.get("GCS_PUBLIC_BASE", "https://gaiverland.gaiver-it.fr").rstrip("/")

app = FastAPI(title="Gaiverland Web")


def _publicize(url: str) -> str:
    """Réécrit une URL AzuraCast (interne docker OU IP LAN) vers la base publique AZ_PUBLIC.
    Appliqué uniquement aux URLs AzuraCast (stream, pochettes) : on remplace le
    scheme://host[:port] par AZ_PUBLIC et on garde le chemin. Sans quoi un visiteur web
    reçoit une URL LAN (http://172.x…) injoignable → pas de son ni de pochette."""
    if not url or not AZ_PUBLIC:
        return url
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return url  # déjà relative → laissée telle quelle
    return AZ_PUBLIC + url[len(parts.scheme) + 3 + len(parts.netloc):]

WEATHER_POETRY = {
    "calm":  "Ciel tranquille au-dessus du site",
    "warm":  "L'air est chaud, les basses aussi",
    "windy": "Le vent porte le son plus loin ce soir",
    "storm": "L'orage gronde, le festival répond",
    "rain":  "La pluie danse avec la foule",
    "cold":  "Nuit fraîche, son bouillant",
}

STAGE_LABEL = {
    "mainstage": "Mainstage", "rush": "Rush Stage",
    "sunset": "Sunset Stage", "night": "Night Stage",
}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


@app.get("/health")
def health():
    return {"status": "ok", "service": "gcs-web"}


@app.get("/metrics")
def metrics():
    """Métriques Prometheus pour la supervision (Kade/Aphrodite) : listeners par scène,
    état des stations, taille du catalogue. Données non sensibles (radio publique)."""
    lines = []

    def emit(name, help_, typ, samples):
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {typ}")
        lines.extend(samples)

    # AzuraCast : listeners + online par scène (1 seul appel /api/nowplaying)
    by_id = {}
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying", timeout=6)
        for np in (r.json() if r.status_code == 200 else []):
            sid = (np.get("station") or {}).get("id")
            if sid is not None:
                by_id[sid] = np
    except Exception:
        pass

    lis, onl, total = [], [], 0
    for key, sid in STATIONS.items():
        np = by_id.get(sid, {})
        listeners = int(((np.get("listeners") or {}).get("current")) or 0)
        online = 1 if np.get("is_online") else 0
        total += listeners
        lis.append(f'gaiverland_station_listeners{{station="{key}"}} {listeners}')
        onl.append(f'gaiverland_station_online{{station="{key}"}} {online}')
    emit("gaiverland_station_listeners", "Auditeurs actuels par scene", "gauge", lis)
    emit("gaiverland_station_online", "Scene en ligne (1) ou non (0)", "gauge", onl)
    emit("gaiverland_listeners_total", "Auditeurs toutes scenes confondues", "gauge",
         [f"gaiverland_listeners_total {total}"])

    # Catalogue : nombre de titres analysés en base
    tracks = -1
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM tracks")
            tracks = cur.fetchone()["n"]
        conn.close()
    except Exception:
        pass
    emit("gaiverland_tracks_total", "Titres analyses en base (-1 = DB injoignable)", "gauge",
         [f"gaiverland_tracks_total {tracks}"])

    emit("gaiverland_up", "Endpoint metriques Gaiverland joignable", "gauge", ["gaiverland_up 1"])
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/api/live")
def live(station: str = "main"):
    az_sid = STATIONS.get(station, AZ_STATION)
    out = {"track": {}, "state": {}, "station": station,
           "stream_url": STREAM_URL if station == "main" else "", "art": "", "listeners": 0}
    # AzuraCast nowplaying (art, listen_url, listeners)
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{az_sid}", timeout=4)
        if r.status_code == 200:
            np = r.json()
            song = np.get("now_playing", {}).get("song", {})
            out["track"] = {
                "title":    song.get("title", ""),
                "artist":   song.get("artist", ""),
                "elapsed":  np.get("now_playing", {}).get("elapsed", 0),
                "duration": np.get("now_playing", {}).get("duration", 0),
                "song_id":  song.get("id", ""),
            }
            out["art"]       = _publicize(song.get("art", ""))
            out["listeners"] = np.get("listeners", {}).get("current", 0)
            if not out["stream_url"]:
                mounts = np.get("station", {}).get("mounts", [])
                if mounts:
                    out["stream_url"] = mounts[0].get("url", "")
                else:
                    out["stream_url"] = np.get("station", {}).get("listen_url", "")
            out["stream_url"] = _publicize(out["stream_url"])
    except Exception:
        pass
    # Festival state
    try:
        r = httpx.get(f"{STATE_URL}/state/current", timeout=3)
        if r.status_code == 200:
            s = r.json()
            out["state"] = {
                "city":         s.get("city", "Toulon"),
                "stage":        STAGE_LABEL.get(s.get("stage_active", "mainstage"), "Mainstage"),
                "energy":       s.get("energy_level", 3),
                "tod":          s.get("time_of_day", "day"),
                "weather":      WEATHER_POETRY.get(s.get("weather_mood", "calm"),
                                                   WEATHER_POETRY["calm"]),
                "phase":        s.get("festival_phase", "live"),
            }
    except Exception:
        pass
    return out


@app.get("/api/events")
def events(limit: int = 12):
    conn = get_conn()
    with conn.cursor() as cur:
        # Le journal raconte l'HISTOIRE du festival : on exclut les répliques de Rebexis
        # (ce n'est pas le log micro de l'animatrice, c'est le lore du festival).
        cur.execute("""
            SELECT type, description, city, created_at FROM lore_events
            WHERE type <> 'rebexis_intervention'
            ORDER BY created_at DESC LIMIT %s
        """, (min(limit, 30),))
        rows = cur.fetchall()
    conn.close()
    # Ordre chronologique (journal/diary), pas newest-first (log) : on renverse.
    return {"events": [
        {"type": r["type"], "text": r["description"], "city": r["city"],
         "at": r["created_at"].strftime("%H:%M")} for r in reversed(rows)
    ]}


_visuals_cache = {"city": "", "at": 0.0, "imgs": []}

# Fichiers à écarter du fond : blasons, cartes, drapeaux, logos, plans, portraits…
_PHOTO_REJECT = re.compile(
    r"(blason|armoiries|coat[_ ]?of[_ ]?arms|wappen|logo|drapeau|flag|"
    r"carte|\bmap\b|plan_|localisation|location_|situation|position|"
    r"seal|sceau|diagram|graph|chart|\.svg)", re.I)


def _wm_upscale(src: str, target: int = 1920) -> str:
    """Régénère une vignette Wikimedia plus large (URLs .../NNNpx-Nom.jpg, à la volée)."""
    return re.sub(r"/(\d+)px-", f"/{target}px-", src, count=1)


def _good_photo(src: str) -> bool:
    low = src.lower()
    if not low.endswith((".jpg", ".jpeg")):
        return False  # on veut des photos (écarte SVG/PNG cartes/blasons)
    return not _PHOTO_REJECT.search(src)


def _wiki_media(title: str, lang: str = "fr") -> list:
    """Photos JPG d'une page Wikipedia (media-list), upscalées + filtrées."""
    out = []
    try:
        r = httpx.get(f"https://{lang}.wikipedia.org/api/rest_v1/page/media-list/{title}",
                      headers={"User-Agent": "GaiverlandRadio/1.0 (festival visuals)"}, timeout=6)
        if r.status_code == 200:
            for m in r.json().get("items", []):
                if m.get("type") != "image":
                    continue
                ss = m.get("srcset") or []
                if not ss:
                    continue
                src = ss[-1].get("src", "")
                if src.startswith("//"):
                    src = "https:" + src
                if not _good_photo(src):
                    continue
                mw = re.search(r"/(\d+)px-", src)
                if mw and int(mw.group(1)) < 800:
                    continue  # trop basse résolution même à l'origine
                out.append(_wm_upscale(src))
    except Exception:
        pass
    return out


def _wiki_coords(city: str):
    """(lat, lon) de la ville via l'API Wikipedia, sinon (None, None)."""
    try:
        r = httpx.get("https://fr.wikipedia.org/w/api.php",
                      params={"action": "query", "prop": "coordinates",
                              "titles": city, "format": "json"},
                      headers={"User-Agent": "GaiverlandRadio/1.0"}, timeout=5)
        for p in r.json().get("query", {}).get("pages", {}).values():
            co = p.get("coordinates")
            if co:
                return co[0]["lat"], co[0]["lon"]
    except Exception:
        pass
    return None, None


def _commons_geosearch(lat, lon, radius_m: int, limit: int = 20) -> list:
    """Photos Commons autour de coordonnées (élargissement géographique par rayon)."""
    out = []
    try:
        r = httpx.get("https://commons.wikimedia.org/w/api.php",
                      params={"action": "query", "generator": "geosearch",
                              "ggscoord": f"{lat}|{lon}", "ggsradius": radius_m,
                              "ggslimit": limit, "ggsnamespace": 6,
                              "prop": "imageinfo", "iiprop": "url|size",
                              "iiurlwidth": 1920, "format": "json"},
                      headers={"User-Agent": "GaiverlandRadio/1.0"}, timeout=7)
        for p in r.json().get("query", {}).get("pages", {}).values():
            ii = (p.get("imageinfo") or [{}])[0]
            src = ii.get("thumburl") or ii.get("url", "")
            w = ii.get("width", 0) or 0
            if src and _good_photo(src) and (w == 0 or w >= 1000):
                out.append(src)
    except Exception:
        pass
    return out


def _city_photos(city: str):
    """Fond du 'clip'/plein écran : photos de la zone du festival. Caché 30 min, fail-safe.
    1) page Wikipedia de la ville (upscalée, filtrée) ; 2) si peu de belles photos,
    élargissement géographique via Commons (rayon croissant ville→agglo→région)."""
    if not city:
        return []
    now = time.time()
    if _visuals_cache["city"] == city and now - _visuals_cache["at"] < 1800:
        return _visuals_cache["imgs"]
    imgs = _wiki_media(city)
    if len(imgs) < 6:
        lat, lon = _wiki_coords(city)
        if lat is not None:
            for radius in (8000, 25000, 60000):  # ~ville, agglo, département/région
                imgs += _commons_geosearch(lat, lon, radius)
                if len(imgs) >= 8:
                    break
    seen, uniq = set(), []
    for s in imgs:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
        if len(uniq) >= 14:
            break
    _visuals_cache.update(city=city, at=now, imgs=uniq)
    return uniq


@app.get("/api/visuals")
def visuals():
    """Images du 'clip' in-page : cover courante + photos de la ville. Fail-safe (cover au minimum)."""
    imgs = []
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=4)
        if r.status_code == 200:
            art = _publicize(r.json().get("now_playing", {}).get("song", {}).get("art", ""))
            if art:
                imgs.append(art)
    except Exception:
        pass
    # Ville : GCS_CITY (fiable, statique) ; le state-engine, s'il est joignable, peut surcharger.
    city = os.environ.get("GCS_CITY", "").strip()
    try:
        r = httpx.get(f"{STATE_URL}/state/current", timeout=2)
        if r.status_code == 200:
            c = r.json().get("city", "")
            if c:
                city = c
    except Exception:
        pass
    imgs += _city_photos(city)
    return {"images": imgs}


# ── Session : cookie signé HMAC, identité opaque provider:sub ───────────────
def _sign(payload: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return payload + "." + sig

def _verify(token: str) -> str:
    if not token or "." not in token:
        return ""
    payload, sig = token.rsplit(".", 1)
    good = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return payload if hmac.compare_digest(sig, good) else ""

def _uid(request: Request) -> str:
    """Identité opaque de session (hash de provider:sub) ou '' si non connecté."""
    ident = _verify(request.cookies.get("gsid", ""))
    return hashlib.sha256(ident.encode()).hexdigest()[:32] if ident else ""

def _login_ok(provider: str, sub: str):
    r = RedirectResponse("/?login=ok", status_code=302)
    r.set_cookie("gsid", _sign(f"{provider}:{sub}"), max_age=2592000,
                 httponly=True, secure=True, samesite="lax")
    r.delete_cookie("gstate")
    return r

def _oauth_start(auth_url: str):
    state = secrets.token_urlsafe(16)
    r = RedirectResponse(auth_url + "&state=" + state, status_code=302)
    r.set_cookie("gstate", state, max_age=600, httponly=True, secure=True, samesite="lax")
    return r


@app.get("/api/auth/google")
def auth_google():
    return _oauth_start(
        "https://accounts.google.com/o/oauth2/v2/auth?response_type=code"
        f"&client_id={GOOGLE_ID}&redirect_uri={PUBLIC_BASE}/api/auth/google/callback"
        "&scope=openid%20email")

@app.get("/api/auth/google/callback")
def auth_google_cb(request: Request, code: str = "", state: str = ""):
    if not code or not state or state != request.cookies.get("gstate", ""):
        return RedirectResponse("/?login=err", status_code=302)
    sub = ""
    try:
        tok = httpx.post("https://oauth2.googleapis.com/token", data={
            "code": code, "client_id": GOOGLE_ID, "client_secret": GOOGLE_SECRET,
            "redirect_uri": f"{PUBLIC_BASE}/api/auth/google/callback",
            "grant_type": "authorization_code"}, timeout=8).json()
        p = tok.get("id_token", "").split(".")
        payload = json.loads(base64.urlsafe_b64decode(p[1] + "=" * (-len(p[1]) % 4)))
        sub = payload.get("sub", "")
    except Exception:
        pass
    return _login_ok("google", sub) if sub else RedirectResponse("/?login=err", status_code=302)

@app.get("/api/auth/discord")
def auth_discord():
    return _oauth_start(
        "https://discord.com/api/oauth2/authorize?response_type=code"
        f"&client_id={DISCORD_ID}&redirect_uri={PUBLIC_BASE}/api/auth/discord/callback"
        "&scope=identify")

@app.get("/api/auth/discord/callback")
def auth_discord_cb(request: Request, code: str = "", state: str = ""):
    if not code or not state or state != request.cookies.get("gstate", ""):
        return RedirectResponse("/?login=err", status_code=302)
    sub = ""
    try:
        tok = httpx.post("https://discord.com/api/oauth2/token", data={
            "code": code, "client_id": DISCORD_ID, "client_secret": DISCORD_SECRET,
            "redirect_uri": f"{PUBLIC_BASE}/api/auth/discord/callback",
            "grant_type": "authorization_code"},
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=8).json()
        me = httpx.get("https://discord.com/api/users/@me",
                       headers={"Authorization": f"Bearer {tok.get('access_token','')}"}, timeout=8).json()
        sub = str(me.get("id", ""))
    except Exception:
        pass
    return _login_ok("discord", sub) if sub else RedirectResponse("/?login=err", status_code=302)

@app.get("/api/me")
def me(request: Request):
    ident = _verify(request.cookies.get("gsid", ""))
    return {"logged_in": bool(ident), "provider": ident.split(":", 1)[0] if ident else ""}

@app.get("/api/logout")
def logout():
    r = RedirectResponse("/", status_code=302)
    r.delete_cookie("gsid")
    return r


@app.post("/api/vote")
def vote(request: Request, body: dict = Body(...)):
    uid = _uid(request)
    if not uid:
        return {"ok": False, "error": "login requis", "need_login": True}
    v = str(body.get("vote", "")).upper()
    if v not in ("ENCORE", "REVIEW", "SKIP"):
        return {"ok": False, "error": "vote invalide"}
    # Résoudre le morceau en cours
    song_id = ""
    try:
        r = httpx.get(f"{TRACK_URL}/track/current", timeout=3)
        if r.status_code == 200:
            song_id = r.json().get("song_id", "")
    except Exception:
        pass
    if not song_id:
        return {"ok": False, "error": "pas de morceau en cours"}
    try:
        r = httpx.post(f"{VOTE_URL}/vote",
                       json={"song_id": song_id, "vote": v, "user_role": "user", "user_id": uid},
                       timeout=5)
        if r.status_code == 200:
            return {"ok": True, "vote": v}
        return {"ok": False, "error": f"vote-service {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


@app.post("/api/propose")
def propose(request: Request, body: dict = Body(...)):
    uid = _uid(request)
    if not uid:
        return {"ok": False, "error": "login requis", "need_login": True}
    title = str(body.get("title", "")).strip()[:200]
    if len(title) < 2:
        return {"ok": False, "error": "titre trop court"}
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS title_proposals (
                id SERIAL PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
                title TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())""")
            cur.execute("INSERT INTO title_proposals (user_id, title) VALUES (%s,%s)", (uid, title))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


@app.get("/api/proposals")
def proposals(limit: int = 30):
    """Propositions récentes (pour Régis / affichage). Regroupées par titre + nb de votants distincts."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT title, COUNT(DISTINCT user_id) AS n, MAX(created_at) AS last
                           FROM title_proposals GROUP BY title ORDER BY n DESC, last DESC LIMIT %s""",
                        (min(limit, 100),))
            rows = cur.fetchall()
        conn.close()
        return {"proposals": [{"title": r["title"], "count": r["n"]} for r in rows]}
    except Exception:
        return {"proposals": []}


PAGE = """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gaiverland — Le festival permanent</title>
<style>
:root{
  --sun1:#ff9a5a; --sun2:#ff5e7a; --sun3:#8b5cf6; --nightblue:#191036;
  --cream:#fff4e6; --ink:#2a1a33;
}
*{margin:0;padding:0;box-sizing:border-box}
body{
  font-family:Georgia,'Times New Roman',serif;
  background:linear-gradient(175deg,var(--nightblue) 0%,#3d1d5c 30%,var(--sun3) 55%,var(--sun2) 78%,var(--sun1) 100%);
  background-attachment:fixed; color:var(--cream); min-height:100vh;
}
.wrap{max-width:980px;margin:0 auto;padding:24px 20px 80px}
header{text-align:center;padding:48px 0 20px}
header .fete{font-size:15px;letter-spacing:6px;text-transform:uppercase;opacity:.85}
header h1{
  font-size:clamp(52px,10vw,96px);letter-spacing:2px;line-height:.95;
  background:linear-gradient(90deg,#ffd29a,#ff8fa3,#c9b6ff);
  -webkit-background-clip:text;background-clip:text;color:transparent;
  text-shadow:0 0 60px rgba(255,150,120,.25);
}
header .tagline{font-style:italic;font-size:18px;margin-top:10px;opacity:.9}
.pennant{display:flex;justify-content:center;gap:6px;margin:18px 0 0;font-size:22px;letter-spacing:8px}
.card{
  background:rgba(255,244,230,.08);border:1px solid rgba(255,244,230,.22);
  border-radius:18px;padding:22px;margin-top:26px;backdrop-filter:blur(8px);
}
h2{font-size:14px;letter-spacing:4px;text-transform:uppercase;opacity:.8;margin-bottom:16px}
.live-badge{display:inline-block;background:#ff3b5c;color:#fff;font-family:sans-serif;
  font-size:11px;font-weight:700;letter-spacing:2px;padding:3px 10px;border-radius:20px;
  animation:pulse 2s infinite;vertical-align:middle;margin-left:10px}
@keyframes pulse{50%{opacity:.55}}
.np{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.np img{width:110px;height:110px;border-radius:14px;object-fit:cover;
  box-shadow:0 8px 30px rgba(0,0,0,.4);background:rgba(0,0,0,.3)}
.np .t{font-size:26px;font-weight:bold}
.np .a{font-size:17px;opacity:.85;font-style:italic;margin-top:4px}
.np .meta{font-size:13px;opacity:.7;margin-top:10px;font-family:sans-serif}
audio{width:100%;margin-top:18px;border-radius:30px}
.bar{height:5px;background:rgba(255,244,230,.18);border-radius:3px;margin-top:14px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#ffd29a,#ff8fa3);transition:width 1s linear}
.votes{display:flex;gap:12px;margin-top:20px;flex-wrap:wrap}
.votes button{
  flex:1;min-width:110px;padding:14px 8px;border:none;border-radius:14px;cursor:pointer;
  font-family:Georgia,serif;font-size:16px;color:var(--ink);transition:transform .15s;
}
.votes button:hover{transform:translateY(-3px) rotate(-1deg)}
.v-encore{background:linear-gradient(135deg,#ffd29a,#ffb56b)}
.v-review{background:linear-gradient(135deg,#c9b6ff,#a48fff)}
.v-skip{background:linear-gradient(135deg,#ffb1c0,#ff8fa3)}
.votemsg{font-size:14px;margin-top:10px;font-style:italic;min-height:18px;opacity:.9}
.authbar{margin-top:16px;font-size:14px;font-family:sans-serif;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.authtxt,.authok{opacity:.88}
.authbtn{padding:6px 14px;border-radius:20px;text-decoration:none;color:var(--ink);font-weight:700}
.authbtn.g{background:#fff}
.authbtn.d{background:#5865F2;color:#fff}
.authlink{color:var(--cream);opacity:.55;font-size:12px}
.propose{display:flex;gap:8px;flex-wrap:wrap}
.propose input{flex:1;min-width:200px;padding:12px 14px;border-radius:12px;border:1px solid rgba(255,244,230,.3);background:rgba(255,244,230,.08);color:var(--cream);font-family:Georgia,serif;font-size:15px}
.propbtn{padding:12px 18px;border:none;border-radius:12px;cursor:pointer;font-family:Georgia,serif;font-size:15px;color:var(--ink);background:linear-gradient(135deg,#c9b6ff,#a48fff);font-weight:700}
.propmsg{font-size:14px;margin-top:10px;font-style:italic;min-height:18px;opacity:.9}
.stages{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px}
.stage{border-radius:14px;padding:18px 14px;text-align:center;position:relative;
  border:1px dashed rgba(255,244,230,.35)}
.stage.on{border:1px solid rgba(255,244,230,.5);background:rgba(255,244,230,.1)}
.stage .ico{font-size:30px}
.stage .nm{margin-top:8px;font-size:16px}
.stage .st{font-family:sans-serif;font-size:10px;letter-spacing:2px;text-transform:uppercase;
  margin-top:8px;padding:2px 8px;border-radius:10px;display:inline-block;
  background:rgba(0,0,0,.28);opacity:.9}
.stage.on .st{background:#ff3b5c}
.city{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.city .pin{font-size:44px}
.city .cn{font-size:30px;font-weight:bold}
.city .wx{font-style:italic;opacity:.85;margin-top:4px}
.city .next{margin-left:auto;text-align:right;font-size:14px;opacity:.75;font-style:italic}
.journal .ev{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid rgba(255,244,230,.12);
  font-size:15px;align-items:baseline}
.journal .ev:last-child{border:none}
.journal .at{font-family:sans-serif;font-size:11px;opacity:.6;min-width:42px}
.journal .ic{min-width:24px}
.soon{opacity:.75;text-align:center;padding:26px 10px;font-style:italic;font-size:16px}
footer{text-align:center;margin-top:44px;font-size:13px;opacity:.65;font-style:italic}
footer .c15{margin-top:6px;font-size:12px}
.hero{position:relative;width:100%;aspect-ratio:4/3;border-radius:16px;overflow:hidden;
  background:#1a1030;box-shadow:0 10px 40px rgba(0,0,0,.45);margin:4px 0 16px}
.hero-bg{position:absolute;inset:0;background-size:cover;background-position:center;
  filter:brightness(.5) saturate(1.15);transform:scale(1.06);transition:background-image .5s ease}
.hero-fg{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:12px;padding:22px;text-align:center}
.hero-cover{width:min(48%,240px);aspect-ratio:1;border-radius:12px;object-fit:cover;
  box-shadow:0 14px 44px rgba(0,0,0,.65);background:rgba(0,0,0,.35)}
.hero-t{font-size:clamp(17px,3.2vw,24px);font-weight:bold;line-height:1.2;
  text-shadow:0 2px 14px rgba(0,0,0,.9);max-width:92%}
.hero-a{font-size:15px;font-style:italic;opacity:.92;text-shadow:0 2px 10px rgba(0,0,0,.9)}
.playbtn{flex:0 0 auto;width:64px;height:64px;border-radius:50%;border:none;cursor:pointer;font-size:24px;color:var(--ink);
  background:linear-gradient(135deg,#ffd29a,#ffb56b);box-shadow:0 6px 24px rgba(0,0,0,.35);transition:transform .15s}
.playbtn:hover{transform:scale(1.06)}
.reloadbtn{flex:0 0 auto;width:44px;height:44px;border-radius:50%;border:1px solid rgba(255,255,255,.22);background:transparent;cursor:pointer;font-size:18px;color:var(--ink);opacity:.7;transition:transform .3s,opacity .15s}
.reloadbtn:hover{transform:rotate(180deg);opacity:1}
#player{display:none}
/* Scènes cliquables (stations Live) vs à venir (Bientôt) */
.stage.live{cursor:pointer;border-style:solid;border-color:rgba(255,244,230,.4);transition:all .15s}
.stage.live:hover{background:rgba(255,244,230,.13);transform:translateY(-2px)}
.stage.live .st{background:#2fae60}
/* ── Mode plein écran (tel / TV / PC) ─────────────────────────────── */
.hero-fs{position:absolute;top:10px;right:10px;z-index:5;width:38px;height:38px;border:none;
  border-radius:10px;cursor:pointer;font-size:18px;color:var(--cream);
  background:rgba(0,0,0,.35);backdrop-filter:blur(3px);transition:transform .15s}
.hero-fs:hover{transform:scale(1.08);background:rgba(0,0,0,.5)}
#fs{position:fixed;inset:0;z-index:1000;display:flex;align-items:center;justify-content:center;overflow:hidden;
  background:linear-gradient(175deg,var(--nightblue),#3d1d5c 35%,var(--sun3) 65%,var(--sun2) 88%,var(--sun1))}
#fs.fs-hidden{display:none}
#fs-bg{position:absolute;inset:0;background-size:cover;background-position:center;
  filter:brightness(.45) saturate(1.15);transform:scale(1.06);transition:background-image .6s ease}
#fs-viz{position:absolute;left:0;right:0;bottom:0;width:100%;height:36vh;z-index:1;opacity:.85;pointer-events:none}
#fs-scrim{position:absolute;inset:0;z-index:2;background:radial-gradient(ellipse at center,rgba(0,0,0,.12),rgba(0,0,0,.55))}
#fs-center{position:relative;z-index:3;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:2.2vh;padding:4vh 5vw;text-align:center;max-width:1100px}
#fs-wordmark{font-family:Georgia,serif;font-weight:bold;font-size:clamp(40px,9vw,150px);letter-spacing:2px;line-height:.9;
  background:linear-gradient(90deg,#ffd29a,#ff8fa3,#c9b6ff);-webkit-background-clip:text;background-clip:text;
  color:transparent;text-shadow:0 0 60px rgba(255,150,120,.25)}
#fs-cover{width:min(38vh,42vw);aspect-ratio:1;border-radius:16px;object-fit:cover;
  box-shadow:0 20px 60px rgba(0,0,0,.7);background:rgba(0,0,0,.35)}
#fs-title{font-size:clamp(22px,3.6vw,46px);font-weight:bold;line-height:1.15;text-shadow:0 3px 18px rgba(0,0,0,.85);max-width:90%}
#fs-artist{font-size:clamp(15px,2vw,26px);font-style:italic;opacity:.9;text-shadow:0 2px 12px rgba(0,0,0,.85)}
#fs-play{width:clamp(62px,8vh,92px);height:clamp(62px,8vh,92px);border-radius:50%;border:none;cursor:pointer;
  font-size:clamp(24px,3vh,34px);color:var(--ink);background:linear-gradient(135deg,#ffd29a,#ffb56b);
  box-shadow:0 8px 30px rgba(0,0,0,.4);transition:transform .15s}
#fs-play:hover{transform:scale(1.06)}
#fs-bar{width:min(560px,80vw);height:6px;background:rgba(255,244,230,.2);border-radius:4px;overflow:hidden}
#fs-bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,#ffd29a,#ff8fa3);transition:width 1s linear}
#fs-top{position:absolute;top:16px;right:16px;z-index:4;display:flex;gap:10px}
#fs-top button{width:44px;height:44px;border-radius:12px;border:none;cursor:pointer;font-size:20px;color:var(--cream);
  background:rgba(0,0,0,.35);backdrop-filter:blur(3px);transition:transform .15s}
#fs-top button:hover{transform:scale(1.08);background:rgba(0,0,0,.5)}
#fs-settings{position:absolute;top:70px;right:16px;z-index:5;background:rgba(20,10,35,.92);
  border:1px solid rgba(255,244,230,.25);border-radius:14px;padding:16px 18px;font-family:sans-serif;font-size:14px;
  backdrop-filter:blur(6px);min-width:210px}
#fs-settings.fs-hidden{display:none}
#fs-settings .fs-set-title{font-size:11px;letter-spacing:2px;text-transform:uppercase;opacity:.7;margin-bottom:10px}
#fs-settings label{display:flex;align-items:center;gap:10px;padding:7px 0;cursor:pointer}
#fs-settings input{width:16px;height:16px;accent-color:#ff8fa3}
@media (max-width:600px){#fs-center{gap:1.6vh}#fs-cover{width:min(30vh,55vw)}}
</style></head><body><div class="wrap">

<header>
  <div class="fete">✦ le festival permanent ✦</div>
  <h1>GAIVERLAND</h1>
  <div class="tagline">La musique ne s'arrête jamais. Le festival non plus.</div>
  <div class="pennant">🎪 🎡 🎠 🎢 🎆</div>
</header>

<div class="card">
  <h2>En direct <span class="live-badge">EN DIRECT</span></h2>
  <div class="hero">
    <button class="hero-fs" onclick="openFs()" aria-label="Plein écran" title="Plein écran">⛶</button>
    <div class="hero-bg" id="hero-bg"></div>
    <div class="hero-fg">
      <img class="hero-cover" id="hero-cover" alt="" onerror="this.style.visibility='hidden'">
      <div class="hero-t" id="title">…</div>
      <div class="hero-a" id="artist"></div>
    </div>
  </div>
  <div class="np">
    <div style="flex:1;min-width:200px">
      <div class="meta" id="meta"></div>
      <div class="bar"><i id="prog"></i></div>
    </div>
    <button id="playbtn" class="playbtn" onclick="togglePlay()" aria-label="Lecture">▶</button>
    <button class="reloadbtn" onclick="playLive()" aria-label="Revenir au direct" title="Revenir au direct (resync flux)">⟳</button>
  </div>
  <audio id="player" preload="none"></audio>
  <div class="authbar" id="authbar"></div>
  <div class="votes">
    <button class="v-encore" onclick="vote('ENCORE')">🔥 ENCORE</button>
    <button class="v-review" onclick="vote('REVIEW')">🤔 À REVOIR</button>
    <button class="v-skip"   onclick="vote('SKIP')">⏭ PASSER</button>
  </div>
  <div class="votemsg" id="votemsg"></div>
</div>

<div class="card">
  <h2>Propose un titre 🎶</h2>
  <div class="propose">
    <input id="proptitle" type="text" placeholder="Artiste — Titre" maxlength="200">
    <button class="propbtn" onclick="proposeTitle()">Envoyer au convoi 🚐</button>
  </div>
  <div class="propmsg" id="propmsg"></div>
</div>

<div class="card">
  <h2>La tournée</h2>
  <div class="city">
    <div class="pin">📍</div>
    <div>
      <div class="cn" id="city">…</div>
      <div class="wx" id="wx"></div>
    </div>
    <div class="next">prochaine ville :<br>le convoi décidera. 🚐</div>
  </div>
</div>

<div class="card">
  <h2>Les scènes</h2>
  <div class="stages">
    <div class="stage live on" data-st="main" onclick="selectStation('main')"><div class="ico">🎪</div><div class="nm">Mainstage</div><div class="st">Live</div></div>
    <div class="stage live" data-st="chill" onclick="selectStation('chill')"><div class="ico">🌙</div><div class="nm">Chill</div><div class="st">Live</div></div>
    <div class="stage live" data-st="hard" onclick="selectStation('hard')"><div class="ico">🔥</div><div class="nm">Hard</div><div class="st">Live</div></div>
    <div class="stage live" data-st="phonk" onclick="selectStation('phonk')"><div class="ico">🏎️</div><div class="nm">Phonk</div><div class="st">Live</div></div>
    <div class="stage live" data-st="lofi" onclick="selectStation('lofi')"><div class="ico">🎧</div><div class="nm">Lofi</div><div class="st">Live</div></div>
    <div class="stage live" data-st="synthwave" onclick="selectStation('synthwave')"><div class="ico">🌆</div><div class="nm">Synthwave</div><div class="st">Live</div></div>
  </div>
</div>

<div class="card journal">
  <h2>Journal du festival</h2>
  <div id="events"><div class="soon">Le journal s'écrit en ce moment même…</div></div>
</div>

<div class="card">
  <h2>Galerie</h2>
  <div class="soon">📸 Les souvenirs du festival arrivent bientôt.<br>
  Le stagiaire a promis de retrouver la carte SD.</div>
</div>

<footer>
  Gaiverland Radio — présente, comme toujours.
  <div class="c15">Le C15 veille sur ce site. Personne ne sait pourquoi.</div>
  <div style="margin-top:14px"><a href="/equipe" style="color:rgba(255,244,230,.55);font-size:12px;letter-spacing:1px;text-decoration:none;border-bottom:1px solid rgba(255,244,230,.28);padding-bottom:2px">L'équipe du festival →</a></div>
</footer>

</div>

<div id="fs" class="fs-hidden">
  <div id="fs-bg"></div>
  <canvas id="fs-viz"></canvas>
  <div id="fs-scrim"></div>
  <div id="fs-top">
    <button id="fs-gear" onclick="toggleFsSettings()" aria-label="Réglages" title="Réglages">⚙</button>
    <button onclick="closeFs()" aria-label="Quitter le plein écran" title="Quitter">✕</button>
  </div>
  <div id="fs-settings" class="fs-hidden">
    <div class="fs-set-title">Affichage</div>
    <label><input type="checkbox" id="opt-wordmark"> Titre GAIVERLAND</label>
    <label><input type="checkbox" id="opt-bg"> Fond photos de la zone</label>
    <label><input type="checkbox" id="opt-bar"> Barre de progression</label>
    <label><input type="checkbox" id="opt-viz"> Visualizer audio</label>
  </div>
  <div id="fs-center">
    <div id="fs-wordmark">GAIVERLAND</div>
    <img id="fs-cover" alt="" onerror="this.style.visibility='hidden'">
    <div id="fs-title">…</div>
    <div id="fs-artist"></div>
    <button id="fs-play" onclick="togglePlay()" aria-label="Lecture">▶</button>
    <div id="fs-bar"><i id="fs-prog"></i></div>
  </div>
</div>

<script>
const ICO={rebexis_intervention:'🎙',c15_event:'🚐',stagiaire_event:'🧢',city_transition:'📍'};
let audioUrl="";
let cityPhotos=[], bgIdx=0, lastTitle="";
let curStation='main';  // station écoutée : 'main' (Mainstage) ou 'chill'

// Flux LIVE : on ne "reprend" jamais un flux radio (le buffer serait périmé →
// grésillements/décalage au retour). On repart toujours du DIRECT avec un flux frais.
function playLive(){
  const a=document.getElementById('player');
  if(!audioUrl) return;
  // anti-cache : force une nouvelle connexion sur le bord live, pas le buffer navigateur
  a.src = audioUrl + (audioUrl.indexOf('?')>=0?'&':'?') + '_=' + Date.now();
  a.load();
  a.play().catch(()=>{});
}
function stopStream(){
  // pause = on COUPE vraiment le flux (on lâche le buffer) → au retour = direct frais
  const a=document.getElementById('player');
  a.pause();
  a.removeAttribute('src');
  a.load();
}
function togglePlay(){
  const a=document.getElementById('player');
  if(a.paused){ playLive(); } else { stopStream(); }
}
// Clip in-page : composition fixe par morceau — fond Toulon + cover devant + titre dessous.
async function loadVisuals(){
  try{
    const d=await (await fetch('/api/visuals')).json();
    const imgs=d.images||[];
    cityPhotos = imgs.length>1 ? imgs.slice(1) : imgs;  // photos de ville (index 0 = cover)
    if(cityPhotos.length && !document.getElementById('hero-bg').style.backgroundImage) setBg();
  }catch(e){}
}
function setBg(){
  if(!cityPhotos.length) return;
  const url=cityPhotos[bgIdx % cityPhotos.length];
  const img=new Image();
  img.onload=()=>{ const u="url('"+url.replace(/'/g,'%27')+"')";
    document.getElementById('hero-bg').style.backgroundImage=u;
    if(fsOpen) document.getElementById('fs-bg').style.backgroundImage=u; };
  img.src=url;
}
function selectStation(s){
  if(s===curStation) return;
  curStation=s;
  document.querySelectorAll('.stage[data-st]').forEach(t=>t.classList.toggle('on', t.dataset.st===s));
  const a=document.getElementById('player');
  const wasPlaying=!a.paused;
  a.pause(); a.removeAttribute('src'); a.load();  // détache l'ancien flux
  audioUrl=''; lastTitle='';                       // force refresh à re-remplir
  refresh().then(()=>{ if(wasPlaying) playLive(); });
}
async function refresh(){
  try{
    const d=await (await fetch('/api/live?station='+curStation)).json();
    const t=d.track||{};
    document.getElementById('title').textContent=t.title||'Gaiverland Radio';
    document.getElementById('artist').textContent=t.artist||'';
    if(d.art) document.getElementById('hero-cover').src=d.art;
    if(t.title && t.title!==lastTitle){ lastTitle=t.title; bgIdx++; setBg(); }  // nouveau fond Toulon par morceau
    const l=d.listeners?d.listeners+' personne(s) dans la foule':'';
    document.getElementById('meta').textContent=l;
    // Media Session — titre/artiste/cover sur l'écran verrouillé + widgets média de l'OS
    if('mediaSession' in navigator && (t.title||t.artist)){
      navigator.mediaSession.metadata=new MediaMetadata({
        title:t.title||'Gaiverland Radio', artist:t.artist||'Gaiverland Radio',
        album:'Gaiverland — le festival permanent',
        artwork:d.art?[{src:d.art,sizes:'512x512',type:'image/jpeg'}]:[]
      });
    }
    if(t.duration>0){document.getElementById('prog').style.width=Math.min(100,100*t.elapsed/t.duration)+'%';}
    if(d.stream_url){audioUrl=d.stream_url;}
    const s=d.state||{};
    document.getElementById('city').textContent=s.city||'Quelque part';
    document.getElementById('wx').textContent=(s.weather||'')+(s.stage?' — scène active : '+s.stage:'');
    if(fsOpen) fsSync();
  }catch(e){}
}
async function loadEvents(){
  try{
    const d=await (await fetch('/api/events')).json();
    if(!d.events||!d.events.length)return;
    document.getElementById('events').innerHTML=d.events.map(e=>
      '<div class="ev"><span class="at">'+e.at+'</span><span class="ic">'+(ICO[e.type]||'✦')+
      '</span><span>'+e.text.replace(/</g,'&lt;')+'</span></div>').join('');
  }catch(e){}
}
async function vote(v){
  const m=document.getElementById('votemsg');
  try{
    const r=await (await fetch('/api/vote',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({vote:v})})).json();
    if(r.need_login){ m.textContent='Connecte-toi pour voter 👇'; return; }
    m.textContent=r.ok?'Vote "'+v+'" enregistré. Le festival vous a entendu. ✦'
                       :'Hmm… '+(r.error||'réessayez');
  }catch(e){m.textContent='Le stagiaire a débranché quelque chose. Réessayez.';}
  setTimeout(()=>m.textContent='',6000);
}
async function loadAuth(){
  try{
    const d=await (await fetch('/api/me')).json();
    const bar=document.getElementById('authbar');
    if(d.logged_in){
      bar.innerHTML='<span class="authok">✓ Connecté'+(d.provider?' via '+d.provider:'')+' — ton vote compte.</span> <a class="authlink" href="/api/logout">déconnexion</a>';
    }else{
      bar.innerHTML='<span class="authtxt">Connecte-toi pour voter :</span> <a class="authbtn g" href="/api/auth/google">Google</a> <a class="authbtn d" href="/api/auth/discord">Discord</a>';
    }
  }catch(e){}
}
async function proposeTitle(){
  const inp=document.getElementById('proptitle'), m=document.getElementById('propmsg');
  const title=(inp.value||'').trim();
  if(title.length<2){ m.textContent='Écris un titre (artiste + titre).'; return; }
  try{
    const r=await (await fetch('/api/propose',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:title})})).json();
    if(r.need_login){ m.textContent='Connecte-toi pour proposer 👆'; return; }
    m.textContent=r.ok?'Proposé ! Le convoi transmet à Régis. ✦':('Hmm… '+(r.error||'réessaye'));
    if(r.ok) inp.value='';
  }catch(e){ m.textContent='Le stagiaire a mangé la proposition. Réessaye.'; }
  setTimeout(()=>m.textContent='',6000);
}
// ── Mode plein écran (tel / TV / PC) ──────────────────────────────
const FS_KEY='gvl_fs_opts';
let fsOpen=false, vizRAF=0, vizBars=[], vizT=0;
function gid(id){return document.getElementById(id);}
function fsGetOpts(){let o={};try{o=JSON.parse(localStorage.getItem(FS_KEY))||{};}catch(e){}return Object.assign({wordmark:true,bg:true,bar:true,viz:true},o);}
function fsApplyOpts(){
  const o=fsGetOpts();
  gid('opt-wordmark').checked=o.wordmark; gid('opt-bg').checked=o.bg; gid('opt-bar').checked=o.bar; gid('opt-viz').checked=o.viz;
  gid('fs-wordmark').style.display=o.wordmark?'':'none';
  gid('fs-bg').style.display=o.bg?'':'none';
  gid('fs-bar').style.display=o.bar?'':'none';
  gid('fs-viz').style.display=o.viz?'':'none';
  if(fsOpen && o.viz) startViz(); else stopViz();
}
function fsSaveOpts(){
  try{localStorage.setItem(FS_KEY,JSON.stringify({wordmark:gid('opt-wordmark').checked,bg:gid('opt-bg').checked,bar:gid('opt-bar').checked,viz:gid('opt-viz').checked}));}catch(e){}
  fsApplyOpts();
}
function toggleFsSettings(){ gid('fs-settings').classList.toggle('fs-hidden'); }
function fsSync(){
  gid('fs-title').textContent=gid('title').textContent;
  gid('fs-artist').textContent=gid('artist').textContent;
  const cov=gid('hero-cover').src;
  if(cov){const c=gid('fs-cover'); c.src=cov; c.style.visibility='';}
  gid('fs-bg').style.backgroundImage=gid('hero-bg').style.backgroundImage;
  gid('fs-prog').style.width=gid('prog').style.width;
  gid('fs-play').textContent=gid('player').paused?'▶':'⏸';
}
function openFs(){
  fsOpen=true; gid('fs').classList.remove('fs-hidden'); fsSync(); fsApplyOpts();
  const el=document.documentElement; if(el.requestFullscreen) el.requestFullscreen().catch(()=>{});
}
function closeFs(){
  fsOpen=false; gid('fs').classList.add('fs-hidden'); gid('fs-settings').classList.add('fs-hidden'); stopViz();
  if(document.fullscreenElement && document.exitFullscreen) document.exitFullscreen().catch(()=>{});
}
document.addEventListener('keydown',e=>{ if(e.key==='Escape'&&fsOpen) closeFs(); });
// Visualizer décoratif : n'accède PAS au flux audio (aucun risque de couper le
// stream Icecast cross-origin, aucune dépendance CORS). Barres animées, plus
// vives quand ça joue. Le vrai spectre exigerait du CORS sur Icecast (piste infra).
function startViz(){
  if(!vizBars.length){for(let i=0;i<48;i++)vizBars.push({p:Math.random()*6.28,s:0.5+Math.random()});}
  if(!vizRAF) drawViz();
}
function stopViz(){
  if(vizRAF){cancelAnimationFrame(vizRAF);vizRAF=0;}
  const c=gid('fs-viz'); if(c){const x=c.getContext('2d'); if(x)x.clearRect(0,0,c.width,c.height);}
}
function drawViz(){
  const c=gid('fs-viz'); if(!c){vizRAF=0;return;}
  const x=c.getContext('2d'); if(!x){vizRAF=0;return;}
  const w=c.width=c.clientWidth||1, h=c.height=c.clientHeight||1;
  const playing=!gid('player').paused;
  vizT+=playing?0.08:0.02;
  x.clearRect(0,0,w,h);
  const n=vizBars.length, bw=w/n;
  for(let i=0;i<n;i++){
    const b=vizBars[i];
    const amp=playing?(0.35+0.55*Math.abs(Math.sin(vizT*b.s+b.p))):(0.10+0.06*Math.sin(vizT*0.5+b.p));
    const bh=amp*h*0.9;
    const g=x.createLinearGradient(0,h,0,h-bh);
    g.addColorStop(0,'rgba(255,210,154,.10)'); g.addColorStop(1,'rgba(255,143,163,.6)');
    x.fillStyle=g; x.fillRect(i*bw+bw*0.15, h-bh, bw*0.7, bh);
  }
  vizRAF=requestAnimationFrame(drawViz);
}
(function(){const a=document.getElementById('player'),b=document.getElementById('playbtn'),fb=document.getElementById('fs-play');
 a.addEventListener('play',()=>{b.textContent='⏸'; if(fb)fb.textContent='⏸'; if('mediaSession' in navigator)navigator.mediaSession.playbackState='playing';});
 a.addEventListener('pause',()=>{b.textContent='▶'; if(fb)fb.textContent='▶'; if('mediaSession' in navigator)navigator.mediaSession.playbackState='paused';});
 ['opt-wordmark','opt-bg','opt-bar','opt-viz'].forEach(id=>{const e=gid(id); if(e)e.addEventListener('change',fsSaveOpts);});
 if('mediaSession' in navigator){
   navigator.mediaSession.setActionHandler('play',togglePlay);
   navigator.mediaSession.setActionHandler('pause',stopStream);
 }})();
refresh();loadEvents();
loadVisuals();loadAuth();
setInterval(refresh,10000);setInterval(loadEvents,30000);setInterval(loadVisuals,300000);
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return PAGE


@app.get("/equipe", response_class=HTMLResponse)
def equipe():
    if not TEAM_HTML:
        return RedirectResponse("/", status_code=302)
    # Lien retour discret vers la radio, injecté en fin de page.
    back = ('<a href="/" style="position:fixed;top:16px;left:18px;z-index:99;'
            'color:#fff4e6;background:rgba(0,0,0,.28);border:1px solid rgba(255,244,230,.35);'
            'border-radius:20px;padding:7px 14px;font:13px Helvetica,Arial,sans-serif;'
            'text-decoration:none;backdrop-filter:blur(3px)">← Retour à la radio</a>')
    return HTMLResponse(TEAM_HTML.replace("</body>", back + "</body>", 1))


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8099)
