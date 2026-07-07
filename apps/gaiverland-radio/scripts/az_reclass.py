#!/usr/bin/env python3
"""
Reclassement mood de la bibliothèque à partir des features déjà analysées
(bpm/energy/genre en DB — pas de re-scan audio).
Règles alignées sur analyzer.py (apply_bpm_guard + apply_energy_guard) :
  - genre hard (Hardbass, Rawstyle, Gabber...)          → intense
  - festival/energique + bpm >= 150                      → intense
  - festival/energique + energy >= 0.82 et bpm >= 138    → intense
  - festival/energique/intense + bpm < 112               → melodique
Usage:
  python3 az_reclass.py         — dry-run (montre ce qui changerait)
  python3 az_reclass.py apply   — applique les changements
"""
import sys, os

def _install():
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "psycopg2-binary"], check=True)

try:
    import psycopg2, psycopg2.extras
except ImportError:
    _install()
    import psycopg2, psycopg2.extras

DB_URL = os.environ["DATABASE_URL"]
APPLY  = len(sys.argv) > 1 and sys.argv[1] == "apply"

HARD_GENRES = {
    "Hardstyle", "Hardcore", "Gabber", "Hard Techno", "Industrial",
    "Speedcore", "UK Hardcore", "Frenchcore", "Hardbass", "Hard Bass",
    "Rawstyle", "Uptempo", "Terrorcore", "Hard Dance", "Hard House",
    "Schranz", "Makina", "Donk", "Jump Up", "Happy Hardcore", "Hard Trance",
}


def new_mood(mood, bpm, energy, genre):
    bpm    = float(bpm or 0)
    energy = float(energy or 0)
    if genre in HARD_GENRES:
        return "intense"
    if mood in ("festival", "energique", "intense") and 0 < bpm < 112:
        return "melodique"
    if mood in ("festival", "energique") and bpm >= 150:
        return "intense"
    if mood in ("festival", "energique") and energy >= 0.82 and bpm >= 138:
        return "intense"
    return mood


conn = psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)

with conn.cursor() as cur:
    cur.execute("""
        SELECT id, title, artist, mood, bpm, energy, genre_top1
        FROM tracks WHERE analyzed = TRUE
    """)
    rows = cur.fetchall()

changes = []
for r in rows:
    nm = new_mood(r["mood"], r["bpm"], r["energy"], r["genre_top1"] or "")
    if nm != r["mood"]:
        changes.append((r["id"], r["mood"], nm, r))

print(f"Bibliotheque analysee : {len(rows)} tracks")
print(f"Reclassements proposes : {len(changes)}\n")

from collections import Counter
moves = Counter(f"{old} -> {new}" for _, old, new, _ in changes)
for move, n in moves.most_common():
    print(f"  {n:4d}  {move}")

print("\nExemples :")
for _, old, nm, r in changes[:20]:
    print(f"  [{old} -> {nm}] {r['artist']} - {r['title']}"
          f"  (bpm={round(float(r['bpm'] or 0))} e={round(float(r['energy'] or 0), 2)}"
          f" g={r['genre_top1']})")

if not APPLY:
    print("\nDRY-RUN — rien modifie. Relancer avec 'apply' pour ecrire.")
    conn.close()
    sys.exit(0)

with conn.cursor() as cur:
    for tid, _, nm, _ in changes:
        cur.execute("UPDATE tracks SET mood=%s WHERE id=%s", (nm, tid))
conn.commit()
conn.close()
print(f"\nAPPLIQUE : {len(changes)} tracks reclassees.")
print("Le scheduler regenerera la playlist au prochain cycle (3 min).")
