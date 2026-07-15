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
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, Response
import secrets, hmac, hashlib, base64

# Page « L'équipe » (maquette Cassy, avatars embarqués). Import guardé : si le
# module manque, le site reste debout, seul /equipe est indisponible.
try:
    from team_page import HTML as TEAM_HTML
except Exception:
    TEAM_HTML = None

DB_URL      = os.environ["DATABASE_URL"]
TRACK_URL   = os.environ.get("GCS_TRACK_URL",        "http://gcs-track-service:8090")
# Fuseau du festival : le conteneur tourne en UTC, mais le journal doit afficher
# l'heure de Toulon (sinon « 05:19 » alors qu'il est 07:19 sur place).
try:
    from zoneinfo import ZoneInfo
    _LOCAL_TZ = ZoneInfo(os.environ.get("LORE_TZ", "Europe/Paris"))
except Exception:
    import datetime as _dt
    _LOCAL_TZ = _dt.timezone.utc

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

    # Métriques issues de la base (1 seule connexion / 1 requête)
    db = {}
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                  (SELECT count(*) FROM tracks)          AS tracks,
                  (SELECT count(*) FROM votes)           AS votes,
                  (SELECT count(*) FROM lore_events)     AS lore,
                  (SELECT count(*) FROM title_proposals) AS proposals,
                  (SELECT count(*) FROM play_history WHERE played_at > now()-interval '1 hour') AS plays_1h,
                  (SELECT extract(epoch FROM now()-max(played_at)) FROM play_history)   AS last_play_age,
                  (SELECT chars_used  FROM el_monthly_quota ORDER BY month DESC LIMIT 1) AS el_used,
                  (SELECT chars_limit FROM el_monthly_quota ORDER BY month DESC LIMIT 1) AS el_limit,
                  (SELECT energy_level FROM gcs_state ORDER BY updated_at DESC LIMIT 1) AS energy,
                  (SELECT extract(epoch FROM now()-updated_at)
                     FROM gcs_state ORDER BY updated_at DESC LIMIT 1)                   AS state_age
            """)
            db = cur.fetchone() or {}
        conn.close()
    except Exception:
        pass

    def num(v, default=-1):
        try:
            return round(float(v), 3) if v is not None else default
        except Exception:
            return default

    emit("gaiverland_tracks_total", "Titres analyses en base (-1 = DB injoignable)", "gauge",
         [f"gaiverland_tracks_total {num(db.get('tracks'))}"])
    emit("gaiverland_votes_total", "Votes enregistres", "gauge",
         [f"gaiverland_votes_total {num(db.get('votes'))}"])
    emit("gaiverland_lore_events_total", "Evenements de lore generes", "gauge",
         [f"gaiverland_lore_events_total {num(db.get('lore'))}"])
    emit("gaiverland_proposals_total", "Propositions de titres recues", "gauge",
         [f"gaiverland_proposals_total {num(db.get('proposals'))}"])
    emit("gaiverland_plays_last_hour", "Titres joues dans la derniere heure (activite antenne)", "gauge",
         [f"gaiverland_plays_last_hour {num(db.get('plays_1h'))}"])
    emit("gaiverland_last_play_age_seconds", "Secondes depuis le dernier titre joue (heartbeat antenne : haut = probleme)", "gauge",
         [f"gaiverland_last_play_age_seconds {num(db.get('last_play_age'))}"])
    emit("gaiverland_state_age_seconds", "Secondes depuis la derniere maj du state-engine (heartbeat pipeline)", "gauge",
         [f"gaiverland_state_age_seconds {num(db.get('state_age'))}"])
    emit("gaiverland_state_energy", "Energie courante de l'antenne (echelle moteur d'etat)", "gauge",
         [f"gaiverland_state_energy {num(db.get('energy'))}"])

    # ElevenLabs : garde-fou credits (ratio restant → alerte <0.15, critique <0.02)
    el_used, el_limit = db.get("el_used"), db.get("el_limit")
    emit("gaiverland_elevenlabs_chars_used", "Caracteres ElevenLabs consommes ce mois", "gauge",
         [f"gaiverland_elevenlabs_chars_used {num(el_used)}"])
    emit("gaiverland_elevenlabs_chars_limit", "Quota mensuel ElevenLabs", "gauge",
         [f"gaiverland_elevenlabs_chars_limit {num(el_limit)}"])
    if el_used is not None and el_limit:
        remaining = round(max(0.0, 1.0 - float(el_used) / float(el_limit)), 4)
        emit("gaiverland_elevenlabs_remaining_ratio", "Fraction de credits ElevenLabs restants (0-1)", "gauge",
             [f"gaiverland_elevenlabs_remaining_ratio {remaining}"])

    emit("gaiverland_up", "Endpoint metriques Gaiverland joignable", "gauge", ["gaiverland_up 1"])
    return PlainTextResponse("\n".join(lines) + "\n")


@app.get("/api/live")
def live(station: str = "main"):
    az_sid = STATIONS.get(station, AZ_STATION)
    out = {"track": {}, "state": {}, "station": station,
           "stream_url": STREAM_URL if station == "main" else "", "art": "", "listeners": 0,
           # Heure serveur : le front s'en sert pour caler son horloge sur celle d'AzuraCast
           # (sinon une horloge locale décalée fausse tout le calcul de synchro).
           "server_time": time.time()}
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
                # Horodatage ABSOLU du début du morceau. `elapsed` est inexploitable pour
                # se synchroniser : l'API nowplaying d'AzuraCast est mise en cache (~15 s,
                # observé figé sur 3 sondages), donc `elapsed` avance par paliers. `played_at`,
                # lui, est une date fixe → le front peut calculer la position réelle au
                # centième près et afficher le morceau que l'auditeur ENTEND (cf. front).
                "played_at": np.get("now_playing", {}).get("played_at", 0),
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
                "home_city":    s.get("home_city", "Toulon"),
                "is_miniscene": bool(s.get("is_miniscene", False)),
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
    # Heure affichée = heure LOCALE du festival (Europe/Paris), pas l'UTC du serveur
    # (created_at est un timestamptz tz-aware → astimezone convertit proprement).
    return {"events": [
        {"type": r["type"], "text": r["description"], "city": r["city"],
         "at": r["created_at"].astimezone(_LOCAL_TZ).strftime("%H:%M")} for r in reversed(rows)
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

# Empreintes fondateur (sha256("provider:sub")[:32]) → bouton blacklist + « passer » immédiat.
# Miroir de gcs_vote_service.FOUNDER_IDS (même env FOUNDER_IDS).
FOUNDER_IDS = {f.strip() for f in os.environ.get("FOUNDER_IDS",
    "e49e9b4f66961841181c2fa7751fdabc,bc29f0601314241ebd7a6974a8541f88,2194b5b1bd76539a5aac0dd5fb314f25"
    ).split(",") if f.strip()}

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
    return {"logged_in": bool(ident), "provider": ident.split(":", 1)[0] if ident else "",
            "founder": _uid(request) in FOUNDER_IDS}

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


@app.post("/api/pass")
def api_pass(request: Request):
    """« Passer » le titre en cours : fondateur = immédiat, public = démocratique (côté vote-service)."""
    uid = _uid(request)
    if not uid:
        return {"ok": False, "error": "login requis", "need_login": True}
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
        r = httpx.post(f"{VOTE_URL}/pass", json={"song_id": song_id, "user_id": uid}, timeout=6)
        if r.status_code == 200:
            return {"ok": True, **r.json()}
        return {"ok": False, "error": f"vote-service {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


@app.post("/api/blacklist")
def api_blacklist(request: Request):
    """Bannir DÉFINITIVEMENT le titre en cours de la mainstage. Réservé au fondateur."""
    uid = _uid(request)
    if not uid:
        return {"ok": False, "error": "login requis", "need_login": True}
    if uid not in FOUNDER_IDS:
        return {"ok": False, "error": "réservé au chef"}
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
        r = httpx.post(f"{VOTE_URL}/blacklist", json={"song_id": song_id, "user_id": uid}, timeout=6)
        if r.status_code == 200:
            return {"ok": True, **r.json()}
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


@app.get("/api/loved")
def loved(limit: int = 10):
    """Coups de cœur communauté : titres au score ENCORE net positif (les plus soutenus)."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.title, t.artist, ts.score, ts.vote_count
                FROM track_scores ts JOIN tracks t ON t.song_id = ts.song_id
                WHERE ts.score > 0 AND t.title IS NOT NULL
                ORDER BY ts.score DESC, ts.vote_count DESC
                LIMIT %s
            """, (min(limit, 30),))
            rows = cur.fetchall()
        conn.close()
        return {"loved": [{"title": r["title"], "artist": r["artist"] or "",
                           "score": round(float(r["score"]), 2), "votes": r["vote_count"]} for r in rows]}
    except Exception:
        return {"loved": []}


PAGE = """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gaiverland — Le festival permanent</title>
<meta name="theme-color" content="#8b5cf6">
<meta name="description" content="La radio-festival permanente. Une antenne qui ne dort jamais.">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Gaiverland">
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<link rel="apple-touch-icon" href="/icon.svg">
<link rel="manifest" href="/manifest.webmanifest">
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
.v-review{background:linear-gradient(135deg,#e6ddc9,#c9bfa6)}
.v-skip{background:linear-gradient(135deg,#ffb1c0,#ff8fa3)}
.v-pass{background:linear-gradient(135deg,#bfe0ff,#8fc4ff)}
.v-blacklist{background:linear-gradient(135deg,#5a5a5a,#2e2e2e);color:#ffe;font-weight:700}
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
/* Bouton « copier le lien mp3 » : coin de la tuile, discret, se révèle au survol.
   Toujours visible au doigt (pas de :hover sur mobile) → opacité de base non nulle. */
.cp{position:absolute;top:6px;right:6px;border:0;border-radius:8px;cursor:pointer;
    background:rgba(255,244,230,.12);color:#fff4e6;font-size:12px;line-height:1;
    padding:5px 6px;opacity:.35;transition:opacity .15s,background .15s}
.stage:hover .cp{opacity:.9}
.cp:hover{background:rgba(255,244,230,.28)}
.cp.ok{opacity:1;background:#2ecc71;color:#08210f}
@media(hover:none){.cp{opacity:.75}}
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
.ms-badge{display:inline-block;background:linear-gradient(90deg,#ff8a3d,#ff3b5c);color:#fff;
  font-size:12px;font-weight:700;letter-spacing:.03em;padding:3px 9px;border-radius:999px;
  margin-bottom:5px;box-shadow:0 2px 8px rgba(255,59,92,.35)}
.journal .ev{display:flex;gap:12px;padding:10px 0;border-bottom:1px solid rgba(255,244,230,.12);
  font-size:15px;align-items:baseline}
.journal .ev:last-child{border:none}
.journal .at{font-family:sans-serif;font-size:11px;opacity:.6;min-width:42px}
.journal .ic{min-width:24px}
.soon{opacity:.75;text-align:center;padding:26px 10px;font-style:italic;font-size:16px}
.loved-row{display:flex;align-items:center;gap:12px;padding:9px 0;border-bottom:1px solid rgba(255,244,230,.1)}
.loved-row:last-child{border-bottom:none}
.loved-rank{flex:0 0 auto;width:24px;height:24px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;background:linear-gradient(120deg,var(--sun2),var(--sun1));color:#2a1a33}
.loved-t{flex:1;font-size:15px;line-height:1.3}
.loved-fire{flex:0 0 auto;opacity:.85;font-size:14px;white-space:nowrap}
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
/* ── Contrôles audio (Web Audio) ── */
.fxbar{display:flex;align-items:center;gap:10px;margin:14px 0 0}
.fx-ico{font-size:16px;opacity:.85}
.fx-vol{flex:1;height:5px;border-radius:4px;-webkit-appearance:none;appearance:none;background:rgba(255,244,230,.22);cursor:pointer}
.fx-vol::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;border-radius:50%;background:var(--sun1);cursor:pointer;box-shadow:0 0 8px rgba(255,154,90,.6)}
.fx-vol::-moz-range-thumb{width:16px;height:16px;border:none;border-radius:50%;background:var(--sun1);cursor:pointer}
.fx-toggle{flex:0 0 auto;border:1px solid rgba(255,244,230,.28);background:transparent;color:var(--cream);border-radius:16px;padding:6px 12px;font:13px inherit;cursor:pointer;transition:background .15s}
.fx-toggle:hover,.fx-toggle.on{background:rgba(255,244,230,.14)}
.fx-panel{margin-top:12px;padding:14px;border-radius:14px;background:rgba(25,16,54,.5);border:1px solid rgba(255,244,230,.12)}
.fx-panel.fx-hidden{display:none}
.fx-lab{font-size:11px;letter-spacing:1.5px;text-transform:uppercase;opacity:.65;margin:10px 0 6px}
.fx-lab:first-child{margin-top:0}
.fx-presets,.fx-viz{display:flex;flex-wrap:wrap;gap:7px}
.fxb{border:1px solid rgba(255,244,230,.24);background:transparent;color:var(--cream);border-radius:14px;padding:6px 12px;font:12px inherit;cursor:pointer;transition:border-color .15s,background .15s}
.fxb:hover{border-color:var(--sun1)}
.fxb.active{background:linear-gradient(120deg,var(--sun2),var(--sun1));border-color:transparent;color:#2a1a33;font-weight:600}
.fx-range{width:100%;height:5px;border-radius:4px;-webkit-appearance:none;appearance:none;background:rgba(255,244,230,.22);cursor:pointer;margin:2px 0 4px}
.fx-range::-webkit-slider-thumb{-webkit-appearance:none;width:15px;height:15px;border-radius:50%;background:var(--sun1);cursor:pointer}
.fx-range::-moz-range-thumb{width:15px;height:15px;border:none;border-radius:50%;background:var(--sun1)}
.fx-checks{display:flex;flex-direction:column;gap:8px;margin:4px 0}
.fx-checks label{display:flex;align-items:center;gap:9px;cursor:pointer;font-size:14px}
.fx-checks input{width:16px;height:16px;accent-color:var(--sun2)}
.fx-checks span{opacity:.55;font-size:12px}
.fx-note{font-size:12px;opacity:.6;margin-top:10px;line-height:1.4}
.fs-vol{display:flex;align-items:center;gap:9px}
.main-viz{width:100%;height:56px;display:block;margin:14px 0 2px;border-radius:10px;background:rgba(25,16,54,.25)}
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
  <audio id="player-b" preload="none"></audio>
  <canvas id="main-viz" class="main-viz"></canvas>
  <div class="fxbar">
    <span class="fx-ico" aria-hidden="true">🔊</span>
    <input class="fx-vol js-vol" type="range" min="0" max="100" value="100" aria-label="Volume">
    <button class="fx-toggle" onclick="toggleFxPanel()" aria-label="Effets audio">⚙ effets</button>
  </div>
  <div id="fx-panel" class="fx-panel fx-hidden">
    <div class="fx-lab">Ambiance</div>
    <div class="fx-presets js-presets"></div>
    <div class="fx-lab">Basses <span class="js-bassval">0</span> dB</div>
    <input class="fx-range js-bass" type="range" min="0" max="12" step="1" value="0" aria-label="Bass boost">
    <div class="fx-checks">
      <label><input type="checkbox" class="js-keepbass"> <b>Keep-bass</b> <span>baisse sans étouffer</span></label>
      <label><input type="checkbox" class="js-loud"> <b>Loudness</b> <span>niveau constant</span></label>
      <label><input type="checkbox" class="js-mono"> <b>Mono</b> <span>1 seul HP</span></label>
    </div>
    <div class="fx-lab">Visualizer</div>
    <div class="fx-viz js-vizstyle"></div>
    <div class="fx-lab">Minuterie sommeil 😴</div>
    <div class="fx-viz js-sleep"></div>
    <div class="fx-note js-sleepstatus" style="margin-top:6px"></div>
    <div class="fx-note js-fxnote"></div>
  </div>
  <div class="authbar" id="authbar"></div>
  <div class="votes">
    <button class="v-encore" onclick="vote('ENCORE')">🔥 j'adore</button>
    <button class="v-review" onclick="vote('REVIEW')">😐 bof</button>
    <button class="v-skip"   onclick="vote('SKIP')">👎 j'aime pas</button>
    <button class="v-pass"   onclick="passTrack()">⏭ passer</button>
    <button class="v-blacklist" id="btnBlacklist" style="display:none" onclick="blacklistTrack()">🚫 blacklist</button>
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
      <div class="ms-badge" id="ms-badge" hidden>🚐 Mini-scène</div>
      <div class="cn" id="city">…</div>
      <div class="wx" id="wx"></div>
    </div>
    <div class="next" id="tour-next">prochaine ville :<br>le convoi décidera. 🚐</div>
  </div>
</div>

<div class="card">
  <h2>Les scènes</h2>
  <div class="stages">
    <div class="stage live on" data-st="main" onclick="selectStation('main')"><div class="ico">🎪</div><div class="nm">Mainstage</div><div class="st">Live</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'main')">🔗</button></div>
    <div class="stage live" data-st="chill" onclick="selectStation('chill')"><div class="ico">🌙</div><div class="nm">Chill</div><div class="st">Live</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'chill')">🔗</button></div>
    <div class="stage live" data-st="hard" onclick="selectStation('hard')"><div class="ico">🔥</div><div class="nm">Hard</div><div class="st">Live</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'hard')">🔗</button></div>
    <div class="stage live" data-st="phonk" onclick="selectStation('phonk')"><div class="ico">🏎️</div><div class="nm">Phonk</div><div class="st">Live</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'phonk')">🔗</button></div>
    <div class="stage live" data-st="lofi" onclick="selectStation('lofi')"><div class="ico">🎧</div><div class="nm">Lofi</div><div class="st">Live</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'lofi')">🔗</button></div>
    <div class="stage live" data-st="synthwave" onclick="selectStation('synthwave')"><div class="ico">🌆</div><div class="nm">Synthwave</div><div class="st">Live</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'synthwave')">🔗</button></div>
  </div>
</div>

<div class="card">
  <h2>Coups de cœur de la communauté ❤️</h2>
  <div id="loved"><div class="soon">Les titres que vous soutenez le plus (🔥 ENCORE) apparaîtront ici…</div></div>
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
    <div class="fs-set-title" style="margin-top:12px">Son</div>
    <div class="fs-vol"><span aria-hidden="true">🔊</span><input class="fx-vol js-vol" type="range" min="0" max="100" value="100" aria-label="Volume"></div>
    <div class="fx-presets js-presets" style="margin:6px 0"></div>
    <div class="fx-viz js-vizstyle"></div>
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
let curStation='main';
// 2 éléments audio : A routé Web Audio (FX+viz réel, Mainstage same-origin /live.mp3),
// B direct (scènes cross-origin — Web Audio les muterait sans header CORS).
const A=document.getElementById('player'), B=document.getElementById('player-b');
// Kade a posé un lien mp3 same-origin par station → Web Audio lisible PARTOUT.
const ROUTE={main:'/live.mp3',chill:'/chill.mp3',hard:'/hard.mp3',phonk:'/phonk.mp3',lofi:'/lofi.mp3',synthwave:'/synthwave.mp3'};
function waOK(st){ return !!ROUTE[st]; }             // toutes les stations = same-origin

// ── Copier le lien mp3 d'une station (VLC, tel, partage) ──
// stopPropagation : le bouton est DANS la tuile, sans ça un clic changerait de station.
// clipboard.writeText exige HTTPS (ou localhost) → repli execCommand pour les autres cas.
function copyLink(ev, st){
  ev.stopPropagation();
  const url = location.origin + (ROUTE[st] || '');
  const btn = ev.currentTarget;
  const done = ok => {
    btn.textContent = ok ? '✓' : '✕';
    btn.classList.toggle('ok', ok);
    setTimeout(() => { btn.textContent = '🔗'; btn.classList.remove('ok'); }, 1400);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(url).then(() => done(true), () => done(false));
  } else {
    const ta = document.createElement('textarea');
    ta.value = url; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    done(ok);
  }
}
function AA(){ return waOK(curStation)?A:B; }        // élément actif (A = Web Audio)

// ── Moteur audio (Web Audio) ──
const FX_KEY='gvl_fx';
const PRESETS=[['flat','Flat'],['bassboost','Bass boost'],['keepbass','Keep-bass'],['night','Nuit'],['clarity','Clarté'],['club','Club']];
const VIZS=[['bars','Barres'],['radial','Radial'],['wave','Onde']];
const AFX={
  ctx:null,src:null,n:null,an:null,freq:null,tim:null,ready:false,
  o:Object.assign({vol:1,preset:'flat',bass:0,keepbass:false,loud:false,mono:false,viz:'bars'},
     (function(){try{return JSON.parse(localStorage.getItem(FX_KEY))||{};}catch(e){return {};}})()),
  save(){try{localStorage.setItem(FX_KEY,JSON.stringify(this.o));}catch(e){}},
  setup(){
    if(this.ready) return true;
    try{
      const C=window.AudioContext||window.webkitAudioContext; if(!C) return false;
      this.ctx=new C();
      this.src=this.ctx.createMediaElementSource(A);
      const c=this.ctx;
      const bass=c.createBiquadFilter(); bass.type='lowshelf'; bass.frequency.value=115;
      const mid=c.createBiquadFilter(); mid.type='peaking'; mid.frequency.value=2300; mid.Q.value=0.9;
      const treb=c.createBiquadFilter(); treb.type='highshelf'; treb.frequency.value=6200;
      const comp=c.createDynamicsCompressor();
      const mono=c.createGain();
      const master=c.createGain();
      const an=c.createAnalyser(); an.fftSize=512; an.smoothingTimeConstant=0.82;
      this.src.connect(bass); bass.connect(mid); mid.connect(treb); treb.connect(comp);
      comp.connect(mono); mono.connect(master); master.connect(an); an.connect(c.destination);
      this.n={bass:bass,mid:mid,treb:treb,comp:comp,mono:mono,master:master}; this.an=an;
      this.freq=new Uint8Array(an.frequencyBinCount); this.tim=new Uint8Array(an.fftSize);
      A.volume=1; this.ready=true; this.apply();
      return true;
    }catch(e){ return false; }
  },
  apply(){
    const o=this.o;
    B.volume=o.vol;
    if(!this.ready){ A.volume=o.vol; return; }
    const n=this.n; let bG=0,mG=0,tG=0,comp=false;
    if(o.preset==='bassboost') bG=9;
    else if(o.preset==='night'){ tG=-7; bG=2; }
    else if(o.preset==='clarity'){ mG=5; tG=2; }
    else if(o.preset==='club'){ bG=6; tG=2; comp=true; }
    bG+=o.bass;
    if(o.keepbass||o.preset==='keepbass'){ bG+=(1-o.vol)*13; tG+=(1-o.vol)*3; }  // compensation de sonie
    n.bass.gain.value=bG; n.mid.gain.value=mG; n.treb.gain.value=tG;
    const uc=comp||o.loud;
    n.comp.threshold.value=uc?-22:0; n.comp.ratio.value=uc?4:1; n.comp.knee.value=uc?26:0;
    n.comp.attack.value=0.003; n.comp.release.value=0.25;
    n.mono.channelCount=o.mono?1:2; n.mono.channelCountMode=o.mono?'explicit':'max'; n.mono.channelInterpretation='speakers';
    A.volume=1; n.master.gain.value=o.vol;
  },
  set(k,v){ this.o[k]=v; this.save(); this.apply(); },
  resume(){ if(this.ctx&&this.ctx.state==='suspended') this.ctx.resume().catch(function(){}); }
};

function playLive(){
  const a=AA();
  if(!audioUrl) return;
  a.src = audioUrl + (audioUrl.indexOf('?')>=0?'&':'?') + '_=' + Date.now();
  a.load();
  if(a===A){ AFX.setup(); AFX.resume(); }
  a.play().catch(function(){});
}
function stopStream(){ const a=AA(); a.pause(); a.removeAttribute('src'); a.load(); }
function togglePlay(){ const a=AA(); if(a.paused){ playLive(); } else { stopStream(); } }
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
  const wasPlaying=!AA().paused;
  A.pause(); A.removeAttribute('src'); A.load();     // couper les deux éléments
  B.pause(); B.removeAttribute('src'); B.load();
  curStation=s;
  document.querySelectorAll('.stage[data-st]').forEach(t=>t.classList.toggle('on', t.dataset.st===s));
  audioUrl=''; lastTitle=''; npHist=[];   // autre scène = autre timeline : on repart à zéro
  updateFxAvail();
  refresh().then(()=>{ if(wasPlaying) playLive(); });
}
// ── Synchro affichage ⇄ oreille ──────────────────────────────────────────────
// Le site affichait le DIRECT (le morceau que le serveur joue à l'instant T), alors que
// l'auditeur entend le flux EN RETARD : burst Icecast (2,7 s à 192 kbps) + tampon du
// navigateur (variable, plusieurs secondes). D'où le titre qui ne colle pas à l'oreille.
// On affiche donc le morceau RÉELLEMENT ENTENDU :
//     instant écouté (horloge serveur) = maintenant − latence audio mesurée
// puis on cherche le morceau dont la fenêtre [played_at, played_at+durée) le contient.
const BURST_S = 2.7;        // burst Icecast : 65535 octets à 192 kbps
let clockOff = 0;           // horloge serveur − horloge locale (corrige un PC mal réglé)
let npHist = [];            // derniers morceaux vus (fenêtre glissante)

function serverNow(){ return Date.now()/1000 + clockOff; }

// Latence = ce que le navigateur garde en avance + ce que le serveur a envoyé d'un bloc.
// À l'arrêt, aucune latence : on affiche le direct.
function audioLag(){
  const a = AA();
  if(!a || a.paused) return 0;
  let ahead = 0;
  try{
    if(a.buffered && a.buffered.length)
      ahead = Math.max(0, a.buffered.end(a.buffered.length-1) - a.currentTime);
  }catch(e){}
  return Math.min(60, ahead + BURST_S);   // borne haute : jamais de dérive absurde
}

// Le morceau que l'auditeur entend maintenant (et non celui du direct).
function heardTrack(live){
  const pos = serverNow() - audioLag();
  for(let i=npHist.length-1; i>=0; i--){
    const e = npHist[i];
    if(!e.played_at) continue;
    const end = e.duration>0 ? e.played_at + e.duration : Infinity;
    if(e.played_at <= pos && pos < end) return e;
  }
  for(let i=npHist.length-1; i>=0; i--)         // sinon : le dernier commencé avant
    if(npHist[i].played_at && npHist[i].played_at <= pos) return npHist[i];
  return live;                                   // pas d'historique → direct
}

// Repeint le titre depuis le modèle local, sans requête réseau (appelé chaque seconde) :
// la bascule tombe pile au moment où l'auditeur entend le changement.
function paintTrack(live, art){
  const t = heardTrack(live) || {};
  const cover = t.art || art || '';
  document.getElementById('title').textContent=t.title||'Gaiverland Radio';
  document.getElementById('artist').textContent=t.artist||'';
  if(cover) document.getElementById('hero-cover').src=cover;
  if(t.title && t.title!==lastTitle){ lastTitle=t.title; bgIdx++; setBg(); }  // nouveau fond par morceau
  if('mediaSession' in navigator && (t.title||t.artist)){
    navigator.mediaSession.metadata=new MediaMetadata({
      title:t.title||'Gaiverland Radio', artist:t.artist||'Gaiverland Radio',
      album:'Gaiverland — le festival permanent',
      artwork:cover?[{src:cover,sizes:'512x512',type:'image/jpeg'}]:[]
    });
  }
  // Progression calculée sur l'instant ENTENDU (played_at est fiable, elapsed est caché).
  if(t.duration>0 && t.played_at){
    const el=Math.max(0,Math.min(t.duration, serverNow()-audioLag()-t.played_at));
    document.getElementById('prog').style.width=(100*el/t.duration)+'%';
  } else if(t.duration>0 && t.elapsed>=0){
    document.getElementById('prog').style.width=Math.min(100,100*t.elapsed/t.duration)+'%';
  }
}

let lastLive={}, lastArt='';
async function refresh(){
  try{
    const d=await (await fetch('/api/live?station='+curStation)).json();
    const t=d.track||{};
    if(d.server_time) clockOff = d.server_time - Date.now()/1000;
    // Historique : un morceau n'entre qu'une fois (l'API est sondée plus souvent qu'elle ne change).
    if(t.song_id){
      const last=npHist[npHist.length-1];
      if(!last || last.song_id!==t.song_id){
        npHist.push(Object.assign({}, t, {art:d.art||''}));
        if(npHist.length>6) npHist.shift();
      }
    }
    lastLive=t; lastArt=d.art||'';
    paintTrack(t, d.art);
    const l=d.listeners?d.listeners+' personne(s) dans la foule':'';
    document.getElementById('meta').textContent=l;
    audioUrl = ROUTE[curStation] || (d.stream_url||'');  // flux same-origin par station (Web Audio partout)
    const s=d.state||{};
    // Mini-scène : le festival est en déplacement dans une ville proche → on l'affiche
    // clairement (« Mini-scène de Marseille ») ; sinon la ville-mère normale.
    const ms=!!s.is_miniscene, home=s.home_city||'Toulon';
    document.getElementById('city').textContent = ms ? ('Mini-scène de '+(s.city||'?')) : (s.city||'Quelque part');
    const badge=document.getElementById('ms-badge'); if(badge) badge.hidden=!ms;
    const nx=document.getElementById('tour-next');
    if(nx) nx.innerHTML = ms ? ('de retour à '+home+' bientôt 🚐') : 'prochaine ville :<br>le convoi décidera. 🚐';
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
async function loadLoved(){
  try{
    const d=await (await fetch('/api/loved')).json();
    if(!d.loved||!d.loved.length) return;
    document.getElementById('loved').innerHTML=d.loved.map((t,i)=>
      '<div class="loved-row"><span class="loved-rank">'+(i+1)+'</span>'+
      '<span class="loved-t">'+(t.artist?t.artist.replace(/</g,'&lt;')+' — ':'')+t.title.replace(/</g,'&lt;')+'</span>'+
      '<span class="loved-fire">🔥 '+t.votes+'</span></div>').join('');
  }catch(e){}
}
const VLABEL={ENCORE:"j'adore",REVIEW:"bof",SKIP:"j'aime pas"};
async function vote(v){
  const m=document.getElementById('votemsg');
  try{
    const r=await (await fetch('/api/vote',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({vote:v})})).json();
    if(r.need_login){ m.textContent='Connecte-toi pour voter 👇'; return; }
    m.textContent=r.ok?'« '+(VLABEL[v]||v)+' » enregistré. Le festival vous a entendu. ✦'
                       :'Hmm… '+(r.error||'réessayez');
  }catch(e){m.textContent='Le stagiaire a débranché quelque chose. Réessayez.';}
  setTimeout(()=>m.textContent='',6000);
}
async function passTrack(){
  const m=document.getElementById('votemsg');
  try{
    const r=await (await fetch('/api/pass',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({})})).json();
    if(r.need_login){ m.textContent='Connecte-toi pour passer un titre 👇'; return; }
    if(r.skipped){ m.textContent=r.founder?'Titre passé. ⏭':'Assez de monde a voté : titre passé ! ⏭'; }
    else if(r.reason && r.reason.indexOf('pépite')>=0){ m.textContent='Titre protégé (coup de cœur du chef) — pas passé.'; }
    else if(r.ok){ m.textContent='« Passer » noté'+(r.needed?' ('+(r.passes||1)+'/'+r.needed+' pour sauter)':'')+'. ⏭'; }
    else { m.textContent='Hmm… '+(r.error||'réessayez'); }
  }catch(e){ m.textContent='Le stagiaire a débranché quelque chose. Réessayez.'; }
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
    const bl=document.getElementById('btnBlacklist');
    if(bl) bl.style.display = d.founder ? '' : 'none';
  }catch(e){}
}
async function blacklistTrack(){
  const m=document.getElementById('votemsg');
  if(!confirm('Bannir DÉFINITIVEMENT ce titre de la mainstage ?')) return;
  try{
    const r=await (await fetch('/api/blacklist',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({})})).json();
    if(r.need_login){ m.textContent='Connecte-toi 👇'; return; }
    m.textContent=r.ok?('🚫 Banni de la mainstage'+(r.skipped?' + titre passé.':'.')):('Hmm… '+(r.error||'réessaye'));
  }catch(e){ m.textContent='Le stagiaire a débranché quelque chose. Réessaye.'; }
  setTimeout(()=>m.textContent='',6000);
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
// Visualizer RÉEL : lit le spectre via l'AnalyserNode (Mainstage /live.mp3 same-origin).
// Sur une scène (pas de Web Audio), retombe sur une animation décorative douce.
function startViz(){
  if(!vizBars.length){for(let i=0;i<48;i++)vizBars.push({p:Math.random()*6.28,s:0.5+Math.random()});}
  if(!vizRAF) drawViz();
}
function stopViz(){
  if(vizRAF){cancelAnimationFrame(vizRAF);vizRAF=0;}
  const c=gid('fs-viz'); if(c){const x=c.getContext('2d'); if(x)x.clearRect(0,0,c.width,c.height);}
  const hc=gid('hero-cover'); if(hc) hc.style.transform='';
}
function bassEnergy(){
  if(!AFX.ready||AA()!==A||A.paused) return 0;
  AFX.an.getByteFrequencyData(AFX.freq);
  let s=0; for(let i=0;i<6;i++)s+=AFX.freq[i];
  return (s/6)/255;
}
function paintViz(c){
  const x=c&&c.getContext('2d'); if(!x) return;
  const w=c.width=c.clientWidth||1, h=c.height=c.clientHeight||1; x.clearRect(0,0,w,h);
  const real=AFX.ready && AA()===A && !A.paused, style=AFX.o.viz||'bars';
  if(real && style==='wave'){
    AFX.an.getByteTimeDomainData(AFX.tim); const nn=AFX.tim.length;
    x.lineWidth=2.5; x.strokeStyle='rgba(255,143,163,.85)'; x.beginPath();
    for(let i=0;i<nn;i++){const xx=i/nn*w, yy=h/2+((AFX.tim[i]-128)/128)*h*0.42; i?x.lineTo(xx,yy):x.moveTo(xx,yy);}
    x.stroke();
  }else if(real && style==='radial'){
    AFX.an.getByteFrequencyData(AFX.freq); const N=64, cx=w/2, cy=h/2, r0=Math.min(w,h)*0.16;
    for(let i=0;i<N;i++){const v=AFX.freq[i*2]/255, a2=i/N*6.283, len=r0+v*Math.min(w,h)*0.30;
      x.strokeStyle='rgba(255,'+((120+100*v)|0)+','+((120+40*v)|0)+','+(0.35+0.6*v)+')'; x.lineWidth=3;
      x.beginPath(); x.moveTo(cx+Math.cos(a2)*r0,cy+Math.sin(a2)*r0); x.lineTo(cx+Math.cos(a2)*len,cy+Math.sin(a2)*len); x.stroke();}
  }else if(real){
    AFX.an.getByteFrequencyData(AFX.freq); const N=64, bw=w/N;
    for(let i=0;i<N;i++){const v=AFX.freq[i*2]/255, bh=v*h*0.92;
      const g=x.createLinearGradient(0,h,0,h-bh); g.addColorStop(0,'rgba(255,210,154,.15)'); g.addColorStop(1,'rgba(255,143,163,.78)');
      x.fillStyle=g; x.fillRect(i*bw+bw*0.15, h-bh, bw*0.7, bh);}
  }else{ // fallback décoratif (pas de Web Audio)
    const playing=!AA().paused; vizT+=playing?0.08:0.02; const n=vizBars.length, bw=w/n;
    for(let i=0;i<n;i++){const b=vizBars[i], amp=playing?(0.30+0.45*Math.abs(Math.sin(vizT*b.s+b.p))):(0.10+0.06*Math.sin(vizT*0.5+b.p)), bh=amp*h*0.9;
      const g=x.createLinearGradient(0,h,0,h-bh); g.addColorStop(0,'rgba(255,210,154,.10)'); g.addColorStop(1,'rgba(255,143,163,.5)');
      x.fillStyle=g; x.fillRect(i*bw+bw*0.15, h-bh, bw*0.7, bh);}
  }
}
function drawViz(){
  const showFs = fsOpen && gid('opt-viz') && gid('opt-viz').checked;
  const playing = !AA().paused;
  const mv=gid('main-viz');
  if(!showFs && !playing){ vizRAF=0;
    if(mv){const mx=mv.getContext('2d'); if(mx)mx.clearRect(0,0,mv.width,mv.height);}
    const hc=gid('hero-cover'); if(hc)hc.style.transform=''; return; }
  if(playing && mv) paintViz(mv);      // visualizer DANS le player
  if(showFs) paintViz(gid('fs-viz'));   // visualizer plein écran
  const be=bassEnergy();
  if(be>0){ const sc='scale('+(1+be*0.06).toFixed(3)+')';
    const hc=gid('hero-cover'); if(hc) hc.style.transform=sc;
    const fc=gid('fs-cover'); if(fc&&fsOpen) fc.style.transform=sc; }
  vizRAF=requestAnimationFrame(drawViz);
}
// ── UI des effets audio ──
function toggleFxPanel(){ const p=gid('fx-panel'); if(!p)return; p.classList.toggle('fx-hidden');
  document.querySelectorAll('.fx-toggle').forEach(b=>b.classList.toggle('on', !p.classList.contains('fx-hidden'))); }
function updateFxAvail(){
  document.querySelectorAll('.js-fxnote').forEach(el=>{ el.textContent='Effets + visualizer réactif sur toutes les scènes. Réglages sauvés sur cet appareil.'; });
}
function syncFxUI(){
  const o=AFX.o;
  document.querySelectorAll('.js-vol').forEach(el=>el.value=Math.round(o.vol*100));
  document.querySelectorAll('.js-bass').forEach(el=>el.value=o.bass);
  document.querySelectorAll('.js-bassval').forEach(el=>el.textContent=o.bass);
  document.querySelectorAll('.js-keepbass').forEach(el=>el.checked=o.keepbass);
  document.querySelectorAll('.js-loud').forEach(el=>el.checked=o.loud);
  document.querySelectorAll('.js-mono').forEach(el=>el.checked=o.mono);
  document.querySelectorAll('[data-preset]').forEach(el=>el.classList.toggle('active', el.dataset.preset===o.preset));
  document.querySelectorAll('[data-viz]').forEach(el=>el.classList.toggle('active', el.dataset.viz===o.viz));
}
// ── Minuterie sommeil : fondu doux puis pause après N minutes ──
const SLEEPS=[[0,'Off'],[15,'15 min'],[30,'30'],[45,'45'],[60,'60'],[90,'90']];
let sleepTO=null, sleepIV=null, sleepEnd=0;
function sleepClear(){
  if(sleepTO){clearTimeout(sleepTO);sleepTO=null;}
  if(sleepIV){clearInterval(sleepIV);sleepIV=null;}
  sleepEnd=0;
  document.querySelectorAll('.js-sleepstatus').forEach(el=>el.textContent='');
}
function sleepFadeStop(){
  if(AFX.ready){
    try{ const g=AFX.n.master.gain, t=AFX.ctx.currentTime; g.cancelScheduledValues(t); g.setValueAtTime(g.value,t); g.linearRampToValueAtTime(0.0001,t+8); }catch(e){}
    setTimeout(function(){ stopStream(); try{AFX.n.master.gain.value=AFX.o.vol;}catch(e){} }, 8300);
  }else{
    const a=AA(); let v=a.volume||1; const iv=setInterval(function(){ v-=0.06; if(v<=0.02){clearInterval(iv); stopStream(); a.volume=AFX.o.vol;} else a.volume=v; }, 480);
  }
  sleepClear();
  document.querySelectorAll('[data-sleep]').forEach(el=>el.classList.toggle('active', el.dataset.sleep==='0'));
}
function sleepSet(min){
  sleepClear();
  document.querySelectorAll('[data-sleep]').forEach(el=>el.classList.toggle('active', +el.dataset.sleep===min));
  if(min<=0) return;
  sleepEnd=Date.now()+min*60000;
  sleepTO=setTimeout(sleepFadeStop, min*60000);
  sleepIV=setInterval(function(){
    const left=Math.max(0,sleepEnd-Date.now()), m=Math.floor(left/60000), s=Math.floor((left%60000)/1000);
    document.querySelectorAll('.js-sleepstatus').forEach(el=>el.textContent='😴 extinction dans '+m+':'+(s<10?'0':'')+s);
  }, 1000);
}
function initFxUI(){
  document.querySelectorAll('.js-presets').forEach(box=>{ box.innerHTML=PRESETS.map(p=>'<button class="fxb" data-preset="'+p[0]+'">'+p[1]+'</button>').join(''); });
  document.querySelectorAll('.js-vizstyle').forEach(box=>{ box.innerHTML=VIZS.map(v=>'<button class="fxb" data-viz="'+v[0]+'">'+v[1]+'</button>').join(''); });
  document.querySelectorAll('.js-sleep').forEach(box=>{ box.innerHTML=SLEEPS.map(x=>'<button class="fxb'+(x[0]===0?' active':'')+'" data-sleep="'+x[0]+'">'+x[1]+'</button>').join(''); });
  document.addEventListener('click',e=>{
    const pb=e.target.closest&&e.target.closest('[data-preset]'); if(pb){ AFX.set('preset',pb.dataset.preset); syncFxUI(); return; }
    const vb=e.target.closest&&e.target.closest('[data-viz]'); if(vb){ AFX.set('viz',vb.dataset.viz); syncFxUI(); return; }
    const sb=e.target.closest&&e.target.closest('[data-sleep]'); if(sb){ sleepSet(+sb.dataset.sleep); }
  });
  document.addEventListener('input',e=>{
    if(e.target.classList.contains('js-vol')){ AFX.set('vol',(+e.target.value)/100); syncFxUI(); }
    else if(e.target.classList.contains('js-bass')){ AFX.set('bass',+e.target.value); syncFxUI(); }
  });
  document.addEventListener('change',e=>{
    if(e.target.classList.contains('js-keepbass')) AFX.set('keepbass',e.target.checked);
    else if(e.target.classList.contains('js-loud')) AFX.set('loud',e.target.checked);
    else if(e.target.classList.contains('js-mono')) AFX.set('mono',e.target.checked);
  });
  syncFxUI(); updateFxAvail(); AFX.apply();
}
(function(){
  const bmain=gid('playbtn'), bfs=gid('fs-play');
  function onPlay(){ if(bmain)bmain.textContent='⏸'; if(bfs)bfs.textContent='⏸'; if('mediaSession' in navigator)navigator.mediaSession.playbackState='playing'; startViz(); }
  function onPause(){ if(bmain)bmain.textContent='▶'; if(bfs)bfs.textContent='▶'; if('mediaSession' in navigator)navigator.mediaSession.playbackState='paused'; }
  [A,B].forEach(el=>{ el.addEventListener('play',onPlay); el.addEventListener('pause',onPause); });
  ['opt-wordmark','opt-bg','opt-bar','opt-viz'].forEach(id=>{const e=gid(id); if(e)e.addEventListener('change',fsSaveOpts);});
  if('mediaSession' in navigator){ navigator.mediaSession.setActionHandler('play',togglePlay); navigator.mediaSession.setActionHandler('pause',stopStream); }
  initFxUI();
})();
refresh();loadEvents();loadLoved();
loadVisuals();loadAuth();
setInterval(refresh,10000);setInterval(loadEvents,30000);setInterval(loadVisuals,300000);setInterval(loadLoved,60000);
// Repeint chaque seconde SANS requête réseau : le titre bascule à l'instant précis où
// l'auditeur entend le changement (le sondage réseau, lui, reste à 10 s).
setInterval(function(){ if(lastLive && lastLive.song_id) paintTrack(lastLive, lastArt); },1000);
if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js').catch(function(){});}
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(PAGE, headers={"Cache-Control": "no-cache, must-revalidate"})


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


# ── PWA : favicon SVG on-brand, manifest installable, service worker ──────────
_ICON_SVG = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
<stop offset="0" stop-color="#191036"/><stop offset=".5" stop-color="#8b5cf6"/>
<stop offset=".78" stop-color="#ff5e7a"/><stop offset="1" stop-color="#ff9a5a"/>
</linearGradient></defs>
<rect width="512" height="512" rx="116" fill="url(#g)"/>
<circle cx="256" cy="248" r="104" fill="#fff4e6"/>
<rect x="86" y="330" width="340" height="150" fill="url(#g)"/>
<rect x="96" y="352" width="320" height="20" rx="10" fill="#fff4e6" opacity=".95"/>
<rect x="128" y="392" width="256" height="16" rx="8" fill="#fff4e6" opacity=".8"/>
<rect x="160" y="428" width="192" height="14" rx="7" fill="#fff4e6" opacity=".65"/>
</svg>'''

_MANIFEST = {
    "name": "Gaiverland — Le festival permanent",
    "short_name": "Gaiverland",
    "description": "La radio-festival permanente. Une antenne qui ne dort jamais.",
    "start_url": "/", "scope": "/",
    "display": "standalone", "orientation": "portrait-primary",
    "background_color": "#191036", "theme_color": "#8b5cf6",
    "icons": [
        {"src": "/icon.svg", "sizes": "any",     "type": "image/svg+xml", "purpose": "any"},
        {"src": "/icon.svg", "sizes": "512x512", "type": "image/svg+xml", "purpose": "maskable"},
    ],
}

# Network-first + fallback cache (shell). N'intercepte NI le flux .mp3 NI les API live.
_SW_JS = '''const CACHE='gaiverland-v2';
self.addEventListener('install',function(e){self.skipWaiting();});
self.addEventListener('activate',function(e){e.waitUntil(
  caches.keys().then(function(ks){return Promise.all(ks.filter(function(k){return k!==CACHE;}).map(function(k){return caches.delete(k);}));}).then(function(){return self.clients.claim();})
);});
self.addEventListener('fetch',function(e){
  if(e.request.method!=='GET')return;
  var u=new URL(e.request.url);
  // JAMAIS de cache sur le HTML/navigations, les API et les flux → toujours frais
  if(e.request.mode==='navigate'||u.pathname==='/'||u.pathname==='/equipe'||u.pathname.indexOf('/api/')===0||u.pathname.slice(-4)==='.mp3'||u.pathname.indexOf('/live')===0) return;
  // assets statiques (icon/manifest) : network-first + fallback cache offline
  e.respondWith(
    fetch(e.request).then(function(r){
      if(r&&r.status===200&&r.type==='basic'){var cp=r.clone();caches.open(CACHE).then(function(c){c.put(e.request,cp);});}
      return r;
    }).catch(function(){return caches.match(e.request);})
  );
});'''


@app.get("/icon.svg")
def icon_svg():
    return Response(_ICON_SVG, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/manifest.webmanifest")
def manifest():
    return Response(json.dumps(_MANIFEST, ensure_ascii=False),
                    media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return Response(_SW_JS, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8099)
