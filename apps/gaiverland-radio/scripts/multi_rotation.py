"""
Moteur de rotation MULTI-STATION Gaiverland (Yann).

Généralise la rotation (jusqu'ici Mainstage-only) à toutes les stations thématiques
définies dans stations.json. Pour chaque station :
  1. sélectionne les tracks analysées correspondant à son filtre (thème = sous-dossier
     média `music/<theme>/`, et/ou genre_top1 ∈ genres, + plages BPM/énergie) ;
  2. les ordonne de façon cohérente (départ énergie douce puis enchaînement BPM proche —
     même esprit que order_for_coherence de playlist.py, version compacte) ;
  3. (ré)assigne cette liste à une playlist AzuraCast dédiée de la station + fixe l'ordre.

Réutilise les patterns d'az_utils mais PARAMÉTRÉS par station_id (az_utils est mono-station).
Idempotent : la playlist « Gaiverland Rotation » de chaque station est gérée par ce seul
moteur, on la re-synchronise à chaque passe. Les stations sans az_station_id (pas encore
provisionnées) sont loggées et sautées — dès qu'on renseigne leur id dans stations.json,
elles entrent automatiquement dans la rotation.
"""
import os, re
import sys
import json
import httpx
import psycopg2
import psycopg2.extras

DB_URL          = os.environ["DATABASE_URL"]
AZ_URL          = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY          = os.environ.get("AZURACAST_API_KEY", "")
STATIONS_CONFIG = os.environ.get("STATIONS_CONFIG", "/app/stations.json")
PL_NAME         = os.environ.get("ROTATION_PLAYLIST_NAME", "Gaiverland Rotation")
MAX_TRACKS      = int(os.environ.get("ROTATION_MAX_TRACKS", "250"))
# Plancher : une scène dont le bac est en cours de remplissage ne doit PAS partir à l'antenne
# avec une poignée de titres — 5 titres = une boucle de 18 min, c'est pire que de garder
# l'ancien contenu. En dessous de ce seuil, on laisse la rotation en place et on le dit.
MIN_TRACKS      = int(os.environ.get("ROTATION_MIN_TRACKS", "25"))
MEDIA_MARKER    = "/media/"   # tracks.file_path = …/stations/<st>/media/music/<theme>/x.mp3
_TIMEOUT        = 30

# Effet des votes sur les scènes — mêmes seuils/logique que la Mainstage (playlist.py).
# Avant, les votes ne touchaient QUE la Mainstage ; désormais ils s'appliquent partout.
VOTE_WINDOW_DAYS      = int(os.environ.get("VOTE_WINDOW_DAYS", "14"))
VOTE_SKIP_THRESHOLD   = float(os.environ.get("VOTE_SKIP_THRESHOLD", "-0.25"))
VOTE_ENCORE_THRESHOLD = float(os.environ.get("VOTE_ENCORE_THRESHOLD", "0.25"))
# REVIEW n'est plus une pile manuelle : il compte comme soft-négatif dans le score
# (0 avant). Gentil : un REVIEW seul ne met pas en quarantaine (plafond user 0.3×0.5=0.15
# < seuil 0.25), mais REVIEW + SKIP fait basculer. Tunable / neutralisable (=0) par env.
VOTE_REVIEW_VALUE     = float(os.environ.get("VOTE_REVIEW_VALUE", "-0.5"))


def _ok() -> bool:
    return bool(AZ_KEY) and AZ_KEY not in ("", "__CONFIGURE__")


def _headers() -> dict:
    return {"X-API-Key": AZ_KEY, "Content-Type": "application/json"}


# ─────────────────────────────────────────────
# AzuraCast — paramétré par station_id (multi-station)
# ─────────────────────────────────────────────
def az_list_playlists(sid: int) -> list:
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{sid}/playlists", headers=_headers(), timeout=_TIMEOUT)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def az_get_or_create_playlist(sid: int, name: str) -> int:
    for pl in az_list_playlists(sid):
        if pl.get("name") == name:
            return pl["id"]
    body = {"name": name, "type": "default", "weight": 3, "is_enabled": True, "order": "sequential"}
    try:
        r = httpx.post(f"{AZ_URL}/api/station/{sid}/playlists", headers=_headers(), json=body, timeout=_TIMEOUT)
        if r.status_code in (200, 201):
            return r.json().get("id", 0)
    except Exception as e:
        print(f"  ⚠ create playlist station {sid}: {e}", flush=True)
    return 0


def az_station_files(sid: int) -> list:
    """Tous les fichiers d'une station (id, path, playlists). Média partagé → chemins stables."""
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{sid}/files",
                      headers=_headers(), params={"rowsPerPage": 5000}, timeout=60)
        if r.status_code != 200:
            return []
        rows = r.json()
        return rows.get("rows", []) if isinstance(rows, dict) else rows
    except Exception as e:
        print(f"  ⚠ files station {sid}: {e}", flush=True)
        return []


def az_batch_assign(sid: int, paths: list, pid: int) -> bool:
    """Ajoute (idempotent) des fichiers à une playlist via le batch endpoint (chemins)."""
    if not paths:
        return False
    try:
        r = httpx.put(f"{AZ_URL}/api/station/{sid}/files/batch", headers=_headers(),
                      json={"do": "playlist", "files": paths, "playlists": [str(pid)]}, timeout=60)
        d = r.json() if r.status_code in (200, 201) else {}
        return bool(d.get("success", False))
    except Exception as e:
        print(f"  ⚠ batch assign station {sid}: {e}", flush=True)
        return False


def az_unassign_file(sid: int, fid: int, keep_playlists: list) -> bool:
    """Retire un fichier d'une playlist en réécrivant sa liste (le batch est additif)."""
    try:
        r = httpx.put(f"{AZ_URL}/api/station/{sid}/file/{fid}", headers=_headers(),
                      json={"playlists": keep_playlists}, timeout=15)
        return r.status_code < 400
    except Exception:
        return False


def az_set_order(sid: int, pid: int, file_ids: list) -> bool:
    if not file_ids:
        return False
    try:
        r = httpx.put(f"{AZ_URL}/api/station/{sid}/playlist/{pid}/order",
                      headers=_headers(), json={"order": file_ids}, timeout=60)
        return r.status_code in (200, 204)
    except Exception as e:
        print(f"  ⚠ set order station {sid}: {e}", flush=True)
        return False


# ─────────────────────────────────────────────
# Sélection + ordonnancement
# ─────────────────────────────────────────────
def select_tracks(cur, st: dict) -> list:
    """Tracks analysées correspondant au filtre de la station.
    Le DOSSIER thème est AUTORITÉ : une scène = SES dossiers (music/<theme>/ +
    gaiverland_<theme>/media/), JAMAIS par genre — Essentia mis-tague dans les deux sens
    → sans ça, un titre mainstage genre 'Electro' (ex Rock That Body dans up1/) fuitait dans
    la synthwave. Le genre ne sert qu'en dernier recours, pour une scène SANS dossier thème."""
    flt = st.get("filter", {}) or {}
    where, params = [], []
    theme = st.get("theme") or flt.get("theme")   # theme = champ RACINE de la station (pas dans filter)
    extra = st.get("extra_genres") or []
    if theme:
        # Renfort par genre, UNIQUEMENT si la station le demande explicitement (extra_genres).
        # C'est l'exception assumée à « dossier = autorité » : le chef veut que la Hard vienne
        # piocher les hardstyle qui vivent dans le dossier mainstage, SANS les en retirer.
        # N'ouvrir qu'à des genres où Essentia ne se trompe pas (Hardstyle oui, Hardcore NON).
        cond = "(file_path LIKE %s OR file_path LIKE %s"
        params += [f"%{MEDIA_MARKER}music/{theme}/%", f"%/gaiverland_{theme}/media/%"]
        if extra:
            cond += " OR genre_top1 = ANY(%s)"
            params.append(extra)
        where.append(cond + ")")
    elif flt.get("genres"):
        where.append("genre_top1 = ANY(%s)")
        params.append(flt["genres"])
    else:
        return []  # station sans filtre exploitable

    bpm = st.get("bpm")
    if bpm and len(bpm) == 2:
        where.append("bpm BETWEEN %s AND %s")
        params += [bpm[0], bpm[1]]
    energy = st.get("energy")
    if energy and len(energy) == 2:
        where.append("energy BETWEEN %s AND %s")
        params += [energy[0], energy[1]]

    sql = f"""SELECT id, file_path, artist, title, bpm, energy, key_note, key_scale
              FROM tracks
              WHERE analyzed = true
                AND file_path NOT LIKE '%%rebexis_%%'
                AND {' AND '.join(where)}
              LIMIT 2000"""
    cur.execute(sql, params)
    rows = cur.fetchall()

    # Dédoublonnage PAR CHANSON (même logique que playlist.py) : la bibliothèque contient
    # plusieurs copies du même titre sous des noms de fichier différents. Sans ça, élargir le
    # bac fait remonter « Vielleicht Vielleicht » trois fois et « Katyusha » quatre fois.
    vus, uniques = set(), []
    for r in rows:
        cle = re.sub(r"[^a-z0-9]+", "",
                     ((r.get("artist") or "") + (r.get("title") or "")).lower())
        if cle and cle in vus:
            continue
        vus.add(cle)
        uniques.append(r)
    if len(uniques) < len(rows):
        print(f"  ↺ {len(rows) - len(uniques)} doublon(s) de chanson écarté(s)", flush=True)
    return uniques


def order_coherent(rows: list, limit: int) -> list:
    """Départ sur l'énergie la plus basse puis glouton : prochain titre au coût BPM+énergie
    minimal. Version compacte de order_for_coherence (playlist.py) — sans clé Camelot, suffisant
    pour une station thématique (le fondu smart d'AzuraCast lisse la transition)."""
    pool = [r for r in rows if r.get("bpm")]
    if not pool:
        pool = list(rows)
    pool.sort(key=lambda t: float(t.get("energy") or 0.5))
    ordered = [pool.pop(0)]
    while pool and len(ordered) < limit:
        last = ordered[-1]
        lb = float(last.get("bpm") or 0)
        le = float(last.get("energy") or 0.5)

        def cost(t):
            db = abs(float(t.get("bpm") or 0) - lb)
            # BPM proche OU double/moitié tempo (mix harmonique classique)
            db = min(db, abs(db - lb)) if lb else db
            de = abs(float(t.get("energy") or 0.5) - le)
            return 0.6 * min(1.0, db / 12.0) + 0.4 * min(1.0, de / 0.3)

        nxt = min(pool, key=cost)
        pool.remove(nxt)
        ordered.append(nxt)
    return ordered[:limit]


# ─────────────────────────────────────────────
# Effet des votes (réplique de playlist.py, appliqué par station)
# ─────────────────────────────────────────────
def vote_scores(cur) -> dict:
    """Score voté pondéré (0.6 chef / 0.3 users / 0.1 IA) par track.id sur 14 j.
    ENCORE=+1, SKIP=-1, REVIEW=VOTE_REVIEW_VALUE. Relié à tracks via song_id.
    Connexion en autocommit : chaque requête est sa propre transaction, donc si la
    table votes/song_id manque on renvoie {} sans rien abîmer (pas besoin de savepoint)."""
    try:
        cur.execute(f"""
            SELECT t.id AS id,
               0.6*COALESCE(avg(CASE WHEN v.user_role='founder'   THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0)
             + 0.3*COALESCE(avg(CASE WHEN v.user_role='user'      THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0)
             + 0.1*COALESCE(avg(CASE WHEN v.user_role='system_ai' THEN (CASE v.vote WHEN 'ENCORE' THEN 1.0 WHEN 'SKIP' THEN -1.0 WHEN 'REVIEW' THEN {VOTE_REVIEW_VALUE:.3f} ELSE 0.0 END) END),0) AS score
            FROM tracks t JOIN votes v ON v.song_id = t.song_id
            WHERE t.song_id IS NOT NULL
              AND v.created_at > NOW() - (%s * INTERVAL '1 day')
            GROUP BY t.id
        """, (VOTE_WINDOW_DAYS,))
        return {r["id"]: float(r["score"]) for r in cur.fetchall()}
    except Exception as e:
        print(f"  ⚠ effet votes ignoré (scène): {e}", flush=True)
        return {}


def denylisted_ids(cur, sid) -> set:
    """track.id bannis DÉFINITIVEMENT de CETTE station (station_denylist, bouton blacklist
    scène-aware). Dégradé-safe : {} si la table n'existe pas encore (autocommit = pas de savepoint)."""
    try:
        cur.execute("""SELECT t.id FROM tracks t JOIN station_denylist d
                       ON d.song_id = t.song_id AND d.station_id = %s""", (sid,))
        return {r["id"] for r in cur.fetchall()}
    except Exception as e:
        print(f"  ⚠ denylist scène ignorée: {e}", flush=True)
        return set()


# ─────────────────────────────────────────────
# Rotation d'une station
# ─────────────────────────────────────────────
def rotate_station(cur, st: dict) -> None:
    label = st.get("label", st.get("slug", "?"))
    sid = st.get("az_station_id")
    if not st.get("enabled", True):
        return
    if not sid:
        print(f"  ⏭ {label}: pas d'az_station_id (à provisionner) — sauté", flush=True)
        return

    rows = select_tracks(cur, st)
    if not rows:
        print(f"  ∅ {label}: bac vide (0 track analysée pour ce filtre) — rotation inchangée", flush=True)
        return

    # ── Denylist de la station (bouton blacklist scène-aware) : bannissement DUR ──
    dl = denylisted_ids(cur, sid)
    if dl:
        rows = [r for r in rows if r["id"] not in dl]
        if not rows:
            print(f"  ∅ {label}: tout le bac est blacklisté — rotation inchangée", flush=True)
            return
        print(f"  🚫 {label}: {len(dl)} titre(s) blacklisté(s) exclu(s)", flush=True)

    # ── Effet des votes (auto, comme la Mainstage) ──
    vs = vote_scores(cur)
    n_before = len(rows)
    quarantined = {tid for tid, s in vs.items() if s <= VOTE_SKIP_THRESHOLD}
    if quarantined:
        kept = [r for r in rows if r["id"] not in quarantined]
        if kept:                      # garde-fou : ne JAMAIS vider une scène sur les seuls votes
            rows = kept
    ordered = order_coherent(rows, MAX_TRACKS)
    if len(ordered) < MIN_TRACKS:
        print(f"  ⏳ {label}: bac en remplissage ({len(ordered)}/{MIN_TRACKS} titres) — "
              f"rotation actuelle CONSERVÉE, bascule au prochain passage une fois le seuil atteint",
              flush=True)
        return
    encored = {tid for tid, s in vs.items() if s >= VOTE_ENCORE_THRESHOLD}
    if encored:
        ordered.sort(key=lambda t: 0 if t["id"] in encored else 1)  # tri STABLE → ENCORE en tête, ordre cohérent préservé
    n_q = n_before - len(rows)
    n_e = sum(1 for t in ordered if t["id"] in encored)

    # Map chemin absolu DB → fichier AzuraCast de CETTE station (média partagé, matché par basename).
    files = az_station_files(sid)
    base_to_file = {}
    for f in files:
        p = f.get("path")
        if p:
            base_to_file.setdefault(os.path.basename(p), f)

    desired_paths, desired_ids = [], []
    for t in ordered:
        f = base_to_file.get(os.path.basename(t["file_path"]))
        if f:
            desired_paths.append(f["path"])
            desired_ids.append(f["id"])
    if not desired_ids:
        print(f"  ⚠ {label}: {len(ordered)} tracks sélectionnées mais 0 mappée sur AzuraCast "
              f"(média pas encore scanné/partagé sur station {sid})", flush=True)
        return

    pid = az_get_or_create_playlist(sid, PL_NAME)
    if not pid:
        print(f"  ⚠ {label}: playlist '{PL_NAME}' introuvable/incréable", flush=True)
        return

    # Assigner les voulus + retirer les périmés (fichiers encore dans la playlist mais plus voulus).
    az_batch_assign(sid, desired_paths, pid)
    desired_set = set(desired_ids)
    removed = 0
    for f in files:
        fid = f.get("id")
        pls = [p["id"] for p in f.get("playlists", [])]
        if pid in pls and fid not in desired_set:
            if az_unassign_file(sid, fid, [p for p in pls if p != pid]):
                removed += 1
    az_set_order(sid, pid, desired_ids)
    print(f"  ✓ {label} (station {sid}): {len(desired_ids)} titres en rotation"
          + (f", {removed} périmés retirés" if removed else "")
          + (f" | votes: -{n_q} quarantaine, {n_e} boostés" if (n_q or n_e) else ""), flush=True)


def run_once() -> None:
    if not _ok():
        print("  ⏸ rotation multi-station: clé AzuraCast absente — sauté", flush=True)
        return
    try:
        with open(STATIONS_CONFIG) as f:
            cfg = json.load(f)
    except Exception as e:
        print(f"  ⚠ config stations illisible ({STATIONS_CONFIG}): {e}", flush=True)
        return
    stations = cfg.get("stations", [])
    print(f"🔁 Rotation multi-station — {len(stations)} station(s) configurée(s)", flush=True)
    conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for st in stations:
                try:
                    rotate_station(cur, st)
                except Exception as e:
                    print(f"  ⚠ rotation {st.get('slug','?')}: {e}", flush=True)
    finally:
        conn.close()


if __name__ == "__main__":
    run_once()
