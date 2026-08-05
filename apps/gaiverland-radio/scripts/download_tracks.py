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
import json
import time
import subprocess
import psycopg2
import psycopg2.extras

DB_URL       = os.environ["DATABASE_URL"]
COOKIES      = os.environ.get("YT_COOKIES", "/cookies/youtube-cookies.txt")
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR",
                              "/var/azuracast/stations/gaiverlandradio/media/music/community")
INTERVAL_S   = int(os.environ.get("DOWNLOAD_INTERVAL_S", "600"))    # 10 min entre passes
DAILY_LIMIT  = int(os.environ.get("DOWNLOAD_DAILY_LIMIT", "45"))    # communauté peu active → on remplit les bacs plus vite
BATCH        = int(os.environ.get("DOWNLOAD_BATCH", "5"))           # par passe

# ── Bacs thématiques (phonk, lofi, synthwave…) ────────────────────────────────
# stations.json définit, par station, un `theme` (= sous-dossier média) et une liste
# de `seeds` (titres à télécharger, curation Régis). On télécharge chaque seed dans
# music/<theme>/ → tag déterministe par dossier (indépendant de la classif Discogs),
# l'analyzer capte bpm/énergie/clé, le moteur de rotation multi-station sélectionne
# ensuite par thème. Partage la MÊME limite quotidienne + les mêmes cookies que les
# propositions communauté (une seule vanne YouTube).
STATIONS_CONFIG = os.environ.get("STATIONS_CONFIG", "/app/stations.json")


def _load_stations() -> dict:
    try:
        with open(STATIONS_CONFIG) as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ config stations illisible ({STATIONS_CONFIG}): {e}", flush=True)
        return {}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


MAX_ATTEMPTS = int(os.environ.get("DOWNLOAD_MAX_ATTEMPTS", "3"))  # re-essais avant d'abandonner


def _ensure_schema(cur):
    cur.execute("ALTER TABLE proposal_decisions ADD COLUMN IF NOT EXISTS downloaded_at TIMESTAMP")
    cur.execute("ALTER TABLE proposal_decisions ADD COLUMN IF NOT EXISTS download_status TEXT")
    cur.execute("ALTER TABLE proposal_decisions ADD COLUMN IF NOT EXISTS download_attempts INT DEFAULT 0")


def _downloaded_today(cur) -> int:
    cur.execute("SELECT count(*) AS n FROM proposal_decisions WHERE downloaded_at::date = CURRENT_DATE")
    return cur.fetchone()["n"]


def _thematic_schema(cur):
    """Table de suivi des seeds thématiques : 1 ligne par (theme, query)."""
    cur.execute("""CREATE TABLE IF NOT EXISTS thematic_seeds (
                       theme            TEXT NOT NULL,
                       query            TEXT NOT NULL,
                       status           TEXT DEFAULT 'pending',
                       attempts         INT  DEFAULT 0,
                       downloaded_at    TIMESTAMP,
                       PRIMARY KEY (theme, query)
                   )""")


def _sync_seeds(cur, cfg: dict):
    """Synchronise thematic_seeds avec stations.json : ajoute les nouvelles seeds ET
    retire celles qui ont été enlevées de la config (curation modifiée) tant qu'elles
    ne sont pas encore téléchargées. Les seeds déjà 'ok' sont conservées."""
    for st in cfg.get("stations", []):
        theme = st.get("theme")
        if not theme or not st.get("enabled", True):
            continue
        seeds = st.get("seeds", []) or []
        for q in seeds:
            cur.execute("""INSERT INTO thematic_seeds (theme, query) VALUES (%s, %s)
                           ON CONFLICT (theme, query) DO NOTHING""", (theme, q))
        if seeds:
            # Prune : seeds encore en attente mais plus dans la config → curation changée.
            cur.execute("""DELETE FROM thematic_seeds
                           WHERE theme=%s AND status IN ('pending','retry')
                             AND query <> ALL(%s)""", (theme, seeds))


def _thematic_today(cur) -> int:
    cur.execute("SELECT count(*) AS n FROM thematic_seeds WHERE downloaded_at::date = CURRENT_DATE")
    return cur.fetchone()["n"]


def _total_today(cur) -> int:
    """Compteur quotidien COMBINÉ (proposals communauté + seeds thématiques)."""
    return _downloaded_today(cur) + _thematic_today(cur)


def process_thematic_seeds(cur, cfg: dict, budget: int) -> tuple:
    """Télécharge jusqu'à `budget` seeds thématiques en attente, dans music/<theme>/.
    Retourne (tried, fails)."""
    media_root = cfg.get("media_root", "/var/azuracast/stations/gaiverlandradio/media/music")
    # Suffixe de recherche par thème (ex: "phonk", "lofi") — accolé à la requête YT pour
    # biaiser vers le bon genre et éviter les mauvais matchs sur des seeds peu connus.
    suffixes = {st.get("theme"): st.get("search_suffix", "")
                for st in cfg.get("stations", []) if st.get("theme")}
    cur.execute("""SELECT theme, query, COALESCE(attempts,0) AS attempts
                   FROM thematic_seeds
                   WHERE status IN ('pending','retry') AND downloaded_at IS NULL
                     AND COALESCE(attempts,0) < %s
                   ORDER BY attempts ASC, theme ASC LIMIT %s""", (MAX_ATTEMPTS, budget))
    tried = fails = 0
    for s in cur.fetchall():
        target = os.path.join(media_root, s["theme"])
        q = f'{s["query"]} {suffixes.get(s["theme"], "")}'.strip()
        ok = download_one(q, target)
        tried += 1
        att = s["attempts"] + 1
        if ok:
            cur.execute("""UPDATE thematic_seeds SET downloaded_at=NOW(), status='ok',
                           attempts=%s WHERE theme=%s AND query=%s""", (att, s["theme"], s["query"]))
        elif att >= MAX_ATTEMPTS:
            fails += 1
            cur.execute("""UPDATE thematic_seeds SET downloaded_at=NOW(), status='failed',
                           attempts=%s WHERE theme=%s AND query=%s""", (att, s["theme"], s["query"]))
        else:
            fails += 1
            cur.execute("""UPDATE thematic_seeds SET status='retry',
                           attempts=%s WHERE theme=%s AND query=%s""", (att, s["theme"], s["query"]))
        print(f"  {'✓' if ok else '✗'} [{s['theme']}] {s['query']}"
              + ("" if ok else f" (essai {att}/{MAX_ATTEMPTS})"), flush=True)
    return tried, fails


def _cookies_ok() -> bool:
    try:
        return os.path.exists(COOKIES) and os.path.getsize(COOKIES) > 0
    except Exception:
        return False


# Marqueurs de clip vidéo (intro/outro/voix parlée = « casse le mood »). Exclus par
# défaut du téléchargement pour préférer l'audio propre (Short Edit / Original Mix /
# canal « - Topic »). On GARDE les « Lyric Video » (leur audio est le master propre) et
# les tags promo ([OUT NOW], BEATPORT…). Le « music video » du label gagne sinon la
# recherche même avec « official audio » (constaté sur la campagne Tomorrowland → re-source
# des 44 clips). `!~=` = le titre NE doit PAS matcher.
_CLEAN_FILTER = ("duration>=45 & duration<=900 & "
                 "title!~=(?i)(official.?music.?video|official.?video|music.?video|album teaser)")
_DUR_FILTER   = "duration>=45 & duration<=900"


def _ytdlp(query: str, out: str, match_filter: str) -> subprocess.CompletedProcess:
    cmd = ["yt-dlp", "--cookies", COOKIES, "-f", "bestaudio",
           "-x", "--audio-format", "mp3", "--audio-quality", "0",
           "--no-playlist", "--embed-metadata", "--no-progress",
           "--match-filter", match_filter, "--max-downloads", "1",
           "-o", out, f"ytsearch8:{query} audio"]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300)


def download_one(query: str, target_dir: str = DOWNLOAD_DIR) -> bool:
    """Cherche + télécharge le meilleur audio PROPRE en MP3 dans target_dir. True si OK.

    Passe 1 : filtre anti-clip (exclut Official/Music Video → prend l'audio propre).
    Passe 2 (fallback) : si aucun résultat propre (titre qui n'existe QU'en clip), on
    réessaie sans le filtre vidéo — mieux vaut un clip que rien. ytsearch8 = marge pour
    que le filtre trouve une version propre parmi les résultats. Le filtre durée écarte
    les compilations « 12 HOURS » (disque plein + analyzer étranglé)."""
    os.makedirs(target_dir, exist_ok=True)
    out = os.path.join(target_dir, "%(artist,uploader)s - %(title)s.%(ext)s")
    try:
        for match_filter in (_CLEAN_FILTER, _DUR_FILTER):
            r = _ytdlp(query, out, match_filter)
            blob = (r.stdout or "") + (r.stderr or "")
            # 101 = limite --max-downloads atteinte = 1 titre bien téléchargé (succès).
            if r.returncode in (0, 101) or "has already been downloaded" in blob:
                return True
            tail = blob[-250:]
            if "sign in" in tail.lower() or "cookies" in tail.lower() or "not a bot" in tail.lower():
                print("  🔴 COOKIES YouTube invalides/expirés — refaire la procédure "
                      "(procedure-cookies-downloader.md)", flush=True)
                return False  # inutile de retenter le fallback si les cookies sont morts
            # Passe 1 sans résultat propre → on tombe dans le fallback (filtre durée seul).
        print(f"  ⚠ échec '{query}': {tail}", flush=True)
        return False
    except Exception as e:
        print(f"  ⚠ download '{query}': {e}", flush=True)
        return False


def _sante(cur, statut: str, detail: str):
    """Dépose l'état du downloader dans system_health → affiché sur /regie/musique.
    Le chef voit s'il tourne, ce qu'il a fait aujourd'hui, et pourquoi il est en pause."""
    try:
        cur.execute("""CREATE TABLE IF NOT EXISTS system_health (
                           cle TEXT PRIMARY KEY, statut TEXT NOT NULL,
                           detail TEXT, maj TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        cur.execute("""INSERT INTO system_health (cle, statut, detail, maj)
                       VALUES ('downloader',%s,%s,NOW())
                       ON CONFLICT (cle) DO UPDATE
                         SET statut=EXCLUDED.statut, detail=EXCLUDED.detail, maj=NOW()""",
                    (statut, detail))
    except Exception:
        pass


def _limite_jour(cur) -> int:
    """Quota quotidien : réglable À CHAUD depuis la page de régie (table system_health),
    sinon la valeur d'environnement. Permet au chef d'accélérer un gros import d'artiste
    sans redéployer ni toucher au serveur."""
    try:
        cur.execute("SELECT statut FROM system_health WHERE cle='downloader_limite'")
        r = cur.fetchone()
        if r:
            v = int(r[0] if not isinstance(r, dict) else r.get("statut"))
            if 1 <= v <= 500:
                return v
    except Exception:
        pass
    return DAILY_LIMIT


def _en_pause(cur) -> bool:
    """Interrupteur posé par le chef depuis la page de régie (aucun SSH nécessaire)."""
    try:
        cur.execute("SELECT statut FROM system_health WHERE cle='downloader_pause'")
        r = cur.fetchone()
        return bool(r) and (r[0] if not isinstance(r, dict) else r.get("statut")) == "on"
    except Exception:
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
                cfg = _load_stations()
                _thematic_schema(cur)
                _sync_seeds(cur, cfg)
                fait_auj = _total_today(cur)
                if _en_pause(cur):
                    _sante(cur, "PAUSE", "mis en pause depuis la page de régie")
                    if not idle_logged:
                        print("  ⏸ downloader en pause (demandé depuis /regie/musique)", flush=True)
                        idle_logged = True
                elif not _cookies_ok():
                    _sante(cur, "SANS COOKIES",
                           "cookies YouTube absents ou expirés — aucun téléchargement possible")
                    if not idle_logged:
                        print("  ⏸ pas de cookies YouTube — downloader en pause "
                              "(cf procedure-cookies-downloader.md)", flush=True)
                        idle_logged = True
                elif fait_auj >= _limite_jour(cur):
                    lim = _limite_jour(cur)
                    _sante(cur, "QUOTA ATTEINT",
                           f"{fait_auj}/{lim} téléchargés aujourd'hui — reprise demain")
                    print(f"  ⏸ limite quotidienne atteinte ({lim})", flush=True)
                else:
                    idle_logged = False
                    _sante(cur, "ACTIF", f"{fait_auj}/{_limite_jour(cur)} téléchargés aujourd'hui")
                    # ── 1) Propositions communauté (priorité — alimente la Mainstage) ──
                    cur.execute("""SELECT title, artist, canon_title,
                                          COALESCE(download_attempts,0) AS attempts
                                   FROM proposal_decisions
                                   WHERE verdict='accept' AND downloaded_at IS NULL
                                     AND COALESCE(download_attempts,0) < %s
                                   ORDER BY votes DESC NULLS LAST LIMIT %s""", (MAX_ATTEMPTS, BATCH))
                    tried = fails = 0
                    for p in cur.fetchall():
                        q = f"{p['artist']} {p['canon_title'] or p['title']}".strip() or p["title"]
                        ok = download_one(q)
                        tried += 1
                        att = p["attempts"] + 1
                        if ok:
                            cur.execute("""UPDATE proposal_decisions SET downloaded_at=NOW(),
                                           download_status='ok', download_attempts=%s WHERE title=%s""",
                                        (att, p["title"]))
                        elif att >= MAX_ATTEMPTS:
                            fails += 1  # abandon après N essais → marqué traité (plus de reboucle)
                            cur.execute("""UPDATE proposal_decisions SET downloaded_at=NOW(),
                                           download_status='failed', download_attempts=%s WHERE title=%s""",
                                        (att, p["title"]))
                        else:
                            fails += 1  # laisse downloaded_at NULL → réessai à la prochaine passe
                            cur.execute("""UPDATE proposal_decisions SET download_status='retry',
                                           download_attempts=%s WHERE title=%s""", (att, p["title"]))
                        print(f"  {'✓' if ok else '✗'} {q}"
                              + ("" if ok else f" (essai {att}/{MAX_ATTEMPTS})"), flush=True)
                        if _total_today(cur) >= _limite_jour(cur):
                            break
                    # ── 2) Seeds thématiques (bacs phonk/lofi/synthwave…) sur le budget restant ──
                    remaining = _limite_jour(cur) - _total_today(cur)
                    if remaining > 0:
                        t2, f2 = process_thematic_seeds(cur, cfg, min(BATCH, remaining))
                        tried += t2
                        fails += f2
                    # Alerte maintenance : toute la passe en échec = cookies expirés ou
                    # yt-dlp/Deno à mettre à jour (jeu du chat et de la souris YouTube).
                    if tried and fails == tried:
                        print(f"  🔴 MAINTENANCE : {fails}/{tried} downloads en échec — vérifier les "
                              f"cookies (expirés ?) ou mettre à jour l'image downloader "
                              f"(docker rmi gaiverland-radio-gw-downloader + reinstall = yt-dlp/Deno à jour).",
                              flush=True)
                        # Une passe entière en échec doit s'AFFICHER sur /regie, pas seulement
                        # dans un journal : statut hors liste blanche → pastille rouge, et la
                        # page Téléchargeur dit quoi faire (cause n°1 : cookies expirés).
                        _sante(cur, "EN PANNE",
                               f"{fails}/{tried} téléchargements en échec — dépose des cookies "
                               f"YouTube frais sur la page, ou attends la mise à jour auto de "
                               f"yt-dlp (lundi 04h40)")
            conn.close()
        except Exception as e:
            print(f"  ⚠ downloader loop: {e}", flush=True)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    loop()
