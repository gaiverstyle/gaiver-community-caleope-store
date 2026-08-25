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
import os, sys, random, datetime
import psycopg2, psycopg2.extras

DB = os.environ["DATABASE_URL"]
# Doit matcher le SCENE_PATH_RE de playlist.py (exclut les bacs scènes → mainstage only).
SCENE_RE = os.environ.get(
    "SCENE_PATH_RE",
    r"(/music/(chill|phonk|synthwave|hard|lofi|lofi2|phonk2)/|/gaiverland_[a-z0-9]+/media/)")

# ── Automate de sets (le chef ne veut QUE écouter) ────────────────────────────
# Têtes d'affiche festival Tomorrowland ; filtrées par nb de titres au lancement.
AUTO_CANDIDATES = [
    "David Guetta", "Dimitri Vegas", "Martin Garrix", "Swedish House Mafia",
    "Calvin Harris", "Avicii", "Hardwell", "Tiesto", "Afrojack", "Alesso",
    "Armin van Buuren", "Steve Aoki", "Don Diablo", "W&W", "Nicky Romero",
    "Oliver Heldens", "Axwell", "Timmy Trumpet", "Robin Schulz", "Marshmello",
]
AUTO_ENABLED   = os.environ.get("AUTO_DJSET_ENABLED", "1") == "1"
AUTO_PERIOD    = int(os.environ.get("AUTO_DJSET_PERIOD_MIN", "180"))   # écart mini entre 2 débuts de set
AUTO_DURATION  = int(os.environ.get("AUTO_DJSET_DURATION_MIN", "60"))  # durée d'un set auto
AUTO_MIN_TRACKS = int(os.environ.get("AUTO_DJSET_MIN_TRACKS", "15"))   # titres DISTINCTS mini (1h set ~15 créneaux) sinon l'artiste boucle
AUTO_COOLDOWN  = int(os.environ.get("AUTO_DJSET_COOLDOWN", "4"))       # pas de reprise dans les N derniers sets
AUTO_HOURS     = os.environ.get("AUTO_DJSET_HOURS", "10-23")           # heures Paris autorisées (début de set)

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
    # Compte les CHANSONS DISTINCTES (clé robuste titre sans (), [] ni préfixe « Artiste - »),
    # PAS les fichiers : Robin Schulz avait 12 fichiers mais 8 titres distincts → un set d'1h
    # (~15 créneaux) l'épuisait et rejouait (bug chef 25/08). On veut assez de titres UNIQUES.
    with c.cursor() as cur:
        cur.execute("""SELECT count(DISTINCT regexp_replace(lower(regexp_replace(regexp_replace(
                         coalesce(title,''), '\\([^)]*\\)|\\[[^\\]]*\\]', '', 'g'), '^.*? - ', '')),
                         '[^a-z0-9]', '', 'g')) AS n FROM tracks
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


def _hours_ok(spec):
    """spec 'h0-h1' (heures Paris) → True si l'heure locale actuelle est dans la plage.
    Le conteneur scheduler a TZ=Europe/Paris → datetime.now() = heure de Paris."""
    try:
        h0, h1 = (int(x) for x in spec.split("-"))
    except Exception:
        return True
    h = datetime.datetime.now().hour
    return h0 <= h <= h1 if h0 <= h1 else (h >= h0 or h <= h1)  # gère les plages nuit (22-2)


def auto_tick(verbose=False):
    """Appelé à chaque cycle du scheduler. Lance un set auto si : activé, dans la plage
    horaire, aucun set actif, et le dernier set a débuté il y a ≥ AUTO_PERIOD. Choisit une
    tête d'affiche viable hors des AUTO_COOLDOWN derniers sets. No-op sinon (silencieux)."""
    if not AUTO_ENABLED:
        return
    if not _hours_ok(AUTO_HOURS):
        return
    c = _conn()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT 1 FROM dj_set WHERE ends_at > NOW() LIMIT 1")
            if cur.fetchone():
                return                                            # set déjà en cours
            cur.execute("SELECT 1 FROM dj_set WHERE started_at > NOW() - (%s * INTERVAL '1 minute') LIMIT 1",
                        (AUTO_PERIOD,))
            if cur.fetchone():
                return                                            # pas encore l'heure du prochain
            cur.execute("SELECT label FROM dj_set ORDER BY started_at DESC LIMIT %s", (AUTO_COOLDOWN,))
            recent = {r["label"].lower() for r in cur.fetchall()}
        viable = [a for a in AUTO_CANDIDATES
                  if a.lower() not in recent and _count(c, a) >= AUTO_MIN_TRACKS]
        if not viable:  # tout en cooldown ? on relâche pour ne pas rester bloqué
            viable = [a for a in AUTO_CANDIDATES if _count(c, a) >= AUTO_MIN_TRACKS]
        if not viable:
            if verbose:
                print("  (auto set : aucun artiste viable)")
            return
        artist = random.choice(viable)
        with c.cursor() as cur:
            cur.execute("""INSERT INTO dj_set (label, artist_filter, ends_at)
                           VALUES (%s, %s, NOW() + (%s * INTERVAL '1 minute'))""",
                        (artist, artist, AUTO_DURATION))
        c.commit()
        print(f"  🎧 Set AUTO lancé : {artist} ({AUTO_DURATION} min)")
    except Exception as e:
        print(f"  ⚠ auto_tick: {e}")
    finally:
        try: c.close()
        except Exception: pass


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
    elif a[0] == "auto":            # test manuel d'un tick d'automate
        auto_tick(verbose=True)
    else:
        print(__doc__)
