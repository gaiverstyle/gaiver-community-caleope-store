"""
Découverte de titres via TRACKLISTS PUBLIÉES (Yann — cahier des charges Cassy 19/07).

Lit des listes de titres officielles (playlists Spotify de Tomorrowland & co) et insère
chaque (artiste, titre) dans `title_proposals` → le validateur de Régis (`proposal_validator`)
filtre par style → le downloader existant télécharge depuis SA source → analyse → rotation.

RÈGLES (non négociables) :
  - On lit des NOMS DE TITRES publiés. On ne capte/fingerprinte/re-héberge AUCUN flux.
  - Spotify (API officielle) > scraping. 1001Tracklists = extension future (autre source).
  - Dédup obligatoire (bibliothèque + propositions + déjà vus). Plafond de titres/run.
  - Ignorer les « ID - ID » (exclus non nommés) et toute entrée sans artiste OU sans titre.
  - Idempotent : un titre déjà vu/proposé n'est jamais réinséré.

Sans SPOTIFY_CLIENT_ID/SECRET → NO-OP (log + sortie). Rien ne casse tant que la clé n'est
pas posée. Lancé périodiquement (scheduler / cron). CPU-only, aucune dépendance lourde.
"""
import os, re, json, base64, time, unicodedata, urllib.request, urllib.parse
import psycopg2, psycopg2.extras

DB_URL         = os.environ["DATABASE_URL"]
SPOTIFY_ID     = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
SPOTIFY_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
# Playlists officielles à parser : "<id>:<source_tag>,<id>:<tag>". Défaut = TML 2026 Mainstage.
PLAYLISTS_RAW  = os.environ.get("SPOTIFY_PLAYLISTS",
                                "0yS25E7g9xQZ1Dst5SqUZn:tml_2026_mainstage")
INSERT_CAP     = int(os.environ.get("TRACKLIST_INSERT_CAP", "30"))   # nouveaux titres / run (plafond)
HTTP_PAUSE_S   = float(os.environ.get("TRACKLIST_HTTP_PAUSE_S", "0.3"))  # rate-limit courtois


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def norm(s: str) -> str:
    """Clé de comparaison : sans accents, minuscule, alphanum + espaces."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def is_bad_entry(artist: str, title: str) -> bool:
    """Rejette les « ID - ID » (exclus non nommés) et toute entrée incomplète."""
    a, t = (artist or "").strip(), (title or "").strip()
    if not a or not t:
        return True
    if norm(a) in ("id", "unknown", "unreleased") or norm(t) in ("id", "unknown", "unreleased"):
        return True
    return False


# ── Spotify (Client Credentials — lecture publique, aucun compte utilisateur) ──
def spotify_token() -> str:
    if not (SPOTIFY_ID and SPOTIFY_SECRET):
        return ""
    auth = base64.b64encode(f"{SPOTIFY_ID}:{SPOTIFY_SECRET}".encode()).decode()
    data = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request("https://accounts.spotify.com/api/token", data=data,
                                 headers={"Authorization": "Basic " + auth,
                                          "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("access_token", "")


def spotify_playlist_entries(pid: str, source_tag: str, token: str) -> list:
    """Retourne [(artiste, titre, source_tag)] d'une playlist Spotify (paginé)."""
    out, url = [], (f"https://api.spotify.com/v1/playlists/{pid}/tracks"
                    "?fields=next,items(track(name,artists(name)))&limit=100")
    while url:
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
        with urllib.request.urlopen(req, timeout=15) as r:
            d = json.load(r)
        for it in d.get("items", []):
            tr = (it or {}).get("track") or {}
            title = tr.get("name", "")
            artists = tr.get("artists") or []
            artist = artists[0].get("name", "") if artists else ""
            if not is_bad_entry(artist, title):
                out.append((artist, title, source_tag))
        url = d.get("next")
        if url:
            time.sleep(HTTP_PAUSE_S)
    return out


# ── Dédup + insertion dans la file de propositions ────────────────────────────
def _state_schema(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS tracklist_seen (
                       k       TEXT PRIMARY KEY,      -- clé normalisée artiste+titre
                       source  TEXT,
                       seen_at TIMESTAMPTZ DEFAULT now())""")


def existing_keys(cur) -> set:
    """Tout ce qu'on a DÉJÀ : bibliothèque + propositions + décisions + déjà vus.
    On ne re-propose jamais un titre déjà connu (anti re-download)."""
    keys = set()
    cur.execute("SELECT artist, title FROM tracks WHERE title IS NOT NULL")
    for r in cur.fetchall():
        keys.add(norm(f"{r['artist']} {r['title']}"))
    for tbl in ("title_proposals", "proposal_decisions"):
        try:
            cur.execute(f"SELECT title FROM {tbl}")
            for r in cur.fetchall():
                keys.add(norm(r["title"]))                       # « Artiste - Titre »
        except Exception:
            pass
    cur.execute("SELECT k FROM tracklist_seen")
    keys.update(r["k"] for r in cur.fetchall())
    return keys


def insert_proposals(cur, entries: list, cap: int) -> int:
    """Insère les entrées NOUVELLES dans title_proposals (plafonné). Retourne le nb inséré."""
    _state_schema(cur)
    have = existing_keys(cur)
    n = 0
    for artist, title, source in entries:
        if n >= cap:
            break
        key = norm(f"{artist} {title}")
        if not key or key in have:
            continue
        proposal = f"{artist} - {title}"
        uid = ("tl:" + source)[:64]     # traçabilité de la source dans user_id
        cur.execute("INSERT INTO title_proposals (user_id, title) VALUES (%s, %s)", (uid, proposal))
        cur.execute("""INSERT INTO tracklist_seen (k, source) VALUES (%s, %s)
                       ON CONFLICT (k) DO NOTHING""", (key, source))
        have.add(key)
        n += 1
        print(f"  + {proposal}  [{source}]", flush=True)
    return n


def parse_playlists_conf(raw: str) -> list:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        pid, _, tag = part.partition(":")
        out.append((pid.strip(), (tag.strip() or pid.strip())))
    return out


def main():
    if not (SPOTIFY_ID and SPOTIFY_SECRET):
        print("⏸ discover_tracklists : pas de clé Spotify (SPOTIFY_CLIENT_ID/SECRET) — no-op.",
              flush=True)
        return
    playlists = parse_playlists_conf(PLAYLISTS_RAW)
    print(f"🎧 discover_tracklists : {len(playlists)} playlist(s), plafond {INSERT_CAP}/run", flush=True)
    try:
        token = spotify_token()
    except Exception as e:
        print(f"  🔴 auth Spotify échouée : {e}", flush=True)
        return
    if not token:
        print("  🔴 pas de token Spotify.", flush=True)
        return

    entries = []
    for pid, tag in playlists:
        try:
            e = spotify_playlist_entries(pid, tag, token)
            print(f"  playlist {tag} ({pid}) : {len(e)} titres lus", flush=True)
            entries.extend(e)
        except Exception as ex:
            print(f"  ⚠ playlist {pid} : {ex}", flush=True)

    conn = get_conn(); conn.autocommit = True
    with conn.cursor() as cur:
        added = insert_proposals(cur, entries, INSERT_CAP)
    conn.close()
    print(f"✓ {added} nouvelle(s) proposition(s) insérée(s) "
          f"(les autres = déjà connues ou plafond atteint).", flush=True)


if __name__ == "__main__":
    main()
