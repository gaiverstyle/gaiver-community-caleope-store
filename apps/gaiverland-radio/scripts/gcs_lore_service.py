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
# Anti-répétition : nb de dernières phrases interdites au tirage (36 dispo par demi-journée).
LORE_RECENT_WINDOW = int(os.environ.get("LORE_RECENT_WINDOW", "26"))


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
    "{artist} squatte les cœurs. « {title} », encore réclamé.",
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


# ── MINI-ARCS narratifs (dispatch Cassy 15/07) ────────────────────────────────
# Un arc = une SÉQUENCE ORDONNÉE d'étapes qui se déroule sur plusieurs heures. Le temps
# est porté par l'ORDRE, jamais par le texte (aucune ancre horaire : contrainte Rebexis).
# Chaque étape doit rester lisible isolée. Ex. : « On a envoyé le stagiaire chercher un
# câble. » → « Toujours pas de stagiaire. Le câble non plus. » → « Le câble est arrivé.
# Sans le stagiaire. »
#
# CONTENU = domaine de Rebexis (`lore-arcs-rebexis.md`). Yann ne câble que la mécanique.
# Tant que le dict est vide, le moteur est un NO-OP total → déployable sans risque.
# Format (rempli à la livraison de Rebexis, parsé depuis son .md) :
#   LORE_ARCS = {"<cle_arc>": {"type": "stagiaire_event", "steps": ["étape 1", "étape 2", ...]}}
LORE_ARCS: dict = {
    "stagiaire_le_cable": {"type": "stagiaire_event", "steps": [
        "On a envoyé le stagiaire chercher un câble. Il a dit « deux minutes ».",
        "Toujours pas de stagiaire. Le câble non plus. On s'habitue.",
        "Le câble est arrivé. Sans le stagiaire. On ne pose plus de questions.",
    ]},
    "stagiaire_le_cafe": {"type": "stagiaire_event", "steps": [
        "Le stagiaire s'est porté volontaire pour le café. Personne ne l'a forcé, c'est ça le plus troublant.",
        "Il y a une odeur de café quelque part. Aucune trace de café. Aucune trace de stagiaire.",
        "On a retrouvé le café. Froid, posé sur une enceinte, avec un mot : « j'arrive ». Touchant.",
    ]},
    "stagiaire_le_badge": {"type": "stagiaire_event", "steps": [
        "Le stagiaire a perdu son badge. Il nous l'annonce fièrement, comme un exploit.",
        "Le badge a été retrouvé. Il servait à caler une table. Ingénieux, on ne peut pas lui enlever ça.",
        "Le stagiaire a un nouveau badge. Il l'a déjà perdu. Le record tient toujours.",
    ]},
    "stagiaire_le_truc_a_verifier": {"type": "stagiaire_event", "steps": [
        "Le stagiaire a dit qu'il allait « juste vérifier un truc ». Personne n'a demandé quoi.",
        "Le truc n'a pas été vérifié. Le stagiaire non plus, d'ailleurs.",
        "Le stagiaire est revenu. Il ne sait plus quel truc. Nous non plus. Dossier classé.",
    ]},
    "stagiaire_le_talkie": {"type": "stagiaire_event", "steps": [
        "On a donné un talkie au stagiaire. Grand moment de confiance collective.",
        "Le talkie grésille tout seul depuis un moment. On préfère croire que c'est lui.",
        "Le talkie est revenu. Sans pile, sans stagiaire, sans explication. Le trio habituel.",
    ]},
    "stagiaire_la_chaise": {"type": "stagiaire_event", "steps": [
        "On a installé une chaise pour le stagiaire, avec son nom dessus. Un vrai geste.",
        "La chaise est vide. Le nom est toujours là. C'est déjà ça de pris.",
        "Quelqu'un s'est assis sur la chaise du stagiaire. Ce n'était pas le stagiaire. Personne n'a bronché.",
    ]},
    "c15_le_demarrage": {"type": "c15_event", "steps": [
        "Le c15 refuse de démarrer. On lui a parlé gentiment, on en est là.",
        "Le c15 a toussé une fois. Petit espoir dans l'équipe, on n'ose rien dire.",
        "Le c15 a démarré. Personne n'a compris pourquoi. On applaudit quand même.",
    ]},
    "c15_le_lavage": {"type": "c15_event", "steps": [
        "Quelqu'un a proposé de laver le c15. Silence dans les rangs.",
        "Le c15 est toujours sale. La proposition, elle, a disparu.",
        "Le c15 a un point de rouille en plus. On appelle ça de la personnalité, maintenant.",
    ]},
    "c15_l_autoradio": {"type": "c15_event", "steps": [
        "L'autoradio du c15 a lâché. On a fait comme si de rien n'était.",
        "Le c15 n'avance plus sans musique. On avait prévenu.",
        "On a remis l'autoradio. Le c15 est reparti. Coïncidence, sûrement.",
    ]},
    "c15_la_place_de_parking": {"type": "c15_event", "steps": [
        "Le c15 s'est garé de travers. On a tracé une place autour de lui, c'était plus simple.",
        "Quelqu'un a voulu redresser le c15. Le c15 a fait un bruit. Le sujet est clos.",
        "La place de travers est devenue officielle. Le c15 avait raison depuis le début.",
    ]},
    "festival_le_cran_de_trop": {"type": "festival_moment", "steps": [
        "On a monté le son d'un cran. Juste pour voir.",
        "Personne ne s'est plaint. On a monté encore un cran.",
        "On en est à trois crans. Toujours aucune plainte. Le public est complice.",
    ]},
    "festival_la_vibration": {"type": "festival_moment", "steps": [
        "Il y a un truc qui vibre dans les graves. On surveille, mollement.",
        "Le truc vibre toujours. On a décidé que c'était voulu.",
        "Le truc a arrêté de vibrer. Ça nous manque déjà. Dossier clos.",
    ]},
    "festival_le_morceau_prefere": {"type": "festival_moment", "steps": [
        "Quelqu'un dans la foule vient de trouver son nouveau morceau préféré. Ça se voit d'ici.",
        "La même personne le redemande. On note. On ne promet rien.",
        "Le morceau est repassé. La personne a disparu dans la foule, heureuse. De rien.",
    ]},
}

ARC_START_P     = float(os.environ.get("LORE_ARC_START_P", "0.25"))   # proba de lancer un arc
ARC_GAP_MIN_H   = float(os.environ.get("LORE_ARC_GAP_MIN_H", "2"))    # espacement entre étapes
ARC_GAP_MAX_H   = float(os.environ.get("LORE_ARC_GAP_MAX_H", "4"))


def _arc_init(conn):
    """État de l'arc en cours, persistant (survit aux redémarrages)."""
    with conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS lore_arc_state (
                         id        INTEGER PRIMARY KEY DEFAULT 1,
                         arc_key   VARCHAR(64),
                         step_idx  INTEGER NOT NULL DEFAULT 0,
                         next_due  TIMESTAMPTZ,
                         recent    JSONB NOT NULL DEFAULT '[]')""")
        cur.execute("INSERT INTO lore_arc_state (id) VALUES (1) ON CONFLICT DO NOTHING")


def _arc_state(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT arc_key, step_idx, next_due, recent FROM lore_arc_state WHERE id=1")
        return dict(cur.fetchone() or {})


def _arc_save(conn, arc_key, step_idx, next_due, recent):
    with conn.cursor() as cur:
        cur.execute("""UPDATE lore_arc_state SET arc_key=%s, step_idx=%s, next_due=%s, recent=%s::jsonb
                       WHERE id=1""", (arc_key, step_idx, next_due, json.dumps(recent)))


def _arc_next(conn):
    """Retourne (type, texte) de la prochaine étape à publier, ou None.

    Fait avancer l'arc en cours quand son étape est due ; sinon en démarre un
    parfois. None => le générateur sort une phrase normale.
    """
    if not LORE_ARCS:
        return None
    st  = _arc_state(conn)
    now = datetime.datetime.now(datetime.timezone.utc)
    key, idx = st.get("arc_key"), st.get("step_idx") or 0
    recent   = st.get("recent") or []
    gap      = lambda: now + datetime.timedelta(hours=random.uniform(ARC_GAP_MIN_H, ARC_GAP_MAX_H))

    # 1) Arc en cours : on publie l'étape suivante si elle est due.
    if key and key in LORE_ARCS:
        steps = LORE_ARCS[key]["steps"]
        due   = st.get("next_due")
        if idx < len(steps) and (not due or now >= due):
            text = steps[idx]
            nxt  = idx + 1
            if nxt >= len(steps):      # arc terminé → on le retient pour ne pas le rejouer
                recent = (recent + [key])[-6:]
                _arc_save(conn, None, 0, None, recent)
            else:
                _arc_save(conn, key, nxt, gap(), recent)
            return (LORE_ARCS[key].get("type", "festival_moment"), text)
        return None                    # arc en attente de son heure → phrase normale

    # 2) Aucun arc en cours : on en démarre un de temps en temps.
    if random.random() < ARC_START_P:
        candidates = [k for k in LORE_ARCS if k not in recent]
        if not candidates:
            # Peu d'arcs → tous « récents ». On autorise le rejeu, mais JAMAIS celui qu'on
            # vient de terminer (sinon il repart 1h après sa chute — vu en simulation).
            last = recent[-1] if recent else None
            candidates = [k for k in LORE_ARCS if k != last] or list(LORE_ARCS)
        key   = random.choice(candidates)
        steps = LORE_ARCS[key]["steps"]
        if not steps:
            return None
        if len(steps) > 1:
            _arc_save(conn, key, 1, gap(), recent)
        else:
            _arc_save(conn, None, 0, None, (recent + [key])[-6:])
        return (LORE_ARCS[key].get("type", "festival_moment"), steps[0])
    return None


def _lore_generator():
    time.sleep(20)  # laisser init_db finir
    last_type = None
    recent = []          # anti-répétition : on ne rejoue pas les dernières phrases
    while True:
        try:
            conn = get_conn(); conn.autocommit = True
            city = _current_city(conn)   # suit la mini-scène en cours
            _arc_init(conn)

            # Priorité 1 : une étape d'arc narratif si elle est due (le journal RACONTE).
            arc = _arc_next(conn)
            # ~1 génération sur 4 : saluer un titre plébiscité (lore RÉACTIF aux votes).
            hot = None if arc else (_hot_track(conn) if random.random() < 0.25 else None)
            if arc:
                etype, desc = arc[0], arc[1].replace("{ville}", city).replace("{city}", city)
            elif hot and hot[1]:
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
                # Fenêtre élargie 12 → 26 (demande Cassy 15/07 : « 25-30 »). Il y a 36 phrases
                # par demi-journée (3 types × 12) : à 26, on exclut la grande majorité du vivier
                # avant de rejouer quoi que ce soit. Le `or pool` plus haut évite la famine si
                # la fenêtre vide un type (on retombe alors sur son pool complet).
                if len(recent) > LORE_RECENT_WINDOW:
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
