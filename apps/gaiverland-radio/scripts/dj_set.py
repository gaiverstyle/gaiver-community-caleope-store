#!/usr/bin/env python3
"""
Set DJ virtuel — bloc mainstage dédié à un artiste/setlist pendant N minutes.

Tant qu'un set est actif, la mainstage (playlist.py) ne pioche QUE dans les titres de
l'artiste (match artiste OU titre, tous moods), enchaînés cohérent, anti-répétition
conservée si l'artiste a assez de titres. Rebexis annonce l'ouverture et la clôture
(via le scheduler, au même titre que ses autres interventions).

Usage (dans le conteneur scheduler) :
  python3 dj_set.py start "David Guetta" 60     # set Guetta, 60 min
  python3 dj_set.py start guetta                # défaut 60 min
  python3 dj_set.py stop                        # clôt le set en cours (annonce de fin)
  python3 dj_set.py status                      # état + nb de titres dispo
"""
import os, sys
import psycopg2, psycopg2.extras

DB = os.environ["DATABASE_URL"]
# Doit matcher le SCENE_PATH_RE de playlist.py (exclut les bacs scènes → mainstage only).
SCENE_RE = os.environ.get(
    "SCENE_PATH_RE",
    r"(/music/(chill|phonk|synthwave|hard|lofi|lofi2|phonk2)/|/gaiverland_[a-z0-9]+/media/)")

DDL = """
CREATE TABLE IF NOT EXISTS dj_set (
  id serial PRIMARY KEY,
  label text NOT NULL,
  artist_filter text NOT NULL,
  started_at timestamptz DEFAULT now(),
  ends_at timestamptz NOT NULL,
  start_announced boolean DEFAULT false,
  end_announced boolean DEFAULT false
);
"""


def _conn():
    c = psycopg2.connect(DB, cursor_factory=psycopg2.extras.RealDictCursor)
    with c.cursor() as cur:
        cur.execute(DDL)
    c.commit()
    return c


def _count(c, flt):
    with c.cursor() as cur:
        cur.execute("""SELECT count(*) AS n FROM tracks
                       WHERE analyzed AND az_id IS NOT NULL AND file_path !~ %s
                         AND (artist ILIKE %s OR title ILIKE %s)""",
                    (SCENE_RE, f"%{flt}%", f"%{flt}%"))
        return cur.fetchone()["n"]


def start(label, minutes):
    c = _conn()
    flt = label.strip()
    if not flt:
        print("✗ Nom d'artiste vide."); return
    n = _count(c, flt)
    if n == 0:
        print(f"✗ Aucun titre pour « {flt} » dans le pool → set NON lancé.")
        print("  (vérifie l'orthographe, ou télécharge d'abord des titres de l'artiste.)")
        return
    with c.cursor() as cur:
        cur.execute("UPDATE dj_set SET ends_at=NOW() WHERE ends_at > NOW()")  # clôt un set en cours
        cur.execute("""INSERT INTO dj_set (label, artist_filter, ends_at)
                       VALUES (%s, %s, NOW() + (%s * INTERVAL '1 minute')) RETURNING id""",
                    (flt.title(), flt, minutes))
        sid = cur.fetchone()["id"]
    c.commit()
    warn = "  ⚠ peu de titres → il tournera en boucle." if n < 8 else ""
    print(f"✓ Set DJ #{sid} : « {flt.title()} » pour {minutes} min — {n} titres dispo.{warn}")
    print("  Mainstage bascule au prochain cycle (≤3 min). Rebexis annonce l'ouverture.")


def stop():
    c = _conn()
    with c.cursor() as cur:
        cur.execute("UPDATE dj_set SET ends_at=NOW() WHERE ends_at > NOW() RETURNING label")
        rows = cur.fetchall()
    c.commit()
    if rows:
        print(f"✓ Set clos : {', '.join(r['label'] for r in rows)}. Retour normal au prochain cycle "
              "(Rebexis annonce la fin).")
    else:
        print("  (aucun set actif)")


def status():
    c = _conn()
    with c.cursor() as cur:
        cur.execute("""SELECT label, artist_filter,
                              round(extract(epoch FROM (ends_at - NOW()))/60)::int AS restantes
                       FROM dj_set WHERE ends_at > NOW() ORDER BY started_at DESC LIMIT 1""")
        r = cur.fetchone()
    if r:
        print(f"🎧 Set actif : « {r['label']} » — {r['restantes']} min restantes, "
              f"{_count(c, r['artist_filter'])} titres.")
    else:
        print("  Aucun set actif (rotation normale).")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        print(__doc__); sys.exit(0)
    if a[0] == "start" and len(a) >= 3:
        start(a[1], int(a[2]))
    elif a[0] == "start" and len(a) == 2:
        start(a[1], 60)
    elif a[0] == "stop":
        stop()
    elif a[0] == "status":
        status()
    else:
        print(__doc__)
