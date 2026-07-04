"""
Scheduler Gaiverland — orchestre playlist + Rebexis + TTS.
Gère les playlists AzuraCast 0.23.4 directement via API.

Stratégie playlist :
  - "Gaiverland IA" (default, weight=3) : 20-30 titres mood-appropriés, mis à jour toutes les 3 min
  - "Rebexis"       (once_per_x_songs)  : jingles de Rebexis générés à l'avance
"""
import os, sys, subprocess, time, datetime
from zoneinfo import ZoneInfo
LOCAL_TZ = ZoneInfo("Europe/Paris")

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "httpx", "psycopg2-binary", "tzdata"], check=True)

try:
    import httpx, psycopg2
except ImportError:
    install_deps()
    import httpx, psycopg2

import psycopg2.extras
import sys
sys.path.insert(0, "/app")
from az_utils import (get_or_create_playlist, batch_assign_playlist, replace_playlist,
                      set_playlist_order, get_station, now_playing, get_queue, update_playlist)

DB_URL       = os.environ["DATABASE_URL"]
PLAYLIST_URL = "http://gaiverland-playlist:8080"
REBEXIS_URL  = "http://gaiverland-rebexis:8081"
TTS_URL      = "http://gaiverland-tts:8082"
CYCLE_SEC    = 180  # 3 minutes
AZ_KEY       = os.environ.get("AZURACAST_API_KEY", "")

# Intervalle Rebexis (en nombre de morceaux entre chaque jingle)
REBEXIS_SONGS_INTERVAL = 3
# Timer indépendant pour les phrases lore (en secondes)
LORE_INTERVAL_S = 45 * 60  # 1 phrase lore toutes les 45 min

# Créneaux horaires : jour = EDM/energique, nuit = nocturne
NIGHT_START = (22, 0)  # 22h00
NIGHT_END   = (7, 0)   # 7h00


_lore_last_time: float = 0.0
_current_slot: str = ""


def get_time_slot() -> str:
    now = datetime.datetime.now(LOCAL_TZ)
    h, m = now.hour, now.minute
    if h >= NIGHT_START[0] or (h, m) < NIGHT_END:
        return "night"
    return "day"


def maybe_update_slot_mood(gw_id: int, wd_id: int = 0, fr_id: int = 0):
    """Bascule mood selon le créneau : jour=energique, nuit=nocturne."""
    global _current_slot
    slot = get_time_slot()
    if slot == _current_slot:
        return
    _current_slot = slot
    try:
        target_mood = "intense" if slot == "night" else "festival"
        httpx.post(f"{PLAYLIST_URL}/state/mood", params={"mood": target_mood}, timeout=5)
        icon = "nuit" if slot == "night" else "soleil"
        print(f"  {icon} Créneau {slot} -> mood = {target_mood}")
    except Exception as e:
        print(f"  warning Slot mood: {e}")


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def wait_for(url: str, name: str, retries: int = 30):
    for i in range(retries):
        try:
            if httpx.get(f"{url}/health", timeout=5).status_code == 200:
                print(f"  ✓ {name} prêt")
                return True
        except Exception:
            pass
        print(f"  ⏳ {name}... ({i+1}/{retries})")
        time.sleep(10)
    print(f"  ⚠ {name} non disponible")
    return False


def setup_playlists(conn):
    """Crée ou retrouve les playlists Gaiverland dans AzuraCast."""
    print("  → Vérification playlists AzuraCast...")

    gw_id = get_or_create_playlist("Gaiverland IA", pl_type="default", weight=3)
    rb_id = get_or_create_playlist("Rebexis",
                                    pl_type="once_per_x_songs",
                                    weight=1,
                                    play_per_songs=REBEXIS_SONGS_INTERVAL)

    if gw_id or rb_id:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE radio_state
                SET az_gw_playlist=COALESCE(%s, az_gw_playlist),
                    az_rb_playlist=COALESCE(%s, az_rb_playlist),
                    updated_at=NOW()
                WHERE id=1
            """, (gw_id, rb_id))
        conn.commit()
        print(f"  ✓ Playlist 'Gaiverland IA' : ID {gw_id}")
        print(f"  ✓ Playlist 'Rebexis'       : ID {rb_id} (1 jingle/{REBEXIS_SONGS_INTERVAL} morceaux)")
    else:
        print("  ⚠ Playlists AzuraCast non configurées (clé API manquante ?)")

    # Playlists secondaires désactivées
    for pl_name in ("Travail Decouverte", "Bien Francais"):
        pl_id = get_or_create_playlist(pl_name, pl_type="default", weight=2)
        if pl_id:
            update_playlist(pl_id, is_enabled=False)

    return gw_id, rb_id, 0, 0


def update_gaiverland_playlist(conn, gw_playlist_id: int):
    """Met à jour la playlist 'Gaiverland IA' avec les titres mood-appropriés."""
    if not gw_playlist_id:
        return

    try:
        resp = httpx.get(f"{PLAYLIST_URL}/playlist/next", params={"count": 25}, timeout=15)
        data = resp.json()
        tracks = data.get("tracks", [])
        mood   = data.get("mood", "?")

        az_ids = [t["az_id"] for t in tracks if t.get("az_id")]
        if not az_ids:
            print(f"  ℹ Playlist [{mood}] — aucun az_id disponible (analyzer pas encore synchro ?)")
            return

        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT az_id FROM tracks WHERE az_playlist_assigned=true AND az_id IS NOT NULL")
            prev_az_ids = [row["az_id"] for row in cur.fetchall()]

        ok = replace_playlist(az_ids, gw_playlist_id, prev_az_ids=prev_az_ids)

        if ok:
            conn = get_conn()
            with conn.cursor() as cur:
                cur.execute("UPDATE tracks SET az_playlist_assigned=false WHERE az_playlist_assigned=true")
                if az_ids:
                    cur.execute("UPDATE tracks SET az_playlist_assigned=true WHERE az_id = ANY(%s)", (az_ids,))
            conn.commit()
            print(f"  📻 Playlist [{mood}] mise à jour — {len(az_ids)} titres")
        else:
            print(f"  ⚠ Mise à jour playlist échouée")
    except Exception as e:
        print(f"  ⚠ update_playlist: {e}")


def process_tts_queue():
    """Convertit en audio les interventions Rebexis en attente."""
    try:
        pending = httpx.get(f"{TTS_URL}/pending", timeout=10).json().get("sessions", [])
        for s in pending[:2]:
            print(f"  → TTS session {s['id']}: {s['intervention'][:50]}…")
            httpx.post(f"{TTS_URL}/synthesize",
                       params={"session_id": s["id"], "text": s["intervention"]},
                       timeout=120)
    except Exception as e:
        print(f"  ⚠ TTS queue: {e}")


def maybe_generate_rebexis():
    """Déclenche la génération d'une intervention Rebexis.

    Les phrases "lore" (identité de Rebexis) ont leur propre timer indépendant
    (LORE_INTERVAL_S). Les phrases mood-appropriées suivent l'interval check du
    moteur Rebexis (INT_MIN/INT_MAX). Les deux timers sont décorrélés pour éviter
    que le lore monopolise toutes les interventions.
    """
    global _lore_last_time
    try:
        now = time.time()
        force_lore = (now - _lore_last_time >= LORE_INTERVAL_S)

        if not force_lore:
            state = httpx.get(f"{PLAYLIST_URL}/state", timeout=5).json()
            mood  = state.get("mood", "energique")
        else:
            mood = "lore"

        # Titre en cours
        np_data = now_playing()
        context = ""
        if np_data:
            song = np_data.get("now_playing", {}).get("song", {})
            context = f"{song.get('artist', '')} — {song.get('title', '')}".strip(" —")

        # Prochain titre musical (skip les jingles Rebexis)
        next_music = ""
        try:
            queue = get_queue(limit=6)
            for entry in queue:
                song = entry.get("song", {})
                title = song.get("title", "")
                artist = song.get("artist", "")
                if title and "rebexis" not in title.lower() and "rebexis" not in artist.lower():
                    next_music = f"{artist} — {title}".strip(" —") if artist else title
                    break
        except Exception:
            pass

        resp = httpx.post(f"{REBEXIS_URL}/generate",
                          params={"mood": mood, "context_track": context,
                                  "next_track": next_music, "force": str(force_lore).lower()},
                          timeout=30)
        data = resp.json()
        if data.get("intervention"):
            if force_lore:
                _lore_last_time = now
            suffix = f" → {next_music[:35]}" if next_music else ""
            print(f"  🎙 Rebexis [{mood}]{suffix} : {data['intervention'][:60]}…")
    except Exception as e:
        print(f"  ⚠ Rebexis: {e}")


def main():
    print("⚙  Scheduler Gaiverland démarré")
    wait_for(PLAYLIST_URL, "Playlist Engine")
    wait_for(REBEXIS_URL,  "Rebexis Engine")
    wait_for(TTS_URL,      "TTS Worker")

    station = get_station()
    if station:
        print(f"  ✓ AzuraCast connecté — station : {station.get('name', '?')}")
    else:
        print("  ⚠ AzuraCast non joignable — scheduler en mode dégradé (Rebexis + TTS actifs)")

    conn = get_conn()
    gw_id, rb_id, wd_id, fr_id = setup_playlists(conn)

    print("\n✅ Boucle principale active.\n")
    cycle = 0
    while True:
        cycle += 1
        print(f"\n── Cycle #{cycle} ─────────────────────────────────")

        # 1. Traitement TTS en attente (priorité : audio prêt avant diffusion)
        process_tts_queue()

        # 2. Générer Rebexis si intervalle atteint (lore timer indépendant)
        maybe_generate_rebexis()

        # 3. Vérifier et switcher le créneau horaire (travail/standard)
        maybe_update_slot_mood(gw_id, wd_id, fr_id)

        # 4. Mettre à jour la playlist Gaiverland IA dans AzuraCast
        if gw_id:
            conn = get_conn()
            update_gaiverland_playlist(conn, gw_id)

        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    main()
