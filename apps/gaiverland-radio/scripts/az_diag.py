#!/usr/bin/env python3
"""
Diagnostic playlist + skip track courant.
Usage:
  python3 az_diag.py           — diagnostic uniquement
  python3 az_diag.py skip      — skip + diagnostic
  python3 az_diag.py fix       — corrige les tracks NULL-genre dans la playlist
"""
import sys, os, httpx, json

AZ_URL     = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY     = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))
DB_URL     = os.environ.get("DATABASE_URL", "")
MODE       = sys.argv[1] if len(sys.argv) > 1 else "diag"

az_headers = {"X-API-Key": AZ_KEY, "Content-Type": "application/json"}

# ── DB ──────────────────────────────────────────────────────────────────────

def get_conn():
    import psycopg2, psycopg2.extras
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


# ── SKIP ────────────────────────────────────────────────────────────────────

if MODE == "skip":
    r = httpx.post(f"{AZ_URL}/api/station/{AZ_STATION}/backend/skip",
                   headers=az_headers, timeout=5)
    if r.status_code in (200, 204):
        print("SKIP OK")
    else:
        print(f"SKIP HTTP {r.status_code}: {r.text[:200]}")


# ── DIAGNOSTIC ──────────────────────────────────────────────────────────────

print("\n=== NOW PLAYING ===")
try:
    r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=5)
    if r.status_code == 200:
        np = r.json()
        song = np.get("now_playing", {}).get("song", {})
        pl   = np.get("now_playing", {}).get("playlist", "?")
        print(f"  {song.get('artist','?')} — {song.get('title','?')}")
        print(f"  playlist active: {pl}")
except Exception as e:
    print(f"  AZ error: {e}")

if not DB_URL:
    print("\nNo DATABASE_URL — DB checks skipped")
    sys.exit(0)

conn = get_conn()

print("\n=== BAR BREEZE / TRACKS HORS-THEME ===")
with conn.cursor() as cur:
    cur.execute("""
        SELECT id, title, artist, genre_top1, mood, analyzed, az_playlist_assigned, gaiverland_score
        FROM tracks
        WHERE LOWER(title) LIKE '%breeze%' OR LOWER(title) LIKE '%bar breeze%'
        LIMIT 10
    """)
    rows = cur.fetchall()
for r in rows:
    print(f"  [{r['id']}] {r['artist']} — {r['title']}")
    print(f"       genre={r['genre_top1']}  mood={r['mood']}  analyzed={r['analyzed']}"
          f"  in_playlist={r['az_playlist_assigned']}  score={r['gaiverland_score']}")

print("\n=== TRACKS NULL-GENRE DANS LA PLAYLIST ===")
with conn.cursor() as cur:
    cur.execute("""
        SELECT id, title, artist, genre_top1, mood, analyzed
        FROM tracks
        WHERE az_playlist_assigned = true AND genre_top1 IS NULL
        ORDER BY title LIMIT 30
    """)
    rows = cur.fetchall()
print(f"  {len(rows)} tracks NULL-genre actuellement dans 'Gaiverland IA':")
for r in rows:
    print(f"  [{r['id']}] {r['artist']} — {r['title']}  mood={r['mood']}  analyzed={r['analyzed']}")

print("\n=== STATS LIBRARY ===")
with conn.cursor() as cur:
    cur.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN analyzed THEN 1 END) as analyzed,
            SUM(CASE WHEN analyzed AND genre_top1 IS NULL THEN 1 END) as analyzed_null_genre,
            SUM(CASE WHEN az_playlist_assigned THEN 1 END) as in_playlist,
            SUM(CASE WHEN az_playlist_assigned AND genre_top1 IS NULL THEN 1 END) as playlist_null_genre
        FROM tracks
    """)
    s = cur.fetchone()
print(f"  total={s['total']}  analyzed={s['analyzed']}  analyzed+NULL_genre={s['analyzed_null_genre']}")
print(f"  in_playlist={s['in_playlist']}  in_playlist+NULL_genre={s['playlist_null_genre']}")

print("\n=== PLAYLISTS AZURACAST ===")
try:
    r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/playlists",
                  headers=az_headers, timeout=10)
    if r.status_code == 200:
        for pl in r.json():
            enabled = "ON" if pl.get("is_enabled") else "off"
            print(f"  [{enabled}] {pl.get('name')} (id={pl.get('id')})"
                  f"  type={pl.get('type')}  weight={pl.get('weight')}")
except Exception as e:
    print(f"  AZ playlists error: {e}")

# ── FIX ─────────────────────────────────────────────────────────────────────

if MODE == "fix":
    print("\n=== FIX: retirer les NULL-genre de la playlist ===")
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE tracks SET az_playlist_assigned = false
            WHERE az_playlist_assigned = true AND genre_top1 IS NULL
        """)
        n = cur.rowcount
    conn.commit()
    print(f"  {n} tracks NULL-genre marquées az_playlist_assigned=false")
    print("  Le scheduler les exclura au prochain cycle (3 min)")

conn.close()
print("\nDone.")
