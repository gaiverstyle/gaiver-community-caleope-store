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

OK, WARN, FAIL = "OK", "WARN", "FAIL"
results = []  # (name, status, detail)


def add(name, status, detail):
    results.append((name, status, detail))


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
        add("rotation_vivante", st, f"dernier passage il y a {age:.0f} min")
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

    # 4. Clips vidéo / déchet revenus dans le pool jour
    try:
        n = q1(cur, "SELECT count(*) FROM tracks WHERE analyzed AND mood = ANY(%s) AND title ~* %s",
               (list(DAY_MOODS), CLIP_RE))
        add("clips_dechet", OK if n == 0 else WARN, f"{n} titre(s) clip/déchet dans le pool jour")
    except Exception as e:
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

    # 6. Titres tronqués (<90s) diffusés récemment (previews/rips coupés)
    try:
        n = q1(cur, "SELECT count(distinct t.id) FROM play_history ph JOIN tracks t ON t.id=ph.track_id "
                    "WHERE ph.played_at > now()-interval '4 hours' AND t.duration < 90")
        add("titres_tronques", OK if n == 0 else WARN, f"{n} titre(s) <90s diffusé(s) sur 4h")
    except Exception as e:
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

    worst = FAIL if any(s == FAIL for _, s, _ in results) else \
            WARN if any(s == WARN for _, s, _ in results) else OK
    icon = {OK: "✅", WARN: "⚠️", FAIL: "🔴"}
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"[{ts}] QC Gaiverland — {icon[worst]} {worst}"]
    for name, st, detail in results:
        lines.append(f"  {icon[st]} {name}: {detail}")
    report = "\n".join(lines)
    print(report, flush=True)

    # Alerte Discord si anomalie ET webhook configuré, AVEC throttle (anti-spam) :
    # on n'alerte qu'au CHANGEMENT d'état ou au max 1×/heure pour un même niveau.
    if worst != OK and WEBHOOK:
        state_path = "/tmp/qc_state.json"
        try:
            prev = json.load(open(state_path))
        except Exception:
            prev = {}
        now_ts = datetime.datetime.now().timestamp()
        changed = prev.get("worst") != worst
        stale = (now_ts - float(prev.get("alert_ts", 0))) > 3600
        if changed or stale:
            anomalies = [f"{icon[st]} **{name}** — {detail}" for name, st, detail in results if st != OK]
            body = {"content": f"{icon[worst]} **QC Gaiverland {worst}** ({ts})\n" + "\n".join(anomalies)}
            try:
                req = urllib.request.Request(WEBHOOK, data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=8).read()
                prev = {"worst": worst, "alert_ts": now_ts}
            except Exception as e:
                print(f"  ⚠ alerte Discord échouée: {e}", flush=True)
        try:
            json.dump({"worst": worst, "alert_ts": prev.get("alert_ts", 0)}, open(state_path, "w"))
        except Exception:
            pass

    sys.exit(0 if worst == OK else 1 if worst == WARN else 2)


if __name__ == "__main__":
    main()
