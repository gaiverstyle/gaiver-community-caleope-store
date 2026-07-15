"""
GCS Lore Service — Phase 6.
Mémoire immuable : events C15, stagiaire, Rebexis, villes.
"""
import os, sys, subprocess, json, threading, random, time

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary"], check=True)

try:
    import fastapi, uvicorn, psycopg2
except ImportError:
    _install()
    import fastapi, uvicorn, psycopg2

import psycopg2.extras
from fastapi import FastAPI, HTTPException
from typing import Optional
import datetime          # jour/nuit du lore (heure locale du festival)

DB_URL = os.environ["DATABASE_URL"]
VALID_TYPES = {"c15_event","stagiaire_event","city_transition",
               "rebexis_intervention","track_milestone","festival_moment"}

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
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lore_type ON lore_events(type)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_lore_time ON lore_events(created_at DESC)")
    conn.commit()
    conn.close()


# ── Générateur de lore (STARTER — catalogue à enrichir par Rebexis) ──────────
# Fait vivre le « Journal du festival » : insère périodiquement un event lié à
# l'état réel (ville). Contenu minimal de démarrage ; le vrai catalogue/ton = Rebexis.
GCS_CITY = os.environ.get("GCS_CITY", "Toulon")
LORE_INTERVAL_S = int(os.environ.get("LORE_GEN_INTERVAL_S", "1500"))  # ~25 min
LORE_STARTER = {
    "c15_event": {
        # ☀️ jour
        "day": [   # ☀️ jour
        "Le c15 a démarré du premier coup aujourd'hui. On a applaudi. Il l'a mérité.",
        "Le c15 est garé de travers derrière la scène. En plein soleil, personne n'ose le déplacer, moi la première.",
        "Quelqu'un a lavé le c15 en douce. Aujourd'hui, une enquête est ouverte.",
        "Le c15 chauffe au soleil et sent le vieux vinyle et l'essence. C'est le parfum officiel du festival, encadrez-le.",
        "Une mouette s'est posée sur le toit du c15. Elle reste pour l'ambiance, comme tout le monde à cette heure.",
        "On a chargé le c15 à ras bord pour la journée. Il tient. Il tient toujours. C'est un poète.",
        "Le c15 a sa place réservée près de la scène, en plein jour. Il l'a prise lui-même.",
        "Le c15 a plus de kilomètres au compteur que nous d'heures de sommeil. Et en plein soleil, ça ne se voit sur personne.",
        "On a proposé une remorque au c15 aujourd'hui. Il a refusé. Dignement.",
        "Le compteur du c15 est bloqué depuis des lustres. Franchement, il a bien raison, surtout un jour pareil.",
        "Le c15 refuse d'avancer sans musique. Sur ce point, en plein jour, on se comprend.",
        "Le c15 a un point de rouille qu'on appelle affectueusement « la mélodie ». En pleine lumière, il brille presque.",
        ],
        "night": [   # 🌙 nuit
        "Le c15 ronronne dans la nuit de {ville}. Il veille.",
        "Phares éteints, le c15 monte la garde derrière la scène. On dort mieux en le sachant là.",
        "La nuit, le c15 grince à chaque coup de vent. On dit qu'il chante en sourdine.",
        "Il paraît que le c15 était déjà là avant le premier morceau. La nuit, on y croit vraiment, et on n'ose pas vérifier.",
        "Le c15 connaît la route par cœur. La nuit, c'est lui qui la garde au chaud pour demain.",
        "Dans la boîte à gants du c15 : trois cafés froids et une setlist qui a de la bouteille. Parfait pour la veille.",
        "Le c15 démarre mieux quand on lui parle gentiment. À voix basse, la nuit, encore mieux.",
        "Une lueur traîne sur le tableau de bord du c15. Il ne dort pas non plus, on est deux.",
        "Le c15 n'a jamais raté un festival. La nuit, il compte les étoiles au-dessus de {ville} à notre place.",
        "On a voulu changer l'autoradio du c15. Il a fait la tête pendant deux nuits.",
        "La nuit, le c15 sent le café froid et la fatigue heureuse. C'est notre odeur d'after à nous.",
        "Quelqu'un a proposé de repeindre le c15. Silence gêné dans le noir, sujet clos pour la nuit.",
        ],
    },
    "stagiaire_event": {
        # ☀️ jour
        "day": [   # ☀️ jour
        "Le stagiaire a disparu en plein jour. La musique, elle, tient bon.",
        "On a envoyé le stagiaire chercher un câble aujourd'hui. On garde espoir, mollement.",
        "Le stagiaire a laissé son gilet sur une chaise en plein soleil. Le gilet est là. Le stagiaire, mystère.",
        "Quelqu'un jure avoir vu le stagiaire près de la buvette. En plein jour. Information non confirmée, comme toujours.",
        "On a crié le nom du stagiaire trois fois en plein jour. Rien. Le classique.",
        "Le stagiaire aurait appris à faire le café. On demande à voir. On demande surtout à goûter, là, maintenant.",
        "On a mis une part de pizza de côté pour le stagiaire aujourd'hui. On va devoir se dévouer.",
        "Bonne nouvelle : le stagiaire n'a rien cassé aujourd'hui. Parce qu'il n'est pas là.",
        "On a confié une tâche simple au stagiaire en plein jour. On a bien fait de préciser « simple ».",
        "Le stagiaire a coché « présent » aujourd'hui. Nous, on coche « à confirmer ».",
        "Le badge du stagiaire a été retrouvé sur une table. Le stagiaire qui va avec, toujours pas.",
        "Si vous croisez le stagiaire aujourd'hui, dites-lui qu'on l'aime bien. Et qu'on attend toujours le câble.",
        ],
        "night": [   # 🌙 nuit
        "La nuit tombe sur {ville}, et le stagiaire reste introuvable. La régularité, au moins, il l'a.",
        "Le talkie du stagiaire répond « pschit » dans le noir. C'est déjà plus que d'habitude.",
        "On a laissé un mot au stagiaire avant la nuit. Le mot a disparu. Cohérent.",
        "Le stagiaire a promis de revenir dans cinq minutes. C'était il y a plusieurs heures. Belle promesse.",
        "Quelqu'un a demandé où était le stagiaire à la nuit. Grand moment de solidarité dans le vide.",
        "Le stagiaire est officiellement notre meilleur fantôme. Et la nuit, c'est sa spécialité.",
        "On a gardé une lampe allumée pour le stagiaire. Au cas où. On n'y croit pas trop.",
        "Le stagiaire a un talent rare : être partout où on ne le cherche pas, surtout la nuit.",
        "La nuit, on jure entendre le stagiaire brancher quelque chose quelque part. Tout le monde retient son souffle.",
        "On a rangé une chaise pour le stagiaire près de la régie de nuit. Elle est toujours vide, fidèlement.",
        "Le stagiaire aurait laissé une trace de café encore tiède. Preuve qu'il existe, la nuit s'en contentera.",
        "Dernière nouvelle de la nuit : toujours pas de stagiaire. On l'aime quand même, ce courant d'air.",
        ],
    },
    "festival_moment": {
        # ☀️ jour
        "day": [   # ☀️ jour
        "Le soleil cogne, les basses répondent. Voilà, c'est le genre de journée que je préfère.",
        "Il y a ce moment où tout le monde lève la tête pile en même temps, plein soleil. On y est.",
        "La poussière danse dans la lumière de {ville}. Nous, on appelle ça la déco.",
        "Quelque part dans la foule, quelqu'un vient de trouver son nouveau morceau préféré. De rien.",
        "Les enceintes chauffent, le public aussi. Tout est parfaitement sous contrôle. Ou pas.",
        "Un inconnu vient de se faire trois amis sur un refrain, en plein jour. C'est exactement ça, ici.",
        "Les mains sont en l'air avant même le drop. En plein soleil, on appelle ça de la confiance.",
        "On a monté le son d'un cran, tiens. Juste pour voir. On garde, évidemment.",
        "Le sol vibre, les sourires suivent. Rien de cassé, tout de branché.",
        "On a compté trois éclats de rire pendant l'intro. En plein jour, bon présage.",
        "Il y a une odeur de fête dans l'air chaud et un truc qui va lâcher dans les graves. J'adore.",
        "Quelqu'un danse comme si personne ne regardait. Tout le monde regarde. Tout le monde adore.",
        ],
        "night": [   # 🌙 nuit
        "La nuit s'installe sur {ville}, les basses baissent la voix mais ne se taisent jamais.",
        "On a perdu le fil du temps il y a deux morceaux. En pleine nuit, on ne compte pas le chercher.",
        "Le festival ne dort jamais. Nous non plus, visiblement, et on assume, lueurs aux yeux.",
        "On dirait que la nuit a décidé de rester un peu plus longtemps sur {ville}. Bonne décision.",
        "Il est tard, les projecteurs dessinent des ombres qui dansent mieux que nous. On les laisse faire.",
        "Le silence entre deux morceaux a duré une seconde dans le noir. Personne n'a eu le temps d'avoir peur.",
        "Ce morceau-là, gardez-le quelque part. La nuit de {ville}, elle, s'en souviendra.",
        "La régie veille, les cœurs aussi. Rien à signaler, tout à savourer, en douceur.",
        "Il y a des nuits où tout tombe juste. Devinez quelle nuit on est.",
        "Les lueurs remplacent le soleil, l'énergie reste. On appelle ça l'heure des vrais.",
        "Le festival respire plus doucement, là, maintenant. Restez, la nuit vaut le détour.",
        "Quelqu'un s'endort presque sur un accord et se réveille sur le suivant. Nuit parfaite.",
        ],
    },
}


# ── Jour / nuit ──────────────────────────────────────────────────────────────
# Le générateur piochait dans une liste PLATE : il pouvait sortir « Le c15 a démarré ce
# matin » à 19h34. Les phrases sont désormais rangées ☀️ jour / 🌙 nuit et on pioche
# dans la bonne moitié.
# ⚠️ HEURE LOCALE DU FESTIVAL, pas celle du serveur : le conteneur tourne en UTC, donc
# un simple datetime.now() décalerait la bascule de 2 h l'été (nuit à 22h chez le chef)
# et ferait sonner les phrases à contretemps — c'est justement le bug qu'on corrige.
LORE_TZ          = os.environ.get("LORE_TZ", "Europe/Paris")
LORE_DAY_START   = int(os.environ.get("LORE_DAY_START", "6"))    # 06h → ☀️ (on pense aux lève-tôt)
LORE_NIGHT_START = int(os.environ.get("LORE_NIGHT_START", "20"))  # 20h → 🌙


def _time_of_day() -> str:
    try:
        from zoneinfo import ZoneInfo
        h = datetime.datetime.now(ZoneInfo(LORE_TZ)).hour
    except Exception:
        h = datetime.datetime.now().hour      # repli : mieux que rien
    return "day" if LORE_DAY_START <= h < LORE_NIGHT_START else "night"


# ── Lore réactif (#1) : saluer un titre qui cartonne aux votes ENCORE ─────────
REACTIVE_SALUTE = [
    "Le public a tranché : {artist} — « {title} » repasse, et personne ne râle.",
    "« {title} » de {artist} met tout le monde d'accord. Les ENCORE pleuvent.",
    "On garde {artist} dans la rotation : « {title} » a chauffé la foule pour de bon.",
    "{artist} squatte les cœurs ce soir. « {title} », encore réclamé.",
    "Vous avez voté, on a écouté : « {title} » de {artist} reste à l'affiche.",
    "« {title} » — {artist}. La foule en redemande, la régie s'exécute.",
]


def _current_city(conn) -> str:
    """Ville COURANTE (peut être une mini-scène), partagée via gcs_state. Repli sur GCS_CITY."""
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT city FROM gcs_state WHERE id=1")
            r = cur.fetchone()
            if r and r.get("city"):
                return r["city"]
    except Exception:
        pass
    return GCS_CITY


def _hot_track(conn):
    """Un titre qui cartonne aux ENCORE (score net positif, voté récemment). None sinon."""
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT t.artist, t.title
                           FROM track_scores ts JOIN tracks t ON t.song_id = ts.song_id
                           WHERE ts.score > 0.5 AND ts.last_vote > now() - interval '24 hours'
                             AND t.title IS NOT NULL AND t.title <> ''
                           ORDER BY ts.score DESC, ts.vote_count DESC LIMIT 5""")
            rows = cur.fetchall()
        if rows:
            r = random.choice(rows)
            return (r.get("artist") or "", r.get("title") or "")
    except Exception:
        pass
    return None


def _lore_generator():
    time.sleep(20)  # laisser init_db finir
    last_type = None
    recent = []          # anti-répétition : on ne rejoue pas les dernières phrases
    while True:
        try:
            conn = get_conn(); conn.autocommit = True
            city = _current_city(conn)   # suit la mini-scène en cours

            # ~1 génération sur 4 : saluer un titre plébiscité (lore RÉACTIF aux votes).
            hot = _hot_track(conn) if random.random() < 0.25 else None
            if hot and hot[1]:
                artist, title = hot
                desc  = (random.choice(REACTIVE_SALUTE)
                         .replace("{artist}", artist or "le mix")
                         .replace("{title}", title))
                # festival_moment (et PAS rebexis_intervention) : ce dernier est exclu du
                # journal (gcs_web.events l.267). Le salut aux votes DOIT apparaître au journal.
                etype = "festival_moment"
            else:
                types = [t for t in LORE_STARTER if t != last_type] or list(LORE_STARTER)
                etype = random.choice(types)
                last_type = etype
                tod  = _time_of_day()
                pool = LORE_STARTER[etype].get(tod) or LORE_STARTER[etype].get("day") or []
                fresh = [p for p in pool if p not in recent] or pool
                if not fresh:
                    conn.close(); time.sleep(LORE_INTERVAL_S); continue
                phrase = random.choice(fresh)
                recent.append(phrase)
                if len(recent) > 12:
                    recent.pop(0)
                desc = phrase.replace("{ville}", city).replace("{city}", city)

            with conn.cursor() as cur:
                cur.execute("INSERT INTO lore_events (type,description,city) VALUES (%s,%s,%s)",
                            (etype, desc, city))
            conn.close()
            print(f"  ✎ lore [{etype}] {desc}")
        except Exception as e:
            print(f"  ⚠ lore gen: {e}")
        time.sleep(LORE_INTERVAL_S)


@app.on_event("startup")
def startup():
    init_db()
    threading.Thread(target=_lore_generator, daemon=True).start()


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
    event_type  = body.get("type", "festival_moment")
    description = body.get("description", "").strip()
    city        = body.get("city", "")
    metadata    = body.get("metadata", {})
    if not description:
        raise HTTPException(400, "description required")
    if event_type not in VALID_TYPES:
        event_type = "festival_moment"
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO lore_events (type,description,city,metadata)
            VALUES (%s,%s,%s,%s::jsonb) RETURNING id, created_at
        """, (event_type, description, city, json.dumps(metadata)))
        row = cur.fetchone()
    conn.commit()
    conn.close()
    return {"ok": True, "id": row["id"], "created_at": str(row["created_at"])}


@app.get("/events")
def get_events(type: Optional[str] = None, limit: int = 20):
    conn = get_conn()
    with conn.cursor() as cur:
        if type:
            cur.execute("""SELECT * FROM lore_events WHERE type=%s
                           ORDER BY created_at DESC LIMIT %s""", (type, limit))
        else:
            cur.execute("SELECT * FROM lore_events ORDER BY created_at DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    conn.close()
    return {"events": [dict(r) for r in rows]}


@app.get("/summary")
def summary():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT type, COUNT(*) AS n FROM lore_events GROUP BY type ORDER BY n DESC")
        counts = {r["type"]: r["n"] for r in cur.fetchall()}
        cur.execute("""SELECT description, created_at FROM lore_events
                       WHERE type IN ('c15_event','stagiaire_event')
                       ORDER BY created_at DESC LIMIT 3""")
        recent_lore = [dict(r) for r in cur.fetchall()]
        cur.execute("""SELECT description FROM lore_events
                       WHERE type='rebexis_intervention'
                       ORDER BY created_at DESC LIMIT 5""")
        recent_rebexis = [r["description"] for r in cur.fetchall()]
    conn.close()
    return {
        "event_counts":  counts,
        "recent_lore":   recent_lore,
        "recent_rebexis": recent_rebexis,
        "lore_state": {"c15_status": "active", "stagiaire_status": "unknown",
                       "festival_is_permanent": True},
    }


@app.get("/rebexis-context")
def rebexis_context():
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("""SELECT description FROM lore_events
                       WHERE type='rebexis_intervention'
                       ORDER BY created_at DESC LIMIT 5""")
        recent = [r["description"] for r in cur.fetchall()]
    conn.close()
    return {"recent_interventions": recent}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8096)
