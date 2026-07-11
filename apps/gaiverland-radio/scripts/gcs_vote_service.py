"""
GCS Vote Service — Phase 5.
ENCORE / REVIEW / SKIP — weighted scoring.
Influence sur gcs-state-engine via event.
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
from typing import Optional

DB_URL    = os.environ["DATABASE_URL"]
STATE_URL = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")

# Poids par rôle d'utilisateur (spec Bible v1.1)
ROLE_WEIGHTS = {"founder": 0.6, "user": 0.3, "system_ai": 0.1}
VALID_VOTES  = {"ENCORE", "REVIEW", "SKIP"}
# REVIEW = soft-négatif (auto, plus de pile manuelle). Cohérent avec playlist.py/multi_rotation.py.
REVIEW_VALUE = float(os.environ.get("VOTE_REVIEW_VALUE", "-0.5"))

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
                created_at  TIMESTAMPTZ  DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS track_scores (
                song_id     VARCHAR(100) PRIMARY KEY,
                score       FLOAT        NOT NULL DEFAULT 0.0,
                vote_count  INTEGER      NOT NULL DEFAULT 0,
                last_vote   TIMESTAMPTZ  DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_votes_song ON votes(song_id)")
    conn.commit()
    conn.close()


def compute_score(song_id: str, conn) -> float:
    """Weighted score: ENCORE=+1, REVIEW=0, SKIP=-1, weighted by role."""
    with conn.cursor() as cur:
        cur.execute("SELECT vote, user_weight FROM votes WHERE song_id=%s", (song_id,))
        rows = cur.fetchall()
    if not rows:
        return 0.0
    score = 0.0
    for r in rows:
        delta = 1.0 if r["vote"] == "ENCORE" else (-1.0 if r["vote"] == "SKIP" else REVIEW_VALUE)
        score += delta * r["user_weight"]
    return round(score / len(rows), 3)


def notify_state_engine(score: float, vote: str):
    """Push score signal to gcs-state-engine to influence energy."""
    try:
        if vote == "ENCORE" and score > 0.5:
            httpx.post(f"{STATE_URL}/state/phase", json={"festival_phase": "live"}, timeout=2)
    except Exception:
        pass


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/vote")
def cast_vote(body: dict):
    """
    Body: { song_id, vote: ENCORE|REVIEW|SKIP, user_role?: founder|user|system_ai }
    """
    song_id   = body.get("song_id", "").strip()
    vote      = body.get("vote", "").upper()
    user_role = body.get("user_role", "user")

    if not song_id:
        raise HTTPException(400, "song_id required")
    if vote not in VALID_VOTES:
        raise HTTPException(400, f"vote must be one of {VALID_VOTES}")
    if user_role not in ROLE_WEIGHTS:
        user_role = "user"

    weight = ROLE_WEIGHTS[user_role]
    conn   = get_conn()

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO votes (song_id, vote, user_role, user_weight)
            VALUES (%s,%s,%s,%s)
        """, (song_id, vote, user_role, weight))
    conn.commit()

    score = compute_score(song_id, conn)

    # Upsert track_scores
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO track_scores (song_id, score, vote_count)
            VALUES (%s,%s,1)
            ON CONFLICT (song_id) DO UPDATE SET
                score      = EXCLUDED.score,
                vote_count = track_scores.vote_count + 1,
                last_vote  = NOW()
        """, (song_id, score))
    conn.commit()
    conn.close()

    notify_state_engine(score, vote)
    print(f"  ✓ vote [{user_role}] {vote} → {song_id[:20]} score={score}")

    return {"ok": True, "song_id": song_id, "vote": vote, "score": score}


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
        cur.execute("""
            SELECT song_id, score, vote_count
            FROM track_scores ORDER BY score DESC LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"tracks": [dict(r) for r in rows]}


@app.get("/skip-candidates")
def skip_candidates(limit: int = 5):
    """Tracks with most SKIP votes — useful for playlist filtering."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT song_id, score, vote_count
            FROM track_scores WHERE score < -0.3
            ORDER BY score ASC LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"tracks": [dict(r) for r in rows]}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8095)
