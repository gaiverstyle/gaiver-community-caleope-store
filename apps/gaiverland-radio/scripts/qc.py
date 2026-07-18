#!/usr/bin/env python3
"""
Contrôle qualité Gaiverland — vérifie l'EXPÉRIENCE D'ÉCOUTE (pas juste « est-ce up »,
ça c'est le watchdog infra). Pensé pour tourner en cron régulier (ex. /20 min) et alerter
si « ça part en cacahuète » — surtout utile quand la radio a de l'audience.

Chaque contrôle → OK / WARN / FAIL + détail. Sortie lisible + code retour (0 OK, 1 WARN, 2 FAIL).
Alerte Discord si QC_DISCORD_WEBHOOK est défini ET au moins un WARN/FAIL.

Lancer : docker exec gaiverland-scheduler python3 /app/qc.py
"""
import os, sys, json, urllib.request, datetime

import psycopg2, psycopg2.extras

DB = os.environ["DATABASE_URL"]
AZ = os.environ.get("AZURACAST_URL", "http://azuracast")
WEBHOOK = os.environ.get("QC_DISCORD_WEBHOOK", "").strip()
DAY_MOODS = ("festival", "energique", "melodique")
SCENE_RE = r"(/music/(chill|phonk|synthwave|hard|lofi|lofi2|phonk2)/|/gaiverland_[a-z0-9]+/media/)"
CLIP_RE = r"(official music video|official video|music video|clip officiel|\[hd\]|\(hd\)|official mv|type beat|album teaser)"
HARD_TITLE_RE = r"(hardstyle|rawstyle|frenchcore|uptempo|tekno|hardcore|gabber|sub ?zero)"
SONG_KEY = "lower(regexp_replace(coalesce(artist,'')||' '||coalesce(title,''),'[^a-z0-9]','','g'))"

OK, FIXED, WARN, FAIL = "OK", "FIXÉ", "WARN", "FAIL"
results = []  # (name, status, detail)
_flags = {"needs_restart": False}   # demandé au wrapper hôte (redémarrage docker)


def add(name, status, detail):
    results.append((name, status, detail))


def _quarantine(cur, pred_sql, pred_params, limit=300):
    """Met en quarantaine (station_denylist mainstage) les titres jour ciblés — NON destructif,
    RÉVERSIBLE (retirer de station_denylist les rétablit). Requiert un song_id. Retourne le nb."""
    cur.execute(f"""
        INSERT INTO station_denylist (station_id, song_id, artist, title, added_by, added_at)
        SELECT 1, t.song_id, t.artist, t.title, 'qc-auto', NOW()
        FROM tracks t
        WHERE {pred_sql} AND t.song_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM station_denylist d
                          WHERE d.station_id = 1 AND d.song_id = t.song_id)
        LIMIT %s
    """, list(pred_params) + [limit])
    return cur.rowcount


def q1(cur, sql, params=None):
    cur.execute(sql, params or ())
    r = cur.fetchone()
    return r[0] if r else None


def check_all():
    conn = psycopg2.connect(DB)
    cur = conn.cursor()
    now = datetime.datetime.now()          # heure Paris (TZ conteneur)
    day = 10 <= now.hour <= 22             # créneau « jour » pour le contrôle mood

    # 1. Cerveau vivant : dernier morceau enregistré récemment
    try:
        age = q1(cur, "SELECT extract(epoch FROM (now()-max(played_at)))/60 FROM play_history")
        age = float(age) if age is not None else 9999
        st = OK if age < 12 else WARN if age < 25 else FAIL
        if st == FAIL:
            _flags["needs_restart"] = True     # le wrapper hôte redémarre scheduler+playlist
        add("rotation_vivante", st,
            f"dernier passage il y a {age:.0f} min" + (" → redémarrage demandé" if st == FAIL else ""))
    except Exception as e:
        add("rotation_vivante", WARN, f"erreur: {e}")

    # 2. Flux en ligne (AzuraCast) + un vrai titre en cours
    try:
        d = json.load(urllib.request.urlopen(f"{AZ}/api/nowplaying/1", timeout=8))
        d = d[0] if isinstance(d, list) else d
        online = d.get("is_online")
        txt = (d.get("now_playing", {}).get("song", {}) or {}).get("text", "")
        st = OK if (online and txt) else FAIL
        add("flux_en_ligne", st, f"online={online} | {txt[:50]}")
    except Exception as e:
        add("flux_en_ligne", WARN, f"nowplaying illisible: {e}")

    # 3. Répétitions (4h) : diversité distinct/plays. Un set DJ réduit la diversité
    #    VOLONTAIREMENT (1 artiste) → on ne flague pas si un set a tourné dans la fenêtre.
    try:
        try:
            set_recent = q1(cur, "SELECT count(*) FROM dj_set WHERE ends_at > now()-interval '4 hours'") or 0
        except Exception:
            conn.rollback(); set_recent = 0
        cur.execute("SELECT count(*), count(distinct track_id) FROM play_history "
                    "WHERE played_at > now()-interval '4 hours'")
        plays, distinct = cur.fetchone()
        if not plays or plays < 12:
            add("repetitions", OK, f"trop peu de passages ({plays}) pour juger")
        elif set_recent:
            add("repetitions", OK, f"set DJ récent → diversité réduite normale ({distinct}/{plays})")
        else:
            ratio = distinct / plays
            st = OK if ratio >= 0.70 else WARN if ratio >= 0.50 else FAIL
            add("repetitions", st, f"{distinct}/{plays} distincts sur 4h ({ratio:.0%})")
    except Exception as e:
        add("repetitions", WARN, f"erreur: {e}")

    # 4. Clips vidéo / déchet revenus dans le pool jour → AUTO-QUARANTAINE (réversible)
    try:
        pred = "t.analyzed AND t.mood = ANY(%s) AND t.title ~* %s"
        n = q1(cur, f"SELECT count(*) FROM tracks t WHERE {pred} AND t.song_id IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM station_denylist d WHERE d.station_id=1 AND d.song_id=t.song_id)",
               (list(DAY_MOODS), CLIP_RE))
        if n == 0:
            add("clips_dechet", OK, "0 clip/déchet actif dans le pool jour")
        else:
            qn = _quarantine(cur, pred, (list(DAY_MOODS), CLIP_RE))
            conn.commit()
            add("clips_dechet", FIXED if qn else WARN, f"{n} clip/déchet → {qn} mis en quarantaine")
    except Exception as e:
        conn.rollback()
        add("clips_dechet", WARN, f"erreur: {e}")

    # 5. Doublons (copies en trop) dans le pool jour
    try:
        n = q1(cur, f"WITH n AS (SELECT {SONG_KEY} k FROM tracks WHERE analyzed AND mood = ANY(%s)) "
                    "SELECT coalesce(sum(c-1),0) FROM (SELECT k,count(*) c FROM n GROUP BY k HAVING count(*)>1) d",
               (list(DAY_MOODS),))
        n = int(n or 0)
        add("doublons", OK if n <= 25 else WARN, f"{n} copies en trop dans le pool jour")
    except Exception as e:
        add("doublons", WARN, f"erreur: {e}")

    # 6. Titres tronqués (<90s) encore actifs dans le pool jour → AUTO-QUARANTAINE
    try:
        pred = "t.analyzed AND t.mood = ANY(%s) AND t.duration < 90"
        n = q1(cur, f"SELECT count(*) FROM tracks t WHERE {pred} AND t.song_id IS NOT NULL AND NOT EXISTS "
                    "(SELECT 1 FROM station_denylist d WHERE d.station_id=1 AND d.song_id=t.song_id)",
               (list(DAY_MOODS),))
        if n == 0:
            add("titres_tronques", OK, "aucun titre <90s actif dans le pool jour")
        else:
            qn = _quarantine(cur, pred, (list(DAY_MOODS),))
            conn.commit()
            add("titres_tronques", FIXED if qn else WARN, f"{n} titre(s) <90s → {qn} mis en quarantaine")
    except Exception as e:
        conn.rollback()
        add("titres_tronques", WARN, f"erreur: {e}")

    # 7. Musique dure / nuit en pleine journée (le chef tolère un peu, pas beaucoup)
    try:
        cur.execute("SELECT count(*) FROM (SELECT t.mood, t.title FROM play_history ph "
                    "JOIN tracks t ON t.id=ph.track_id ORDER BY ph.played_at DESC LIMIT 12) x "
                    "WHERE mood IN ('intense','nocturne') OR title ~* %s", (HARD_TITLE_RE,))
        hard = cur.fetchone()[0]
        if day:
            st = OK if hard <= 3 else WARN if hard <= 6 else FAIL
            add("dur_en_journee", st, f"{hard}/12 derniers = dur/nuit (jour)")
        else:
            add("dur_en_journee", OK, f"nuit → mood dur normal ({hard}/12)")
    except Exception as e:
        add("dur_en_journee", WARN, f"erreur: {e}")

    # 8. Taille du pool jour (ne doit pas s'effondrer)
    try:
        n = q1(cur, "SELECT count(*) FROM tracks WHERE analyzed AND mood = ANY(%s)", (list(DAY_MOODS),))
        st = OK if n >= 500 else WARN if n >= 250 else FAIL
        add("taille_pool_jour", st, f"{n} titres jour")
    except Exception as e:
        add("taille_pool_jour", WARN, f"erreur: {e}")

    # 9. Sets DJ : pas d'emballement (auto ~1/3h → max ~3 sur 6h) ni de set géant coincé
    try:
        n6 = q1(cur, "SELECT count(*) FROM dj_set WHERE started_at > now()-interval '6 hours'") or 0
        stuck = q1(cur, "SELECT count(*) FROM dj_set WHERE ends_at > now()+interval '130 minutes'") or 0
        st = OK if (n6 <= 3 and stuck == 0) else WARN
        add("sets_dj", st, f"{n6} set(s) sur 6h, {stuck} anormalement long")
    except Exception:
        add("sets_dj", OK, "table dj_set absente (aucun set encore)")

    # 10. Disque (best-effort : visible seulement si le média est monté)
    try:
        path = "/var/azuracast/stations/gaiverlandradio/media"
        path = path if os.path.isdir(path) else "/"
        s = os.statvfs(path)
        used = 100 * (1 - s.f_bavail / s.f_blocks)
        st = OK if used < 85 else WARN if used < 92 else FAIL
        add("disque", st, f"{used:.0f}% utilisé ({path})")
    except Exception as e:
        add("disque", OK, f"non mesurable ici ({e})")

    cur.close()
    conn.close()


def main():
    try:
        check_all()
    except Exception as e:
        add("qc_lui_meme", FAIL, f"le QC a planté: {e}")

    icon = {OK: "✅", FIXED: "🔧", WARN: "⚠️", FAIL: "🔴"}
    worst = FAIL if any(s == FAIL for _, s, _ in results) else \
            WARN if any(s == WARN for _, s, _ in results) else \
            FIXED if any(s == FIXED for _, s, _ in results) else OK
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"[{ts}] QC Gaiverland — {icon[worst]} {worst}"]
    for name, st, detail in results:
        lines.append(f"  {icon[st]} {name}: {detail}")
    print("\n".join(lines), flush=True)

    # Alerte Discord si anomalie/auto-fix ET webhook configuré, throttlée (changement d'état
    # ou 1×/h). Sépare « auto-corrigé » (info) de « à regarder » (demande une action humaine).
    if worst != OK and WEBHOOK:
        state_path = "/tmp/qc_state.json"
        try:
            prev = json.load(open(state_path))
        except Exception:
            prev = {}
        now_ts = datetime.datetime.now().timestamp()
        if prev.get("worst") != worst or (now_ts - float(prev.get("alert_ts", 0))) > 3600:
            fixed = [f"🔧 **{n}** — {d}" for n, s, d in results if s == FIXED]
            probs = [f"{icon[s]} **{n}** — {d}" for n, s, d in results if s in (WARN, FAIL)]
            content = f"{icon[worst]} **QC Gaiverland {worst}** ({ts})"
            if fixed:
                content += "\n__Auto-corrigé (rien à faire) :__\n" + "\n".join(fixed)
            if probs:
                content += "\n__À regarder :__\n" + "\n".join(probs)
            try:
                req = urllib.request.Request(WEBHOOK, data=json.dumps({"content": content}).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=8).read()
                prev = {"worst": worst, "alert_ts": now_ts}
            except Exception as e:
                print(f"  ⚠ alerte Discord échouée: {e}", flush=True)
        try:
            json.dump({"worst": worst, "alert_ts": prev.get("alert_ts", 0)}, open(state_path, "w"))
        except Exception:
            pass

    # Code retour : 3 = demande de redémarrage au wrapper hôte ; sinon 2 FAIL / 1 WARN|FIXÉ / 0 OK.
    if _flags["needs_restart"]:
        sys.exit(3)
    sys.exit(0 if worst == OK else 2 if worst == FAIL else 1)


if __name__ == "__main__":
    main()
