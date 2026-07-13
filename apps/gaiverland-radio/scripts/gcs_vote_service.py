"""
GCS Vote Service — Phase 5.
ENCORE / REVIEW / SKIP — weighted scoring.
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

import psycopg2.extras
from fastapi import FastAPI, HTTPException

DB_URL    = os.environ["DATABASE_URL"]
STATE_URL = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")

ROLE_WEIGHTS = {"founder": 0.6, "user": 0.3, "system_ai": 0.1}
VALID_VOTES  = {"ENCORE", "REVIEW", "SKIP"}
# Empreintes OAuth du fondateur (sha256("provider:sub")[:32]). Le front code le rôle en dur
# à "user" (gcs_web l.520) ; on reconnaît le patron ici, au niveau du service = autoritaire.
# Le chef vote avec 3 comptes : son Google, son Discord, le Google Gaiverland.
FOUNDER_IDS = {f.strip() for f in os.environ.get("FOUNDER_IDS",
    "e49e9b4f66961841181c2fa7751fdabc,bc29f0601314241ebd7a6974a8541f88,2194b5b1bd76539a5aac0dd5fb314f25"
    ).split(",") if f.strip()}

# --- Skip démocratique : ≥ SKIP_VOTE_RATIO des auditeurs votent SKIP sur le titre
#     EN COURS → on passe automatiquement (POST AzuraCast backend/skip). ---
AZ_URL       = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY       = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION   = os.environ.get("AZURACAST_STATION_ID", "1")
AZ_SHORTCODE = os.environ.get("AZURACAST_SHORTCODE", "gaiverlandradio")
SKIP_VOTE_RATIO    = float(os.environ.get("SKIP_VOTE_RATIO", "0.5"))   # 50% par défaut
SKIP_MIN_LISTENERS = int(os.environ.get("SKIP_MIN_LISTENERS", "3"))    # pas de skip démocratique sous une VRAIE audience (1-2 auditeurs = curation suffit)
SKIP_MIN_VOTES     = int(os.environ.get("SKIP_MIN_VOTES", "2"))        # jamais un skip sur une seule voix, quel que soit le ratio

app = FastAPI(title="GCS Vote Service")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                id          SERIAL PRIMARY KEY,
                song_id     VARCHAR(100) NOT NULL,
                vote        VARCHAR(10)  NOT NULL CHECK (vote IN ('ENCORE','REVIEW','SKIP')),
                user_role   VARCHAR(20)  NOT NULL DEFAULT 'user',
                user_weight FLOAT        NOT NULL DEFAULT 0.3,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS track_scores (
                song_id     VARCHAR(100) PRIMARY KEY,
                score       FLOAT        NOT NULL DEFAULT 0.0,
                vote_count  INTEGER      NOT NULL DEFAULT 0,
                last_vote   TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_song ON votes(song_id)")
        # Migration : identité du votant (pour le skip démocratique 1 identité = 1 voix)
        cur.execute("ALTER TABLE votes ADD COLUMN IF NOT EXISTS user_id VARCHAR(64)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_skip ON votes(song_id, vote, created_at)")
        # « passer » = action de saut du titre EN COURS, DISTINCTE du vote « j'aime pas » (SKIP).
        # 1 identité = 1 passer sur le titre courant (seuil démocratique = SKIP_VOTE_RATIO × auditeurs).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS passes (
                id         SERIAL PRIMARY KEY,
                song_id    VARCHAR(100) NOT NULL,
                user_id    VARCHAR(64),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_passes ON passes(song_id, created_at)")
        # 1 VOTE PAR PERSONNE PAR MUSIQUE (anti-spam) : dédup (garder le PLUS RÉCENT par
        # (song_id, user_id) identifié) + index unique partiel. Revoter = changer son vote
        # (UPSERT dans cast_vote), pas empiler → un spammeur ne peut plus gonfler la moyenne.
        cur.execute("""
            DELETE FROM votes a USING votes b
            WHERE a.user_id IS NOT NULL AND a.user_id = b.user_id AND a.song_id = b.song_id
              AND (a.created_at < b.created_at OR (a.created_at = b.created_at AND a.id < b.id))
        """)
        cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_votes_user_song
                       ON votes(song_id, user_id) WHERE user_id IS NOT NULL""")
        # Denylist mainstage : titres bannis DÉFINITIVEMENT de la rotation jour par le fondateur
        # (bouton blacklist). Distinct de la quarantaine votes (14j) : ici c'est permanent, et
        # ça capture la finesse d'« ambiance » qu'aucun feature ne mesure. playlist.py l'exclut.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS mainstage_denylist (
                song_id  VARCHAR(100) PRIMARY KEY,
                artist   TEXT,
                title    TEXT,
                added_by VARCHAR(64),
                added_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
    conn.commit()
    conn.close()


def _now_playing() -> dict:
    import urllib.request, json
    try:
        with urllib.request.urlopen(f"{AZ_URL}/api/nowplaying/{AZ_SHORTCODE}", timeout=5) as r:
            return json.load(r)
    except Exception as e:
        return {"_err": str(e)}


def _is_current(song_id: str, np: dict) -> bool:
    return ((np.get("now_playing") or {}).get("song") or {}).get("id") == song_id


def _do_skip() -> bool:
    """POST backend/skip sur AzuraCast (saute le titre EN COURS). True si OK."""
    import urllib.request
    try:
        req = urllib.request.Request(f"{AZ_URL}/api/station/{AZ_STATION}/backend/skip",
                                     method="POST", headers={"X-API-Key": AZ_KEY})
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception as e:
        print(f"  ⚠ skip API: {e}")
        return False


def _founder_gem(song_id: str, conn) -> bool:
    """Titre au vote fondateur net positif (ENCORE>SKIP, 14j) = pépite protégée du passer démocratique."""
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) FILTER (WHERE vote='ENCORE') AS enc,
                              COUNT(*) FILTER (WHERE vote='SKIP')   AS skp
                       FROM votes WHERE song_id=%s AND user_role='founder'
                         AND created_at >= NOW() - INTERVAL '14 days'""", (song_id,))
        f = cur.fetchone()
    return (f["enc"] or 0) > (f["skp"] or 0)


def _democratic_pass(song_id: str, conn) -> dict:
    """Passe le titre EN COURS si ≥ SKIP_VOTE_RATIO des auditeurs ont cliqué « passer » dessus.
    Compte la table `passes` (action « passer »), PAS les votes SKIP (« j'aime pas » = quarantaine)."""
    import math
    np = _now_playing()
    if np.get("_err"):
        return {"skipped": False, "reason": f"nowplaying err: {np['_err']}"}
    if not _is_current(song_id, np):
        return {"skipped": False, "reason": "titre plus en cours"}
    listeners = int((np.get("listeners") or {}).get("current", 0) or 0)
    if listeners < SKIP_MIN_LISTENERS:
        return {"skipped": False, "reason": "trop peu d'auditeurs", "listeners": listeners}
    if _founder_gem(song_id, conn):
        return {"skipped": False, "reason": "pépite protégée (ENCORE fondateur)", "listeners": listeners}
    started = (np.get("now_playing") or {}).get("played_at", 0)
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(DISTINCT user_id) AS n FROM passes
                       WHERE song_id=%s AND user_id IS NOT NULL
                         AND created_at >= to_timestamp(%s)""", (song_id, started))
        n = cur.fetchone()["n"]
    needed = max(SKIP_MIN_VOTES, math.ceil(SKIP_VOTE_RATIO * listeners))
    if n >= needed and _do_skip():
        print(f"  ⏭ PASSER DÉMOCRATIQUE : {n}/{listeners} auditeurs → titre passé")
        return {"skipped": True, "passes": n, "listeners": listeners}
    return {"skipped": False, "passes": n, "listeners": listeners, "needed": needed}


def compute_score(song_id: str, conn) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT vote, user_weight FROM votes WHERE song_id=%s", (song_id,))
        rows = cur.fetchall()
    if not rows:
        return 0.0
    score = sum((1.0 if r["vote"]=="ENCORE" else -1.0 if r["vote"]=="SKIP" else 0.0)
                * r["user_weight"] for r in rows)
    return round(score / len(rows), 3)


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/vote")
def cast_vote(body: dict):
    song_id   = body.get("song_id", "").strip()
    vote      = body.get("vote", "").upper()
    user_role = body.get("user_role", "user")
    user_id   = (body.get("user_id") or "").strip() or None
    if not song_id:
        raise HTTPException(400, "song_id required")
    if vote not in VALID_VOTES:
        raise HTTPException(400, f"vote must be one of {VALID_VOTES}")
    if user_role not in ROLE_WEIGHTS:
        user_role = "user"
    # Autorité serveur : le fondateur est reconnu à son empreinte, quoi que déclare le front.
    if user_id and user_id in FOUNDER_IDS:
        user_role = "founder"
    weight = ROLE_WEIGHTS[user_role]
    conn   = get_conn()
    with conn.cursor() as cur:
        # UPSERT : 1 vote par (song_id, user_id) → revoter change le vote (anti-spam).
        # user_id NULL (anonyme/legacy) : pas de conflit sur l'index partiel → insert simple.
        cur.execute("""INSERT INTO votes (song_id,vote,user_role,user_weight,user_id)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (song_id, user_id) WHERE user_id IS NOT NULL
                       DO UPDATE SET vote=EXCLUDED.vote, user_role=EXCLUDED.user_role,
                                     user_weight=EXCLUDED.user_weight, created_at=NOW()""",
                    (song_id, vote, user_role, weight, user_id))
    conn.commit()
    # NB : « j'aime pas » (SKIP) est un pur vote (quarantaine 14j via le score) — il ne saute
    # PLUS le titre en cours. Sauter = action « passer » distincte (endpoint /pass ci-dessous).
    score = compute_score(song_id, conn)
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO track_scores (song_id,score,vote_count)
            VALUES (%s,%s,1)
            ON CONFLICT (song_id) DO UPDATE SET
                score=EXCLUDED.score, vote_count=track_scores.vote_count+1, last_vote=NOW()
        """, (song_id, score))
    conn.commit()
    conn.close()
    print(f"  ✓ vote [{user_role}] {vote} score={score}")
    return {"ok": True, "song_id": song_id, "vote": vote, "score": score}


@app.post("/pass")
def pass_track(body: dict):
    """« Passer » le titre EN COURS. Fondateur = saut immédiat (autorité). Public = démocratique
    (≥ SKIP_VOTE_RATIO des auditeurs, plancher SKIP_MIN_LISTENERS/SKIP_MIN_VOTES, pépite protégée)."""
    song_id = (body.get("song_id") or "").strip()
    user_id = (body.get("user_id") or "").strip() or None
    if not song_id:
        raise HTTPException(400, "song_id required")
    conn = get_conn()
    try:
        if user_id and user_id in FOUNDER_IDS:
            np = _now_playing()
            if np.get("_err") or not _is_current(song_id, np):
                return {"ok": True, "skipped": False, "reason": "titre plus en cours"}
            ok = _do_skip()
            print(f"  ⏭ PASSER FONDATEUR → {'passé' if ok else 'échec'}")
            return {"ok": True, "skipped": ok, "founder": True}
        with conn.cursor() as cur:
            cur.execute("INSERT INTO passes (song_id, user_id) VALUES (%s,%s)", (song_id, user_id))
        conn.commit()
        return {"ok": True, **_democratic_pass(song_id, conn)}
    finally:
        conn.close()


@app.post("/blacklist")
def blacklist(body: dict):
    """Bannir DÉFINITIVEMENT le titre EN COURS de la mainstage (fondateur uniquement)."""
    song_id = (body.get("song_id") or "").strip()
    user_id = (body.get("user_id") or "").strip() or None
    if not song_id:
        raise HTTPException(400, "song_id required")
    if not (user_id and user_id in FOUNDER_IDS):
        raise HTTPException(403, "founder only")
    np   = _now_playing()
    song = ((np.get("now_playing") or {}).get("song")) or {}
    artist = (body.get("artist") or song.get("artist") or "").strip()
    title  = (body.get("title")  or song.get("title")  or "").strip()
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO mainstage_denylist (song_id, artist, title, added_by)
                           VALUES (%s,%s,%s,%s) ON CONFLICT (song_id) DO NOTHING""",
                        (song_id, artist, title, user_id))
        conn.commit()
    finally:
        conn.close()
    skipped = _do_skip() if _is_current(song_id, np) else False
    print(f"  🚫 BLACKLIST {artist} - {title} (skip={skipped})")
    return {"ok": True, "blacklisted": True, "skipped": skipped, "artist": artist, "title": title}


@app.get("/track/{song_id}/score")
def track_score(song_id: str):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM track_scores WHERE song_id=%s", (song_id,))
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else {"song_id": song_id, "score": 0.0, "vote_count": 0}


@app.get("/leaderboard")
def leaderboard(limit: int = 10):
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT song_id,score,vote_count FROM track_scores ORDER BY score DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"tracks": [dict(r) for r in rows]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8095)
