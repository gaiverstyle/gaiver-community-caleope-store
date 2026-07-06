"""
GCS Lore Service — Phase 6.
Mémoire immuable du festival : events C15, stagiaire, historique Rebexis, villes.
Alimenté automatiquement par gcs-rebexis, gcs-track-service, gcs-audio-injector.
"""
import os, sys, subprocess

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary"], check=True)

try:
    import fastapi, uvicorn, psycopg2
except ImportError:
    _install()
    import fastapi, uvicorn, psycopg2

import json
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from typing import Optional

DB_URL = os.environ["DATABASE_URL"]

VALID_EVENT_TYPES = {
    "c15_event",
    "stagiaire_event",
    "city_transition",
    "rebexis_intervention",
    "track_milestone",
    "festival_moment",
}

app = FastAPI(title="GCS Lore Service")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def init_db():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS lore_events (
                id          SERIAL PRIMARY KEY,
                type        VARCHAR(50)  NOT NULL,
                description TEXT         NOT NULL,
                city        VARCHAR(100) DEFAULT '',
                metadata    JSONB        DEFAULT '{}',
                created_at  TIMESTAMPTZ  DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lore_type ON lore_events(type)
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_lore_time ON lore_events(created_at DESC)
        """)
    conn.commit()
    conn.close()


@app.on_event("startup")
def startup():
    init_db()


@app.get("/health")
def health():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM lore_events")
        n = cur.fetchone()["n"]
    conn.close()
    return {"status": "ok", "total_events": n}


@app.post("/events")
def log_event(body: dict):
    """
    Body: { type, description, city?, metadata? }
    type: c15_event | stagiaire_event | city_transition | rebexis_intervention |
          track_milestone | festival_moment
    """
    event_type  = body.get("type", "festival_moment")
    description = body.get("description", "").strip()
    city        = body.get("city", "")
    metadata    = body.get("metadata", {})

    if not description:
        raise HTTPException(400, "description required")
    if event_type not in VALID_EVENT_TYPES:
        event_type = "festival_moment"

    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO lore_events (type, description, city, metadata)
            VALUES (%s,%s,%s,%s::jsonb) RETURNING id, created_at
        """, (event_type, description, city, json.dumps(metadata)))
        row = cur.fetchone()
    conn.commit()
    conn.close()

    print(f"  📜 lore [{event_type}]: {description[:60]}")
    return {"ok": True, "id": row["id"], "created_at": str(row["created_at"])}


@app.get("/events")
def get_events(type: Optional[str] = None, limit: int = 20):
    conn = get_conn()
    with conn.cursor() as cur:
        if type:
            cur.execute("""
                SELECT id, type, description, city, metadata, created_at
                FROM lore_events WHERE type=%s
                ORDER BY created_at DESC LIMIT %s
            """, (type, limit))
        else:
            cur.execute("""
                SELECT id, type, description, city, metadata, created_at
                FROM lore_events ORDER BY created_at DESC LIMIT %s
            """, (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows]}


@app.get("/summary")
def summary():
    """Snapshot rapide du lore — context pour gcs-rebexis."""
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT type, COUNT(*) AS n
            FROM lore_events GROUP BY type ORDER BY n DESC
        """)
        counts = {r["type"]: r["n"] for r in cur.fetchall()}

        cur.execute("""
            SELECT description, created_at FROM lore_events
            WHERE type IN ('c15_event','stagiaire_event')
            ORDER BY created_at DESC LIMIT 3
        """)
        recent_lore = [dict(r) for r in cur.fetchall()]

        cur.execute("""
            SELECT description FROM lore_events
            WHERE type='rebexis_intervention'
            ORDER BY created_at DESC LIMIT 5
        """)
        recent_rebexis = [r["description"] for r in cur.fetchall()]

    conn.close()
    return {
        "event_counts": counts,
        "recent_lore":  recent_lore,
        "recent_rebexis": recent_rebexis,
        "lore_state": {
            "c15_status":          "active",
            "stagiaire_status":    "unknown",
            "festival_is_permanent": True,
        }
    }


@app.get("/rebexis-context")
def rebexis_context():
    """
    Contexte lore condensé pour injection dans gcs-rebexis.
    Retourne uniquement ce dont RPE a besoin pour éviter répétitions.
    """
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            SELECT description FROM lore_events
            WHERE type='rebexis_intervention'
            ORDER BY created_at DESC LIMIT 5
        """)
        recent = [r["description"] for r in cur.fetchall()]
    conn.close()
    return {"recent_interventions": recent}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8096)
