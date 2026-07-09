"""
Scheduler Gaiverland — orchestre playlist + Rebexis + TTS.
Gère les playlists AzuraCast 0.23.4 directement via API.

Stratégie playlist :
  - "Gaiverland IA" (default, weight=3) : 20-30 titres mood-appropriés, mis à jour toutes les 3 min
  - "Rebexis"       (once_per_x_songs)  : jingles de Rebexis générés à l'avance
"""
import os, sys, subprocess, time, datetime, unicodedata
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
                      set_playlist_order, get_station, now_playing, get_queue, update_playlist,
                      _get_all_files)

DB_URL       = os.environ["DATABASE_URL"]
PLAYLIST_URL = "http://gaiverland-playlist:8080"
REBEXIS_URL  = "http://gaiverland-rebexis:8081"
TTS_URL      = "http://gaiverland-tts:8082"
CYCLE_SEC    = 180  # 3 minutes
AZ_KEY       = os.environ.get("AZURACAST_API_KEY", "")
PROPOSAL_INTERVAL_S = int(os.environ.get("PROPOSAL_CHECK_INTERVAL_S", str(6 * 3600)))  # 6h
_last_proposal_check = 0.0

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

    # get_or_create_playlist ne reconfigure PAS une playlist "Rebexis" déjà
    # existante (il renvoie son ID tel quel). Si elle a été créée un jour en
    # rotation générale, ses jingles jouent en série -> plusieurs voix d'affilée.
    # On ré-applique donc le type à chaque démarrage pour garantir 1 jingle/X titres.
    if rb_id:
        update_playlist(rb_id, type="once_per_x_songs",
                        play_per_songs=REBEXIS_SONGS_INTERVAL,
                        weight=1, is_enabled=True)

    # Lecture séquentielle pour la playlist musicale : l'ordre harmonique calculé
    # par le moteur doit être respecté à l'antenne, pas re-mélangé par AzuraCast.
    if gw_id:
        update_playlist(gw_id, order="sequential")

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
            # Imposer l'ordre d'enchaînement calculé (sinon AzuraCast rejoue dans
            # son ordre interne et la cohérence harmonique est perdue).
            if set_playlist_order(gw_playlist_id, az_ids):
                print(f"  🎚 Ordre d'enchaînement appliqué ({len(az_ids)} titres)")
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


_recorded_sh: set = set()  # sh_id AzuraCast déjà enregistrés (dédup)


def _norm(s: str) -> str:
    """Normalise pour un matching robuste : retire accents (NFKD), ne garde que
    l'alphanumérique, minuscules. Indispensable car les titres accentués sont
    parfois corrompus en base ('Dernière' -> 'Dernie?re') → le match exact
    échouait → le morceau n'était pas enregistré → il repassait en boucle."""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return "".join(c for c in s.lower() if c.isalnum())


def record_plays():
    """Enregistre dans play_history les morceaux réellement diffusés (source de
    vérité = historique AzuraCast). Sans ça, l'anti-répétition du moteur de
    playlist n'a rien à exclure → les titres se répètent en journée.
    Dédup par sh_id ; matching titre/artiste NORMALISÉ (robuste accents/encodage).
    Tout est encapsulé : une erreur DB/réseau ne doit jamais tuer la boucle."""
    try:
        np = now_playing()
        if not np:
            return
        entries = []
        if np.get("now_playing"):
            entries.append(np["now_playing"])
        entries += (np.get("song_history") or [])
        pending = [(e.get("sh_id"), e.get("song") or {}) for e in entries
                   if e.get("sh_id") and e.get("sh_id") not in _recorded_sh]
        if not pending:
            return

        recorded = 0
        conn = get_conn()
        try:
            # Index : exact (titre,artiste) + par artiste pour le prefix-match
            by_ta = {}          # (nt, na) -> id
            by_artist = {}      # na -> [(nt, id)]
            with conn.cursor() as cur:
                cur.execute("SELECT id, title, artist FROM tracks WHERE analyzed=TRUE")
                for r in cur.fetchall():
                    nt = _norm(r["title"])
                    if not nt:
                        continue
                    na = _norm(r.get("artist") or "")
                    by_ta[(nt, na)] = r["id"]
                    by_artist.setdefault(na, []).append((nt, r["id"]))

            def _match(nt, na):
                if (nt, na) in by_ta:                  # match exact
                    return by_ta[(nt, na)]
                best = None                            # sinon préfixe (même artiste) :
                for cnt, cid in by_artist.get(na, []): # gère les suffixes AzuraCast
                    if len(cnt) < 8 or len(nt) < 8:    # (feat., [Clean], (Bass Boosted)…)
                        continue                       # trop court = risque de faux positif
                    if nt.startswith(cnt) or cnt.startswith(nt):
                        if best is None or len(cnt) > best[1]:
                            best = (cid, len(cnt))
                return best[0] if best else None

            with conn.cursor() as cur:
                for sh_id, song in pending:
                    nt = _norm(song.get("title") or "")
                    if nt:
                        tid = _match(nt, _norm(song.get("artist") or ""))
                        if tid:
                            cur.execute("INSERT INTO play_history (track_id) VALUES (%s)", (tid,))
                            recorded += 1
                            # Relie le titre au hash AzuraCast (song.id) → clé des votes.
                            # C'est ce chaînon qui permet à l'effet des votes de viser
                            # le bon track dans le moteur de rotation.
                            sid = song.get("id") or ""
                            if sid:
                                cur.execute(
                                    "UPDATE tracks SET song_id=%s WHERE id=%s "
                                    "AND (song_id IS NULL OR song_id<>%s)",
                                    (sid, tid, sid))
                    _recorded_sh.add(sh_id)
            conn.commit()
        finally:
            conn.close()

        if len(_recorded_sh) > 2000:      # borne mémoire
            _recorded_sh.clear()
        if recorded:
            print(f"  📝 {recorded} lecture(s) enregistrée(s) dans play_history")
    except Exception as e:
        print(f"  ⚠ record_plays: {e}")


def maybe_validate_proposals():
    """Classe périodiquement les titres proposés par la communauté (accept/reject
    par genre, via proposal_validator.py). Résultats dans proposal_decisions, à
    disposition de Régis. Ne télécharge rien : l'import musical reste sa main."""
    global _last_proposal_check
    now = time.time()
    if now - _last_proposal_check < PROPOSAL_INTERVAL_S:
        return
    _last_proposal_check = now
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proposal_validator.py")
    if not os.path.exists(script):
        return
    try:
        print("  🔎 validation des propositions communauté…")
        subprocess.run([sys.executable, script], timeout=300, check=False)
    except Exception as e:
        print(f"  ⚠ validation propositions : {e}")


def backfill_song_ids():
    """Backfill one-shot du song_id (hash AzuraCast) sur toute la librairie depuis
    l'API fichiers, en matchant titre/artiste normalisés. Rend l'effet des votes
    opérationnel IMMÉDIATEMENT après un (re)déploiement, sans attendre que chaque
    titre rejoue (record_plays entretient ensuite au fil de l'eau). No-op si la
    couverture est déjà bonne."""
    try:
        conn = get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) AS n, count(song_id) AS s FROM tracks WHERE analyzed=TRUE")
                row = cur.fetchone(); n = row["n"] or 0; s = row["s"] or 0
            if n == 0 or (s / n) >= 0.5:
                return  # déjà couvert → on ne rappelle pas l'API à chaque boot
            files = _get_all_files()
            if not files:
                return
            by_ta, by_art = {}, {}
            with conn.cursor() as cur:
                cur.execute("SELECT id, title, artist FROM tracks WHERE analyzed=TRUE")
                for r in cur.fetchall():
                    nt = _norm(r["title"])
                    if not nt:
                        continue
                    na = _norm(r.get("artist") or "")
                    by_ta[(nt, na)] = r["id"]
                    by_art.setdefault(na, []).append((nt, r["id"]))

            def _match(nt, na):
                if (nt, na) in by_ta:
                    return by_ta[(nt, na)]
                best = None
                for cnt, cid in by_art.get(na, []):
                    if len(cnt) < 8 or len(nt) < 8:
                        continue
                    if nt.startswith(cnt) or cnt.startswith(nt):
                        if best is None or len(cnt) > best[1]:
                            best = (cid, len(cnt))
                return best[0] if best else None

            upd = 0
            with conn.cursor() as cur:
                for f in files:
                    sid = f.get("song_id")
                    if not sid:
                        continue
                    tid = _match(_norm(f.get("title") or ""), _norm(f.get("artist") or ""))
                    if tid:
                        cur.execute(
                            "UPDATE tracks SET song_id=%s WHERE id=%s "
                            "AND (song_id IS NULL OR song_id<>%s)", (sid, tid, sid))
                        upd += cur.rowcount
            conn.commit()
            if upd:
                print(f"  🔗 backfill song_id : {upd} titre(s) reliés (effet votes prêt)")
        finally:
            conn.close()
    except Exception as e:
        print(f"  ⚠ backfill song_id: {e}")


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

    # Beatmatch niveau 2 : câbler le fondu « smart » de Liquidsoap (idempotent).
    # L'ordre par BPM proche est fait par playlist.py (Régis) ; ici on active le
    # crossfade intelligent qui beatmatche les tempos rapprochés. CPU négligeable.
    if station:
        try:
            from az_crossfade import ensure_smart_crossfade
            ensure_smart_crossfade()
        except Exception as e:
            print(f"  ⚠ crossfade non appliqué : {e}")

    conn = get_conn()
    # Colonne song_id (hash AzuraCast) : chaînon tracks ↔ votes pour l'effet des
    # votes du moteur de rotation. Idempotent — no-op si déjà présente.
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS song_id VARCHAR(64)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tracks_song_id ON tracks(song_id)")
        conn.commit()
    except Exception as e:
        print(f"  ⚠ migration song_id: {e}")
        conn.rollback()
    backfill_song_ids()  # rend l'effet des votes opérationnel dès le boot
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

        # 3b. Enregistrer les morceaux diffusés (alimente l'anti-répétition)
        record_plays()

        # 4. Mettre à jour la playlist Gaiverland IA dans AzuraCast
        if gw_id:
            conn = get_conn()
            update_gaiverland_playlist(conn, gw_id)

        # 5. Classer les propositions de titres de la communauté (toutes les 6 h)
        maybe_validate_proposals()

        time.sleep(CYCLE_SEC)


if __name__ == "__main__":
    main()
