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
from fastapi import FastAPI, Body, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
import secrets, hmac, hashlib, base64

# Page « L'équipe » (maquette Cassy, avatars embarqués). Import guardé : si le
# module manque, le site reste debout, seul /equipe est indisponible.
try:
    from team_page import HTML as TEAM_HTML
except Exception:
    TEAM_HTML = None

# Page « Notre modèle » — Tomorrowland (saisonnière). On EMBARQUE l'officiel, jamais de
# re-stream (cf module). Import guardé : si absent, le site reste debout.
try:
    import modele_page
except Exception:
    modele_page = None

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
    "club": int(os.environ.get("GCS_CLUB_STATION", "9")),
    "classics": int(os.environ.get("GCS_CLASSICS_STATION", "8")),
    "buvette": int(os.environ.get("GCS_BUVETTE_STATION", "10")),
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

# ── Assets statiques embarqués (photos de la galerie, etc.) ───────────────────
# Copiés du store → app-config/scripts/assets par setup.sh, montés /app/assets:ro.
# check_dir=False : si le dossier manque (install partiel), le site reste debout,
# seule la galerie est vide. On sert /assets/... en lecture seule.
_ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
app.mount("/assets", StaticFiles(directory=_ASSETS_DIR, check_dir=False), name="assets")


def _scan_balade():
    """Liste les photos de la balade présentes dans assets/balade (triées par nom).
    Dynamique : on sert TOUT ce qui a été déployé, sans nom de fichier codé en dur —
    le chef peut ajouter/retirer des recadrages, un simple redeploy suffit. Chaque
    entrée doit exister en plein format ET en vignette (thumbs/) pour être retenue."""
    base = os.path.join(_ASSETS_DIR, "balade")
    thumbs = os.path.join(base, "thumbs")
    out = []
    try:
        for fn in sorted(os.listdir(base)):
            if not fn.lower().endswith((".jpg", ".jpeg")):
                continue
            if not os.path.isfile(os.path.join(thumbs, fn)):
                continue
            out.append(fn)
    except FileNotFoundError:
        pass
    return out


# Légendes lisibles dérivées du nom de fichier (balade-01-mer-ile-rochers → « Mer ile rochers »).
# Générique, sans ville : on ignore le préfixe 'balade' et le numéro d'ordre.
def _balade_caption(fn: str) -> str:
    stem = re.sub(r"\.(jpe?g)$", "", fn, flags=re.I)
    words = " ".join(p for p in stem.split("-") if p and p != "balade" and not p.isdigit())
    return (words[:1].upper() + words[1:]) if words else "Gaiverland"


def _gallery_html() -> str:
    """Grille de la galerie, rendue côté serveur. Vignettes lazy-load + lightbox.
    Fallback (aucune photo) = le gag stagiaire d'origine, pour ne jamais afficher un trou."""
    photos = _scan_balade()
    if not photos:
        return ('<div class="soon">📸 Les souvenirs du festival arrivent bientôt.<br>'
                'Le stagiaire a promis de retrouver la carte SD.</div>')
    cells = []
    for fn in photos:
        cap = _balade_caption(fn).replace('"', "&quot;").replace("<", "&lt;")
        cells.append(
            '<button type="button" class="gal-cell" '
            'onclick="openGal(this)" '
            'data-full="/assets/balade/{fn}" data-cap="{cap}" aria-label="{cap}">'
            '<img loading="lazy" decoding="async" src="/assets/balade/thumbs/{fn}" alt="{cap}">'
            '</button>'.format(fn=fn, cap=cap)
        )
    intro = ('<p class="gal-intro">De la rade de Toulon aux îles d\'Hyères — '
             'la côte que le festival regarde en respirant.</p>')
    return intro + '<div class="gal-grid">' + "".join(cells) + '</div>'


# Rendu figé au démarrage : le conteneur redémarre à chaque déploiement (setup.sh
# restart gcs-web), donc la liste est rafraîchie sans logique de cache à maintenir.
_GALLERY_HTML = _gallery_html()


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
    # statement_timeout : une requête qui traîne >15s est tuée au lieu de bloquer un worker
    # indéfiniment. Sans ça, une requête coincée derrière un verrou s'empilait à chaque poll
    # du site (44 connexions bloquées → gcs-web étranglé, incident 15/07).
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor,
                            options="-c statement_timeout=15000")


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


# ── Auditeurs externes (Discord voice, etc.) ─────────────────────────────────
# Sources non-AzuraCast qui écoutent la Mainstage (ex. le bot Discord compte les
# membres en vocal avec lui). Uniquement un NOMBRE, anonyme, avec TTL : si la source
# arrête de rafraîchir, son compte disparaît. Table créée à la première écriture.
_ext_cache = {"n": 0, "at": 0.0}
def _ext_listeners_total() -> int:
    # Caché 12s : /api/live est poll toutes les 10s par chaque client → sans cache, ça
    # ouvrait une connexion DB par appel sur l'endpoint le plus chaud. Le bot ne poste que
    # toutes les 45s, donc 12s de fraîcheur suffisent largement.
    now = time.time()
    if now - _ext_cache["at"] < 12:
        return _ext_cache["n"]
    conn = None
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(SUM(count),0) AS n FROM ext_listeners
                           WHERE updated_at > now() - interval '90 seconds'""")
            r = cur.fetchone()
        _ext_cache["n"] = int(r["n"]) if r else 0
        _ext_cache["at"] = now
        return _ext_cache["n"]
    except Exception:
        return _ext_cache["n"]   # table absente / DB indispo → dernière valeur, jamais bloquant
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


@app.post("/api/ext/listeners")
def ext_listeners(request: Request, source: str = "ext", count: int = 0):
    # Sécurité SANS secret : gcs-web:8099 n'est joignable QUE sur le réseau Docker interne
    # (le public passe forcément par NPM, qui pose X-Forwarded-For / X-Real-IP). On refuse
    # donc tout appel porteur de ces en-têtes = venu du proxy public. Seul un conteneur
    # interne (le bot) appelle en direct, sans ces en-têtes.
    if request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip"):
        raise HTTPException(status_code=403, detail="internal only")
    n   = max(0, min(100000, int(count)))
    src = (source or "ext")[:32]
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS ext_listeners (
                             source     VARCHAR(32) PRIMARY KEY,
                             count      INTEGER NOT NULL DEFAULT 0,
                             updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("""INSERT INTO ext_listeners (source,count,updated_at)
                           VALUES (%s,%s,now())
                           ON CONFLICT (source) DO UPDATE
                             SET count=EXCLUDED.count, updated_at=now()""", (src, n))
        conn.commit(); conn.close()
        return {"ok": True, "source": src, "count": n}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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
            base_lis = np.get("listeners", {}).get("current", 0) or 0
            # Le bot Discord diffuse la Mainstage → on ajoute les gens en vocal avec lui
            # (nombre anonyme, aucune donnée perso). Uniquement sur main.
            out["listeners"] = base_lis + (_ext_listeners_total() if station == "main" else 0)
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
# … + vieilles cartes postales / photos N&B / gravures (années 1800→1959) : pas l'ambiance festival.
_PHOTO_REJECT = re.compile(
    r"(blason|armoiries|coat[_ ]?of[_ ]?arms|wappen|logo|drapeau|flag|"
    r"carte|\bmap\b|plan_|localisation|location_|situation|position|"
    r"seal|sceau|diagram|graph|chart|\.svg|"
    r"postcard|postale|ancienne?|vintage|s[ée]pia|noir[_ ]et[_ ]blanc|"
    r"black[_ ]?and[_ ]?white|\bn&b\b|gravure|lithograph|estampe)", re.I)
    # NB : PAS de motif d'année (18xx/19xx) — il matchait le « 1920px » des URLs Wikimedia
    # et rejetait TOUTES les photos. Les mots-clés ci-dessus suffisent pour les cartes postales.


def _wm_upscale(src: str, target: int = 1920) -> str:
    """Régénère une vignette Wikimedia plus large (URLs .../NNNpx-Nom.jpg, à la volée)."""
    return re.sub(r"/(\d+)px-", f"/{target}px-", src, count=1)


def _good_photo(src: str) -> bool:
    # Wikimedia ajoute désormais une query string (utm_*) aux thumburl → on la retire
    # avant de tester l'extension, sinon TOUTES les photos sont rejetées.
    low = src.lower().split("?", 1)[0]
    if not low.endswith((".jpg", ".jpeg")):
        return False  # on veut des photos (écarte SVG/PNG cartes/blasons)
    return not _PHOTO_REJECT.search(low)


def _bg_own_photos() -> list:
    """Fond du player = UNIQUEMENT les photos du chef (balade + Toulon), locales et sûres.
    Remplace Wikipédia → plus aucune rue à voitures ni carte postale N&B parasite.
    Trié pour un ordre stable ; le front alterne dessus."""
    out = []
    for sub in ("balade", "toulon"):
        base = os.path.join(_ASSETS_DIR, sub)
        try:
            for fn in sorted(os.listdir(base)):
                if fn.lower().endswith((".jpg", ".jpeg")):
                    out.append(f"/assets/{sub}/{fn}")
        except FileNotFoundError:
            pass
    return out


def _wiki_media(title: str, lang: str = "fr") -> list:
    """Photos JPG d'une page Wikipedia (media-list), upscalées + filtrées.
    NB : plus appelé pour le fond (on ne sert QUE les photos du chef) — conservé au cas où."""
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
    """Images du fond du player : cover + photos RÉGIONALES de la ville courante du festival
    (tournée Bretagne) + les photos du chef en filet de sécurité. Fail-safe (cover au minimum)."""
    imgs = []
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=4)
        if r.status_code == 200:
            art = _publicize(r.json().get("now_playing", {}).get("song", {}).get("art", ""))
            if art:
                imgs.append(art)
    except Exception:
        pass
    # Ville courante du festival (mini-scène) — lue DIRECTEMENT en base (STATE_URL absent
    # dans gcs-web) : c'est ainsi que le fond suit la tournée (Toulon → villes bretonnes).
    city = os.environ.get("GCS_CITY", "").strip()
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT city FROM gcs_state WHERE id=1")
            row = cur.fetchone()
        conn.close()
        if row and row.get("city"):
            city = row["city"]
    except Exception:
        pass
    imgs += _city_photos(city)      # photos régionales de la ville courante (dominent)
    imgs += _bg_own_photos()        # tes photos = filet de sécurité (jamais de fond vide)
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

def _station_current(station_key: str):
    """(song_id, artist, title, az_station_id) du titre EN COURS sur la STATION écoutée.
    AzuraCast accepte l'ID station comme shortcode → /api/nowplaying/{id}. Sert aux boutons
    vote/passer/blacklist station-aware (avant : tout tapait sur la mainstage)."""
    az_sid = STATIONS.get(station_key, AZ_STATION)
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{az_sid}", timeout=4)
        if r.status_code == 200:
            song = (r.json().get("now_playing", {}) or {}).get("song", {}) or {}
            return song.get("id", ""), song.get("artist", ""), song.get("title", ""), az_sid
    except Exception:
        pass
    return "", "", "", az_sid


def _login_ok(provider: str, sub: str, request: Request = None):
    # Si la régie nous a envoyé ici (cookie gnext), on y RETOURNE après connexion —
    # sinon le chef atterrirait sur l'accueil et devrait retaper /regie.
    suivant = "/?login=ok"
    if request is not None:
        gn = request.cookies.get("gnext", "")
        if gn in ("/regie", "/regie/voix", "/regie/sante", "/regie/musique"):
            suivant = gn
    r = RedirectResponse(suivant, status_code=302)
    r.set_cookie("gsid", _sign(f"{provider}:{sub}"), max_age=2592000,
                 httponly=True, secure=True, samesite="lax")
    r.delete_cookie("gstate")
    r.delete_cookie("gnext")
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
    return _login_ok("google", sub, request) if sub else RedirectResponse("/?login=err", status_code=302)

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
    return _login_ok("discord", sub, request) if sub else RedirectResponse("/?login=err", status_code=302)

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
    # Résoudre le morceau en cours SUR LA STATION ÉCOUTÉE (pas forcément la mainstage)
    song_id, _a, _t, _s = _station_current(str(body.get("station") or "main"))
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
def api_pass(request: Request, body: dict = Body(default={})):
    """« Passer » le titre en cours de la STATION écoutée : fondateur = immédiat, public = démocratique."""
    uid = _uid(request)
    if not uid:
        return {"ok": False, "error": "login requis", "need_login": True}
    song_id, _a, _t, az_sid = _station_current(str(body.get("station") or "main"))
    if not song_id:
        return {"ok": False, "error": "pas de morceau en cours"}
    try:
        r = httpx.post(f"{VOTE_URL}/pass",
                       json={"song_id": song_id, "user_id": uid,
                             "station_id": az_sid, "shortcode": str(az_sid)}, timeout=6)
        if r.status_code == 200:
            return {"ok": True, **r.json()}
        return {"ok": False, "error": f"vote-service {r.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:60]}


@app.post("/api/blacklist")
def api_blacklist(request: Request, body: dict = Body(default={})):
    """Bannir DÉFINITIVEMENT le titre en cours de la STATION écoutée. Réservé au fondateur."""
    uid = _uid(request)
    if not uid:
        return {"ok": False, "error": "login requis", "need_login": True}
    if uid not in FOUNDER_IDS:
        return {"ok": False, "error": "réservé au chef"}
    song_id, artist, title, az_sid = _station_current(str(body.get("station") or "main"))
    if not song_id:
        return {"ok": False, "error": "pas de morceau en cours"}
    try:
        r = httpx.post(f"{VOTE_URL}/blacklist",
                       json={"song_id": song_id, "user_id": uid, "station_id": az_sid,
                             "shortcode": str(az_sid), "artist": artist, "title": title}, timeout=6)
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


# ── RÉGIE VOIX : écoute + validation des jingles Rebexis ─────────────────────
# Le chef veut valider la QUALITÉ (prononciation : « façons » lu « facons », etc.)
# AVANT qu'un jingle passe à l'antenne. Cette page liste les jingles de tts_library,
# les fait écouter, et enregistre un verdict. Le worker TTS n'utilisera que les
# jingles non rejetés (cf. filtre côté tts_worker).
TTS_CACHE_DIR = "/tts-cache"
ADMIN_TOKEN = os.environ.get("GCS_ADMIN_TOKEN", "").strip()


def _admin_ok(request: Request) -> bool:
    """Accès régie, deux clés équivalentes :
    1. le COMPTE connecté est un compte fondateur (Google/Discord de FOUNDER_IDS —
       les mêmes identités qui pèsent déjà dans les votes) → la voie normale du chef,
       valable sur n'importe quel appareil après une simple connexion au site ;
    2. le jeton ?k=/cookie — conservé pour les OUTILS (gaiverland-dl, favoris
       existants). Jamais public dans les deux cas."""
    if FOUNDER_IDS and _uid(request) in FOUNDER_IDS:
        return True
    if not ADMIN_TOKEN:
        return False
    given = request.query_params.get("k", "") or request.cookies.get("gadm", "")
    return hmac.compare_digest(given, ADMIN_TOKEN)


def _ensure_review_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tts_review (
            text_hash   VARCHAR(64) PRIMARY KEY,
            status      TEXT NOT NULL DEFAULT 'pending',
            note        TEXT,
            reviewed_at TIMESTAMPTZ DEFAULT NOW()
        )
    """)


@app.get("/api/regie/jingles")
def regie_jingles(request: Request, status: str = "all"):
    """Liste des jingles avec leur texte et leur verdict."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            _ensure_review_table(cur)
            conn.commit()
            cur.execute("""
                SELECT l.text_hash, l.text, l.category, l.audio_file,
                       COALESCE(r.status, 'pending') AS status, r.note
                FROM tts_library l
                LEFT JOIN tts_review r ON r.text_hash = l.text_hash
                WHERE l.audio_file IS NOT NULL
                ORDER BY (COALESCE(r.status,'pending') <> 'pending'), l.category, l.text
            """)
            rows = cur.fetchall()
        conn.close()
        # "v" = date de modif du mp3. Elle entre dans l'URL de lecture → quand un jingle
        # est REGÉNÉRÉ, l'URL change et le navigateur ne peut plus rejouer l'ancien son
        # gardé en cache (piège vécu le 27/07 : le chef a revoté sur l'audio périmé).
        def _ver(af):
            try:
                return int(os.path.getmtime(os.path.join(TTS_CACHE_DIR, os.path.basename(af or ""))))
            except Exception:
                return 0
        items = [{"h": r["text_hash"], "text": r["text"] or "", "cat": r["category"] or "",
                  "status": r["status"], "note": r["note"] or "",
                  "v": _ver(r["audio_file"])} for r in rows]
        if status in ("pending", "ok", "ko"):
            items = [i for i in items if i["status"] == status]
        counts = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        quota = {}
        try:
            base = _tts_base()
            if base:
                quota = httpx.get(base + "/quota", timeout=8).json()
        except Exception:
            quota = {}
        return {"items": items, "counts": counts, "total": len(rows), "quota": quota}
    except Exception as e:
        return {"items": [], "counts": {}, "total": 0, "error": str(e)[:200]}


@app.get("/api/regie/audio/{h}")
def regie_audio(h: str, request: Request):
    """Sert le mp3 d'un jingle. Le nom vient de la base, jamais de l'URL (anti-traversée)."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    if not re.fullmatch(r"[0-9a-f]{8,64}", h or ""):
        raise HTTPException(status_code=400, detail="bad id")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT audio_file FROM tts_library WHERE text_hash=%s", (h,))
            row = cur.fetchone()
        conn.close()
        if not row or not row["audio_file"]:
            raise HTTPException(status_code=404, detail="inconnu")
        name = os.path.basename(row["audio_file"])
        path = os.path.join(TTS_CACHE_DIR, name)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="audio absent")
        size = os.path.getsize(path)
        head = {"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"}
        # Safari (iPhone surtout) REFUSE de lire un <audio> dont la source ne gère pas
        # les requêtes Range : il envoie « Range: bytes=0- » et attend un 206. Sans ça,
        # aucun son ne sort (constaté par le chef le 27/07). On répond donc en 206.
        rng = request.headers.get("range", "")
        m = re.match(r"bytes=(\d*)-(\d*)$", rng.strip()) if rng else None
        if m:
            start = int(m.group(1)) if m.group(1) else 0
            end = int(m.group(2)) if m.group(2) else size - 1
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            with open(path, "rb") as f:
                f.seek(start)
                chunk = f.read(end - start + 1)
            head["Content-Range"] = f"bytes {start}-{end}/{size}"
            return Response(content=chunk, status_code=206,
                            media_type="audio/mpeg", headers=head)
        with open(path, "rb") as f:
            data = f.read()
        return Response(content=data, media_type="audio/mpeg", headers=head)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="erreur")


# ⚠ GCS_TTS_URL est PÉRIMÉE en prod (pointe sur gcs-tts:8093, hôte inexistant ; le vrai
# service est gw-tts:8082). On essaie donc plusieurs adresses et on garde celle qui répond.
_TTS_CANDIDATS = [u for u in (os.environ.get("GCS_TTS_URL", "").rstrip("/"),
                              "http://gw-tts:8082",
                              "http://gaiverland-tts:8082") if u]
_tts_ok = {"url": None}


def _tts_base() -> str:
    if _tts_ok["url"]:
        return _tts_ok["url"]
    for u in _TTS_CANDIDATS:
        try:
            if httpx.get(u + "/health", timeout=4).status_code == 200:
                _tts_ok["url"] = u
                return u
        except Exception:
            continue
    return _TTS_CANDIDATS[0] if _TTS_CANDIDATS else ""
REBEXIS_PL_ID = 3   # playlist « Rebexis » de la Mainstage (AzuraCast)


@app.post("/api/regie/voix/creer")
def regie_voix_creer(request: Request, payload: dict = Body(...)):
    """Le chef écrit une phrase → on la fabrique avec la voix validée, SANS la mettre
    à l'antenne. Elle arrive dans « à écouter » ; c'est « Garder » qui la diffusera."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    texte = str(payload.get("texte", "")).strip()
    cat = str(payload.get("categorie", "rebexis"))
    if cat not in ("rebexis", "cat3_bloc", "cat4_nouveaute", "custom"):
        cat = "rebexis"
    if len(texte) < 4:
        raise HTTPException(status_code=400, detail="phrase trop courte")
    if len(texte) > 300:
        raise HTTPException(status_code=400, detail="phrase trop longue (300 max)")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM tts_library WHERE text=%s", (texte,))
            if cur.fetchone():
                conn.close()
                return {"ok": False, "message": "Cette phrase existe déjà."}
        conn.close()
    except Exception:
        pass
    try:
        # 90 s : la synthèse ElevenLabs + le rendu ffmpeg prennent quelques secondes
        base = _tts_base()
        if not base:
            return {"ok": False, "message": "Moteur de voix introuvable sur le réseau."}
        r = httpx.post(f"{base}/creer", params={"text": texte, "category": cat}, timeout=90)
        if r.status_code != 200:
            return {"ok": False, "message": f"Le moteur de voix a refusé : {r.text[:120]}"}
    except Exception as e:
        return {"ok": False, "message": "Moteur de voix injoignable : " + str(e)[:90]}
    return {"ok": True, "message": "Phrase créée. Elle t'attend dans « à écouter » — "
                                   "elle n'ira à l'antenne qu'après ton Garder."}


def _mettre_a_l_antenne(chemin: str) -> bool:
    """Assigne le fichier à la playlist Rebexis d'AzuraCast (appelé quand le chef GARDE).
    Sans ça, un jingle validé resterait dans la bibliothèque sans jamais passer."""
    cle = os.environ.get("AZURACAST_API_KEY", "")
    if not cle or not chemin:
        return False
    try:
        h = {"X-API-Key": cle, "Accept": "application/json", "Content-Type": "application/json"}
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/files", headers=h,
                      params={"rowsPerPage": 2000}, timeout=45)
        rows = r.json()
        rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
        base = os.path.basename(chemin)
        cible = [f["path"] for f in rows if os.path.basename(f.get("path", "")) == base]
        if not cible:
            return False
        r2 = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/files/batch", headers=h,
                       json={"do": "playlist", "files": cible,
                             "playlists": [str(REBEXIS_PL_ID)]}, timeout=60)
        return r2.status_code == 200
    except Exception:
        return False


def _retirer_de_l_antenne(chemin: str) -> int:
    """Retire le fichier de TOUTES les playlists, sur toutes les stations (appelé quand le
    chef RETIRE un jingle). Symétrique de _mettre_a_l_antenne — son absence était la fuite
    du 05/08 : « Retirer » notait le refus mais laissait le jingle dans la playlist, donc
    à l'antenne. Retourne le nombre de désassignations faites (best-effort : le filet
    horaire côté serveur rattrape de toute façon)."""
    cle = os.environ.get("AZURACAST_API_KEY", "")
    if not cle or not chemin:
        return 0
    base = os.path.basename(chemin)
    n = 0
    h = {"X-API-Key": cle, "Accept": "application/json", "Content-Type": "application/json"}
    try:
        stations = httpx.get(f"{AZ_URL}/api/stations", headers=h, timeout=30).json()
    except Exception:
        return 0
    for st in stations if isinstance(stations, list) else []:
        sid = st.get("id")
        try:
            r = httpx.get(f"{AZ_URL}/api/station/{sid}/files", headers=h,
                          params={"rowsPerPage": 3000}, timeout=45)
            rows = r.json()
            rows = rows.get("rows", rows) if isinstance(rows, dict) else rows
            for f in rows:
                if os.path.basename(f.get("path", "")) != base:
                    continue
                pls = [p["id"] for p in (f.get("playlists") or [])]
                if not pls:
                    continue
                r2 = httpx.put(f"{AZ_URL}/api/station/{sid}/file/{f['id']}", headers=h,
                               json={"playlists": []}, timeout=20)
                if r2.status_code < 400:
                    n += 1
        except Exception:
            continue
    return n


@app.post("/api/regie/review")
def regie_review(request: Request, payload: dict = Body(...)):
    """Enregistre le verdict : ok (garder) / ko (retirer de l'antenne) / pending."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    h = str(payload.get("h", ""))
    st = str(payload.get("status", ""))
    note = str(payload.get("note", ""))[:500]
    if st not in ("ok", "ko", "pending") or not re.fullmatch(r"[0-9a-f]{8,64}", h):
        raise HTTPException(status_code=400, detail="parametres invalides")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            _ensure_review_table(cur)
            cur.execute("""
                INSERT INTO tts_review (text_hash, status, note, reviewed_at)
                VALUES (%s,%s,%s,NOW())
                ON CONFLICT (text_hash) DO UPDATE
                  SET status=EXCLUDED.status, note=EXCLUDED.note, reviewed_at=NOW()
            """, (h, st, note))
        conn.commit()
        # Valider = mettre à l'antenne. Tant que le chef n'a pas gardé le jingle, il
        # n'est pas dans la playlist AzuraCast, donc il ne peut pas passer.
        diffuse = None
        with conn.cursor() as cur:
            cur.execute("SELECT audio_file FROM tts_library WHERE text_hash=%s", (h,))
            row = cur.fetchone()
        if row and row["audio_file"]:
            if st == "ok":
                diffuse = _mettre_a_l_antenne(row["audio_file"])
            else:
                # Retirer / remettre en attente = HORS ANTENNE, immédiatement et partout.
                diffuse = -_retirer_de_l_antenne(row["audio_file"])
        conn.close()
        return {"ok": True, "status": st, "diffuse": diffuse}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


COOKIES_PATH = "/cookies/youtube-cookies.txt"


@app.get("/api/regie/musique")
def regie_musique(request: Request):
    """Demandes de titres + état des cookies YouTube du downloader."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    out = {"attente": [], "recentes": [], "cookies": {}, "stats": {}}
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT p.id, p.title, p.created_at
                           FROM title_proposals p
                           WHERE NOT EXISTS (SELECT 1 FROM proposal_decisions d WHERE d.title = p.title)
                           ORDER BY p.created_at DESC LIMIT 60""")
            out["attente"] = [{"id": r["id"], "titre": r["title"],
                               "quand": r["created_at"].strftime("%d/%m %H:%M") if r["created_at"] else ""}
                              for r in cur.fetchall()]
            cur.execute("""SELECT title, verdict, artist, canon_title, download_status, downloaded_at
                           FROM proposal_decisions ORDER BY decided_at DESC LIMIT 25""")
            out["recentes"] = [{"titre": r["title"], "verdict": r["verdict"] or "?",
                                "artiste": r["artist"] or "", "canon": r["canon_title"] or "",
                                "telecharge": bool(r["downloaded_at"]),
                                "etat": r["download_status"] or ""} for r in cur.fetchall()]
            cur.execute("""SELECT count(*) FILTER (WHERE verdict='accept') acc,
                                  count(*) FILTER (WHERE downloaded_at IS NOT NULL) dl,
                                  count(*) tot FROM proposal_decisions""")
            r = cur.fetchone()
            out["stats"] = {"acceptes": r["acc"], "telecharges": r["dl"], "decisions": r["tot"]}
        conn.close()
    except Exception as e:
        out["erreur"] = str(e)[:120]

    # ── État du downloader (battement de cœur + file + interrupteur) ────────
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FILTER (WHERE verdict='accept' AND downloaded_at IS NULL) file,
                                  count(*) FILTER (WHERE downloaded_at::date = current_date) auj,
                                  max(downloaded_at) dernier FROM proposal_decisions""")
            q = cur.fetchone()
            out["downloader"] = {"en_file": q["file"], "aujourdhui": q["auj"],
                                 "dernier": q["dernier"].strftime("%d/%m %H:%M") if q["dernier"] else "—"}
            try:
                cur.execute("""SELECT cle, statut, detail,
                                      round(extract(epoch FROM (now()-maj))/60) min
                               FROM system_health WHERE cle IN ('downloader','downloader_pause','downloader_limite')""")
                for r in cur.fetchall():
                    if r["cle"] == "downloader":
                        out["downloader"].update({"statut": r["statut"], "detail": r["detail"],
                                                  "vu_il_y_a_min": int(r["min"] or 0)})
                    elif r["cle"] == "downloader_pause":
                        out["downloader"]["en_pause"] = (r["statut"] == "on")
                    else:
                        try:
                            out["downloader"]["limite"] = int(r["statut"])
                        except Exception:
                            pass
            except Exception:
                conn.rollback()
        conn.close()
    except Exception:
        out["downloader"] = {}

    try:
        st = os.stat(COOKIES_PATH)
        jours = (time.time() - st.st_mtime) / 86400
        out["cookies"] = {"present": True, "taille": st.st_size,
                          "depose_le": time.strftime("%d/%m/%Y %H:%M", time.localtime(st.st_mtime)),
                          "age_jours": round(jours, 1),
                          # Les cookies YouTube tiennent quelques semaines : au-delà on prévient.
                          "statut": "OK" if (st.st_size > 500 and jours < 21) else "A RENOUVELER"}
    except Exception:
        out["cookies"] = {"present": False, "statut": "ABSENT"}
    return out


@app.get("/api/regie/recherche")
def regie_recherche(request: Request, q: str = ""):
    """Autocomplétion artiste/titre via le catalogue iTunes (public, sans clé).
    Le serveur fait l'appel : pas de CORS, et le navigateur du chef n'expose rien."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    q = (q or "").strip()
    if len(q) < 2:
        return {"resultats": []}
    try:
        r = httpx.get("https://itunes.apple.com/search",
                      params={"term": q, "media": "music", "entity": "song", "limit": 8},
                      timeout=6)
        res = []
        for x in (r.json().get("results", []) if r.status_code == 200 else []):
            res.append({"artiste": x.get("artistName", ""), "titre": x.get("trackName", ""),
                        "genre": x.get("primaryGenreName", ""),
                        "annee": (x.get("releaseDate") or "")[:4]})
        return {"resultats": res}
    except Exception:
        return {"resultats": []}


@app.get("/api/suggest")
def api_suggest(q: str = ""):
    """Autocomplétion PUBLIQUE (catalogue iTunes) pour la page « Proposer » du site.
    Non gaté : c'est une simple aide à la saisie, aucune écriture ni donnée sensible ;
    le serveur fait l'appel iTunes (pas de CORS, pas de clé)."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"resultats": []}
    try:
        r = httpx.get("https://itunes.apple.com/search",
                      params={"term": q, "media": "music", "entity": "song", "limit": 8},
                      timeout=6)
        res = []
        for x in (r.json().get("results", []) if r.status_code == 200 else []):
            res.append({"artiste": x.get("artistName", ""), "titre": x.get("trackName", ""),
                        "genre": x.get("primaryGenreName", ""),
                        "annee": (x.get("releaseDate") or "")[:4]})
        return {"resultats": res}
    except Exception:
        return {"resultats": []}


DON_BTC_ADDR = os.environ.get("GCS_DON_BTC", "bc1q36v0s4sx3m7jdg3k7sk6lhe0wgn4pfacjur28e")

@app.get("/api/don")
def api_don():
    """Suivi PUBLIC de la cagnotte de légalisation : total BTC reçu sur l'adresse de dons
    (via mempool.space) converti en €. Objectif réglable via GCS_DON_GOAL_EUR. Best-effort :
    renvoie 0 si mempool est injoignable. Aucune donnée sensible (adresse Bitcoin publique)."""
    goal = int(os.environ.get("GCS_DON_GOAL_EUR", "1200"))
    try:
        s = httpx.get(f"https://mempool.space/api/address/{DON_BTC_ADDR}", timeout=8).json()
        sats = s["chain_stats"]["funded_txo_sum"] + s["mempool_stats"]["funded_txo_sum"]
        btc = sats / 1e8
        price = httpx.get("https://mempool.space/api/v1/prices", timeout=8).json().get("EUR", 0)
        return {"goal": goal, "raised_eur": round(btc * price), "btc": round(btc, 6),
                "addr": DON_BTC_ADDR}
    except Exception:
        return {"goal": goal, "raised_eur": 0, "btc": 0, "addr": DON_BTC_ADDR}


@app.post("/api/regie/musique/ajouter")
def regie_ajouter(request: Request, payload: dict = Body(...)):
    """Ajoute une demande DÉJÀ ACCEPTÉE (c'est le chef) → le downloader la prendra.
    `libre` = texte tel quel, pour les remix que les catalogues ne connaissent pas."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    artiste = str(payload.get("artiste", "")).strip()[:200]
    titre = str(payload.get("titre", "")).strip()[:200]
    recherche = (artiste + " " + titre).strip() if not payload.get("libre") else titre
    if len(recherche) < 3:
        raise HTTPException(status_code=400, detail="demande trop courte")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM proposal_decisions WHERE title=%s", (recherche,))
            if cur.fetchone():
                conn.close()
                return {"ok": False, "message": "Cette demande existe déjà."}
            cur.execute("INSERT INTO title_proposals (user_id, title) VALUES ('regie-chef', %s)",
                        (recherche,))
            cur.execute("""INSERT INTO proposal_decisions (title, verdict, artist, canon_title, decided_at)
                           VALUES (%s,'accept',%s,%s,NOW())""",
                        (recherche, artiste or None, titre or None))
        conn.commit()
        conn.close()
        return {"ok": True, "message": "Ajouté — le downloader va le chercher."}
    except Exception as e:
        return {"ok": False, "message": str(e)[:140]}


@app.post("/api/regie/musique/decision")
def regie_decision(request: Request, payload: dict = Body(...)):
    """Accepte ou refuse une demande en attente."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    titre = str(payload.get("titre", "")).strip()[:300]
    verdict = str(payload.get("verdict", ""))
    if verdict not in ("accept", "reject") or not titre:
        raise HTTPException(status_code=400, detail="parametres invalides")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO proposal_decisions (title, verdict, decided_at)
                           VALUES (%s,%s,NOW()) ON CONFLICT (title) DO UPDATE
                             SET verdict=EXCLUDED.verdict, decided_at=NOW()""", (titre, verdict))
        conn.commit()
        conn.close()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "message": str(e)[:140]}


@app.post("/api/regie/musique/artiste")
def regie_artiste(request: Request, payload: dict = Body(...)):
    """Demande d'un ARTISTE : on prend son CATALOGUE, pas juste quelques titres.

    Deux passes : on résout d'abord son identifiant iTunes puis on liste ses morceaux
    (jusqu'à 200), et on complète par une recherche classique — certains featurings
    n'apparaissent pas dans le catalogue de l'artiste principal. On écarte ce qui est
    déjà en bibliothèque ou déjà demandé, puis on met TOUT le reste en file.
    """
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    artiste = str(payload.get("artiste", "")).strip()[:120]
    # 0 ou absent = tout prendre. Plafond haut : c'est le quota quotidien du
    # téléchargeur qui régule le rythme, pas cette page.
    combien = int(payload.get("combien", 0) or 0)
    if combien <= 0:
        combien = 500
    combien = min(combien, 500)
    if len(artiste) < 2:
        raise HTTPException(status_code=400, detail="nom d'artiste trop court")

    def _cle(a, t):
        return re.sub(r"[^a-z0-9]", "", ((a or "") + (t or "")).lower())

    cible = _cle(artiste, "")
    trouves, vus = [], set()

    def _ajouter(x):
        nom = (x.get("artistName") or "").strip()
        titre = (x.get("trackName") or "").strip()
        if not titre or not nom:
            return
        # on garde les featurings : il suffit que l'artiste demandé soit cité
        if cible not in _cle(nom, "") and _cle(nom, "") not in cible:
            return
        k = _cle(nom, titre)
        if k in vus:
            return
        vus.add(k)
        trouves.append({"artiste": nom, "titre": titre})

    try:
        # 1) catalogue de l'artiste (via son identifiant)
        a = httpx.get("https://itunes.apple.com/search",
                      params={"term": artiste, "media": "music", "entity": "musicArtist",
                              "limit": 1, "country": "FR"}, timeout=10)
        res = a.json().get("results", []) if a.status_code == 200 else []
        if res and res[0].get("artistId"):
            c = httpx.get("https://itunes.apple.com/lookup",
                          params={"id": res[0]["artistId"], "entity": "song",
                                  "limit": 200, "country": "FR"}, timeout=20)
            for x in (c.json().get("results", []) if c.status_code == 200 else []):
                if x.get("wrapperType") == "track":
                    _ajouter(x)
        # 2) complément : recherche classique (featurings, sorties hors catalogue)
        r = httpx.get("https://itunes.apple.com/search",
                      params={"term": artiste, "media": "music", "entity": "song",
                              "limit": 200, "country": "FR"}, timeout=20)
        for x in (r.json().get("results", []) if r.status_code == 200 else []):
            _ajouter(x)
    except Exception as e:
        return {"ok": False, "message": "Catalogue injoignable : " + str(e)[:80]}

    if not trouves:
        return {"ok": False, "message": "Aucun titre trouvé pour cet artiste."}

    ajoutes, deja_la = [], 0
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT artist, title FROM tracks")
            possede = {_cle(x["artist"], x["title"]) for x in cur.fetchall()}
            cur.execute("SELECT title FROM proposal_decisions")
            demande = {_cle("", x["title"]) for x in cur.fetchall()}
            for v in trouves:
                if len(ajoutes) >= combien:
                    break
                recherche = (v["artiste"] + " " + v["titre"]).strip()
                if _cle(v["artiste"], v["titre"]) in possede or _cle("", recherche) in demande:
                    deja_la += 1
                    continue
                cur.execute("INSERT INTO title_proposals (user_id, title) VALUES ('regie-chef', %s)",
                            (recherche,))
                cur.execute("""INSERT INTO proposal_decisions (title, verdict, artist, canon_title, decided_at)
                               VALUES (%s,'accept',%s,%s,NOW()) ON CONFLICT (title) DO NOTHING""",
                            (recherche, v["artiste"], v["titre"]))
                ajoutes.append(recherche)
            cur.execute("SELECT count(*) n FROM proposal_decisions "
                        "WHERE verdict='accept' AND downloaded_at IS NULL")
            en_file = cur.fetchone()["n"]
        conn.commit()
        conn.close()
    except Exception as e:
        return {"ok": False, "message": str(e)[:140]}

    if not ajoutes:
        return {"ok": False,
                "message": f"Rien de neuf : les {len(trouves)} titres trouvés sont déjà là."}
    # Le téléchargeur a un quota quotidien : on annonce le délai réel.
    jours = max(1, -(-en_file // 45))
    return {"ok": True, "ajoutes": ajoutes,
            "message": f"{len(ajoutes)} titre(s) de {artiste} mis en file "
                       f"({len(trouves)} trouvés, {deja_la} déjà présents). "
                       f"File totale : {en_file} → environ {jours} jour(s) au rythme actuel."}


@app.post("/api/regie/downloader")
def regie_downloader(request: Request, payload: dict = Body(...)):
    """Met le downloader en pause / le relance. L'interrupteur vit en base : le
    downloader le lit à chaque passe, aucun accès Docker nécessaire depuis le site."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    action = str(payload.get("action", ""))
    if action not in ("pause", "reprise"):
        raise HTTPException(status_code=400, detail="action invalide")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS system_health (
                               cle TEXT PRIMARY KEY, statut TEXT NOT NULL,
                               detail TEXT, maj TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""INSERT INTO system_health (cle, statut, detail, maj)
                           VALUES ('downloader_pause',%s,%s,NOW())
                           ON CONFLICT (cle) DO UPDATE
                             SET statut=EXCLUDED.statut, detail=EXCLUDED.detail, maj=NOW()""",
                        ("on" if action == "pause" else "off",
                         "demandé depuis la page de régie"))
        conn.commit()
        conn.close()
        return {"ok": True, "message": "Downloader mis en pause." if action == "pause"
                                       else "Downloader relancé (effet à la prochaine passe)."}
    except Exception as e:
        return {"ok": False, "message": str(e)[:140]}


@app.post("/api/regie/downloader/limite")
def regie_limite(request: Request, payload: dict = Body(...)):
    """Règle le quota quotidien du téléchargeur à chaud (1-500). Utile pour absorber
    vite un gros import d'artiste puis revenir à un rythme tranquille."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        v = int(payload.get("limite", 0))
    except Exception:
        v = 0
    if not (1 <= v <= 500):
        raise HTTPException(status_code=400, detail="valeur hors bornes (1-500)")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS system_health (
                               cle TEXT PRIMARY KEY, statut TEXT NOT NULL,
                               detail TEXT, maj TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            cur.execute("""INSERT INTO system_health (cle, statut, detail, maj)
                           VALUES ('downloader_limite',%s,'réglé depuis la page de régie',NOW())
                           ON CONFLICT (cle) DO UPDATE
                             SET statut=EXCLUDED.statut, detail=EXCLUDED.detail, maj=NOW()""",
                        (str(v),))
        conn.commit()
        conn.close()
        return {"ok": True, "message": f"Quota réglé à {v} titres/jour (effet à la prochaine passe)."}
    except Exception as e:
        return {"ok": False, "message": str(e)[:140]}


@app.post("/api/regie/cookies")
def regie_cookies(request: Request, payload: dict = Body(...)):
    """Dépose de nouveaux cookies YouTube (collés depuis le navigateur du chef).
    Évite d'ouvrir un accès à son PC : il exporte, il colle, c'est fini."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    contenu = str(payload.get("contenu", ""))
    if "\t" not in contenu or len(contenu) < 200:
        return {"ok": False, "message": "Format inattendu : il faut le fichier cookies.txt "
                                        "au format Netscape (colonnes séparées par des tabulations)."}
    if ".youtube.com" not in contenu:
        return {"ok": False, "message": "Aucun cookie youtube.com trouvé dans ce fichier."}
    try:
        with open(COOKIES_PATH, "w", encoding="utf-8") as f:
            f.write(contenu if contenu.endswith("\n") else contenu + "\n")
        try:
            os.chmod(COOKIES_PATH, 0o600)
        except Exception:
            pass
        n = sum(1 for l in contenu.splitlines() if l and not l.startswith("#"))
        return {"ok": True, "message": f"Cookies enregistrés ({n} lignes). "
                                       "Le downloader les utilisera à sa prochaine passe."}
    except Exception as e:
        return {"ok": False, "message": "Écriture impossible : " + str(e)[:120]}


@app.get("/regie", response_class=HTMLResponse)
def regie_dash(request: Request):
    """Tableau de bord unique : tout le pilotage au même endroit (ergonomie Caleope UI,
    couleurs Gaiverland). Les pages /regie/voix, /regie/sante et /regie/musique restent
    accessibles séparément — les liens déjà en favori continuent de marcher."""
    if not _admin_ok(request):
        # Non connecté → on l'envoie se connecter sur le site (retour automatique ici).
        # Connecté mais PAS fondateur → 404, la page n'existe pas pour lui.
        if not _uid(request):
            r = RedirectResponse("/?connexion=1", status_code=302)
            r.set_cookie("gnext", "/regie", max_age=600, httponly=True,
                         secure=True, samesite="lax")
            return r
        raise HTTPException(status_code=404, detail="Not Found")
    r = HTMLResponse(REGIE_DASH)
    if request.query_params.get("k"):
        r.set_cookie("gadm", ADMIN_TOKEN, max_age=7776000, httponly=True,
                     secure=True, samesite="lax")
    return r


@app.get("/regie/musique", response_class=HTMLResponse)
def regie_musique_page(request: Request):
    if not _admin_ok(request):
        if not _uid(request):
            r = RedirectResponse("/?connexion=1", status_code=302)
            r.set_cookie("gnext", "/regie/musique", max_age=600, httponly=True,
                         secure=True, samesite="lax")
            return r
        raise HTTPException(status_code=404, detail="Not Found")
    r = HTMLResponse(MUSIQUE_PAGE)
    if request.query_params.get("k"):
        r.set_cookie("gadm", ADMIN_TOKEN, max_age=7776000, httponly=True,
                     secure=True, samesite="lax")
    return r


# Actions que la régie a le droit de demander. La page ne touche JAMAIS à Docker :
# elle dépose une ligne en base, et l'exécuteur de l'hôte (cron root, /1 min) applique
# uniquement ce qui figure dans SA propre liste blanche. Double barrière.
ACTIONS_OK = ("relancer", "journal", "sauvegarde", "controle_qualite")
SERVICES_OK = ("gaiverland-playlist", "gaiverland-scheduler", "gaiverland-rebexis",
               "gaiverland-tts", "gaiverland-downloader", "gaiverland-analyzer",
               "gaiverland-gcs-web", "gaiverland-gcs-vote", "gaiverland-gcs-track",
               "gaiverland-gcs-lore", "gaiverland-gcs-state", "gaiverland-gcs-weather",
               "azuracast", "azuracast-discord-bot")


@app.post("/api/regie/commande")
def regie_commande(request: Request, payload: dict = Body(...)):
    """Dépose une demande de maintenance. Elle sera exécutée dans la minute."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    action = str(payload.get("action", ""))
    cible = str(payload.get("cible", "")).strip() or None
    if action not in ACTIONS_OK:
        raise HTTPException(status_code=400, detail="action non autorisée")
    if action in ("relancer", "journal") and cible not in SERVICES_OK:
        raise HTTPException(status_code=400, detail="service non autorisé")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS system_commandes (
                             id SERIAL PRIMARY KEY, action TEXT NOT NULL, cible TEXT,
                             etat TEXT NOT NULL DEFAULT 'en attente', resultat TEXT,
                             cree_le TIMESTAMPTZ DEFAULT NOW(), traite_le TIMESTAMPTZ)""")
            cur.execute("SELECT count(*) n FROM system_commandes WHERE etat='en attente'")
            if cur.fetchone()["n"] >= 10:
                conn.close()
                return {"ok": False, "message": "Trop de demandes en attente, patiente un peu."}
            cur.execute("INSERT INTO system_commandes (action, cible) VALUES (%s,%s) RETURNING id",
                        (action, cible))
            nid = cur.fetchone()["id"]
        conn.commit()
        conn.close()
        return {"ok": True, "id": nid,
                "message": "Demande enregistrée — elle s'exécute dans la minute."}
    except Exception as e:
        return {"ok": False, "message": str(e)[:140]}


@app.get("/api/regie/commandes")
def regie_commandes(request: Request):
    """Historique récent des demandes de maintenance, avec leur résultat."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT id, action, cible, etat, resultat,
                                  to_char(cree_le,'DD/MM HH24:MI') quand
                           FROM system_commandes ORDER BY id DESC LIMIT 12""")
            out = [{"id": r["id"], "action": r["action"], "cible": r["cible"] or "",
                    "etat": r["etat"], "resultat": (r["resultat"] or "")[:5000],
                    "quand": r["quand"]} for r in cur.fetchall()]
        conn.close()
        return {"commandes": out, "services": list(SERVICES_OK)}
    except Exception as e:
        return {"commandes": [], "services": list(SERVICES_OK), "erreur": str(e)[:120]}


@app.get("/api/regie/voix/reglages")
def regie_voix_reglages_lire(request: Request):
    """Réglages de voix actuellement appliqués (base, sinon défauts du moteur)."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    defauts = {"modele": "eleven_v3", "stability": 0.30, "style": 0.75,
               "similarity_boost": 0.75, "speed": 1.0}
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT cle, statut FROM system_health WHERE cle LIKE 'voix_%'")
            for r in cur.fetchall():
                nom = r["cle"].replace("voix_", "")
                if nom in defauts:
                    defauts[nom] = r["statut"] if nom == "modele" else float(r["statut"])
        conn.close()
    except Exception:
        pass
    return {"reglages": defauts}


@app.post("/api/regie/voix/reglages")
def regie_voix_reglages_ecrire(request: Request, payload: dict = Body(...)):
    """Modifie la voix à chaud. Le moteur relit ces valeurs à chaque phrase générée."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    bornes = {"stability": (0.0, 1.0), "style": (0.0, 1.0),
              "similarity_boost": (0.0, 1.0), "speed": (0.7, 1.3)}
    a_ecrire = []
    for nom, (mini, maxi) in bornes.items():
        if nom in payload:
            try:
                v = float(payload[nom])
            except Exception:
                raise HTTPException(status_code=400, detail=f"{nom} : valeur invalide")
            if not (mini <= v <= maxi):
                raise HTTPException(status_code=400,
                                    detail=f"{nom} doit être entre {mini} et {maxi}")
            a_ecrire.append(("voix_" + nom, str(v)))
    if "modele" in payload:
        mod = str(payload["modele"])
        if mod not in ("eleven_v3", "eleven_multilingual_v2"):
            raise HTTPException(status_code=400, detail="modèle inconnu")
        a_ecrire.append(("voix_modele", mod))
    if not a_ecrire:
        raise HTTPException(status_code=400, detail="rien à modifier")
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS system_health (
                             cle TEXT PRIMARY KEY, statut TEXT NOT NULL,
                             detail TEXT, maj TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
            for cle, val in a_ecrire:
                cur.execute("""INSERT INTO system_health (cle, statut, detail, maj)
                               VALUES (%s,%s,'réglé depuis la régie',NOW())
                               ON CONFLICT (cle) DO UPDATE
                                 SET statut=EXCLUDED.statut, maj=NOW()""", (cle, val))
        conn.commit()
        conn.close()
        return {"ok": True, "message": "Voix ajustée — effet dès la prochaine phrase fabriquée."}
    except Exception as e:
        return {"ok": False, "message": str(e)[:140]}


@app.get("/dl/{nom}")
def telecharger_pack(nom: str):
    """Sert les paquets d'outils (ex. gaiverland-dl.zip) depuis scripts/packs/.
    Route re-posée le 05/08 : elle existait sur l'ancienne instance et s'est perdue
    dans une migration. Anti-traversée : basename + liste du dossier."""
    from fastapi.responses import FileResponse
    nom = os.path.basename(nom)
    rep = os.path.join(os.path.dirname(os.path.abspath(__file__)), "packs")
    chemin = os.path.join(rep, nom)
    if not os.path.isdir(rep) or nom not in os.listdir(rep) or not os.path.isfile(chemin):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(chemin, media_type="application/zip", filename=nom)


# ── Téléchargement LOCAL (gaiverland-dl sur le Mac/PC du chef) ──────────────
# Le serveur est bridé par YouTube (quota + cookies qui brûlent) ; la machine du chef
# ne l'est pas. Le client gaiverland-dl récupère la file, télécharge chez lui, et
# dépose les mp3 ici. Corps BRUT (pas de multipart : python-multipart n'est pas dans
# l'image, et une route Form sans lui ferait planter TOUT le site au démarrage).
MEDIA_DEPOT = "/media-musique"
DOSSIER_RE = re.compile(r"^[a-z0-9_-]{1,24}$")


EL_ENV = "/secrets/elevenlabs.env"


@app.post("/api/regie/voix/cle")
def regie_voix_cle(request: Request, payload: dict = Body(...)):
    """Remplace la clé ElevenLabs depuis la régie. On VALIDE la clé en direct auprès
    d'ElevenLabs avant de l'écrire — une clé refusée ne remplace jamais une clé en
    place. Puis on demande à l'exécuteur de relancer le moteur de voix (le conteneur
    doit être relancé pour relire son env_file)."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    cle = str(payload.get("cle", "")).strip()
    if not re.fullmatch(r"sk_[A-Za-z0-9]{16,96}", cle):
        raise HTTPException(status_code=400,
                            detail="format inattendu — une clé ElevenLabs commence par sk_")
    # Épreuve du feu, en DEUX temps. Les clés ElevenLabs se créent maintenant avec des
    # permissions à cocher : une clé limitée au Text-to-Speech est VALABLE pour parler
    # mais refusée sur l'endpoint « abonnement ». On distingue donc « clé morte » de
    # « clé vivante mais myope », au lieu d'un refus aveugle (vécu par le chef le 12/08).
    avert = ""
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/user/subscription",
                      headers={"xi-api-key": cle}, timeout=25)
    except Exception as e:
        return {"ok": False, "message": "ElevenLabs injoignable : " + str(e)[:80]}
    if r.status_code == 200:
        d = r.json()
    else:
        try:
            detail = r.json().get("detail", {})
            raison = detail.get("message") or detail.get("status") or str(detail)[:80]
        except Exception:
            raison = ""
        # 2e chance : la clé sait-elle au moins parler ? (liste des voix = permission TTS)
        try:
            r2 = httpx.get("https://api.elevenlabs.io/v1/voices",
                           headers={"xi-api-key": cle}, timeout=25)
        except Exception as e:
            return {"ok": False, "message": "ElevenLabs injoignable : " + str(e)[:80]}
        if r2.status_code != 200:
            return {"ok": False, "message": f"clé refusée par ElevenLabs (HTTP {r.status_code}"
                    + (f" — {raison}" if raison else "") + ") — rien n'a été modifié. "
                    "Vérifie que tu as copié la clé complète, ou recrée-la."}
        d = {}
        avert = (" ⚠️ Cette clé est restreinte : la voix marchera, mais la régie ne pourra "
                 "PAS afficher tes crédits (l'accès « User / Read » manque). Pour revoir les "
                 "chiffres, recrée la clé sur elevenlabs.io avec toutes les permissions et "
                 "remplace-la à nouveau ici.")
    try:
        lignes = []
        with open(EL_ENV, encoding="utf-8") as f:
            for l in f:
                if l.startswith("ELEVENLABS_API_KEY="):
                    l = "ELEVENLABS_API_KEY=" + cle + "\n"
                lignes.append(l)
        with open(EL_ENV, "w", encoding="utf-8") as f:
            f.writelines(lignes)
    except Exception as e:
        return {"ok": False, "message": "écriture impossible : " + str(e)[:90]}
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO system_commandes (action, cible)
                           VALUES ('relancer', 'gaiverland-tts')""")
        conn.commit()
        conn.close()
    except Exception:
        pass
    if d:
        msg = ("Clé validée (%s/%s caractères utilisés) — effet immédiat."
               % (d.get("character_count", "?"), d.get("character_limit", "?")))
    else:
        msg = "Clé acceptée — effet immédiat." + avert
    return {"ok": True, "message": msg}


@app.get("/api/regie/dl-local/attente")
def dl_local_attente(request: Request):
    """La file de téléchargement, vue par le client local : propositions acceptées
    non descendues + seeds thématiques en attente."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    out = []
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT title FROM proposal_decisions
                           WHERE verdict='accept' AND downloaded_at IS NULL
                           ORDER BY title LIMIT 300""")
            out += [{"type": "proposition", "cle": r["title"], "dossier": "community"}
                    for r in cur.fetchall()]
            cur.execute("""SELECT theme, query FROM thematic_seeds
                           WHERE status='pending' ORDER BY theme, query LIMIT 300""")
            out += [{"type": "seed", "cle": r["query"], "dossier": r["theme"]}
                    for r in cur.fetchall()]
        conn.close()
    except Exception as e:
        return {"attente": [], "erreur": str(e)[:120]}
    return {"attente": out}


@app.post("/api/regie/dl-local/depot")
async def dl_local_depot(request: Request, type: str = "", cle: str = "",
                         dossier: str = "community", nom: str = ""):
    """Réceptionne un mp3 téléchargé en local et le range dans le bon bac média."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    if type not in ("proposition", "seed") or not cle:
        raise HTTPException(status_code=400, detail="type/cle invalides")
    if not DOSSIER_RE.fullmatch(dossier):
        raise HTTPException(status_code=400, detail="dossier invalide")
    # Nom de fichier : on neutralise tout ce qui ressemble à un chemin.
    nom = os.path.basename(nom).replace("\x00", "").strip()
    if not nom.lower().endswith(".mp3") or len(nom) < 8 or len(nom) > 180:
        raise HTTPException(status_code=400, detail="nom de fichier invalide")
    corps = await request.body()
    if len(corps) < 400_000 or len(corps) > 40_000_000:
        raise HTTPException(status_code=400, detail=f"taille suspecte ({len(corps)} octets)")
    if not (corps[:3] == b"ID3" or corps[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")):
        raise HTTPException(status_code=400, detail="ce n'est pas un mp3")
    rep = os.path.join(MEDIA_DEPOT, dossier)
    try:
        os.makedirs(rep, exist_ok=True)
        chemin = os.path.join(rep, nom)
        with open(chemin, "wb") as f:
            f.write(corps)
        os.chown(chemin, 1000, 1000)
    except Exception as e:
        raise HTTPException(status_code=500, detail="écriture impossible : " + str(e)[:90])
    # Marquer la file + demander UN scan média à l'exécuteur (pas un par fichier).
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            if type == "proposition":
                cur.execute("""UPDATE proposal_decisions SET downloaded_at=NOW(),
                               download_status='ok-local' WHERE title=%s""", (cle,))
            else:
                cur.execute("""UPDATE thematic_seeds SET status='ok', downloaded_at=NOW()
                               WHERE query=%s AND theme=%s""", (cle, dossier))
            cur.execute("""INSERT INTO system_commandes (action, cible)
                           SELECT 'scan_media', NULL
                           WHERE NOT EXISTS (SELECT 1 FROM system_commandes
                                             WHERE action='scan_media' AND etat='en attente')""")
        conn.commit()
        conn.close()
    except Exception as e:
        return {"ok": True, "avert": "fichier posé mais file non marquée : " + str(e)[:90]}
    return {"ok": True, "message": f"{nom} rangé dans music/{dossier}/"}


@app.get("/api/regie/sante")
def regie_sante(request: Request):
    """Tout l'état du festival en un appel — pensé pour que le chef se passe d'agent :
    scènes, services, voix, contenu, système. Aucune IA, que des faits mesurés."""
    if not _admin_ok(request):
        raise HTTPException(status_code=404, detail="Not Found")
    out = {"scenes": [], "services": [], "voix": {}, "contenu": {}, "systeme": {}, "alertes": []}

    # ── Scènes (AzuraCast) ──────────────────────────────────────────────────
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying", timeout=8)
        for st in (r.json() if r.status_code == 200 else []):
            song = (st.get("now_playing", {}) or {}).get("song", {}) or {}
            en_ligne = bool(st.get("is_online"))
            out["scenes"].append({
                "nom": (st.get("station", {}) or {}).get("name", "?"),
                "en_ligne": en_ligne,
                "auditeurs": (st.get("listeners", {}) or {}).get("current", 0),
                "titre": (song.get("title") or "")[:70],
                "artiste": (song.get("artist") or "")[:40],
            })
            if not en_ligne:
                out["alertes"].append("Scène hors ligne : " + (st.get("station", {}) or {}).get("name", "?"))
    except Exception as e:
        out["alertes"].append("AzuraCast injoignable : " + str(e)[:60])

    # ── Services internes (health) ──────────────────────────────────────────
    # On essaie PLUSIEURS adresses : certaines variables d'env sont périmées (ex.
    # GCS_REBEXIS_URL pointait sur gcs-rebexis:8092 qui n'existe pas, le vrai est
    # gw-rebexis:8081) → sans repli on affichait « en panne » à tort.
    # NB : le worker TTS n'a AUCUN serveur HTTP, on ne le sonde donc pas ici.
    SERVICES = (
        ("moteur d'état", "GCS_STATE_ENGINE_URL", ("http://gcs-state-engine:8091",)),
        ("Rebexis",       "GCS_REBEXIS_URL",      ("http://gw-rebexis:8081",)),
        ("titres",        "GCS_TRACK_URL",        ("http://gcs-track-service:8090",)),
        ("journal (lore)", "GCS_LORE_SERVICE_URL", ("http://gcs-lore-service:8096",)),
        ("votes",         "GCS_VOTE_URL",         ("http://gcs-vote-service:8095",)),
        ("météo",         "GCS_WEATHER_URL",      ("http://gcs-weather:8098",)),
    )
    for nom, env, replis in SERVICES:
        candidats = [u for u in (os.environ.get(env, "").rstrip("/"),) if u] + list(replis)
        ok = False
        for base in candidats:
            try:
                if httpx.get(base + "/health", timeout=4).status_code == 200:
                    ok = True
                    break
            except Exception:
                continue
        out["services"].append({"nom": nom, "ok": ok})
        if not ok:
            out["alertes"].append("Service en panne : " + nom)

    # ── Base : voix, contenu, état déposé par le QC et les sauvegardes ──────
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""SELECT COALESCE(r.status,'pending') s, count(*) n
                           FROM tts_library l LEFT JOIN tts_review r ON r.text_hash=l.text_hash
                           WHERE l.audio_file IS NOT NULL GROUP BY 1""")
            out["voix"] = {row["s"]: row["n"] for row in cur.fetchall()}

            cur.execute("""SELECT COALESCE(substring(file_path from '/music/([a-z0-9]+)/'),'mainstage') bac,
                                  count(*) n FROM tracks GROUP BY 1 ORDER BY n DESC""")
            out["contenu"]["bacs"] = [{"nom": r["bac"], "n": r["n"]} for r in cur.fetchall()]

            for cle, sql in (("votes", "SELECT count(*) n FROM votes"),
                             ("propositions", "SELECT count(*) n FROM title_proposals"),
                             ("lore", "SELECT count(*) n FROM lore_events"),
                             ("passages_24h", "SELECT count(*) n FROM play_history "
                                              "WHERE played_at > now()-interval '24 hours'")):
                try:
                    cur.execute(sql)
                    out["contenu"][cle] = cur.fetchone()["n"]
                except Exception:
                    conn.rollback()
                    out["contenu"][cle] = None

            try:
                cur.execute("SELECT cle, statut, detail, "
                            "round(extract(epoch FROM (now()-maj))/60) AS min FROM system_health")
                for r in cur.fetchall():
                    # system_health sert aussi de boîte à réglages (voix_*, downloader_limite…).
                    # Ces lignes-là ne sont PAS des indicateurs de santé : affichées telles quelles
                    # elles s'allument en rouge parce que leur « statut » est une valeur, pas un OK.
                    if r["cle"].startswith("voix_") or r["cle"] in ("downloader_limite",
                                                                    "downloader_pause"):
                        continue
                    out["systeme"][r["cle"]] = {"statut": r["statut"], "detail": r["detail"],
                                                "il_y_a_min": int(r["min"] or 0)}
            except Exception:
                conn.rollback()
        conn.close()
    except Exception as e:
        out["alertes"].append("Base de données injoignable : " + str(e)[:60])

    # ── Disque de l'hôte (vu à travers le bind-mount /app) ──────────────────
    try:
        v = os.statvfs("/app")
        pris = (v.f_blocks - v.f_bfree) * v.f_frsize
        tot = v.f_blocks * v.f_frsize
        pct = round(100 * pris / tot)
        out["systeme"]["disque"] = {"statut": "OK" if pct < 85 else "WARN",
                                    "detail": f"{pris/2**30:.0f} Go utilisés sur {tot/2**30:.0f} Go ({pct} %)",
                                    "il_y_a_min": 0}
        if pct >= 85:
            out["alertes"].append(f"Disque à {pct} %")
    except Exception:
        pass

    # Sauvegarde trop vieille = alerte (le timer tourne à 04:30, >36 h = anormal)
    sv = out["systeme"].get("sauvegarde")
    if sv and sv["il_y_a_min"] > 36 * 60:
        out["alertes"].append("Aucune sauvegarde depuis plus de 36 h")
    qc = out["systeme"].get("qc")
    if qc and qc["statut"] in ("WARN", "FAIL"):
        out["alertes"].append("Contrôle qualité en " + qc["statut"])
    return out


@app.get("/regie/sante", response_class=HTMLResponse)
def regie_sante_page(request: Request):
    if not _admin_ok(request):
        if not _uid(request):
            r = RedirectResponse("/?connexion=1", status_code=302)
            r.set_cookie("gnext", "/regie/sante", max_age=600, httponly=True,
                         secure=True, samesite="lax")
            return r
        raise HTTPException(status_code=404, detail="Not Found")
    r = HTMLResponse(SANTE_PAGE)
    if request.query_params.get("k"):
        r.set_cookie("gadm", ADMIN_TOKEN, max_age=7776000, httponly=True,
                     secure=True, samesite="lax")
    return r


@app.get("/regie/voix", response_class=HTMLResponse)
def regie_page(request: Request):
    if not _admin_ok(request):
        if not _uid(request):
            r = RedirectResponse("/?connexion=1", status_code=302)
            r.set_cookie("gnext", "/regie/voix", max_age=600, httponly=True,
                         secure=True, samesite="lax")
            return r
        raise HTTPException(status_code=404, detail="Not Found")
    r = HTMLResponse(REGIE_PAGE)
    if request.query_params.get("k"):   # mémorise le jeton → lien simple ensuite
        r.set_cookie("gadm", ADMIN_TOKEN, max_age=7776000, httponly=True,
                     secure=True, samesite="lax")
    return r


REGIE_DASH = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Régie Gaiverland</title>
<style>
:root{--cream:#fff4e6;--nuit:#1b1030;--vio:#3d1d5c;--mag:#6b2f6b;
      --or1:#ffd29a;--or2:#ffb56b;--vert:#2ecc71;--rouge:#ff5a5a;--jaune:#ffc44d}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,sans-serif;color:var(--cream);
  background:linear-gradient(175deg,var(--nuit),var(--vio) 45%,var(--mag));min-height:100vh}
.app{display:flex;min-height:100vh}

/* ── Barre latérale (ergonomie Caleope UI) ───────────────────────── */
.sidebar{width:215px;flex:0 0 215px;background:rgba(20,10,35,.72);
  border-right:1px solid rgba(255,244,230,.14);padding:16px 12px;position:sticky;top:0;
  height:100vh;overflow-y:auto}
.marque{font-family:Georgia,serif;font-size:20px;font-weight:bold;letter-spacing:1px;
  background:linear-gradient(90deg,var(--or1),#ff8fa3,#c9b6ff);-webkit-background-clip:text;
  background-clip:text;color:transparent;margin-bottom:2px}
.sstitre{font-size:11px;opacity:.6;margin-bottom:16px}
.sb-groupe{font-size:10px;letter-spacing:1.4px;text-transform:uppercase;opacity:.5;
  margin:16px 0 6px 8px}
.nav-btn{display:flex;align-items:center;gap:9px;width:100%;text-align:left;border:none;
  background:transparent;color:var(--cream);padding:10px 11px;border-radius:10px;cursor:pointer;
  font-size:14px;font-family:inherit;margin-bottom:2px;transition:background .12s}
.nav-btn:hover{background:rgba(255,244,230,.1)}
.nav-btn.on{background:rgba(255,244,230,.17);font-weight:bold}
.nav-btn .ico{font-size:16px;width:20px;text-align:center}
.badge{margin-left:auto;background:var(--rouge);color:#3d0a0a;font-size:10px;font-weight:bold;
  padding:1px 7px;border-radius:9px}
.badge.vert{background:var(--vert);color:#08210f}

/* ── Contenu ──────────────────────────────────────────────────────── */
main{flex:1;padding:20px 22px 60px;max-width:1000px}
.entete{display:flex;align-items:center;gap:12px;margin-bottom:4px;flex-wrap:wrap}
h1{margin:0;font-size:23px}
.maj{font-size:12px;opacity:.6}
.section-content{display:none}
.section-content.actif{display:block}
h2{font-size:13px;letter-spacing:1.3px;text-transform:uppercase;opacity:.72;margin:24px 0 10px}
.card{border:1px solid rgba(255,244,230,.17);border-radius:14px;padding:13px;
  background:rgba(255,244,230,.07);margin-bottom:9px}
.card.alerte{background:rgba(255,90,90,.16);border-color:rgba(255,90,90,.55)}
.card.bien{background:rgba(46,204,113,.13);border-color:rgba(46,204,113,.45)}
.row{display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.nom{font-weight:bold;font-size:15px}
.det{font-size:13px;opacity:.83;margin-top:4px;white-space:pre-line}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:9px}
.kpi{border:1px solid rgba(255,244,230,.17);border-radius:12px;padding:12px;text-align:center;
  background:rgba(255,244,230,.07)}
.kpi b{display:block;font-size:23px;margin-bottom:2px}
.kpi span{font-size:11px;opacity:.72}
.pill{font-size:11px;padding:2px 9px;border-radius:10px;font-weight:bold}
.ok{background:var(--vert);color:#08210f}
.ko{background:var(--rouge);color:#3d0a0a}
.warn{background:var(--jaune);color:#3d2a00}
.field{margin-bottom:11px}
.field-label{display:block;font-size:12px;opacity:.75;margin-bottom:5px}
.field-input{width:100%;padding:11px;border-radius:10px;border:1px solid rgba(255,244,230,.28);
  background:rgba(0,0,0,.3);color:var(--cream);font-size:15px;font-family:inherit}
textarea.field-input{min-height:110px;font-size:12px}
.btn{border:none;border-radius:11px;padding:11px 16px;font-size:14px;font-weight:bold;
  cursor:pointer;min-height:44px;font-family:inherit}
.btn-or{background:linear-gradient(135deg,var(--or1),var(--or2));color:var(--nuit)}
.btn-vert{background:var(--vert);color:#08210f}
.btn-rouge{background:var(--rouge);color:#3d0a0a}
.btn-vio{background:rgba(255,244,230,.16);color:var(--cream)}
.btn-sm{padding:8px 13px;font-size:13px;min-height:38px}
.onglets{display:flex;gap:7px;flex-wrap:wrap;margin-bottom:12px}
.onglets .btn{border-radius:20px;min-height:40px;padding:10px 16px;font-size:13px;
  background:rgba(255,244,230,.1);color:var(--cream);border:1px solid rgba(255,244,230,.28)}
.onglets .btn.on{background:var(--cream);color:var(--nuit)}
.sugg{border:1px solid rgba(255,244,230,.25);border-radius:11px;margin-top:7px;overflow:hidden}
.sugg div{padding:11px 13px;cursor:pointer;font-size:14px;border-bottom:1px solid rgba(255,244,230,.12)}
.sugg div:hover{background:rgba(255,244,230,.13)}
.sugg small{opacity:.63}
.msg{margin-top:9px;font-size:13px;opacity:.92}
.vide{opacity:.6;font-size:13px}
@media(max-width:820px){
  .app{flex-direction:column}
  .sidebar{width:auto;flex:none;height:auto;position:sticky;top:0;z-index:20;
    border-right:none;border-bottom:1px solid rgba(255,244,230,.14);padding:11px}
  .marque,.sstitre,.sb-groupe{display:none}
  .sb-nav{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px}
  .nav-btn{width:auto;flex:0 0 auto;padding:9px 13px;margin:0;white-space:nowrap;font-size:13px}
  .nav-btn .lib{display:none}
  .nav-btn .ico{font-size:18px}
  .nav-btn.on .lib{display:inline}
  main{padding:16px 14px 60px}
}
</style></head><body>
<div class="app">
  <nav class="sidebar" role="navigation">
    <div class="marque">GAIVERLAND</div>
    <div class="sstitre">Régie du festival</div>
    <div class="sb-nav" id="nav"></div>
  </nav>
  <main>
    <div class="entete">
      <h1 id="titre">Vue d'ensemble</h1>
      <span class="maj" id="maj"></span>
      <button class="btn btn-vio btn-sm" style="margin-left:auto" onclick="charger(true)">Rafraîchir</button>
    </div>

    <div class="section-content actif" id="s-vue"></div>
    <div class="section-content" id="s-scenes"></div>
    <div class="section-content" id="s-musique"></div>
    <div class="section-content" id="s-dl"></div>
    <div class="section-content" id="s-voix"></div>
    <div class="section-content" id="s-systeme"></div>
  </main>
</div>
<audio id="pl"></audio>
<script>
const LT=String.fromCharCode(60), GT=String.fromCharCode(62), NL=String.fromCharCode(10);
const AMP=String.fromCharCode(38), GUI=String.fromCharCode(34);
let CMD={commandes:[],services:[]}, VOIX={};
function esc(s){ return String(s==null?"":s)
  .split(AMP).join(AMP+"amp;").split(LT).join(AMP+"lt;")
  .split(GT).join(AMP+"gt;").split(GUI).join(AMP+"quot;"); }
function el(t,c,h){ const e=document.createElement(t); if(c)e.className=c; if(h!=null)e.innerHTML=h; return e; }

const SECTIONS=[
  {id:"vue",     ico:"🎪", lib:"Vue d'ensemble", groupe:"Antenne"},
  {id:"scenes",  ico:"📻", lib:"Les scènes",     groupe:"Antenne"},
  {id:"musique", ico:"🎵", lib:"Musique",        groupe:"Contenu"},
  {id:"dl",      ico:"⬇️", lib:"Téléchargeur",   groupe:"Contenu"},
  {id:"voix",    ico:"🎙️", lib:"Voix Rebexis",   groupe:"Contenu"},
  {id:"systeme", ico:"🛠️", lib:"Système",        groupe:"Maintenance"}
];
let SANTE={}, MUSIQUE={}, JINGLES={}, MODE=0, tmr=null, COURANT="vue", playing=null;

function construireNav(){
  const nav=document.getElementById("nav");
  let groupe=null;
  SECTIONS.forEach(function(s){
    if(s.groupe!==groupe && window.innerWidth>820){
      nav.appendChild(el("div","sb-groupe",esc(s.groupe))); groupe=s.groupe;
    }
    const b=el("button","nav-btn"+(s.id===COURANT?" on":""),
      LT+'span class="ico"'+GT+s.ico+LT+"/span"+GT+
      LT+'span class="lib"'+GT+esc(s.lib)+LT+"/span"+GT+
      LT+'span id="bdg-'+s.id+'"'+GT+LT+"/span"+GT);
    b.onclick=function(){ aller(s.id); };
    b.id="nav-"+s.id;
    nav.appendChild(b);
  });
}

function aller(id){
  COURANT=id;
  SECTIONS.forEach(function(s){
    document.getElementById("nav-"+s.id).classList.toggle("on",s.id===id);
    document.getElementById("s-"+s.id).classList.toggle("actif",s.id===id);
    if(s.id===id) document.getElementById("titre").textContent=s.lib;
  });
  window.scrollTo(0,0);
}

/* ── Rendu des sections ─────────────────────────────────────────── */
function rendreVue(){
  const d=SANTE, z=document.getElementById("s-vue");
  const scOK=(d.scenes||[]).filter(function(s){return s.en_ligne;}).length;
  const svOK=(d.services||[]).filter(function(s){return s.ok;}).length;
  let h="";
  if(d.alertes && d.alertes.length){
    h+=LT+"h2"+GT+"À regarder"+LT+"/h2"+GT;
    d.alertes.forEach(function(a){
      h+=LT+'div class="card alerte"'+GT+LT+'div class="nom"'+GT+"⚠ "+esc(a)+LT+"/div"+GT+LT+"/div"+GT;
    });
  }else{
    h+=LT+"h2"+GT+"État général"+LT+"/h2"+GT+LT+'div class="card bien"'+GT+
       LT+'div class="nom"'+GT+"✅ Tout va bien"+LT+"/div"+GT+
       LT+'div class="det"'+GT+"Aucune anomalie détectée."+LT+"/div"+GT+LT+"/div"+GT;
  }
  const v=d.voix||{}, c=d.contenu||{}, dl=MUSIQUE.downloader||{};
  h+=LT+"h2"+GT+"En un coup d'œil"+LT+"/h2"+GT+LT+'div class="grid"'+GT+
    kpi(scOK+"/"+(d.scenes||[]).length,"scènes en ligne")+
    kpi(svOK+"/"+(d.services||[]).length,"services OK")+
    kpi(v.pending||0,"jingles à écouter")+
    kpi(c.passages_24h==null?"?":c.passages_24h,"titres joués (24 h)")+
    kpi(dl.en_file==null?"?":dl.en_file,"en file de téléchargement")+
    kpi((MUSIQUE.attente||[]).length,"demandes en attente")+
    LT+"/div"+GT;
  z.innerHTML=h;
}
function kpi(v,l){ return LT+'div class="kpi"'+GT+LT+"b"+GT+v+LT+"/b"+GT+
  LT+"span"+GT+esc(l)+LT+"/span"+GT+LT+"/div"+GT; }

function rendreScenes(){
  document.getElementById("s-scenes").innerHTML=
    (SANTE.scenes||[]).map(function(s){
      return LT+'div class="card"'+GT+LT+'div class="row"'+GT+
        LT+'span class="nom"'+GT+esc(s.nom)+LT+"/span"+GT+
        LT+'span class="pill '+(s.en_ligne?"ok":"ko")+'"'+GT+(s.en_ligne?"en ligne":"hors ligne")+LT+"/span"+GT+
        LT+'span class="pill warn"'+GT+s.auditeurs+" auditeur(s)"+LT+"/span"+GT+LT+"/div"+GT+
        LT+'div class="det"'+GT+esc(s.artiste?s.artiste+" — ":"")+esc(s.titre||"—")+LT+"/div"+GT+
        LT+"/div"+GT;
    }).join("");
}

function rendreMusique(){
  const z=document.getElementById("s-musique");
  z.innerHTML=
    LT+"h2"+GT+"Ajouter"+LT+"/h2"+GT+
    LT+'div class="card"'+GT+
      LT+'div class="onglets"'+GT+
        LT+'button class="btn on" id="o0" onclick="mode(0)"'+GT+"Rechercher un titre"+LT+"/button"+GT+
        LT+'button class="btn" id="o1" onclick="mode(1)"'+GT+"Saisie libre (remix)"+LT+"/button"+GT+
        LT+'button class="btn" id="o2" onclick="mode(2)"'+GT+"Un artiste entier"+LT+"/button"+GT+
      LT+"/div"+GT+
      LT+'div id="m0"'+GT+
        LT+'div class="field"'+GT+LT+'label class="field-label"'+GT+"Artiste ou titre"+LT+"/label"+GT+
        LT+'input class="field-input" id="q" autocomplete="off" oninput="chercher()"'+GT+LT+"/div"+GT+
        LT+'div class="sugg" id="sugg" hidden'+GT+LT+"/div"+GT+
      LT+"/div"+GT+
      LT+'div id="m1" hidden'+GT+
        LT+'div class="field"'+GT+LT+'label class="field-label"'+GT+
          "Texte exact à chercher (remix, édition, bootleg)"+LT+"/label"+GT+
        LT+'input class="field-input" id="libre" placeholder="Artiste - Titre (Machin Remix)"'+GT+LT+"/div"+GT+
        LT+'button class="btn btn-or" onclick="ajouterLibre()"'+GT+"Ajouter"+LT+"/button"+GT+
      LT+"/div"+GT+
      LT+'div id="m2" hidden'+GT+
        LT+'div class="field"'+GT+LT+'label class="field-label"'+GT+"Nom de l'artiste"+LT+"/label"+GT+
        LT+'input class="field-input" id="art" placeholder="Ex : Headhunterz"'+GT+LT+"/div"+GT+
        LT+'div class="row"'+GT+
          LT+'span style="font-size:13px;opacity:.8"'+GT+"Combien ?"+LT+"/span"+GT+
          LT+'input class="field-input" id="nb" type="number" min="0" max="500" value="0" style="width:92px"'+GT+
          LT+'span style="font-size:12px;opacity:.7"'+GT+"0 = tout son catalogue"+LT+"/span"+GT+
          LT+'button class="btn btn-or" onclick="ajouterArtiste()"'+GT+"Demander"+LT+"/button"+GT+
        LT+"/div"+GT+
        LT+'div class="msg"'+GT+"On récupère son catalogue complet (jusqu'à 200 titres, featurings "+
          "compris), on écarte ce qui est déjà en bibliothèque, et on met tout le reste en file. "+
          "Le quota quotidien du téléchargeur règle ensuite le rythme."+LT+"/div"+GT+
      LT+"/div"+GT+
      LT+'div class="msg" id="msg-ajout"'+GT+LT+"/div"+GT+
    LT+"/div"+GT+
    LT+"h2"+GT+"Demandes en attente"+LT+"/h2"+GT+LT+'div id="attente"'+GT+LT+"/div"+GT+
    LT+"h2"+GT+"Dernières décisions"+LT+"/h2"+GT+LT+'div id="recentes"'+GT+LT+"/div"+GT;

  const a=document.getElementById("attente"); a.innerHTML="";
  const att=MUSIQUE.attente||[];
  if(!att.length){ a.innerHTML=LT+'p class="vide"'+GT+"Aucune demande en attente."+LT+"/p"+GT; }
  att.forEach(function(x){
    const c=el("div","card",LT+'div class="row"'+GT+
      LT+'span style="flex:1;min-width:150px"'+GT+esc(x.titre)+LT+"/span"+GT+
      LT+'span class="pill warn"'+GT+esc(x.quand)+LT+"/span"+GT+LT+"/div"+GT);
    const r=el("div","row"); r.style.marginTop="9px";
    const b1=el("button","btn btn-vert btn-sm","Accepter"); b1.onclick=function(){ decider(x.titre,"accept"); };
    const b2=el("button","btn btn-rouge btn-sm","Refuser");  b2.onclick=function(){ decider(x.titre,"reject"); };
    r.appendChild(b1); r.appendChild(b2); c.appendChild(r); a.appendChild(c);
  });
  document.getElementById("recentes").innerHTML=(MUSIQUE.recentes||[]).map(function(x){
    return LT+'div class="card"'+GT+LT+'div class="row"'+GT+
      LT+'span style="flex:1;min-width:150px"'+GT+esc(x.titre)+LT+"/span"+GT+
      LT+'span class="pill '+(x.verdict==="accept"?"ok":"ko")+'"'+GT+esc(x.verdict)+LT+"/span"+GT+
      (x.telecharge?LT+'span class="pill ok"'+GT+"téléchargé"+LT+"/span"+GT
                   :LT+'span class="pill warn"'+GT+"en file"+LT+"/span"+GT)+
      LT+"/div"+GT+LT+"/div"+GT;
  }).join("") || LT+'p class="vide"'+GT+"Rien."+LT+"/p"+GT;
}

function rendreDl(){
  const dl=MUSIQUE.downloader||{}, ck=MUSIQUE.cookies||{};
  const pause=!!dl.en_pause, vivant=(dl.vu_il_y_a_min!=null && dl.vu_il_y_a_min<25);
  const z=document.getElementById("s-dl");
  z.innerHTML=
    LT+"h2"+GT+"Téléchargeur"+LT+"/h2"+GT+
    LT+'div class="card'+(pause||!vivant?" alerte":"")+'" id="dlc"'+GT+
      LT+'div class="row"'+GT+LT+'span class="nom" style="flex:1"'+GT+"État"+LT+"/span"+GT+
      LT+'span class="pill '+(pause?"warn":(vivant?"ok":"ko"))+'"'+GT+
        (pause?"EN PAUSE":(dl.statut||(vivant?"ACTIF":"MUET")))+LT+"/span"+GT+LT+"/div"+GT+
      LT+'div class="det"'+GT+(dl.detail?esc(dl.detail):"")+LT+"/div"+GT+
    LT+"/div"+GT+
    LT+'div class="grid"'+GT+
      kpi(dl.en_file==null?"?":dl.en_file,"en file")+
      kpi(dl.aujourdhui==null?"?":dl.aujourdhui,"téléchargés aujourd'hui")+
      kpi(esc(dl.dernier||"—"),"dernier")+
      kpi(dl.vu_il_y_a_min==null?"?":dl.vu_il_y_a_min+" min","dernier signe de vie")+
    LT+"/div"+GT+
    LT+"h2"+GT+"Cookies YouTube"+LT+"/h2"+GT+
    LT+'div class="card'+(ck.statut==="OK"?"":" alerte")+'"'+GT+
      LT+'div class="row"'+GT+LT+'span class="nom" style="flex:1"'+GT+"État des cookies"+LT+"/span"+GT+
      LT+'span class="pill '+(ck.statut==="OK"?"ok":"ko")+'"'+GT+esc(ck.statut||"?")+LT+"/span"+GT+LT+"/div"+GT+
      LT+'div class="det"'+GT+(ck.present
        ? "Déposés le "+esc(ck.depose_le)+" · il y a "+ck.age_jours+" jour(s) · "+
          Math.round((ck.taille||0)/1024)+" ko"
        : "Aucun fichier : le téléchargeur reste en pause.")+LT+"/div"+GT+
    LT+"/div"+GT+
    LT+'div class="card"'+GT+
      LT+'div class="msg"'+GT+"Quand les téléchargements s'arrêtent, ce sont presque toujours les "+
        "cookies qui ont expiré. Exporte-les depuis ton navigateur (extension « Get cookies.txt ») "+
        "et dépose le fichier ici."+LT+"/div"+GT+
      LT+'div class="field" style="margin-top:10px"'+GT+
        LT+'textarea class="field-input" id="ck" placeholder="Ou colle ici le contenu de cookies.txt"'+GT+
        LT+"/textarea"+GT+LT+"/div"+GT+
      LT+'div class="row"'+GT+
        LT+'input type="file" id="fic" accept=".txt" onchange="lireFichier()" style="flex:1;min-width:170px"'+GT+
        LT+'button class="btn btn-or" onclick="envoyerCookies()"'+GT+"Enregistrer"+LT+"/button"+GT+
      LT+"/div"+GT+
      LT+'div class="msg" id="msg-ck"'+GT+LT+"/div"+GT+
    LT+"/div"+GT;
  const z2=el("div","row"); z2.style.marginTop="10px";
  const b=el("button","btn "+(pause?"btn-vert":"btn-rouge"),pause?"Relancer le téléchargeur":"Mettre en pause");
  b.onclick=function(){ pilote(pause?"reprise":"pause"); };
  z2.appendChild(b);
  const lab=el("span",null,"Quota / jour :"); lab.style.cssText="font-size:13px;opacity:.8;margin-left:8px";
  const inp=el("input","field-input"); inp.type="number"; inp.min="1"; inp.max="500";
  inp.id="lim"; inp.value=(dl.limite||45); inp.style.width="92px";
  const bl=el("button","btn btn-vio btn-sm","Régler");
  bl.onclick=function(){ reglerLimite(); };
  z2.appendChild(lab); z2.appendChild(inp); z2.appendChild(bl);
  document.getElementById("dlc").appendChild(z2);
  if(dl.en_file>0){
    const j=Math.max(1,Math.ceil(dl.en_file/(dl.limite||45)));
    document.getElementById("dlc").appendChild(
      el("div","msg","File de "+dl.en_file+" titre(s) — environ "+j+" jour(s) au rythme actuel."));
  }
}

function rendreVoix(){
  const z=document.getElementById("s-voix");
  const items=(JINGLES.items||[]), q=(JINGLES.quota||{});
  const att=items.filter(function(i){return i.status==="pending";});
  const c=JINGLES.counts||{};
  z.innerHTML=LT+"h2"+GT+"Jingles de Rebexis"+LT+"/h2"+GT+
    LT+'div class="grid"'+GT+kpi(c.ok||0,"validés")+kpi(c.ko||0,"retirés")+
      kpi(c.pending||0,"à écouter")+LT+"/div"+GT+
    LT+"h2"+GT+"Crédits de voix"+LT+"/h2"+GT+
    LT+'div class="card'+(q.remaining!=null && q.limit && q.remaining < q.limit*0.15 ? " alerte":"")+'"'+GT+
      LT+'div class="row"'+GT+
        LT+'span class="nom" style="flex:1"'+GT+"ElevenLabs"+LT+"/span"+GT+
        LT+'span class="pill '+(q.remaining==null?"warn":(q.remaining>0?"ok":"ko"))+'"'+GT+
          (q.remaining==null?"indisponible":(q.remaining+" caractères restants"))+LT+"/span"+GT+
      LT+"/div"+GT+
      LT+'div class="det"'+GT+
        (q.limit? (q.used+" utilisés sur "+q.limit+" ("+q.pct+" %)") : "quota inconnu")+
        (q.source==="local"?" · source : compteur local (ElevenLabs injoignable)":"")+
        (q.reset_unix? " · remise à zéro le "+new Date(q.reset_unix*1000).toLocaleDateString("fr-FR"):"")+
      LT+"/div"+GT+
    LT+"/div"+GT+
    LT+'div class="card"'+GT+
      LT+'div class="det" style="margin-bottom:6px"'+GT+
        "Clé ElevenLabs — à remplacer quand les crédits affichent « indisponible » ou "+
        "que les phrases échouent (les clés se périment). Crée-la sur elevenlabs.io, "+
        "profil, cle API : elle commence par sk_. Elle est vérifiée chez ElevenLabs "+
        "avant d'être acceptée."+LT+"/div"+GT+
      LT+'div class="row"'+GT+
        LT+'input type="password" class="field-input" id="elk" style="flex:1" '+
          'placeholder="sk_…" autocomplete="off"'+GT+
        LT+'button class="btn ghost" onclick="remplacerCle()"'+GT+"Remplacer la clé"+LT+"/button"+GT+
      LT+"/div"+GT+
      LT+'div class="msg" id="msg-elk"'+GT+LT+"/div"+GT+
    LT+"/div"+GT+
    LT+"h2"+GT+"Réglages de la voix"+LT+"/h2"+GT+
    LT+'div class="card"'+GT+
      LT+'div class="det" style="margin-bottom:8px"'+GT+
        "Effet immédiat sur la prochaine phrase fabriquée. Les jingles déjà validés "+
        "ne changent pas. Réglage de référence : stabilité 0.30, style 0.75."+LT+"/div"+GT+
      curseur("stability","Stabilité","monotone ↔ variée",0,1)+
      curseur("style","Style","sobre ↔ expressive",0,1)+
      curseur("similarity_boost","Fidélité","libre ↔ collée à la voix d-origine",0,1)+
      curseur("speed","Débit","lent ↔ rapide",0.7,1.3)+
      LT+'div class="row" style="margin-top:8px"'+GT+
        LT+'button class="btn" onclick="enregistrerVoix()"'+GT+"Appliquer"+LT+"/button"+GT+
        LT+'button class="btn ghost" onclick="voixDefaut()"'+GT+
          "Revenir au réglage de référence"+LT+"/button"+GT+
      LT+"/div"+GT+
      LT+'div class="msg" id="msg-voix"'+GT+LT+"/div"+GT+
    LT+"/div"+GT+
    LT+"h2"+GT+"Écrire une phrase"+LT+"/h2"+GT+
    LT+'div class="card"'+GT+
      LT+'div class="field"'+GT+
        LT+'label class="field-label"'+GT+"Ta phrase (3 phrases maximum)"+LT+"/label"+GT+
        LT+'textarea class="field-input" id="ph" style="min-height:80px" oninput="compter()" '+
          'placeholder="Le caisson de basses a demandé une pause. Refusée."'+GT+LT+"/textarea"+GT+
        LT+'div class="msg" id="cout"'+GT+LT+"/div"+GT+
      LT+"/div"+GT+
      LT+'div class="row"'+GT+
        LT+'select class="field-input" id="cat" style="width:auto;min-width:190px"'+GT+
          LT+'option value="rebexis"'+GT+"Intervention libre"+LT+"/option"+GT+
          LT+'option value="cat3_bloc"'+GT+"Transition (la plus jouée)"+LT+"/option"+GT+
          LT+'option value="cat4_nouveaute"'+GT+"Annonce de nouveauté"+LT+"/option"+GT+
        LT+"/select"+GT+
        LT+'button class="btn btn-or" onclick="creerPhrase()"'+GT+"Fabriquer"+LT+"/button"+GT+
      LT+"/div"+GT+
      LT+'div class="msg" id="msg-ph"'+GT+LT+"/div"+GT+
    LT+"/div"+GT+
    LT+'div class="card"'+GT+
      LT+'div class="det"'+GT+
        LT+"b"+GT+"Ce qui marche"+LT+"/b"+GT+" — des objets qui ont une volonté "+
        "(« les enceintes viennent de demander une augmentation »), de l'auto-dérision "+
        "(« j'appuie sur un bouton et je prends le mérite »), de l'understatement "+
        "(« il n'est clairement pas venu pour être discret »), une autorité feinte "+
        "(« on m'a demandé de baisser, j'ai fait semblant de ne pas entendre »)."+
      LT+"/div"+GT+
      LT+'div class="det" style="margin-top:8px"'+GT+
        LT+"b"+GT+"À éviter"+LT+"/b"+GT+" — les phrases neutres de remplissage "+
        "(« retour à la musique »), les blagues longues, le descriptif technique "+
        "(BPM, genre). Les transitions passent très souvent : elles doivent tenir "+
        "à la répétition, donc courtes et sèches."+
      LT+"/div"+GT+
    LT+"/div"+GT+
    LT+"h2"+GT+"À écouter"+LT+"/h2"+GT+LT+'div id="jl"'+GT+LT+"/div"+GT;
  const jl=document.getElementById("jl"); jl.innerHTML="";
  if(!att.length){ jl.innerHTML=LT+'p class="vide"'+GT+
    "Rien à écouter. Tout est trié."+LT+"/p"+GT; return; }
  att.forEach(function(i){
    const card=el("div","card",
      LT+'span class="pill warn"'+GT+esc(i.cat)+LT+"/span"+GT+
      LT+'div class="det" style="margin:8px 0 10px;font-size:15px"'+GT+esc(i.text)+LT+"/div"+GT);
    const r=el("div","row");
    const bp=el("button","btn btn-or btn-sm","▶ Écouter");
    bp.onclick=function(){ ecouter(i,bp); };
    const b1=el("button","btn btn-vert btn-sm","Garder");
    b1.onclick=function(){ voter(i.h,"ok"); };
    const b2=el("button","btn btn-rouge btn-sm","Retirer");
    b2.onclick=function(){ voter(i.h,"ko"); };
    r.appendChild(bp); r.appendChild(b1); r.appendChild(b2);
    card.appendChild(r); jl.appendChild(card);
  });
}

function rendreSysteme(){
  const d=SANTE, z=document.getElementById("s-systeme");
  let h=LT+"h2"+GT+"Services"+LT+"/h2"+GT+LT+'div class="grid"'+GT;
  (d.services||[]).forEach(function(s){
    h+=LT+'div class="kpi"'+GT+LT+"b"+GT+(s.ok?"✅":"🔴")+LT+"/b"+GT+
       LT+"span"+GT+esc(s.nom)+LT+"/span"+GT+LT+"/div"+GT;
  });
  h+=LT+"/div"+GT+LT+"h2"+GT+"Contenu"+LT+"/h2"+GT;
  const c=d.contenu||{};
  h+=LT+'div class="grid"'+GT+
     kpi(c.votes==null?"?":c.votes,"votes")+
     kpi(c.propositions==null?"?":c.propositions,"titres proposés")+
     kpi(c.lore==null?"?":c.lore,"événements de lore")+LT+"/div"+GT;
  if(c.bacs&&c.bacs.length){
    h+=LT+'div class="card"'+GT+LT+'div class="det"'+GT+
       c.bacs.map(function(b){return esc(b.nom)+" : "+b.n;}).join("  ·  ")+LT+"/div"+GT+LT+"/div"+GT;
  }
  h+=LT+"h2"+GT+"Maintenance"+LT+"/h2"+GT;
  const sy=d.systeme||{}, lib={qc:"Contrôle qualité",sauvegarde:"Sauvegarde",disque:"Disque"};
  Object.keys(sy).forEach(function(k){
    const e=sy[k], bon=["OK","FIXÉ","ACTIF","QUOTA ATTEINT","EN PAUSE"].indexOf(e.statut)>=0;
    let det=esc(e.detail||"");
    if(k==="qc"&&e.detail){
      det=e.detail.split(NL).map(function(l){
        const p=l.split("|");
        return (p[0]==="OK"?"✅":p[0]==="FIXÉ"?"🔧":p[0]==="WARN"?"⚠️":"🔴")+" "+esc(p[1]||"")+" : "+esc(p[2]||"");
      }).join(NL);
    }
    h+=LT+'div class="card'+(bon?"":" alerte")+'"'+GT+LT+'div class="row"'+GT+
       LT+'span class="nom"'+GT+esc(lib[k]||k)+LT+"/span"+GT+
       LT+'span class="pill '+(bon?"ok":"ko")+'"'+GT+esc(e.statut)+LT+"/span"+GT+
       LT+'span class="pill warn"'+GT+"il y a "+e.il_y_a_min+" min"+LT+"/span"+GT+LT+"/div"+GT+
       LT+'div class="det"'+GT+det+LT+"/div"+GT+LT+"/div"+GT;
  });

  h+=LT+"h2"+GT+"Agir"+LT+"/h2"+GT+
     LT+'div class="card"'+GT+
       LT+'div class="det" style="margin-bottom:8px"'+GT+
         "Ces boutons déposent une demande. Une machine de l-hôte l-exécute dans la minute "+
         "et te renvoie le résultat ci-dessous."+LT+"/div"+GT+
       LT+'div class="row"'+GT+
         LT+'select class="field-input" id="svc" style="flex:1;min-width:180px"'+GT+
           (CMD.services||[]).map(function(x){
             return LT+"option"+GT+esc(x)+LT+"/option"+GT;}).join("")+
         LT+"/select"+GT+
         LT+'button class="btn" onclick="relancerService()"'+GT+"Relancer"+LT+"/button"+GT+
         LT+'button class="btn ghost" onclick="voirJournal()"'+GT+"Journal"+LT+"/button"+GT+
       LT+"/div"+GT+
       LT+'div class="row" style="margin-top:8px"'+GT+
         LT+'button class="btn ghost" onclick="forcerSauvegarde()"'+GT+
           "Sauvegarder maintenant"+LT+"/button"+GT+
         LT+'button class="btn ghost" onclick="lancerControle()"'+GT+
           "Contrôle qualité"+LT+"/button"+GT+
       LT+"/div"+GT+
       LT+'div class="msg" id="msg-cmd"'+GT+LT+"/div"+GT+
     LT+"/div"+GT;

  (CMD.commandes||[]).forEach(function(c){
    const fini=(c.etat==="fait"), rate=(c.etat==="refusé"||c.etat==="échec");
    h+=LT+'div class="card'+(rate?" alerte":"")+'"'+GT+
       LT+'div class="row"'+GT+
         LT+'span class="nom" style="flex:1"'+GT+
           esc(c.action)+(c.cible?" — "+esc(c.cible):"")+LT+"/span"+GT+
         LT+'span class="pill '+(fini?"ok":(rate?"ko":"warn"))+'"'+GT+esc(c.etat)+LT+"/span"+GT+
         LT+'span class="pill warn"'+GT+esc(c.quand)+LT+"/span"+GT+
       LT+"/div"+GT+
       (c.resultat? LT+'div class="det" style="white-space:pre-wrap;max-height:220px;overflow:auto"'+GT+
                    esc(c.resultat)+LT+"/div"+GT : "")+
       LT+"/div"+GT;
  });

  z.innerHTML=h;
}

/* ── Actions ─────────────────────────────────────────────────────── */
function mode(i){
  MODE=i;
  for(let k=0;k<3;k++){
    document.getElementById("o"+k).classList.toggle("on",k===i);
    document.getElementById("m"+k).hidden=(k!==i);
  }
}
function chercher(){
  clearTimeout(tmr);
  tmr=setTimeout(async function(){
    const q=document.getElementById("q").value.trim(), box=document.getElementById("sugg");
    if(q.length<2){ box.hidden=true; return; }
    try{
      const d=await (await fetch("/api/regie/recherche?q="+encodeURIComponent(q))).json();
      if(!d.resultats.length){ box.hidden=true; return; }
      box.innerHTML="";
      d.resultats.forEach(function(r){
        const e=el("div",null,LT+"b"+GT+esc(r.artiste)+LT+"/b"+GT+" — "+esc(r.titre)+
          LT+"br"+GT+LT+"small"+GT+esc(r.genre)+(r.annee?" · "+esc(r.annee):"")+LT+"/small"+GT);
        e.onclick=function(){ box.hidden=true; envoyer({artiste:r.artiste,titre:r.titre}); };
        box.appendChild(e);
      });
      box.hidden=false;
    }catch(e){ box.hidden=true; }
  },320);
}
function ajouterLibre(){
  const t=document.getElementById("libre").value.trim();
  if(t.length<3){ document.getElementById("msg-ajout").textContent="Trop court."; return; }
  envoyer({titre:t,libre:true});
}
async function ajouterArtiste(){
  const a=document.getElementById("art").value.trim();
  const n=parseInt(document.getElementById("nb").value||"5",10);
  const m=document.getElementById("msg-ajout");
  if(a.length<2){ m.textContent="Nom d'artiste trop court."; return; }
  m.textContent="Recherche des titres...";
  try{
    const d=await (await fetch("/api/regie/musique/artiste",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({artiste:a,combien:n})})).json();
    m.textContent=(d.ok?"✅ ":"⚠️ ")+d.message;
    if(d.ok) charger();
  }catch(e){ m.textContent="Erreur réseau."; }
}
async function envoyer(corps){
  const m=document.getElementById("msg-ajout"); m.textContent="Envoi...";
  try{
    const d=await (await fetch("/api/regie/musique/ajouter",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(corps)})).json();
    m.textContent=(d.ok?"✅ ":"⚠️ ")+d.message;
    if(d.ok) charger();
  }catch(e){ m.textContent="Erreur réseau."; }
}
async function decider(titre,verdict){
  try{ await fetch("/api/regie/musique/decision",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({titre:titre,verdict:verdict})});
    charger(); }catch(e){ alert("Erreur"); }
}
async function reglerLimite(){
  const v=parseInt(document.getElementById("lim").value||"45",10);
  try{
    const d=await (await fetch("/api/regie/downloader/limite",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({limite:v})})).json();
    document.getElementById("msg-ck").textContent=(d.ok?"✅ ":"⚠️ ")+(d.message||"");
    charger();
  }catch(e){ alert("Erreur"); }
}

async function pilote(action){
  try{ const d=await (await fetch("/api/regie/downloader",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({action:action})})).json();
    document.getElementById("msg-ck").textContent=d.message||""; charger(); }catch(e){ alert("Erreur"); }
}
function lireFichier(){
  const f=document.getElementById("fic").files[0]; if(!f) return;
  const r=new FileReader();
  r.onload=function(){ document.getElementById("ck").value=r.result;
    document.getElementById("msg-ck").textContent="Fichier chargé, clique sur Enregistrer."; };
  r.readAsText(f);
}
async function envoyerCookies(){
  const c=document.getElementById("ck").value, m=document.getElementById("msg-ck");
  if(c.trim().length<200){ m.textContent="Contenu trop court."; return; }
  m.textContent="Envoi...";
  try{
    const d=await (await fetch("/api/regie/cookies",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({contenu:c})})).json();
    m.textContent=(d.ok?"✅ ":"⚠️ ")+d.message;
    if(d.ok) charger();
  }catch(e){ m.textContent="Erreur réseau."; }
}
function ecouter(i,btn){
  const pl=document.getElementById("pl");
  document.querySelectorAll(".btn-vio.enlecture").forEach(function(b){
    b.classList.remove("enlecture","btn-vio"); b.classList.add("btn-or"); b.textContent="▶ Écouter"; });
  if(playing===i.h && !pl.paused){ pl.pause(); playing=null;
    btn.textContent="▶ Écouter"; return; }
  pl.src="/api/regie/audio/"+i.h+"?v="+(i.v||0);
  playing=i.h; btn.textContent="⏸ En cours";
  pl.play().catch(function(e){ btn.textContent="▶ Écouter"; playing=null;
    alert("Lecture impossible : "+((e&&e.name)?e.name:e)); });
}
function compter(){
  const t=document.getElementById("ph").value.trim();
  const e=document.getElementById("cout");
  if(!t){ e.textContent=""; return; }
  // eleven_v3 ajoute la balise [playful] avant l'envoi : elle est facturée aussi.
  const cout=t.length+10;
  const reste=(JINGLES.quota||{}).remaining;
  let s=cout+" caractères seront consommés";
  if(reste!=null){
    s+=" · il en restera "+Math.max(0,reste-cout);
    if(cout>reste) s="⚠️ "+cout+" caractères nécessaires, il n'en reste que "+reste;
  }
  e.textContent=s;
}

async function creerPhrase(){
  const t=document.getElementById("ph").value.trim();
  const c=document.getElementById("cat").value;
  const m=document.getElementById("msg-ph");
  if(t.length<4){ m.textContent="Phrase trop courte."; return; }
  m.textContent="Fabrication en cours (quelques secondes)...";
  try{
    const d=await (await fetch("/api/regie/voix/creer",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({texte:t, categorie:c})})).json();
    m.textContent=(d.ok?"✅ ":"⚠️ ")+d.message;
    if(d.ok){ document.getElementById("ph").value=""; charger(); }
  }catch(e){ m.textContent="Erreur réseau (la fabrication peut prendre du temps)."; }
}

async function voter(h,statut){
  try{ await fetch("/api/regie/review",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify({h:h,status:statut})});
    charger(); }catch(e){ alert("Erreur"); }
}

/* ── Chargement ──────────────────────────────────────────────────── */
async function charger(force){
  try{
    const r=await Promise.all([
      fetch("/api/regie/sante").then(function(x){return x.json();}),
      fetch("/api/regie/musique").then(function(x){return x.json();}),
      fetch("/api/regie/jingles?status=all").then(function(x){return x.json();}),
      fetch("/api/regie/commandes").then(function(x){return x.json();}),
      fetch("/api/regie/voix/reglages").then(function(x){return x.json();})
    ]);
    SANTE=r[0]; MUSIQUE=r[1]; JINGLES=r[2]; CMD=r[3]; VOIX=(r[4]||{}).reglages||{};
  }catch(e){ return; }
  rendreVue(); rendreScenes(); rendreMusique(); rendreDl(); rendreVoix(); rendreSysteme();
  const nAl=(SANTE.alertes||[]).length, nJ=(JINGLES.counts||{}).pending||0,
        nD=(MUSIQUE.attente||[]).length;
  badge("vue",nAl); badge("voix",nJ); badge("musique",nD);
  document.getElementById("maj").textContent="lu à "+new Date().toLocaleTimeString("fr-FR");
}
function curseur(nom,titre,aide,mini,maxi){
  const v=(VOIX[nom]!=null?VOIX[nom]:0.5);
  return LT+'div class="field"'+GT+
    LT+'label class="field-label"'+GT+esc(titre)+" — "+esc(aide)+
      LT+'span class="pill warn" id="v-'+nom+'" style="margin-left:8px"'+GT+v+LT+"/span"+GT+
    LT+"/label"+GT+
    LT+'input type="range" class="field-input" id="r-'+nom+'" min="'+mini+'" max="'+maxi+
      '" step="0.05" value="'+v+'" oninput="majCurseur(this)"'+GT+
  LT+"/div"+GT;
}
function majCurseur(e){
  document.getElementById("v-"+e.id.slice(2)).textContent=e.value;
}
async function enregistrerVoix(){
  const corps={};
  ["stability","style","similarity_boost","speed"].forEach(function(n){
    const e=document.getElementById("r-"+n); if(e) corps[n]=parseFloat(e.value);
  });
  const m=document.getElementById("msg-voix");
  m.textContent="…";
  try{
    const rep=await fetch("/api/regie/voix/reglages",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(corps)});
    const d=await rep.json();
    // On ne relance PAS charger() ici : il redessine l onglet et effacerait
    // la confirmation que le chef vient de demander.
    if(d.ok) Object.keys(corps).forEach(function(n){ VOIX[n]=corps[n]; });
    m.textContent=(d.ok?"✅ ":"⚠️ ")+(d.message||d.detail||"");
  }catch(e){ m.textContent="⚠️ réseau"; }
}
async function remplacerCle(){
  const e=document.getElementById("elk"), m=document.getElementById("msg-elk");
  const cle=(e.value||"").trim();
  if(!cle){ m.textContent="⚠️ colle d abord la nouvelle clé"; return; }
  m.textContent="vérification chez ElevenLabs…";
  try{
    const rep=await fetch("/api/regie/voix/cle",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({cle:cle})});
    const d=await rep.json();
    m.textContent=(d.ok?"✅ ":"⚠️ ")+(d.message||d.detail||"");
    if(d.ok){ e.value=""; setTimeout(charger,70000); }
  }catch(err){ m.textContent="⚠️ réseau"; }
}
async function voixDefaut(){
  VOIX={stability:0.30,style:0.75,similarity_boost:0.75,speed:1.0};
  rendreVoix(); enregistrerVoix();
}

async function demander(action,cible){
  const m=document.getElementById("msg-cmd");
  if(m) m.textContent="…";
  try{
    const rep=await fetch("/api/regie/commande",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({action:action,cible:cible})});
    const d=await rep.json();
    if(m) m.textContent=(d.ok?"✅ ":"⚠️ ")+(d.message||d.detail||"");
    setTimeout(charger,4000); setTimeout(charger,20000);
  }catch(e){ if(m) m.textContent="⚠️ réseau"; }
}
function serviceChoisi(){
  const e=document.getElementById("svc"); return e?e.value:"";
}
function relancerService(){
  const s=serviceChoisi();
  if(!confirm("Relancer "+s+" ? La coupure dure quelques secondes.")) return;
  demander("relancer",s);
}
function voirJournal(){ demander("journal",serviceChoisi()); }
function forcerSauvegarde(){ demander("sauvegarde",null); }
function lancerControle(){ demander("controle_qualite",null); }

function badge(id,n){
  const e=document.getElementById("bdg-"+id); if(!e) return;
  e.innerHTML = n>0 ? LT+'span class="badge"'+GT+n+LT+"/span"+GT : "";
}
construireNav(); charger(); setInterval(charger,45000);
</script></body></html>"""


MUSIQUE_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Musique et demandes — Gaiverland</title>
<style>
:root{--cream:#fff4e6}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,sans-serif;color:var(--cream);
  background:linear-gradient(175deg,#1b1030,#3d1d5c 45%,#6b2f6b);padding-bottom:50px}
header{position:sticky;top:0;z-index:9;padding:14px 16px;background:rgba(20,10,35,.95);
  backdrop-filter:blur(6px);border-bottom:1px solid rgba(255,244,230,.18)}
h1{margin:0;font-size:19px}
.sub{font-size:12px;opacity:.7;margin-top:3px}
main{padding:14px 16px;max-width:900px;margin:0 auto}
h2{font-size:14px;letter-spacing:1px;text-transform:uppercase;opacity:.75;margin:24px 0 9px}
.card{border:1px solid rgba(255,244,230,.18);border-radius:13px;padding:12px;
  background:rgba(255,244,230,.07);margin-bottom:9px}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
input,textarea{width:100%;padding:11px;border-radius:10px;border:1px solid rgba(255,244,230,.3);
  background:rgba(0,0,0,.28);color:var(--cream);font-size:15px;font-family:inherit}
textarea{min-height:120px;font-size:12px}
button{border:none;border-radius:11px;padding:11px 15px;font-size:14px;font-weight:bold;
  cursor:pointer;min-height:44px}
.go{background:linear-gradient(135deg,#ffd29a,#ffb56b);color:#1b1030}
.good{background:#2ecc71;color:#08210f}
.bad{background:#ff5a5a;color:#3d0a0a}
.sugg{border:1px solid rgba(255,244,230,.25);border-radius:10px;margin-top:6px;overflow:hidden}
.sugg div{padding:10px 12px;cursor:pointer;font-size:14px;border-bottom:1px solid rgba(255,244,230,.12)}
.sugg small{opacity:.65}
.pill{font-size:11px;padding:2px 9px;border-radius:10px;font-weight:bold}
.ok{background:#2ecc71;color:#08210f}
.ko{background:#ff5a5a;color:#3d0a0a}
.warn{background:#ffc44d;color:#3d2a00}
.msg{margin-top:9px;font-size:13px;opacity:.9}
.vide{opacity:.65;font-size:13px}
.tabs{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.tabs button{padding:8px 14px;font-size:13px;border-radius:20px;background:rgba(255,244,230,.1);
  color:var(--cream);border:1px solid rgba(255,244,230,.3);min-height:auto}
.tabs button.on{background:var(--cream);color:#1b1030}
footer{max-width:900px;margin:22px auto 0;padding:0 16px;font-size:12px;opacity:.6;line-height:1.7}
a{color:#ffd29a}
</style></head><body>
<header>
  <h1>&#127925; Musique et demandes</h1>
  <div class="sub" id="stats">…</div>
</header>
<main>
  <h2>Ajouter un titre</h2>
  <div class="card">
    <div class="tabs">
      <button id="t-reche" class="on" onclick="mode(0)">Rechercher</button>
      <button id="t-libre" onclick="mode(1)">Saisie libre (remix)</button>
      <button id="t-art" onclick="mode(2)">Un artiste entier</button>
    </div>
    <div id="bloc-reche" style="margin-top:11px">
      <input id="q" placeholder="Artiste ou titre..." autocomplete="off" oninput="chercher()">
      <div class="sugg" id="sugg" hidden></div>
    </div>
    <div id="bloc-libre" hidden style="margin-top:11px">
      <input id="libre" placeholder="Ex : Artiste - Titre (Machin Remix)">
      <div class="msg">Pour les remix que les catalogues ne connaissent pas : écris exactement
        ce qu'il faut chercher.</div>
      <div style="margin-top:9px"><button class="go" onclick="ajouterLibre()">Ajouter</button></div>
    </div>
    <div id="bloc-art" hidden style="margin-top:11px">
      <input id="art" placeholder="Ex : Sub Zero Project">
      <div class="row" style="margin-top:9px">
        <span style="font-size:13px;opacity:.85">Combien de titres&nbsp;?</span>
        <input id="nb" type="number" min="1" max="12" value="5" style="width:80px">
        <button class="go" onclick="ajouterArtiste()">Demander</button>
      </div>
      <div class="msg">L'algorithme prend ses morceaux les plus connus, écarte ceux
        déjà dans la bibliothèque, et met le reste en file.</div>
    </div>
    <div class="msg" id="msg-ajout"></div>
  </div>

  <h2>Downloader</h2>
  <div class="card" id="dl-etat"><p class="vide">Chargement...</p></div>

  <h2>Demandes en attente</h2>
  <div id="attente"><p class="vide">Chargement...</p></div>

  <h2>Dernières décisions</h2>
  <div id="recentes"><p class="vide">Chargement...</p></div>

  <h2>Cookies YouTube</h2>
  <div class="card" id="cookies-etat"><p class="vide">Chargement...</p></div>
  <div class="card">
    <div class="msg">Quand les téléchargements s'arrêtent, ce sont presque toujours les cookies
      qui ont expiré. Exporte-les depuis ton navigateur (extension « Get cookies.txt »), puis
      dépose le fichier ici. Aucun accès à ton PC, rien à installer sur le serveur.</div>
    <textarea id="ck" placeholder="Ou colle ici le contenu de cookies.txt"></textarea>
    <div style="margin-top:9px" class="row">
      <input type="file" id="fic" accept=".txt" onchange="lireFichier()" style="flex:1;min-width:170px">
      <button class="go" onclick="envoyerCookies()">Enregistrer</button>
    </div>
    <div class="msg" id="msg-ck"></div>
  </div>
</main>
<footer>
  <a href="/regie/sante">Santé du festival</a> · <a href="/regie/voix">Régie voix</a>
</footer>
<script>
const AMP=String.fromCharCode(38), LT=String.fromCharCode(60), GT=String.fromCharCode(62), GUI=String.fromCharCode(34);
function esc(s){ return String(s==null?"":s)
  .split(AMP).join(AMP+"amp;").split(LT).join(AMP+"lt;")
  .split(GT).join(AMP+"gt;").split(GUI).join(AMP+"quot;"); }
let MODE=0, tmr=null;

function mode(i){
  MODE=i;
  document.getElementById("t-reche").classList.toggle("on",i===0);
  document.getElementById("t-libre").classList.toggle("on",i===1);
  document.getElementById("t-art").classList.toggle("on",i===2);
  document.getElementById("bloc-reche").hidden=(i!==0);
  document.getElementById("bloc-libre").hidden=(i!==1);
  document.getElementById("bloc-art").hidden=(i!==2);
}

async function ajouterArtiste(){
  const a=document.getElementById("art").value.trim();
  const n=parseInt(document.getElementById("nb").value||"5",10);
  const m=document.getElementById("msg-ajout");
  if(a.length<2){ m.textContent="Nom d'artiste trop court."; return; }
  m.textContent="Recherche des titres...";
  try{
    const d=await (await fetch("/api/regie/musique/artiste",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({artiste:a, combien:n})})).json();
    m.textContent=(d.ok?"✅ ":"⚠️ ")+d.message;
    if(d.ok){ document.getElementById("art").value=""; charger(); }
  }catch(e){ m.textContent="Erreur réseau."; }
}

async function pilote(action){
  try{
    const d=await (await fetch("/api/regie/downloader",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({action:action})})).json();
    alert(d.message||"OK"); charger();
  }catch(e){ alert("Erreur"); }
}

function chercher(){
  clearTimeout(tmr);
  tmr=setTimeout(async function(){
    const q=document.getElementById("q").value.trim();
    const box=document.getElementById("sugg");
    if(q.length<2){ box.hidden=true; return; }
    try{
      const d=await (await fetch("/api/regie/recherche?q="+encodeURIComponent(q))).json();
      if(!d.resultats.length){ box.hidden=true; return; }
      box.innerHTML="";
      d.resultats.forEach(function(r){
        const el=document.createElement("div");
        el.innerHTML=LT+"b"+GT+esc(r.artiste)+LT+"/b"+GT+" — "+esc(r.titre)+
          LT+"br"+GT+LT+"small"+GT+esc(r.genre)+(r.annee?" · "+esc(r.annee):"")+LT+"/small"+GT;
        el.onclick=function(){ box.hidden=true; envoyer({artiste:r.artiste, titre:r.titre}); };
        box.appendChild(el);
      });
      box.hidden=false;
    }catch(e){ box.hidden=true; }
  },320);
}

function ajouterLibre(){
  const t=document.getElementById("libre").value.trim();
  if(t.length<3){ document.getElementById("msg-ajout").textContent="Trop court."; return; }
  envoyer({titre:t, libre:true});
}

async function envoyer(corps){
  const m=document.getElementById("msg-ajout");
  m.textContent="Envoi...";
  try{
    const d=await (await fetch("/api/regie/musique/ajouter",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(corps)})).json();
    m.textContent=(d.ok?"✅ ":"⚠️ ")+d.message;
    if(d.ok){ document.getElementById("q").value=""; document.getElementById("libre").value=""; charger(); }
  }catch(e){ m.textContent="Erreur réseau."; }
}

async function decider(titre,verdict){
  try{
    await fetch("/api/regie/musique/decision",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({titre:titre,verdict:verdict})});
    charger();
  }catch(e){ alert("Erreur"); }
}

function lireFichier(){
  const f=document.getElementById("fic").files[0];
  if(!f) return;
  const r=new FileReader();
  r.onload=function(){ document.getElementById("ck").value=r.result;
    document.getElementById("msg-ck").textContent="Fichier chargé, clique sur Enregistrer."; };
  r.readAsText(f);
}

async function envoyerCookies(){
  const c=document.getElementById("ck").value;
  const m=document.getElementById("msg-ck");
  if(c.trim().length<200){ m.textContent="Contenu trop court."; return; }
  m.textContent="Envoi...";
  try{
    const d=await (await fetch("/api/regie/cookies",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify({contenu:c})})).json();
    m.textContent=(d.ok?"✅ ":"⚠️ ")+d.message;
    if(d.ok){ document.getElementById("ck").value=""; charger(); }
  }catch(e){ m.textContent="Erreur réseau."; }
}

function carte(contenu){ const d=document.createElement("div"); d.className="card"; d.innerHTML=contenu; return d; }

async function charger(){
  let d;
  try{ d=await (await fetch("/api/regie/musique")).json(); }catch(e){ return; }
  document.getElementById("stats").textContent=
    (d.stats.telecharges||0)+" titres téléchargés · "+(d.stats.acceptes||0)+" acceptés · "+
    d.attente.length+" en attente";

  const a=document.getElementById("attente");
  a.innerHTML="";
  if(!d.attente.length){ a.innerHTML=LT+'p class="vide"'+GT+"Aucune demande en attente."+LT+"/p"+GT; }
  d.attente.forEach(function(x){
    const c=carte(LT+'div class="row"'+GT+LT+'span style="flex:1;min-width:150px"'+GT+esc(x.titre)+
      LT+"/span"+GT+LT+'span class="pill warn"'+GT+esc(x.quand)+LT+"/span"+GT+LT+"/div"+GT);
    const r=document.createElement("div"); r.className="row"; r.style.marginTop="9px";
    const b1=document.createElement("button"); b1.className="good"; b1.textContent="Accepter";
    b1.onclick=function(){ decider(x.titre,"accept"); };
    const b2=document.createElement("button"); b2.className="bad"; b2.textContent="Refuser";
    b2.onclick=function(){ decider(x.titre,"reject"); };
    r.appendChild(b1); r.appendChild(b2); c.appendChild(r); a.appendChild(c);
  });

  const rec=document.getElementById("recentes");
  rec.innerHTML = d.recentes.length ? d.recentes.map(function(x){
    return LT+'div class="card"'+GT+LT+'div class="row"'+GT+
      LT+'span style="flex:1;min-width:150px"'+GT+esc(x.titre)+LT+"/span"+GT+
      LT+'span class="pill '+(x.verdict==="accept"?"ok":"ko")+'"'+GT+esc(x.verdict)+LT+"/span"+GT+
      (x.telecharge?LT+'span class="pill ok"'+GT+"téléchargé"+LT+"/span"+GT
                   :LT+'span class="pill warn"'+GT+"en file"+LT+"/span"+GT)+
      LT+"/div"+GT+LT+"/div"+GT;
  }).join("") : LT+'p class="vide"'+GT+"Rien."+LT+"/p"+GT;

  const dl=d.downloader||{};
  const enPause=!!dl.en_pause, vivant=(dl.vu_il_y_a_min!=null && dl.vu_il_y_a_min<25);
  document.getElementById("dl-etat").innerHTML=
    LT+'div class="row"'+GT+LT+'span style="flex:1"'+GT+LT+"b"+GT+"Téléchargeur"+LT+"/b"+GT+LT+"/span"+GT+
    LT+'span class="pill '+(enPause?"warn":(vivant?"ok":"ko"))+'"'+GT+
      (enPause?"EN PAUSE":(dl.statut||(vivant?"ACTIF":"MUET")))+LT+"/span"+GT+LT+"/div"+GT+
    LT+'div class="msg"'+GT+
      (dl.detail?esc(dl.detail)+" · ":"")+
      "en file : "+(dl.en_file==null?"?":dl.en_file)+" · aujourd'hui : "+(dl.aujourdhui==null?"?":dl.aujourdhui)+
      " · dernier : "+esc(dl.dernier||"—")+
      (dl.vu_il_y_a_min!=null?" · vu il y a "+dl.vu_il_y_a_min+" min":" · jamais vu (redémarrage requis)")+
    LT+"/div"+GT;
  const zone=document.createElement("div"); zone.className="row"; zone.style.marginTop="9px";
  const b=document.createElement("button");
  b.className=enPause?"good":"bad"; b.textContent=enPause?"Relancer":"Mettre en pause";
  b.onclick=function(){ pilote(enPause?"reprise":"pause"); };
  zone.appendChild(b); document.getElementById("dl-etat").appendChild(zone);

  const ck=d.cookies||{}, bon=(ck.statut==="OK");
  document.getElementById("cookies-etat").innerHTML=
    LT+'div class="row"'+GT+LT+'span style="flex:1"'+GT+LT+"b"+GT+"Cookies du downloader"+LT+"/b"+GT+
    LT+"/span"+GT+LT+'span class="pill '+(bon?"ok":"ko")+'"'+GT+esc(ck.statut)+LT+"/span"+GT+LT+"/div"+GT+
    LT+'div class="msg"'+GT+(ck.present
      ? "Déposés le "+esc(ck.depose_le)+" · il y a "+ck.age_jours+" jour(s) · "+
        Math.round((ck.taille||0)/1024)+" ko"
      : "Aucun fichier de cookies : le downloader reste en pause.")+LT+"/div"+GT;
}
charger();
</script></body></html>"""


SANTE_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Santé du festival — Gaiverland</title>
<style>
:root{--cream:#fff4e6}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,sans-serif;color:var(--cream);
  background:linear-gradient(175deg,#1b1030,#3d1d5c 45%,#6b2f6b);padding-bottom:40px}
header{position:sticky;top:0;z-index:5;padding:14px 16px;background:rgba(20,10,35,.94);
  backdrop-filter:blur(6px);border-bottom:1px solid rgba(255,244,230,.18)}
h1{margin:0;font-size:19px}
.sub{font-size:12px;opacity:.7;margin-top:3px}
main{padding:14px 16px;max-width:960px;margin:0 auto}
h2{font-size:14px;letter-spacing:1px;text-transform:uppercase;opacity:.75;margin:22px 0 9px}
.card{border:1px solid rgba(255,244,230,.18);border-radius:13px;padding:11px 13px;
  background:rgba(255,244,230,.07);margin-bottom:8px}
.row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.pill{font-size:11px;padding:2px 9px;border-radius:10px;font-weight:bold}
.ok{background:#2ecc71;color:#08210f}
.ko{background:#ff5a5a;color:#3d0a0a}
.warn{background:#ffc44d;color:#3d2a00}
.nom{font-weight:bold;font-size:15px}
.det{font-size:13px;opacity:.82;margin-top:3px;white-space:pre-line}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.kpi{border:1px solid rgba(255,244,230,.18);border-radius:11px;padding:10px;text-align:center;
  background:rgba(255,244,230,.07)}
.kpi b{display:block;font-size:22px;margin-bottom:2px}
.kpi span{font-size:11px;opacity:.75}
.alerte{background:rgba(255,90,90,.18);border-color:rgba(255,90,90,.6)}
.vide{opacity:.65;font-size:13px}
footer{max-width:960px;margin:22px auto 0;padding:0 16px;font-size:12px;opacity:.6;line-height:1.6}
a{color:#ffd29a}
</style></head><body>
<header>
  <h1>🎪 Santé du festival</h1>
  <div class="sub">Actualisé automatiquement toutes les 30 s · <span id="maj">…</span></div>
</header>
<main id="app"><p class="vide">Chargement…</p></main>
<footer>
  Page de supervision — aucune IA, que des mesures.
  <a href="/regie/voix">Régie voix (valider les jingles)</a> · <a href="/regie/musique">Musique et demandes</a>
</footer>
<script>
const esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function pill(ok,txtOk,txtKo){return '<span class="pill '+(ok?"ok":"ko")+'">'+(ok?txtOk:txtKo)+"</span>";}

async function charger(){
  let d;
  try{ d=await (await fetch("/api/regie/sante")).json(); }
  catch(e){ document.getElementById("app").innerHTML='<div class="card alerte">Impossible de joindre le serveur.</div>'; return; }
  const h=[];

  if(d.alertes && d.alertes.length){
    h.push("<h2>À regarder</h2>");
    d.alertes.forEach(a=>h.push('<div class="card alerte"><div class="nom">⚠ '+esc(a)+"</div></div>"));
  }else{
    h.push('<h2>État général</h2><div class="card"><div class="nom">✅ Tout va bien</div>'+
           '<div class="det">Aucune anomalie détectée.</div></div>');
  }

  h.push("<h2>Les scènes</h2>");
  (d.scenes||[]).forEach(s=>{
    h.push('<div class="card"><div class="row"><span class="nom">'+esc(s.nom)+"</span>"+
      pill(s.en_ligne,"en ligne","hors ligne")+
      '<span class="pill warn">'+s.auditeurs+" auditeur(s)</span></div>"+
      '<div class="det">'+esc(s.artiste?s.artiste+" — ":"")+esc(s.titre||"—")+"</div></div>");
  });

  h.push('<h2>Services</h2><div class="grid">');
  (d.services||[]).forEach(s=>h.push('<div class="kpi"><b>'+(s.ok?"✅":"🔴")+"</b><span>"+esc(s.nom)+"</span></div>"));
  h.push("</div>");

  const v=d.voix||{};
  h.push('<h2>Voix de Rebexis</h2><div class="grid">'+
    '<div class="kpi"><b>'+(v.ok||0)+"</b><span>validés</span></div>"+
    '<div class="kpi"><b>'+(v.ko||0)+"</b><span>retirés</span></div>"+
    '<div class="kpi"><b>'+(v.pending||0)+"</b><span>à écouter</span></div></div>");

  const c=d.contenu||{};
  h.push('<h2>Contenu</h2><div class="grid">'+
    '<div class="kpi"><b>'+(c.passages_24h==null?"?":c.passages_24h)+"</b><span>titres joués (24 h)</span></div>"+
    '<div class="kpi"><b>'+(c.votes==null?"?":c.votes)+"</b><span>votes</span></div>"+
    '<div class="kpi"><b>'+(c.propositions==null?"?":c.propositions)+"</b><span>titres proposés</span></div>"+
    '<div class="kpi"><b>'+(c.lore==null?"?":c.lore)+"</b><span>événements de lore</span></div></div>");
  if(c.bacs&&c.bacs.length){
    h.push('<div class="card"><div class="det">'+
      c.bacs.map(b=>esc(b.nom)+" : "+b.n).join("  ·  ")+"</div></div>");
  }

  h.push("<h2>Système</h2>");
  const sy=d.systeme||{};
  const libelle={qc:"Contrôle qualité",sauvegarde:"Sauvegarde",disque:"Disque"};
  Object.keys(sy).forEach(k=>{
    const e=sy[k], bon=["OK","FIXÉ","ACTIF","QUOTA ATTEINT","EN PAUSE"].indexOf(e.statut)>=0;
    let det=esc(e.detail||"");
    if(k==="qc"&&e.detail){
      // NL sans séquence d'échappement : cette page est une chaîne Python, toute
      // barre oblique inverse y serait consommée au rendu (piège vécu deux fois).
      const NL=String.fromCharCode(10);
      det=e.detail.split(NL).map(l=>{
        const p=l.split("|");
        return (p[0]==="OK"?"✅":p[0]==="FIXÉ"?"🔧":p[0]==="WARN"?"⚠️":"🔴")+" "+esc(p[1]||"")+" : "+esc(p[2]||"");
      }).join(NL);
    }
    h.push('<div class="card'+(bon?"":" alerte")+'"><div class="row"><span class="nom">'+
      esc(libelle[k]||k)+"</span>"+pill(bon,e.statut,e.statut)+
      '<span class="pill warn">il y a '+e.il_y_a_min+" min</span></div>"+
      '<div class="det">'+det+"</div></div>");
  });

  document.getElementById("app").innerHTML=h.join("");
  document.getElementById("maj").textContent="dernière lecture "+new Date().toLocaleTimeString("fr-FR");
}
charger(); setInterval(charger,30000);
</script></body></html>"""


REGIE_PAGE = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Régie voix — Gaiverland</title>
<style>
:root{--ink:#1b1030;--cream:#fff4e6}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,sans-serif;color:var(--cream);
  background:linear-gradient(175deg,#1b1030,#3d1d5c 45%,#6b2f6b)}
header{position:sticky;top:0;z-index:5;padding:14px 16px;background:rgba(20,10,35,.94);
  backdrop-filter:blur(6px);border-bottom:1px solid rgba(255,244,230,.18)}
h1{margin:0 0 4px;font-size:19px}
.sub{font-size:13px;opacity:.75}
.filters{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.filters button{border:1px solid rgba(255,244,230,.35);background:rgba(255,244,230,.08);
  color:var(--cream);border-radius:20px;padding:7px 14px;font-size:13px;cursor:pointer}
.filters button.on{background:var(--cream);color:var(--ink);font-weight:bold}
main{padding:12px 16px 90px;max-width:900px;margin:0 auto}
.j{border:1px solid rgba(255,244,230,.2);border-radius:14px;padding:12px;margin-bottom:10px;
  background:rgba(255,244,230,.07)}
.j.ok{border-color:rgba(46,204,113,.75);background:rgba(46,204,113,.12)}
.j.ko{border-color:rgba(255,90,90,.75);background:rgba(255,90,90,.12);opacity:.75}
.cat{display:inline-block;font-size:10px;letter-spacing:1px;text-transform:uppercase;
  background:rgba(0,0,0,.3);padding:2px 8px;border-radius:10px;opacity:.85}
.txt{margin:9px 0 11px;font-size:15px;line-height:1.45}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row button{border:none;border-radius:11px;padding:11px 15px;font-size:14px;cursor:pointer;
  font-weight:bold;min-height:44px}
.play{background:linear-gradient(135deg,#ffd29a,#ffb56b);color:var(--ink);min-width:104px}
.play.on{background:linear-gradient(135deg,#c9b6ff,#9b8cff)}
.good{background:#2ecc71;color:#08210f}
.bad{background:#ff5a5a;color:#3d0a0a}
.undo{background:rgba(255,244,230,.18);color:var(--cream)}
.verdict{font-size:12px;opacity:.85;margin-left:auto}
.empty{text-align:center;opacity:.7;padding:40px 10px}
.bar{position:fixed;left:0;right:0;bottom:0;padding:11px 16px;background:rgba(20,10,35,.96);
  border-top:1px solid rgba(255,244,230,.18);font-size:13px;display:flex;gap:14px;
  justify-content:center;flex-wrap:wrap}
</style></head><body>
<header>
  <h1>🎙 Régie voix — jingles de Rebexis</h1>
  <div class="sub">Écoute, puis <b>Garder</b> ou <b>Retirer</b>. Un jingle retiré ne repassera plus à l'antenne.</div>
  <div class="filters">
    <button data-f="pending" class="on">À écouter</button>
    <button data-f="ko">Retirés</button>
    <button data-f="ok">Gardés</button>
    <button data-f="all">Tout</button>
  </div>
</header>
<main id="list"><div class="empty">Chargement…</div></main>
<div class="bar" id="stats"></div>
<audio id="pl"></audio>
<script>
let ITEMS=[], FILTER='pending', playing=null;
const pl=document.getElementById('pl');
const esc=s=>s.replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function load(){
  const r=await fetch('/api/regie/jingles?status=all');
  const d=await r.json();
  ITEMS=d.items||[];
  render();
}
function render(){
  const list=document.getElementById('list');
  const sel=FILTER==='all'?ITEMS:ITEMS.filter(i=>i.status===FILTER);
  const c={pending:0,ok:0,ko:0};
  ITEMS.forEach(i=>c[i.status]=(c[i.status]||0)+1);
  document.getElementById('stats').innerHTML=
    `<span>🎧 à écouter : <b>${c.pending||0}</b></span><span>✅ gardés : <b>${c.ok||0}</b></span>`+
    `<span>🗑 retirés : <b>${c.ko||0}</b></span><span>total : <b>${ITEMS.length}</b></span>`;
  if(!sel.length){ list.innerHTML='<div class="empty">Rien ici. 🎉</div>'; return; }
  list.innerHTML=sel.map(i=>`
    <div class="j ${i.status==='pending'?'':i.status}" id="j-${i.h}">
      <span class="cat">${esc(i.cat)}</span>
      <div class="txt">${esc(i.text)||'<i>(texte inconnu)</i>'}</div>
      <div class="row">
        <button class="play" onclick="play('${i.h}',this)">▶ Écouter</button>
        ${i.status!=='ok'?`<button class="good" onclick="mark('${i.h}','ok')">✅ Garder</button>`:''}
        ${i.status!=='ko'?`<button class="bad" onclick="mark('${i.h}','ko')">🗑 Retirer</button>`:''}
        ${i.status!=='pending'?`<button class="undo" onclick="mark('${i.h}','pending')">↩︎</button>`:''}
        <span class="verdict">${i.status==='ok'?'gardé':i.status==='ko'?'retiré':''}</span>
      </div>
    </div>`).join('');
}
function play(h,btn){
  document.querySelectorAll('.play.on').forEach(b=>{b.classList.remove('on');b.textContent='▶ Écouter';});
  if(playing===h && !pl.paused){ pl.pause(); playing=null; return; }
  pl.src='/api/regie/audio/'+h+'?v='+((ITEMS.find(x=>x.h===h)||{}).v||0);
  playing=h; btn.classList.add('on'); btn.textContent='⏸ En cours';
  // On REMONTE l'erreur au lieu de l'avaler : si un jour aucun son ne sort, le chef doit
  // voir pourquoi (autoplay bloqué, format refusé, réseau) plutôt qu'un bouton inerte.
  pl.play().catch(function(e){
    btn.classList.remove('on'); btn.textContent='▶ Écouter'; playing=null;
    var d=(pl.error?('code média '+pl.error.code+' — '):'')+((e&&e.name)?e.name:e);
    alert("Lecture impossible : "+d);
  });
}
pl.addEventListener('ended',()=>{
  document.querySelectorAll('.play.on').forEach(b=>{b.classList.remove('on');b.textContent='▶ Écouter';});
  playing=null;
});
async function mark(h,status){
  const it=ITEMS.find(x=>x.h===h); if(it) it.status=status;   // retour visuel immédiat
  render();
  try{
    await fetch('/api/regie/review',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({h:h,status:status})});
  }catch(e){ alert("Erreur d'enregistrement — réessaie"); load(); }
}
document.querySelectorAll('.filters button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.filters button').forEach(x=>x.classList.remove('on'));
  b.classList.add('on'); FILTER=b.dataset.f; render();
});
load();
</script></body></html>"""


PAGE = """<!doctype html>
<html lang="fr"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gaiverland — Le festival permanent</title>
<meta name="theme-color" content="#8b5cf6">
<meta name="description" content="La radio-festival permanente. Une antenne qui ne dort jamais.">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Gaiverland">
<meta property="og:title" content="Gaiverland — Le festival permanent">
<meta property="og:description" content="La radio-festival qui ne s'arrête jamais. 9 scènes — du chill à la hyper techno. Écoute en direct.">
<meta property="og:url" content="https://gaiverland.gaiver-it.fr/">
<meta property="og:image" content="https://gaiverland.gaiver-it.fr/assets/og.png">
<meta property="og:image:width" content="1200">
<meta property="og:image:height" content="630">
<meta property="og:image:alt" content="Gaiverland — le festival permanent">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="Gaiverland — Le festival permanent">
<meta name="twitter:description" content="La radio-festival qui ne s'arrête jamais. 9 scènes — du chill à la hyper techno.">
<meta name="twitter:image" content="https://gaiverland.gaiver-it.fr/assets/og.png">
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
html{background:var(--nightblue)}
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
.stages{display:grid;grid-template-columns:repeat(auto-fit,minmax(235px,1fr));gap:14px}
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
.stage .desc{margin-top:7px;font-size:12px;line-height:1.5;font-style:italic;opacity:.8;
  text-align:left;padding:0 3px}
.stage .st{font-family:sans-serif;font-size:10px;letter-spacing:2px;text-transform:uppercase;
  margin-top:8px;padding:2px 8px;border-radius:10px;display:inline-block;
  background:rgba(0,0,0,.28);opacity:.9}
.stage.on .st{background:#ff3b5c}
/* Badge « c'est la station que diffuse le bot Discord » — sur la Mainstage uniquement,
   pour lever l'ambiguïté quand on écoute en vocal et qu'on regarde le site. */
.botb{position:absolute;top:6px;left:6px;background:#5865F2;color:#fff;font-family:sans-serif;
  font-size:9px;font-weight:bold;letter-spacing:.3px;padding:3px 6px;border-radius:8px;
  line-height:1;box-shadow:0 2px 6px rgba(0,0,0,.35);pointer-events:none}
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
/* ── Galerie balade (paysages mer Toulon→Hyères) ── */
.gal-intro{opacity:.75;font-size:14px;line-height:1.5;margin-bottom:14px}
.gal-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.gal-cell{padding:0;border:0;cursor:pointer;border-radius:12px;overflow:hidden;position:relative;
  aspect-ratio:3/2;background:rgba(0,0,0,.25);box-shadow:0 6px 20px rgba(0,0,0,.35);
  transition:transform .18s,box-shadow .18s}
.gal-cell:hover{transform:translateY(-3px);box-shadow:0 12px 34px rgba(0,0,0,.55)}
.gal-cell img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .5s ease}
.gal-cell:hover img{transform:scale(1.06)}
.gal-cell:focus-visible{outline:2px solid var(--sun2);outline-offset:2px}
.gal-lb{position:fixed;inset:0;z-index:120;display:none;align-items:center;justify-content:center;
  background:rgba(10,6,20,.9);backdrop-filter:blur(6px);padding:24px}
.gal-lb.on{display:flex}
.gal-lb img{max-width:96vw;max-height:82vh;border-radius:10px;box-shadow:0 24px 70px rgba(0,0,0,.75)}
.gal-lb-cap{position:absolute;bottom:22px;left:0;right:0;text-align:center;color:var(--cream);
  font-size:14px;letter-spacing:.5px;opacity:.9;text-shadow:0 2px 8px rgba(0,0,0,.8)}
.gal-lb-x{position:absolute;top:16px;right:18px;width:44px;height:44px;border-radius:50%;
  border:1px solid rgba(255,255,255,.22);background:rgba(0,0,0,.4);color:#fff;font-size:20px;
  cursor:pointer;line-height:1}
.gal-lb-x:hover{background:rgba(0,0,0,.6)}
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
/* Retour au direct : pastille lisible « ⟳ Direct » (l'ancien glyphe seul, transparent
   et à 70% d'opacité, était quasi invisible — signalé par le chef). */
.reloadbtn{flex:0 0 auto;height:44px;padding:0 15px;border-radius:22px;
  border:1px solid rgba(255,244,230,.55);background:rgba(255,244,230,.15);cursor:pointer;
  font-size:15px;font-family:sans-serif;font-weight:bold;letter-spacing:.3px;color:var(--cream);
  display:inline-flex;align-items:center;gap:7px;transition:background .15s,transform .1s}
.reloadbtn:hover{background:rgba(255,244,230,.28)}
.reloadbtn:active{transform:scale(.95)}
.reloadbtn .ic{display:inline-block;font-size:18px;line-height:1;transition:transform .4s}
.reloadbtn:hover .ic{transform:rotate(180deg)}
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
/* ===== Refonte lecteur : console (boutons autour du visualizer, desc sous les boutons, player en bas séparé) ===== */
.console-grid{display:grid;grid-template-columns:1fr;gap:14px;align-items:center}
.console-center{display:flex;flex-direction:column;gap:10px;min-width:0}
.stages.col{display:grid;grid-template-columns:repeat(2,1fr);gap:10px;align-content:start}
.stages.col .stage{padding:12px 8px;margin:0}
.stages.col .stage .ico{font-size:26px}
.stages.col .stage .nm{margin-top:4px;font-size:15px}
.stage .desc,.stage .st{display:none}
.stationDesc{font-size:13.5px;line-height:1.55;font-style:italic;opacity:.85;text-align:center;
  min-height:3.4em;padding:4px 6px 0;transition:opacity .2s}
.playbar{display:flex;align-items:center;gap:14px 22px;flex-wrap:wrap;margin-top:18px;
  padding-top:15px;border-top:1px solid rgba(255,244,230,.18)}
.playbar .np{flex:1 1 240px;margin:0}
.playbar .fxbar{margin:0;flex:0 1 auto}
@media(min-width:880px){
  .wrap{max-width:1200px}
  header{padding:16px 0 6px}
  header h1{font-size:clamp(36px,5vw,56px)}
  header .fete{font-size:12px;letter-spacing:5px}
  header .tagline{font-size:15px;margin-top:5px}
  .pennant{display:none}
  .card{padding:16px 20px;margin-top:16px}
  .console-grid{grid-template-columns:188px minmax(0,1fr) 188px;align-items:stretch;gap:12px}
  .stages.col{grid-template-columns:1fr;gap:8px}
  .console-center .hero{aspect-ratio:auto;height:min(34vh,290px)}
  .console-center .hero-fg{justify-content:center;gap:8px;padding:14px}
  .console-center .hero-cover{width:auto;height:44%}
  .console-center .hero-t{font-size:17px;line-height:1.15;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .console-center .hero-a{font-size:13px}
  .stationDesc{min-height:2.6em}
}
@media(max-width:879px){
  .console-center{order:-1}
}
/* ===== Navigation par onglets + vues (site multi-pages sans rechargement) ===== */
.tabs{position:sticky;top:0;z-index:40;display:flex;gap:8px;justify-content:center;flex-wrap:wrap;
  padding:10px 6px;margin:0 -20px 6px;background:rgba(24,14,44,.72);backdrop-filter:blur(10px);
  border-bottom:1px solid rgba(255,244,230,.14)}
.tab{display:flex;align-items:center;gap:7px;padding:9px 16px;border-radius:22px;cursor:pointer;
  border:1px solid rgba(255,244,230,.22);background:rgba(255,244,230,.06);color:var(--cream);
  font-family:Georgia,serif;font-size:15px;transition:background .15s,transform .15s}
.tab .ti{font-size:17px;line-height:1}
.tab:hover{background:rgba(255,244,230,.13)}
.tab.on{background:linear-gradient(135deg,#ffd29a,#ff8fa3);color:var(--ink);border-color:transparent;font-weight:700}
.view{display:none}
.view.on{display:block;animation:fadeview .25s ease}
@keyframes fadeview{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.view-intro{font-style:italic;opacity:.8;font-size:14px;margin:-4px 0 14px}
.propsugg{margin-top:8px;border:1px solid rgba(255,244,230,.2);border-radius:12px;overflow:hidden;background:rgba(20,12,40,.92)}
.propsugg div{padding:10px 14px;cursor:pointer;border-bottom:1px solid rgba(255,244,230,.08);font-size:15px}
.propsugg div:last-child{border-bottom:0}
.propsugg div:hover{background:rgba(255,244,230,.12)}
.propsugg small{opacity:.6;font-family:sans-serif;font-size:12px}
/* ===== Barre lecteur fixe (persistante, façon Spotify) ===== */
body{padding-bottom:96px}
.playerbar{position:fixed;left:0;right:0;bottom:0;z-index:60;display:flex;align-items:center;gap:16px;
  padding:10px 20px;background:rgba(20,12,40,.93);backdrop-filter:blur(14px);
  border-top:1px solid rgba(255,244,230,.16);box-shadow:0 -8px 30px rgba(0,0,0,.35)}
.pb-now{display:flex;align-items:center;gap:12px;flex:1 1 200px;min-width:0}
.pb-cover{width:52px;height:52px;border-radius:10px;object-fit:cover;background:rgba(0,0,0,.3);flex:0 0 auto}
.pb-txt{min-width:0}
.pb-title{font-weight:bold;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pb-artist{font-size:13px;opacity:.8;font-style:italic;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.pb-meta{font-size:11px;opacity:.6;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-family:sans-serif}
.pb-ctrl{display:flex;align-items:center;gap:12px;flex:0 0 auto}
.playerbar .playbtn{width:52px;height:52px;font-size:20px}
.pb-vol{display:flex;align-items:center;gap:8px}
.pb-vol .fx-vol{width:110px}
.pb-bar{position:absolute;top:0;left:0;right:0;height:3px;margin:0;border-radius:0}
@media(max-width:700px){
  .playerbar{gap:10px;padding:8px 12px}
  .pb-vol,.pb-meta{display:none}
  .playerbar .playbtn{width:46px;height:46px}
  .reloadbtn .tl{display:none}
  .tab .tl{display:none}
  .tab{padding:9px 13px}
  .tab .ti{font-size:20px}
}
/* ===== Page « Soutenir » (dons Bitcoin + jauge d'objectif) ===== */
.don-goal{margin:6px 0 20px}
.don-goalhead b{font-size:26px;color:#ffd29a}
.don-of{opacity:.8;font-size:15px}
.don-bar{height:14px;background:rgba(255,244,230,.14);border-radius:8px;overflow:hidden;margin:10px 0 6px}
.don-bar i{display:block;height:100%;width:0;border-radius:8px;background:linear-gradient(90deg,#ffd29a,#ff8fa3,#c9b6ff);transition:width .8s ease}
.don-sub{font-size:13px;opacity:.7;font-style:italic}
.don-pay{display:flex;gap:22px;align-items:center;flex-wrap:wrap;padding:16px;border:1px solid rgba(255,244,230,.18);border-radius:14px;background:rgba(255,244,230,.05)}
.don-qr{width:180px;height:180px;border-radius:12px;background:#fff4e6;padding:8px;flex:0 0 auto}
.don-payinfo{flex:1;min-width:240px}
.don-lab{font-size:13px;letter-spacing:2px;text-transform:uppercase;opacity:.7;margin-bottom:6px}
.don-addr{font-family:ui-monospace,Menlo,monospace;font-size:14px;word-break:break-all;background:rgba(0,0,0,.28);padding:10px 12px;border-radius:10px}
.don-btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:12px}
.don-wallet{text-decoration:none;display:inline-block;background:linear-gradient(135deg,#f7931a,#ffb56b)!important}
.don-note{font-size:12px;opacity:.65;margin-top:10px}
.don-breakdown{margin-top:18px;font-size:13.5px;line-height:1.6;opacity:.85;padding-top:14px;border-top:1px solid rgba(255,244,230,.14)}
/* ===== Partager + inviter le bot ===== */
.spread{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;margin-top:18px;position:relative}
.spreadbtn{padding:12px 18px;border:none;border-radius:26px;cursor:pointer;font-family:Georgia,serif;
  font-size:15px;color:var(--ink);text-decoration:none;display:inline-block;transition:transform .15s}
.spreadbtn:hover{transform:translateY(-2px)}
.spreadbtn.share{background:linear-gradient(135deg,#ffd29a,#ff8fa3)}
.spreadbtn.discord{background:#5865F2;color:#fff;font-weight:700}
.sharemenu{position:absolute;bottom:calc(100% + 10px);left:50%;transform:translateX(-50%);z-index:80;
  display:flex;flex-direction:column;min-width:200px;background:rgba(20,12,40,.98);
  border:1px solid rgba(255,244,230,.2);border-radius:14px;overflow:hidden;box-shadow:0 -12px 34px rgba(0,0,0,.5)}
.sharemenu[hidden]{display:none}  /* sans ça, display:flex écrase l'attribut hidden → menu toujours ouvert */
.sharemenu a,.sharemenu button{padding:12px 16px;text-align:left;color:var(--cream);text-decoration:none;
  background:none;border:0;border-bottom:1px solid rgba(255,244,230,.08);font:inherit;font-size:15px;cursor:pointer}
.sharemenu a:last-child,.sharemenu button:last-child{border-bottom:0}
.sharemenu a:hover,.sharemenu button:hover{background:rgba(255,244,230,.12)}
</style></head><body><div class="wrap">

<header>
  <div class="fete">✦ le festival permanent ✦</div>
  <h1>GAIVERLAND</h1>
  <div class="tagline">La musique ne s'arrête jamais. Le festival non plus.</div>
  <div class="pennant">🎪 🎡 🎠 🎢 🎆</div>
</header>

<nav class="tabs" id="tabs">
  <button class="tab on" data-view="accueil" onclick="showView('accueil')"><span class="ti">🎪</span><span class="tl">Accueil</span></button>
  <button class="tab" data-view="proposer" onclick="showView('proposer')"><span class="ti">🎶</span><span class="tl">Proposer</span></button>
  <button class="tab" data-view="effets" onclick="showView('effets')"><span class="ti">🎛️</span><span class="tl">Effets</span></button>
  <button class="tab" data-view="galerie" onclick="showView('galerie')"><span class="ti">🖼️</span><span class="tl">Galerie</span></button>
  <button class="tab" data-view="festival" onclick="showView('festival')"><span class="ti">🎡</span><span class="tl">Festival</span></button>
  <button class="tab" data-view="soutenir" onclick="showView('soutenir')"><span class="ti">💛</span><span class="tl">Soutenir</span></button>
</nav>

<section class="view on" id="view-accueil">
<div class="card">
  <h2>En direct <span class="live-badge">EN DIRECT</span></h2>
  <div class="console-grid">
    <div class="stages col">
      <div class="stage live on" data-st="main" onclick="selectStation('main')"><div class="botb" title="C'est cette station que le bot Discord diffuse en vocal">🤖 bot</div><div class="ico">🎪</div><div class="nm">Mainstage</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'main')">🔗</button></div>
      <div class="stage live" data-st="chill" onclick="selectStation('chill')"><div class="ico">🌙</div><div class="nm">Chill</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'chill')">🔗</button></div>
      <div class="stage live" data-st="hard" onclick="selectStation('hard')"><div class="ico">🔥</div><div class="nm">Hard</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'hard')">🔗</button></div>
      <div class="stage live" data-st="phonk" onclick="selectStation('phonk')"><div class="ico">🏎️</div><div class="nm">Phonk</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'phonk')">🔗</button></div>
    </div>
    <div class="console-center">
      <div class="hero">
        <button class="hero-fs" onclick="openFs()" aria-label="Plein écran" title="Plein écran">⛶</button>
        <div class="hero-bg" id="hero-bg"></div>
        <div class="hero-fg">
          <img class="hero-cover" id="hero-cover" alt="" onerror="this.style.visibility='hidden'">
          <div class="hero-t" id="title">…</div>
          <div class="hero-a" id="artist"></div>
        </div>
      </div>
      <canvas id="main-viz" class="main-viz"></canvas>
    </div>
    <div class="stages col">
      <div class="stage live" data-st="lofi" onclick="selectStation('lofi')"><div class="ico">🎧</div><div class="nm">Lofi</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'lofi')">🔗</button></div>
      <div class="stage live" data-st="synthwave" onclick="selectStation('synthwave')"><div class="ico">🔊</div><div class="nm">Boost</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'synthwave')">🔗</button></div>
      <div class="stage live" data-st="classics" onclick="selectStation('classics')"><div class="ico">💿</div><div class="nm">Classics</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'classics')">🔗</button></div>
      <div class="stage live" data-st="club" onclick="selectStation('club')"><div class="ico">🪩</div><div class="nm">Club</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'club')">🔗</button></div>
      <div class="stage live" data-st="buvette" onclick="selectStation('buvette')"><div class="ico">🍺</div><div class="nm">Buvette</div><button class="cp" title="Copier le lien mp3" onclick="copyLink(event,'buvette')">🔗</button></div>
    </div>
  </div>
  <div class="stationDesc" id="stationDesc">La grande scène, celle où tout Gaiverland converge quand la nuit tombe et que les basses font trembler le sol. On n'y joue que les hymnes — ceux qui lèvent cent mille bras d'un coup, ceux qu'on reprend en chœur sans connaître les paroles. Si tu ne sais pas où aller, viens là : c'est le cœur qui bat.</div>
  <div class="authbar" id="authbar"></div>
  <div class="votes">
    <button class="v-encore" onclick="vote('ENCORE')">🔥 j'adore</button>
    <button class="v-review" onclick="vote('REVIEW')">😐 bof</button>
    <button class="v-skip"   onclick="vote('SKIP')">👎 j'aime pas</button>
    <button class="v-pass"   onclick="passTrack()">⏭ passer</button>
    <button class="v-blacklist" id="btnBlacklist" style="display:none" onclick="blacklistTrack()">🚫 blacklist</button>
  </div>
  <div class="votemsg" id="votemsg"></div>
  <div class="spread">
    <button class="spreadbtn share" onclick="shareGaiverland()">🔗 Partager la radio</button>
    <a class="spreadbtn discord" href="https://discord.com/oauth2/authorize?client_id=1515364248602284283&permissions=3148800&scope=bot%20applications.commands" target="_blank" rel="noopener">🤖 Ajouter le bot à ton Discord</a>
    <div id="sharemenu" class="sharemenu" hidden>
      <a target="_blank" rel="noopener" id="sh-wa">WhatsApp</a>
      <a target="_blank" rel="noopener" id="sh-tg">Telegram</a>
      <a target="_blank" rel="noopener" id="sh-tw">X / Twitter</a>
      <a target="_blank" rel="noopener" id="sh-fb">Facebook</a>
      <a target="_blank" rel="noopener" id="sh-rd">Reddit</a>
      <button id="sh-copy" onclick="copyShareLink()">Copier le lien</button>
    </div>
  </div>
</div>
</section>

<section class="view" id="view-proposer">
<div class="card">
  <h2>Propose un titre 🎶</h2>
  <p class="view-intro">Cherche un morceau (l'autocomplétion t'aide) ou tape un remix à la main — le convoi l'ajoutera à l'antenne.</p>
  <div class="propose">
    <input id="proptitle" type="text" placeholder="Tape un artiste ou un titre…" maxlength="200" autocomplete="off" oninput="suggProp()">
    <button class="propbtn" onclick="proposeTitle()">Envoyer au convoi 🚐</button>
  </div>
  <div id="propsugg" class="propsugg" hidden></div>
  <div class="propmsg" id="propmsg"></div>
</div>
</section>

<section class="view" id="view-effets">
<div class="card">
  <h2>Effets audio 🎛️</h2>
  <p class="view-intro">Réglages sauvés sur cet appareil, actifs sur toutes les scènes.</p>
  <div class="fx-panel">
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
</div>
</section>

<section class="view" id="view-festival">
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
  <h2>Coups de cœur de la communauté ❤️</h2>
  <div id="loved"><div class="soon">Les titres que vous soutenez le plus (🔥 ENCORE) apparaîtront ici…</div></div>
</div>

<div class="card journal">
  <h2>Journal du festival</h2>
  <div id="events"><div class="soon">Le journal s'écrit en ce moment même…</div></div>
</div>

</section>

<section class="view" id="view-galerie">
<div class="card">
  <h2>Galerie</h2>
  <!--GALERIE-->
</div>
</section>

<section class="view" id="view-soutenir">
<div class="card">
  <h2>Soutenir Gaiverland 💛</h2>
  <p class="view-intro">Gaiverland tourne sur du bénévolat et du matos perso. L'objectif : rendre l'antenne <b>légale</b> (droits SACEM + producteurs) pour garder plusieurs scènes en ligne l'esprit tranquille. Chaque don part <b>uniquement</b> dans les droits de diffusion.</p>
  <div class="don-goal">
    <div class="don-goalhead"><b id="don-raised">…</b> <span class="don-of">réunis sur <span id="don-goal">1200</span> € / an</span></div>
    <div class="don-bar"><i id="don-fill" style="width:0%"></i></div>
    <div class="don-sub" id="don-sub">Cagnotte transparente — suivie en direct sur la blockchain.</div>
  </div>
  <div class="don-pay">
    <img class="don-qr" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAASgAAAEoCAIAAABkZftOAAAGAElEQVR4nO3dQY7bRhBAUU+QWzAnyoF9osgn8N5ZZM0Fg27/Lum9taKhZH10gEKRX79+/vgG/F5//Oa/BwgPGk48CAgPAsKDgPAgIDwICA8CwoOA8CAgPAgIDwLCg4DwICA8CPz59D+4/vr72wSvf74fdf1313Nn93VOuZ5r+O/tjhMPAsKDgPAgIDwICA8CwoOA8GDCHG/VHGOVp3OeVdd593dXvf+qudbT93l6/dW/+2vI7+2OEw8CwoOA8CAgPAgIDwLCg4DwYPIcb/fcY/fcZvdcbvrcrPre3vX35sSDgPAgIDwICA8CwoOA8CAgPHjHOd5pTruv5u79ulXvs+o+nKfNIStOPAgIDwLCg4DwICA8CAgPAsKDwMfN8aq5X3X/z9M+F/9x4kFAeBAQHgSEBwHhQUB4EBAevOMcb8r+1ap9s6fv//R9qn250+aQU39vTjwICA8CwoOA8CAgPAgIDwLCg8lzvHfdy1o17zrteXGrrv+0OeQUTjwICA8CwoOA8CAgPAgIDwLCgwlzvCn7TqucNn9b9fo7q+Z1T9//035vTjwICA8CwoOA8CAgPAgIDwLCg8DXr58/Hv0Hu/fNdu+DPXXaHKn6vLvvq3lF+3W770d6x4kHAeFBQHgQEB4EhAcB4UFAeDBhjnfaPtj0+1s+tfv6qznqa8i8dNV1OvEgIDwICA8CwoOA8CAgPAgIDyY/H2+V3c9bO+31d6rnzp323Lzp88A7TjwICA8CwoOA8CAgPAgIDwLCg8n7eKuctke325T9t9Pmma/hvwcnHgSEBwHhQUB4EBAeBIQHAeHBhH28Vc9JWzWPqvbrdjttr+/p66vv89r8HL9VnHgQEB4EhAcB4UFAeBAQHgSEBxP28abMSarr3L0/dtqem+/5/3HiQUB4EBAeBIQHAeFBQHgQEB5M2Md7umdV7bPtnkft/ru7536rrPo9vD7s/pxOPAgIDwLCg4DwICA8CAgPAsKDwHH7eNV9O6fM/e5U89J3fS7fnVXv78SDgPAgIDwICA8CwoOA8CAgPJi8jzdlv6uay63aV3z6uU6bX01xbZ6LOvEgIDwICA8CwoOA8CAgPAgIDybs460yZW9q1T7YnVXvM2UPcNUe3bX532X39+bEg4DwICA8CAgPAsKDgPAgIDyYsI9XzU+q+2Su2n877fl7T19/2v0/X8PnqE48CAgPAsKDgPAgIDwICA8CwoNPej7e7rncU9Xe3bvuGU75Hp7yfDwYzP9qQkB4EBAeBIQHAeFBQHgw+fl4u/eX7lTzpd17cdVz507be7w2v77ixIOA8CAgPAgIDwLCg4DwICA8mLyPd9oc5rR5VPU+q0zfS7wO2yd04kFAeBAQHgSEBwHhQUB4EBAeTJjj7XbavOW057xN+Vx3TtuLu7P7+3fiQUB4EBAeBIQHAeFBQHgQEB580hzvXedUp92PtNrrq/YzryF7mE48CAgPAsKDgPAgIDwICA8CwoMJz8er5mCnzZ2evk91Pafdl/Lp9dw57XqecuJBQHgQEB4EhAcB4UFAeBAQHgSOu6/mbqvmclOez7Z77/G0+6Be0fMYn3LiQUB4EBAeBIQHwoPP4MSDgPAgMH4f787TOUz1+mq/brdVn/c67HOt4sSDgPAgIDwICA8CwoOA8CAgPJgwx7tTzVWq/bpVqucBVnPFp17R97/7fqpOPAgIDwLCg4DwICA8CAgPAsKDyXO8KfOcal63++8+nUeddp/P6033PO848SAgPAgIDwLCg4DwICA8CAgP3nGOd5rdz7s7bR5YXU/1fL9ryH07nXgQEB4EhAcB4UFAeBAQHgSEB4GPm+O9q93ztOmuw56z58QD4cFncOJBQHgQEB4EhAcB4cE7zvGmz4tOm/9U+4Gr9uJ27xO+hvzenHgQEB4EhAcB4UFAeBAQHgSEB5PneFOeb3bafRrt0bW/n933+bzjxIOA8CAgPAgIDwLCg4DwICA8CHz9+vmj+Lvw0Zx4EBAeBIQHAeFBQHgQEB4EhAcB4UFAeBAQHgSEBwHhQUB4EBAeCA++fYR/ARQjvBrxfUQ1AAAAAElFTkSuQmCC" alt="QR Bitcoin — scanne-moi" width="180" height="180">
    <div class="don-payinfo">
      <div class="don-lab">Don en Bitcoin</div>
      <div class="don-addr" id="don-addr">bc1q36v0s4sx3m7jdg3k7sk6lhe0wgn4pfacjur28e</div>
      <div class="don-btns">
        <button class="propbtn" id="don-copybtn" onclick="copyBtc()">Copier l'adresse</button>
        <a class="propbtn don-wallet" href="bitcoin:bc1q36v0s4sx3m7jdg3k7sk6lhe0wgn4pfacjur28e">Ouvrir mon portefeuille ↗</a>
      </div>
      <div class="don-note">Scanne le QR depuis ton téléphone, ou copie l'adresse dans ton wallet. Monero bientôt.</div>
    </div>
  </div>
  <div class="don-breakdown"><b>À quoi ça sert :</b> droits d'auteur (SACEM ~89 €/an) + droits voisins producteurs &amp; interprètes (~800 €/an pour ~5 scènes). On légalise <b>Mainstage, Hard, Boost, Classics, Club</b> en priorité (Phonk selon la demande).</div>
</div>
</section>

<footer>
  Gaiverland Radio — présente, comme toujours.
  <div class="c15">Le C15 veille sur ce site. Personne ne sait pourquoi.</div>
  <div style="margin-top:14px"><a href="/equipe" style="color:rgba(255,244,230,.55);font-size:12px;letter-spacing:1px;text-decoration:none;border-bottom:1px solid rgba(255,244,230,.28);padding-bottom:2px">L'équipe du festival →</a></div>
  <div id="modele-link" hidden style="margin-top:10px"><a href="/modele" style="color:rgba(255,244,230,.55);font-size:12px;letter-spacing:1px;text-decoration:none;border-bottom:1px solid rgba(255,244,230,.28);padding-bottom:2px">🎪 Notre modèle : Tomorrowland →</a></div>
</footer>

</div>

<audio id="player" preload="none"></audio>
<audio id="player-b" preload="none"></audio>
<div class="playerbar" id="playerbar">
  <div class="pb-now">
    <img class="pb-cover" id="pb-cover" alt="" onerror="this.style.visibility='hidden'">
    <div class="pb-txt">
      <div class="pb-title" id="pb-title">Gaiverland Radio</div>
      <div class="pb-artist" id="pb-artist"></div>
      <div class="meta pb-meta" id="meta"></div>
    </div>
  </div>
  <div class="pb-ctrl">
    <button id="playbtn" class="playbtn" onclick="togglePlay()" aria-label="Lecture">▶</button>
    <button class="reloadbtn" onclick="playLive()" aria-label="Revenir au direct" title="Revenir au direct (resync flux)"><span class="ic">⟳</span><span class="tl">Direct</span></button>
    <div class="pb-vol"><span class="fx-ico" aria-hidden="true">🔊</span><input class="fx-vol js-vol" type="range" min="0" max="100" value="100" aria-label="Volume"></div>
  </div>
  <div class="bar pb-bar"><i id="prog"></i></div>
</div>

<div id="gal-lb" class="gal-lb" onclick="if(event.target===this)closeGal()">
  <button class="gal-lb-x" onclick="closeGal()" aria-label="Fermer" title="Fermer">✕</button>
  <img id="gal-lb-img" src="" alt="">
  <div id="gal-lb-cap" class="gal-lb-cap"></div>
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
const ROUTE={main:'/live.mp3',chill:'/chill.mp3',hard:'/hard.mp3',phonk:'/phonk.mp3',lofi:'/lofi.mp3',synthwave:'/synthwave.mp3',classics:'/classics.mp3',club:'/club.mp3',buvette:'/buvette.mp3'};
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
// Courbe de volume PERCEPTUELLE (loi carrée). L'oreille est logarithmique : un gain
// linéaire tasse tout le ressenti dans le bas du curseur et rend le haut « mou ». v² donne
// une progression naturelle (curseur 50% → ~-12 dB). Le curseur reste linéaire à l'écran
// (o.vol 0..1), seule la SORTIE est courbée. Réglable : v*v (doux) → Math.pow(v,3) (marqué).
function volGain(v){ return v*v; }

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
    B.volume=volGain(o.vol);
    if(!this.ready){ A.volume=volGain(o.vol); return; }
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
    A.volume=1; n.master.gain.value=volGain(o.vol);
  },
  set(k,v){ this.o[k]=v; this.save(); this.apply(); },
  resume(){ if(this.ctx&&this.ctx.state==='suspended') this.ctx.resume().catch(function(){}); }
};

// Auto-reconnect : le flux live peut décrocher (coupure réseau, bascule wifi/4G, onglet
// mobile mis en veille qui met l'audio en pause). On mémorise l'INTENTION de lecture
// (wantPlaying) et on re-branche un src frais avec backoff. Sans ça, le player restait en
// pause après une coupure (pauses signalées sur téléphone le 18/07).
var wantPlaying=false, rcTimer=null, rcDelay=1000;
function _tryReconnect(){
  rcTimer=null;
  if(!wantPlaying) return;
  const a=AA();
  if(a.paused || a.readyState<3){ playLive(); rcDelay=Math.min(rcDelay*2,15000); }
}
function reconnectSoon(){
  if(!wantPlaying || rcTimer || document.hidden) return;   // en arrière-plan on attend le retour
  rcTimer=setTimeout(_tryReconnect, rcDelay);
}
function playLive(){
  const a=AA();
  if(!audioUrl) return;
  wantPlaying=true;
  a.src = audioUrl + (audioUrl.indexOf('?')>=0?'&':'?') + '_=' + Date.now();
  a.load();
  if(a===A){ AFX.setup(); AFX.resume(); }
  a.play().catch(function(){});
}
// Pause : on met SEULEMENT en pause, on garde le src en place. Retirer le src (ce qu'on
// faisait avant) faisait disparaître le morceau de l'écran verrouillé iOS (iOS = « plus de
// média »). En gardant le src, iOS conserve l'info + les boutons natifs (comme Apple Music
// en pause). La reprise passe par playLive() qui repose un src FRAIS → on repart au DIRECT,
// jamais sur du buffer périmé. On garde aussi la métadonnée MediaSession affichée.
function stopStream(){ wantPlaying=false; AA().pause(); }
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
// Descriptions lore par station, affichées SOUS les boutons (mises à jour au clic).
const STATION_DESC={
  main:"La grande scène, celle où tout Gaiverland converge quand la nuit tombe et que les basses font trembler le sol. On n'y joue que les hymnes — ceux qui lèvent cent mille bras d'un coup, ceux qu'on reprend en chœur sans connaître les paroles. Si tu ne sais pas où aller, viens là : c'est le cœur qui bat.",
  chill:"Le jardin suspendu, à l'écart du chaos. Ici pas de kick, juste des nappes qui s'étirent et le temps qui ralentit — ambient et planant pur, à la Tycho, Bonobo ou Jon Hopkins. La scène pour respirer, fermer les yeux et laisser l'esprit dériver.",
  hard:"La zone rouge, tout au fond du site, là où le sol ne s'arrête jamais de cogner. Hardstyle euphorique — les kicks claquent mais les mélodies soulèvent, le genre qui te fait sauter les bras en l'air. Ça tape fort, mais ça reste une fête.",
  phonk:"Les bas-fonds de Gaiverland, sous les néons roses et la brume. Drift et cowbells, basses sales, dark et brazilian phonk — la nuit qui gronde, le bitume qui défile, l'adrénaline froide. Monte le son et roule.",
  lofi:"Le refuge tout doux, la petite pièce où la pluie tombe derrière la vitre. Vinyle qui craque, beats feutrés, rien qui monte — la scène la plus lente du festival. Pour s'endormir, décompresser, ou laisser la nuit passer en douceur.",
  synthwave:"La scène qui pousse. Hyper techno, kicks qui rebondissent, leads euphoriques lancés à pleine vitesse — le tempo te prend les jambes et refuse de les lâcher. La station des montées d'adrénaline. Roule, cours, vole : elle est faite pour ça.",
  classics:"La salle des souvenirs, la scène où le temps s'arrête. Les hymnes de l'âge d'or qu'on connaît par cœur, l'EDM doré et les tubes intemporels, plus les pépites récentes qui tiennent. La nostalgie qui rassemble tout le monde. Ferme les yeux, t'y es déjà.",
  club:"La warehouse de Gaiverland, quand la nuit refuse de finir. House de club groovy et fédératrice — Dom Dolla, MK, Oliver Heldens, la house vocale qui fait bouger sans jamais tomber dans le dur. La scène de ceux qui ne veulent pas rentrer.",
  buvette:"La buvette du festival, la grande tablée où personne ne reste assis. Que des originaux qu'on braille debout, un bras autour du voisin : la chanson française qui fait la fête, l'après-ski allemand qui déraille (Layla, Atemlos, DJ Ötzi) et l'europop kitsch qui ne meurt jamais (Macarena, Barbie Girl, Blue). Pas de basses sérieuses ici — juste des tubes, des chœurs et des tournées. Santé, chef."
};
function showStationDesc(s){const el=document.getElementById('stationDesc');const t=STATION_DESC[s];if(el&&t)el.textContent=t;}

// ── Navigation par onglets (vues) — le son NE s'arrête PAS (aucun rechargement) ──
function showView(v){
  document.querySelectorAll('.view').forEach(function(s){ s.classList.toggle('on', s.id==='view-'+v); });
  document.querySelectorAll('.tab').forEach(function(b){ b.classList.toggle('on', b.dataset.view===v); });
  try{ if(location.hash!=='#'+v) history.replaceState(null,'','#'+v); }catch(e){}
  if(v==='soutenir') loadDon();
  window.scrollTo(0,0);
}
window.addEventListener('hashchange',function(){ var v=(location.hash||'').replace('#',''); if(document.getElementById('view-'+v)) showView(v); });
(function(){ var v=(location.hash||'').replace('#',''); if(v && document.getElementById('view-'+v)) showView(v); })();

// ── Autocomplétion de la page « Proposer » (catalogue iTunes public) ──
var _propTmr=null;
function suggProp(){
  clearTimeout(_propTmr);
  _propTmr=setTimeout(async function(){
    var inp=document.getElementById('proptitle'), box=document.getElementById('propsugg');
    if(!inp||!box) return;
    var q=inp.value.trim();
    if(q.length<2){ box.hidden=true; box.innerHTML=''; return; }
    try{
      var d=await (await fetch('/api/suggest?q='+encodeURIComponent(q))).json();
      if(!d.resultats||!d.resultats.length){ box.hidden=true; return; }
      box.innerHTML='';
      d.resultats.forEach(function(r){
        var el=document.createElement('div');
        var b=document.createElement('b'); b.textContent=r.artiste;
        el.appendChild(b); el.appendChild(document.createTextNode(' — '+r.titre));
        if(r.annee){ var sm=document.createElement('small'); sm.textContent='  '+r.annee+(r.genre?' · '+r.genre:''); el.appendChild(sm); }
        el.onclick=function(){ inp.value=r.artiste+' — '+r.titre; box.hidden=true; box.innerHTML=''; };
        box.appendChild(el);
      });
      box.hidden=false;
    }catch(e){ box.hidden=true; }
  },300);
}

// ── Page « Soutenir » : jauge de cagnotte (dons BTC réels via /api/don) + copie d'adresse ──
async function loadDon(){
  try{
    var d=await (await fetch('/api/don')).json();
    var goal=d.goal||1200, raised=d.raised_eur||0;
    var g=document.getElementById('don-goal'); if(g)g.textContent=goal;
    var r=document.getElementById('don-raised'); if(r)r.textContent=raised+' €';
    var f=document.getElementById('don-fill'); if(f)f.style.width=(goal?Math.min(100,Math.round(raised/goal*100)):0)+'%';
    var sub=document.getElementById('don-sub');
    if(sub) sub.textContent = d.btc ? (d.btc+' BTC reçus — suivi en direct sur la blockchain.')
                                    : 'Sois le premier à soutenir — cagnotte suivie en direct sur la blockchain.';
  }catch(e){}
}
function copyBtc(){
  var a='bc1q36v0s4sx3m7jdg3k7sk6lhe0wgn4pfacjur28e';
  var done=function(){ var b=document.getElementById('don-copybtn'); if(b){ var t=b.textContent; b.textContent='Copié ✓'; setTimeout(function(){ b.textContent=t; },1500); } };
  if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(a).then(done,done); }
  else { try{ var i=document.getElementById('don-addr'),rng=document.createRange(); rng.selectNode(i); var sel=getSelection(); sel.removeAllRanges(); sel.addRange(rng); document.execCommand('copy'); }catch(e){} done(); }
}

// ── Partage réseaux (Web Share natif sur mobile, menu en repli sur PC) + invit bot Discord ──
var SHARE_URL='https://gaiverland.gaiver-it.fr', SHARE_TXT="Gaiverland — la radio-festival qui ne s'arrête jamais 🎪 9 scènes en direct, tu votes, tu proposes tes sons.";
function initShare(){
  var u=encodeURIComponent(SHARE_URL), t=encodeURIComponent(SHARE_TXT), tu=encodeURIComponent(SHARE_TXT+' '+SHARE_URL);
  var set=function(id,h){ var e=document.getElementById(id); if(e)e.href=h; };
  set('sh-wa','https://wa.me/?text='+tu);
  set('sh-tg','https://t.me/share/url?url='+u+'&text='+t);
  set('sh-tw','https://twitter.com/intent/tweet?text='+t+'&url='+u);
  set('sh-fb','https://www.facebook.com/sharer/sharer.php?u='+u);
  set('sh-rd','https://www.reddit.com/submit?url='+u+'&title='+t);
}
function _closeShare(e){
  var m=document.getElementById('sharemenu'), sp=document.querySelector('.spread');
  if(m && sp && !sp.contains(e.target)){ m.hidden=true; document.removeEventListener('click', _closeShare); }
}
async function shareGaiverland(){
  if(navigator.share){ try{ await navigator.share({title:'Gaiverland',text:SHARE_TXT,url:SHARE_URL}); return; }catch(e){ if(e&&e.name==='AbortError') return; } }
  var m=document.getElementById('sharemenu'); if(!m) return;
  m.hidden=!m.hidden;
  if(!m.hidden){ setTimeout(function(){ document.addEventListener('click', _closeShare); }, 0); }
  else { document.removeEventListener('click', _closeShare); }
}
function copyShareLink(){
  var done=function(){ var b=document.getElementById('sh-copy'); if(b){ var t=b.textContent; b.textContent='Copié ✓'; setTimeout(function(){ b.textContent=t; },1500); } };
  if(navigator.clipboard&&navigator.clipboard.writeText){ navigator.clipboard.writeText(SHARE_URL).then(done,done); } else done();
}

function selectStation(s){
  if(s===curStation) return;
  const wasPlaying=!AA().paused;
  A.pause(); A.removeAttribute('src'); A.load();     // couper les deux éléments
  B.pause(); B.removeAttribute('src'); B.load();
  curStation=s;
  document.querySelectorAll('.stage[data-st]').forEach(t=>t.classList.toggle('on', t.dataset.st===s));
  showStationDesc(s);
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
  var _pt=document.getElementById('pb-title'); if(_pt)_pt.textContent=t.title||'Gaiverland Radio';
  var _pa=document.getElementById('pb-artist'); if(_pa)_pa.textContent=t.artist||'';
  if(cover){var _pc=document.getElementById('pb-cover'); if(_pc){_pc.src=cover;_pc.style.visibility='visible';}}
  if(t.title && t.title!==lastTitle){ lastTitle=t.title; bgIdx++; setBg(); }  // nouveau fond par morceau
  // MediaSession : NE PAS reconstruire la métadonnée à chaque repeinture (cette fonction
  // tourne 1×/seconde). Réassigner metadata en boucle fait recharger la pochette et peut
  // faire sauter l'entrée « En cours » de l'écran verrouillé iOS. On ne la pose QUE si le
  // morceau (ou la pochette) a réellement changé.
  if('mediaSession' in navigator && (t.title||t.artist)){
    const sig=(t.title||'')+'|'+(t.artist||'')+'|'+cover;
    if(sig!==lastMediaSig){
      lastMediaSig=sig;
      navigator.mediaSession.metadata=new MediaMetadata({
        title:t.title||'Gaiverland Radio', artist:t.artist||'Gaiverland Radio',
        album:'Gaiverland — le festival permanent',
        artwork:cover?[{src:cover,sizes:'512x512',type:'image/jpeg'}]:[]
      });
    }
  }
  // Progression calculée sur l'instant ENTENDU (played_at est fiable, elapsed est caché).
  if(t.duration>0 && t.played_at){
    const el=Math.max(0,Math.min(t.duration, serverNow()-audioLag()-t.played_at));
    document.getElementById('prog').style.width=(100*el/t.duration)+'%';
  } else if(t.duration>0 && t.elapsed>=0){
    document.getElementById('prog').style.width=Math.min(100,100*t.elapsed/t.duration)+'%';
  }
}

let lastLive={}, lastArt='', lastMediaSig='';
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
      headers:{'Content-Type':'application/json'},body:JSON.stringify({vote:v,station:curStation})})).json();
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
      headers:{'Content-Type':'application/json'},body:JSON.stringify({station:curStation})})).json();
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
      headers:{'Content-Type':'application/json'},body:JSON.stringify({station:curStation})})).json();
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
    setTimeout(function(){ stopStream(); try{AFX.n.master.gain.value=volGain(AFX.o.vol);}catch(e){} }, 8300);
  }else{
    const a=AA(); let v=a.volume||1; const iv=setInterval(function(){ v-=0.06; if(v<=0.02){clearInterval(iv); stopStream(); a.volume=volGain(AFX.o.vol);} else a.volume=v; }, 480);
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
  [A,B].forEach(el=>{
    el.addEventListener('play',onPlay); el.addEventListener('pause',onPause);
    el.addEventListener('playing',function(){ rcDelay=1000; });   // flux OK → reset backoff
    el.addEventListener('stalled',reconnectSoon);
    el.addEventListener('error',reconnectSoon);
    el.addEventListener('ended',reconnectSoon);                    // un live ne "finit" jamais → décrochage
  });
  // Retour d'arrière-plan (déverrouillage tél, retour d'onglet) : mobile gèle les timers,
  // donc l'affichage restait figé jusqu'à 10 s. On resynchronise TOUT de suite l'affichage
  // (refresh) ET on relance le flux s'il a décroché (reconnectSoon).
  document.addEventListener('visibilitychange',function(){ if(!document.hidden){ refresh(); reconnectSoon(); } });
  ['opt-wordmark','opt-bg','opt-bar','opt-viz'].forEach(id=>{const e=gid(id); if(e)e.addEventListener('change',fsSaveOpts);});
  // Contrôles de l'écran verrouillé / centre de contrôle. iOS garde d'autant mieux l'entrée
  // « En cours » que les actions attendues sont déclarées. On neutralise explicitement
  // avance/recul (un direct ne se navigue pas) plutôt que de laisser iOS les proposer.
  if('mediaSession' in navigator){
    const MS=navigator.mediaSession;
    const setH=function(a,fn){ try{ MS.setActionHandler(a,fn); }catch(e){} };
    setH('play',  function(){ playLive(); });
    setH('pause', function(){ stopStream(); });
    setH('stop',  function(){ stopStream(); });
    ['seekbackward','seekforward','seekto','previoustrack','nexttrack'].forEach(function(a){ setH(a,null); });
  }
  initFxUI();
})();
refresh();loadEvents();loadLoved();
loadVisuals();loadAuth();loadDon();initShare();
setInterval(refresh,10000);setInterval(loadEvents,30000);setInterval(loadVisuals,300000);setInterval(loadLoved,60000);setInterval(loadDon,120000);
// Repeint chaque seconde SANS requête réseau : le titre bascule à l'instant précis où
// l'auditeur entend le changement (le sondage réseau, lui, reste à 10 s).
setInterval(function(){ if(lastLive && lastLive.song_id) paintTrack(lastLive, lastArt); },1000);

// Lien « Notre modèle » (Tomorrowland) : affiché seulement pendant l'événement (saisonnier).
fetch('/api/modele-active').then(function(r){return r.json();}).then(function(d){
  if(d && d.active){ var el=document.getElementById('modele-link'); if(el) el.hidden=false; }
}).catch(function(){});
// ── Galerie : lightbox (charge le plein format au clic seulement) ──
function openGal(el){
  var lb=document.getElementById('gal-lb');
  var img=document.getElementById('gal-lb-img');
  var cap=document.getElementById('gal-lb-cap');
  img.src=el.getAttribute('data-full');
  img.alt=el.getAttribute('data-cap')||'';
  cap.textContent=el.getAttribute('data-cap')||'';
  lb.classList.add('on');
}
function closeGal(){
  var lb=document.getElementById('gal-lb');
  lb.classList.remove('on');
  document.getElementById('gal-lb-img').src='';
}
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeGal();});

if('serviceWorker' in navigator){navigator.serviceWorker.register('/sw.js?v=__VER__').catch(function(){});}
// Auto-guérison du cache : si le proxy/navigateur a servi une vieille page, on le détecte
// (version serveur ≠ version embarquée) et on recharge FRAIS une seule fois (garde sessionStorage
// anti-boucle + ?v= qui casse tout cache). Fini « je vois encore l'ancienne version ».
(function(){try{fetch('/api/version?_='+Date.now(),{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
  if(d&&d.ver&&d.ver!=='__VER__'&&!sessionStorage.getItem('gv_upd')){
    sessionStorage.setItem('gv_upd','1');
    try{if('serviceWorker' in navigator)navigator.serviceWorker.getRegistrations().then(function(rs){rs.forEach(function(x){x.unregister();});});}catch(e){}
    try{if(window.caches)caches.keys().then(function(ks){ks.forEach(function(k){caches.delete(k);});});}catch(e){}
    setTimeout(function(){location.replace('/?v='+d.ver+(location.hash||''));},250);
  }
}).catch(function(){});}catch(e){}})();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    html = PAGE.replace("<!--GALERIE-->", _GALLERY_HTML).replace("__VER__", _ASSET_VER)
    return HTMLResponse(html,
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/version")
def api_version():
    """Empreinte du frontend actuel. La page la compare à la sienne au chargement : si le
    proxy/navigateur a servi une vieille version en cache, elle se recharge fraîche (1×).
    Toujours no-store + la page l'appelle avec un ?_=<random> → jamais mis en cache."""
    return Response('{"ver":"' + _ASSET_VER + '"}', media_type="application/json",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


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


def _ui_cfg(key: str) -> dict:
    """Lit une valeur jsonb de la table ui_config (dict), {} si absente / DB indispo."""
    try:
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM ui_config WHERE key=%s", (key,))
            r = cur.fetchone()
        conn.close()
        v = r["value"] if r else None
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


@app.get("/modele", response_class=HTMLResponse)
def modele():
    if not modele_page:
        return RedirectResponse("/", status_code=302)
    cfg = _ui_cfg("tomorrowland")
    # Saisonnière : masquée hors événement. Défaut = active (env de secours possible).
    active = cfg.get("active", os.environ.get("TOMORROWLAND_ACTIVE", "true").lower() == "true")
    if not active:
        return RedirectResponse("/", status_code=302)
    # Streams officiels à embarquer. Format riche : cfg["streams"]=[{"label":..,"yt":..}].
    # Compat : ancien cfg["yt"] (un seul) → Mainstage. Chaque yt = URL/id vidéo/id chaîne.
    streams = []
    for s in (cfg.get("streams") or []):
        src = modele_page.yt_embed_src(s.get("yt", ""))
        streams.append((s.get("label", "Direct"), src))
    if not streams:
        yt = cfg.get("yt") or os.environ.get("TOMORROWLAND_YT", "")
        if yt:
            streams.append(("Mainstage", modele_page.yt_embed_src(yt)))
    back = ('<a href="/" style="position:fixed;top:16px;left:18px;z-index:99;'
            'color:#fff4e6;background:rgba(0,0,0,.28);border:1px solid rgba(255,244,230,.35);'
            'border-radius:20px;padding:7px 14px;font:13px Helvetica,Arial,sans-serif;'
            'text-decoration:none;backdrop-filter:blur(3px)">← Retour à la radio</a>')
    return HTMLResponse(modele_page.render(streams).replace("</body>", back + "</body>", 1),
                        headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/modele-active")
def modele_active():
    """Le front décide d'afficher le bouton « Notre modèle » selon cette réponse."""
    cfg = _ui_cfg("tomorrowland")
    return {"active": bool(cfg.get("active", os.environ.get("TOMORROWLAND_ACTIVE", "true").lower() == "true"))}


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

# Version d'assets = empreinte du frontend + du SW. Elle change dès qu'on touche à l'un
# ou l'autre → on charge le SW via `/sw.js?v=<ver>`. Le proxy public (openresty sur le NPM
# partagé .10) cache le `/sw.js` ~9h en IGNORANT le Cache-Control:no-cache qu'on envoie ;
# une nouvelle URL (nouveau ?v=) est vue comme une nouvelle ressource → le proxy la sert
# fraîche. Ça règle « un appareil marche, pas les autres » (versions figées différentes)
# SANS toucher au proxy partagé (qui sert TOUS les sites du chef = trop risqué à modifier).
_ASSET_VER = hashlib.md5((PAGE + _SW_JS).encode("utf-8")).hexdigest()[:8]


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
    # Nom de cache versionné → à l'activation, le SW purge les anciens caches (il le fait
    # déjà pour tout nom ≠ CACHE). Nouveau frontend = nouvelle version = SW « à jour ».
    body = _SW_JS.replace("gaiverland-v2", "gaiverland-" + _ASSET_VER)
    return Response(body, media_type="application/javascript",
                    headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8099)
