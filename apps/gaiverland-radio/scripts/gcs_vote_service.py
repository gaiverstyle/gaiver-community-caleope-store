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
    conn.commit()
    conn.close()


def _democratic_skip(song_id: str, conn) -> dict:
    """Passe le titre EN COURS si ≥ SKIP_VOTE_RATIO des auditeurs ont voté SKIP dessus."""
    import urllib.request, json, math
    try:
        with urllib.request.urlopen(f"{AZ_URL}/api/nowplaying/{AZ_SHORTCODE}", timeout=5) as r:
            np = json.load(r)
    except Exception as e:
        return {"skipped": False, "reason": f"nowplaying err: {e}"}
    npn    = np.get("now_playing") or {}
    cur_id = (npn.get("song") or {}).get("id")
    if cur_id != song_id:
        return {"skipped": False, "reason": "vote sur un titre plus en cours"}
    listeners = int((np.get("listeners") or {}).get("current", 0) or 0)
    if listeners < SKIP_MIN_LISTENERS:
        return {"skipped": False, "reason": "trop peu d'auditeurs", "listeners": listeners}
    # Garde-fou pépite : un titre ENCORE-é par le fondateur (net positif 14j) est inskippable démocratiquement.
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) FILTER (WHERE vote='ENCORE') AS enc,
                              COUNT(*) FILTER (WHERE vote='SKIP')   AS skp
                       FROM votes WHERE song_id=%s AND user_role='founder'
                         AND created_at >= NOW() - INTERVAL '14 days'""", (song_id,))
        f = cur.fetchone()
    if (f["enc"] or 0) > (f["skp"] or 0):
        return {"skipped": False, "reason": "pépite protégée (ENCORE fondateur)", "listeners": listeners}
    started = npn.get("played_at", 0)
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(DISTINCT user_id) AS n FROM votes
                       WHERE song_id=%s AND vote='SKIP' AND user_id IS NOT NULL
                         AND created_at >= to_timestamp(%s)""", (song_id, started))
        skips = cur.fetchone()["n"]
    needed = max(SKIP_MIN_VOTES, math.ceil(SKIP_VOTE_RATIO * listeners))
    if skips >= needed:
        try:
            req = urllib.request.Request(
                f"{AZ_URL}/api/station/{AZ_STATION}/backend/skip",
                method="POST", headers={"X-API-Key": AZ_KEY})
            urllib.request.urlopen(req, timeout=5).read()
            print(f"  ⏭ SKIP DÉMOCRATIQUE : {skips}/{listeners} auditeurs → titre passé")
            return {"skipped": True, "skips": skips, "listeners": listeners}
        except Exception as e:
            return {"skipped": False, "reason": f"skip API: {e}", "skips": skips, "listeners": listeners}
    return {"skipped": False, "skips": skips, "listeners": listeners, "needed": needed}


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
        cur.execute("INSERT INTO votes (song_id,vote,user_role,user_weight,user_id) VALUES (%s,%s,%s,%s,%s)",
                    (song_id, vote, user_role, weight, user_id))
    conn.commit()
    # Skip démocratique : sur un SKIP, on regarde si le seuil d'auditeurs est atteint
    skip = {"skipped": False}
    if vote == "SKIP":
        try:
            skip = _democratic_skip(song_id, conn)
        except Exception as e:
            print("  skip check err:", e)
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
    print(f"  ✓ vote [{user_role}] {vote} score={score} skip={skip.get('skipped')}")
    return {"ok": True, "song_id": song_id, "vote": vote, "score": score, "skip": skip}


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
