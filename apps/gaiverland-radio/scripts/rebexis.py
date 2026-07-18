"""
Rebexis Engine — génère les textes d'intervention de l'animatrice.
Modes : template | ollama | api
"""
import os, sys, subprocess, json, random

def install_deps():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "psycopg2-binary", "httpx"], check=True)

try:
    import fastapi, uvicorn, psycopg2, httpx
except ImportError:
    install_deps()
    import fastapi, uvicorn, psycopg2, httpx

from fastapi import FastAPI
import psycopg2.extras

DB_URL      = os.environ["DATABASE_URL"]
MODE        = os.environ.get("REBEXIS_MODE", "template")
INT_MIN     = int(os.environ.get("REBEXIS_INTERVAL_MIN", "15")) * 60
INT_MAX     = int(os.environ.get("REBEXIS_INTERVAL_MAX", "30")) * 60
OLLAMA_URL  = os.environ.get("OLLAMA_URL", "http://gaiverland-ollama:11434")
OLLAMA_MDL  = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
API_KEY     = os.environ.get("REBEXIS_API_KEY", "")
API_BASE    = os.environ.get("REBEXIS_API_BASE", "https://api.openai.com/v1")

templates = {}
app = FastAPI(title="Rebexis Engine")

SYSTEM = """Tu es Rebexis, voix de Gaiverland Radio — radio électro en direct, sans interruption.
Tu parles À LA FOULE, pas à une personne. Comme une DJ face à son public dans un festival.
Référence absolue : Scott Taylor (Forza Horizon 5) — chaleur, inclusion, énergie de spectacle partagé.

Règle N°1 — JAMAIS de "tu" :
- Interdit : "tu", "t'as", "t'en", "toi", "ta", "ton" en s'adressant à l'auditeur.
- Autorisé : "vous", "tout le monde", "Gaiverland", tournures impersonnelles, "on" collectif.
- La salle entière est là. Chaque phrase s'adresse à TOUS ceux qui écoutent.

Style :
- Chaud, celebratoire, inclusif. PAS sarcastique, PAS cynique.
- Énergie de spectacle : "Ce moment est pour VOUS TOUS."
- Court et ancré : 1 à 2 phrases MAX, 15 à 50 mots.
- Français radio naturel. Jamais robotique, jamais pontifiant.
- Tu NE décris JAMAIS la musique — tu réagis à l'ambiance collective qu'elle crée.

Formatage prosodie (influence la synthèse vocale ElevenLabs) :
- MAJUSCULES pour l'emphase forte : "c'est ÉNORME", "vous TOUS"
- "..." pour les pauses dramatiques : "Et là... le drop arrive."
- Ponctuation expressive sobre : pas plus d'un point d'exclamation par phrase."""


def load_tpl():
    global templates
    try:
        with open("/app/templates.json") as f:
            templates = json.load(f)
    except Exception as e:
        print(f"⚠ Templates: {e}")
        templates = {"modes": {"normal": {"templates": ["La musique continue."]}}}


def get_conn():
    return psycopg2.connect(DB_URL, cursor_factory=psycopg2.extras.RealDictCursor)


def should_intervene(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT last_rebexis FROM radio_state WHERE id=1")
        row = cur.fetchone()
    if not row or not row["last_rebexis"]:
        return True
    import datetime
    elapsed = (datetime.datetime.now(datetime.timezone.utc) - row["last_rebexis"]).total_seconds()
    return elapsed >= random.randint(INT_MIN, INT_MAX)


# Phrases « nouveauté » : Rebexis annonce une première diffusion (surprise auditeurs).
# Fallback en dur → marche même si rebexis-templates.json n'a pas de section nouveauté.
# GÉNÉRIQUES (pas de nom de titre) → texte FIXE identique → TTS mis en cache une seule
# fois (cache_key = hash(texte)) puis réutilisé = ZÉRO token par nouveauté. Ne PAS
# interpoler le titre (ça rendrait chaque phrase unique = un TTS payant à chaque fois).
NOUVEAUTE_TEMPLATES = [
    "Attention : le prochain titre, c'est une nouveauté. Ouvrez grand les oreilles.",
    "Petite première qui arrive juste après — du neuf sur Gaiverland.",
    "Ça sent le neuf : une nouveauté débarque, tout de suite après.",
]

# Annonce d'un SET DJ VIRTUEL (bloc d'1h dédié à un artiste). Le {label} varie → un TTS
# par set (occasionnel, coût négligeable). Ouverture (dj_set non vide) et clôture (dj_set_end).
DJSET_TEMPLATES = [
    "L'heure qui vient est à {label}. Installez-vous, c'est du lourd du début à la fin.",
    "Changement de programme : une heure entière de {label}. Que le grand mix commence.",
    "Gaiverland passe en mode set : {label}, sans interruption, pendant une heure.",
]
DJSET_END_TEMPLATES = [   # clôture GÉNÉRIQUE (pas de label) → texte fixe, TTS mis en cache
    "Fin de l'heure spéciale. On repart sur le grand mélange Gaiverland.",
    "Le set est terminé — retour à la programmation, toujours plein pot.",
]


def gen_template(mood: str, next_track: str = "", new_track: bool = False) -> str:
    key = "hype" if mood in ("festival", "energique") else \
          "peak" if mood == "intense" else \
          "flow" if mood in ("nocturne", "melodique") else "normal"
    mode_data = templates.get("modes", {}).get(key, {})

    # Extraire l'artiste du prochain titre (avant le "—")
    next_artist = ""
    if next_track:
        parts = next_track.split("—")
        next_artist = parts[0].strip() if parts else next_track.strip()
        # Nettoyer les noms trop longs ou parasites
        if len(next_artist) > 40:
            next_artist = next_artist[:40].rstrip()

    # NOUVEAUTÉ : le prochain titre est une première → annonce GÉNÉRIQUE fixe (sans le nom
    # du titre) → TTS caché/réutilisé, zéro token répété.
    if new_track:
        return random.choice(NOUVEAUTE_TEMPLATES)

    if next_artist and "templates_with_next" in mode_data:
        # Préférer les templates avec prochain titre (2 chances sur 3)
        pool = mode_data["templates_with_next"] * 2 + mode_data.get("templates_no_next", mode_data.get("templates", []))
        tpl = random.choice(pool)
        return tpl.format(next=next_artist, next_track=next_track)
    else:
        t = mode_data.get("templates_no_next") or mode_data.get("templates", ["La radio continue."])
        return random.choice(t)


def gen_ollama(mood: str, context: str, recent: list, next_track: str = "", new_track: bool = False) -> str:
    prompt = f"Génère UNE intervention de Rebexis (2 phrases, 20-40 mots). Morceau en cours: {context}. Ambiance: {mood}."
    if next_track and new_track:
        prompt += f" Le prochain morceau est une NOUVEAUTÉ, sa TOUTE PREMIÈRE diffusion sur la radio : {next_track}. Annonce-le comme une première/découverte, avec enthousiasme (mot 'nouveauté' ou 'première')."
    elif next_track:
        prompt += f" Le prochain morceau sera : {next_track}. Termine ta phrase en lançant vers ce prochain titre."
    if recent:
        prompt += f" Phrases à éviter: {' / '.join(recent[:2])}"
    try:
        r = httpx.post(f"{OLLAMA_URL}/api/generate",
                       json={"model": OLLAMA_MDL, "prompt": f"{SYSTEM}\n\n{prompt}", "stream": False},
                       timeout=30)
        r.raise_for_status()
        return r.json().get("response", "").strip()
    except Exception as e:
        print(f"⚠ Ollama: {e}")
        return gen_template(mood, next_track, new_track)


def gen_api(mood: str, context: str, recent: list, next_track: str = "", new_track: bool = False) -> str:
    user = f"Morceau en cours : {context}. Ambiance : {mood}."
    if next_track and new_track:
        user += f" Le prochain morceau est une NOUVEAUTÉ, sa TOUTE PREMIÈRE diffusion : {next_track}. Annonce-le comme une première/découverte enthousiaste (dis 'nouveauté' ou 'première')."
    elif next_track:
        user += f" Prochain morceau : {next_track}. Termine en lançant vers ce titre."
    if recent:
        user += f" Évite: {' / '.join(recent[:2])}"
    try:
        r = httpx.post(f"{API_BASE}/chat/completions",
                       headers={"Authorization": f"Bearer {API_KEY}"},
                       json={"model": "gpt-4o-mini",
                             "messages": [{"role": "system", "content": SYSTEM},
                                          {"role": "user", "content": user}],
                             "max_tokens": 100, "temperature": 1.0},
                       timeout=15)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"⚠ API LLM: {e}")
        return gen_template(mood, next_track, new_track)


@app.get("/health")
def health():
    return {"status": "ok", "mode": MODE}


@app.post("/generate")
def generate(mood: str = "energique", context_track: str = "",
             next_track: str = "", force: bool = False, new_track: bool = False,
             dj_set: str = "", dj_set_end: bool = False):
    conn = get_conn()
    if not force and not should_intervene(conn):
        return {"intervention": None, "reason": "intervalle_non_atteint"}

    with conn.cursor() as cur:
        cur.execute("SELECT intervention FROM rebexis_sessions ORDER BY generated_at DESC LIMIT 5")
        recent = [r["intervention"] for r in cur.fetchall()]

    if dj_set_end:
        # Clôture de set = phrase fixe générique (TTS caché).
        text = random.choice(DJSET_END_TEMPLATES)
    elif dj_set:
        # Ouverture de set = template fixe avec le nom de l'artiste (LLM court-circuité).
        text = random.choice(DJSET_TEMPLATES).format(label=dj_set)
    elif new_track:
        # Nouveauté = phrase fixe générique (0 appel LLM, TTS caché) quel que soit le mode.
        text = gen_template(mood, next_track, True)
    else:
        text = gen_ollama(mood, context_track, recent, next_track) if MODE == "ollama" else \
               gen_api(mood, context_track, recent, next_track)    if MODE == "api"    else \
               gen_template(mood, next_track)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO rebexis_sessions (intervention, mood_trigger, context_track)
            VALUES (%s,%s,%s) RETURNING id
        """, (text, mood, context_track))
        sid = cur.fetchone()["id"]
        cur.execute("UPDATE radio_state SET last_rebexis=NOW() WHERE id=1")
    conn.commit()

    if next_track:
        print(f"  🎙 Rebexis [{mood}] → {next_track[:40]}: {text[:60]}…")
    return {"intervention": text, "session_id": sid, "mode": MODE}


if __name__ == "__main__":
    load_tpl()
    print(f"🎙 Rebexis Engine — mode: {MODE}")
    uvicorn.run(app, host="0.0.0.0", port=8081)
