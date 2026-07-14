"""
Analyseur musical — Essentia + modèle Discogs EffNet (400 genres).
Extrait BPM précis, énergie, danceability, tonalité, genre électronique.
Mappe sur les moods Gaiverland et synchronise les az_id avec AzuraCast.
"""
import os, sys, subprocess, time, pathlib, json

MODELS_DIR = pathlib.Path(os.environ.get("ESSENTIA_MODELS_DIR", "/essentia-models"))

DISCOGS_MODELS = {
    "effnet_embed": "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.pb",

    "genre_labels": "https://essentia.upf.edu/models/music-style-classification/discogs-effnet/discogs-effnet-bs64-1.json",
    # Voix/instrumental (réutilise les embeddings effnet) → vocalness 0→1 (« ça se chante »).
    "voice_model":  "https://essentia.upf.edu/models/classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.pb",
    "voice_labels": "https://essentia.upf.edu/models/classification-heads/voice_instrumental/voice_instrumental-discogs-effnet-1.json",
}

# Mapping genres Discogs → moods Gaiverland
GENRE_TO_MOOD = {
    # Intense
    "Hardstyle": "intense", "Hardcore": "intense", "Gabber": "intense",
    "Hard Techno": "intense", "Industrial": "intense", "Speedcore": "intense",
    "UK Hardcore": "intense", "Frenchcore": "intense",
    "Hardbass": "intense", "Hard Bass": "intense", "Rawstyle": "intense",
    "Uptempo": "intense", "Terrorcore": "intense", "Hard Dance": "intense",
    "Hard House": "intense", "Schranz": "intense", "Makina": "intense",
    "Donk": "intense", "Jump Up": "intense",
    # Festival / Big energy
    "Trance": "festival", "Euro House": "festival", "Happy Hardcore": "festival",
    "Jumpstyle": "festival", "Electro House": "festival", "Big Room": "festival",
    "Dance": "festival", "Eurodance": "festival", "Hi NRG": "festival",
    # Energique
    "Techno": "energique", "Tech House": "energique", "Minimal": "energique",
    "Electroclash": "energique", "EBM": "energique", "Electro": "energique",
    "Acid Techno": "energique", "Detroit Techno": "energique",
    # Melodique
    "Progressive House": "melodique", "Progressive Trance": "melodique",
    "Melodic Techno": "melodique", "Deep House": "melodique",
    "Melodic House": "melodique", "Electronica": "melodique",
    "Italo House": "melodique", "Dream House": "melodique",
    # Nocturne
    "Ambient": "nocturne", "Downtempo": "nocturne", "Chillout": "nocturne",
    "Dark Ambient": "nocturne", "Drone": "nocturne", "New Age": "nocturne",
}

AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".wav", ".aac", ".m4a"}

# Préfixes de fichiers à exclure de l'analyse (jingles, TTS, etc.)
SKIP_PREFIXES = ("rebexis_",)


def install_deps():
    pkgs = ["essentia-tensorflow", "psycopg2-binary", "inotify-simple", "httpx", "mutagen"]
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet"] + pkgs, check=True)


try:
    import essentia, essentia.standard as es, psycopg2, inotify_simple, httpx, mutagen
except ImportError:
    print("→ Installation dépendances analyzer (essentia-tensorflow, ~500Mo)...")
    install_deps()
    import essentia, essentia.standard as es, psycopg2, inotify_simple, httpx, mutagen

import numpy as np
import sys
sys.path.insert(0, "/app")
from az_utils import find_file_by_path, batch_assign_playlist

# ── Silence du logger C++ d'Essentia ───────────────────────────────────────────
# Les algos TensorflowPredict* émettent ~8 000 warnings « No network created... »
# PAR FICHIER analysé (mesuré). Bénins fonctionnellement, mais à ce débit ils
# saturent le démon Docker (json-log de 2,2 Go observé le 13/07 → dockerd à 380 %
# de CPU → tout le LXC affamé, le bot Discord larguait le vocal). On coupe INFO et
# WARNING d'Essentia ; les vraies erreurs Python continuent de remonter normalement.
try:
    essentia.log.warningActive = False
    essentia.log.infoActive = False
except Exception:
    pass

DB_URL = os.environ["DATABASE_URL"]
AZ_KEY = os.environ.get("AZURACAST_API_KEY", "")
WATCH_DIR = "/var/azuracast/stations"

_genre_labels = []
_effnet_model = None
_genre_model = None
_embed_model = None      # effnet embeddings (PartitionedCall:1) — entrée du modèle voix
_voice_model = None      # classifieur voix/instrumental
_voice_classes = []      # ordre des classes (typiquement ["instrumental", "voice"])


def download_models():
    global _genre_labels
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    import urllib.request
    for name, url in DISCOGS_MODELS.items():
        dest = MODELS_DIR / pathlib.Path(url).name
        if not dest.exists():
            print(f"  → Téléchargement modèle Essentia : {dest.name} (~{40 if 'effnet' in name else 2}Mo)...")
            # Best-effort : un modèle injoignable (404, réseau) ne doit JAMAIS crash-looper
            # l'analyzer. On loggue et on continue ; load_models() dégrade proprement si un
            # modèle manque (ex. voix absente → vocalness désactivé, genre/BPM intacts).
            try:
                print(f"  → download {url}")
                urllib.request.urlretrieve(url, str(dest))
                print(f"  ✓ {dest.name}")
            except Exception as edl:
                if dest.exists():
                    try: dest.unlink()
                    except Exception: pass
                print(f"  ⚠ modèle {dest.name} indisponible ({edl}) — ignoré, analyse dégradée", flush=True)
    # Charger les labels genre
    labels_path = MODELS_DIR / pathlib.Path(DISCOGS_MODELS["genre_labels"]).name
    if labels_path.exists():
        with open(labels_path) as f:
            meta = json.load(f)
        _genre_labels = meta.get("classes", [])
    print(f"  ✓ {len(_genre_labels)} classes de genres chargées")


def load_models():
    global _effnet_model, _genre_model
    effnet_path = MODELS_DIR / pathlib.Path(DISCOGS_MODELS["effnet_embed"]).name
    # Utiliser le même modèle effnet pour la classification directe (PartitionedCall:0)
    _effnet_model = es.TensorflowPredictEffnetDiscogs(
        graphFilename=str(effnet_path), output="PartitionedCall:0"
    )
    _genre_model = _effnet_model  # Alias — même modèle, output direct
    # ── Voix/instrumental : embeddings effnet (PartitionedCall:1) → classifieur 2D ──
    global _embed_model, _voice_model, _voice_classes
    try:
        _embed_model = es.TensorflowPredictEffnetDiscogs(
            graphFilename=str(effnet_path), output="PartitionedCall:1")
        voice_path = MODELS_DIR / pathlib.Path(DISCOGS_MODELS["voice_model"]).name
        _voice_model = es.TensorflowPredict2D(
            graphFilename=str(voice_path), output="model/Softmax")
        vlabels_path = MODELS_DIR / pathlib.Path(DISCOGS_MODELS["voice_labels"]).name
        if vlabels_path.exists():
            with open(vlabels_path) as f:
                _voice_classes = json.load(f).get("classes", ["instrumental", "voice"])
        print(f"  ✓ Modèle voix/instrumental chargé (classes: {_voice_classes})")
    except Exception as ev:
        _embed_model = _voice_model = None
        print(f"  ⚠ modèle voix indisponible ({ev}) — vocalness désactivé (analyse continue)")
    print("  ✓ Modèles Essentia chargés en mémoire")


def infer_mood_from_bpm_energy(bpm: float, energy: float) -> str:
    """Fallback si pas de genre détecté."""
    if bpm > 155 and energy > 0.65:
        return "intense"
    elif bpm > 145 and energy > 0.55:
        return "festival"
    elif bpm > 128:
        return "energique" if energy > 0.4 else "melodique"
    elif bpm > 110:
        return "melodique"
    return "nocturne"


def apply_bpm_guard(mood: str, bpm: float) -> str:
    """Corrige les incohérences flagrantes BPM/mood après classification genre.
    Évite ex: Happy Hardcore 161 BPM → festival, ou House 104 BPM → festival.
    """
    if mood in ("festival", "energique", "intense") and bpm < 112:
        return "melodique"
    if mood in ("festival", "energique") and bpm >= 150:
        return "intense"
    if mood == "intense" and bpm < 115:
        return "festival"
    if mood in ("nocturne", "melodique") and bpm > 165:
        return "intense"  # double-tempo probable mais safe fallback
    return mood


def apply_energy_guard(mood: str, bpm: float, energy: float) -> str:
    """Gros kick + loudness écrasée = hard music même sous 150 BPM
    (hardbass, rawstyle mid-tempo). Réservé à la nuit via mood=intense."""
    if mood in ("festival", "energique") and energy >= 0.82 and bpm >= 138:
        return "intense"
    return mood


# Mots-clés qui trahissent un style dur (nuit uniquement, 22h-06h)
_HARD_HINTS = (
    "frenchcore", "hardcore", "hardstyle", "uptempo", "gabber", "rawstyle",
    "terror", "speedcore", "tekstyle", "hardbass", "hard bass", "makina",
    "happy hardcore", "hard techno", "hard trance", "mainstream hardcore",
    "hard dance", "donk",
)


def apply_hard_genre_guard(mood: str, title: str, genre_top1: str, genre_top2: str) -> str:
    """Titre OU genre trahissant un style dur → nuit (intense), quel que soit le BPM.
    Indispensable car les frenchcore/hardstyle (~200 BPM) sont souvent détectés à
    demi-tempo (~100 BPM) et/ou mal classés (ex: 'Tribal') → l'inférence BPM/genre
    les laissait fuiter en journée. Le titre est le signal le plus fiable
    ('... Frenchcore Remix', 'Hardstyle Edit')."""
    hay = f"{title} {genre_top1} {genre_top2}".lower()
    if any(h in hay for h in _HARD_HINTS):
        return "intense"
    return mood


def apply_half_tempo_guard(mood: str, bpm: float, energy: float) -> str:
    """Hard music détecté à demi-tempo : énergie très haute avec un BPM anormalement
    bas pour de la journée (le vrai tempo est ~2x). Un vrai titre de jour à 90-108 BPM
    est downtempo/doux, pas à énergie ≥ 0.88 → on bascule en nuit."""
    if mood in ("festival", "energique", "melodique") and energy >= 0.88 and 90 <= bpm <= 108:
        return "intense"
    return mood


def analyze_file(path: str) -> dict:
    try:
        # ── Chargement audio ─────────────────────────────────────────
        loader = es.MonoLoader(filename=path, sampleRate=44100)
        audio_44k = loader()

        # Pour les modèles Essentia (16kHz)
        loader16 = es.MonoLoader(filename=path, sampleRate=16000)
        audio_16k = loader16()

        # ── Extracteur BPM, énergie, tonalité ────────────────────────
        extractor = es.MusicExtractor()
        features, _ = extractor(path)

        bpm          = float(features["rhythm.bpm"])
        energy       = float(features["lowlevel.average_loudness"])
        energy_norm  = min(1.0, max(0.0, (energy + 1.0) / 2.0))
        danceability = float(features["rhythm.danceability"])
        # tonal keys: variantes selon version Essentia
        try:
            key_note  = str(features["tonal.key_temperley.key"])
            key_scale = str(features["tonal.key_temperley.scale"])
        except Exception:
            try:
                key_note  = str(features["tonal.key_edma.key"])
                key_scale = str(features["tonal.key_edma.scale"])
            except Exception:
                key_note, key_scale = "", ""

        # Détection vocaux (énergie mid vs low)
        spec = np.abs(es.Spectrum()(audio_44k[:44100]))
        freqs = np.linspace(0, 22050, len(spec))
        vocal_e = float(np.mean(spec[(freqs > 300) & (freqs < 3400)])) + 1e-9
        low_e   = float(np.mean(spec[freqs <= 300])) + 1e-9
        has_vocals = (vocal_e / low_e) > 0.25

        # ── Vocalness (modèle Essentia voix/instrumental, 0→1) : « ça se chante » ──
        # Signal fiable pour la préférence mainstage du chef (vocal anthems > instrumental
        # techno). Réutilise audio_16k. Dégradé-safe : None si le modèle n'est pas dispo.
        vocalness = None
        if _embed_model is not None and _voice_model is not None:
            try:
                emb   = _embed_model(audio_16k)
                vpred = _voice_model(emb)
                vmean = np.mean(vpred, axis=0)
                idx   = _voice_classes.index("voice") if "voice" in _voice_classes else -1
                vocalness = round(max(0.0, min(1.0, float(vmean[idx]))), 3)
            except Exception as evv:
                print(f"  ⚠ vocalness ({os.path.basename(path)}): {evv}")

        # ── Genre Discogs (Essentia ML) ───────────────────────────────
        genre_top1 = genre_top2 = ""
        genre_scores = {}
        mood = "energique"  # default

        if _effnet_model and _genre_labels:
            try:
                predictions  = _effnet_model(audio_16k)
                mean_scores  = np.mean(predictions, axis=0)

                # Top 10 genres par score
                top_idx = np.argsort(mean_scores)[::-1][:10]
                for i in top_idx:
                    if i < len(_genre_labels) and float(mean_scores[i]) > 0.05:
                        label = _genre_labels[i].split("---")[-1].strip()
                        genre_scores[label] = round(float(mean_scores[i]), 4)

                # Genres principaux
                sorted_genres = sorted(genre_scores.items(), key=lambda x: -x[1])
                if sorted_genres:
                    genre_top1 = sorted_genres[0][0]
                if len(sorted_genres) > 1:
                    genre_top2 = sorted_genres[1][0]

                # Mood : premier genre reconnu dans le mapping
                for label, _ in sorted_genres:
                    if label in GENRE_TO_MOOD:
                        mood = GENRE_TO_MOOD[label]
                        break
                else:
                    mood = infer_mood_from_bpm_energy(bpm, energy_norm)
            except Exception as eg:
                print(f"  ⚠ Genre detection: {eg} — fallback BPM")
                mood = infer_mood_from_bpm_energy(bpm, energy_norm)
        else:
            mood = infer_mood_from_bpm_energy(bpm, energy_norm)

        # Garde BPM : corrige les incohérences flagrantes genre/BPM
        mood = apply_bpm_guard(mood, bpm)
        mood = apply_energy_guard(mood, bpm, energy_norm)

        # ── Métadonnées ID3/tags ──────────────────────────────────────
        meta = mutagen.File(path, easy=True) or {}
        title    = str(meta.get("title",  [""])[0]) or os.path.basename(path)
        artist   = str(meta.get("artist", [""])[0]) or "Inconnu"
        album    = str(meta.get("album",  [""])[0]) or ""
        duration = float(features["metadata.audio_properties.length"])

        # Gardes anti-fuite hard en journée (titre fiable + demi-tempo)
        mood = apply_hard_genre_guard(mood, title, genre_top1, genre_top2)
        mood = apply_half_tempo_guard(mood, bpm, energy_norm)

        return {
            "file_path": path, "title": title, "artist": artist, "album": album,
            "duration": round(duration, 1), "bpm": round(bpm, 1),
            "energy": round(energy_norm, 3), "danceability": round(danceability, 3),
            "has_vocals": has_vocals, "vocalness": vocalness,
            "key_note": key_note, "key_scale": key_scale,
            "mood": mood, "genre_top1": genre_top1, "genre_top2": genre_top2,
            "genre_scores": json.dumps(genre_scores), "analyzed": True,
        }
    except Exception as exc:
        print(f"  ⚠ Analyse échouée {os.path.basename(path)}: {exc}")
        return {"file_path": path, "analyzed": False}


def get_conn():
    return psycopg2.connect(DB_URL)


def save_track(conn, data: dict):
    if not data.get("analyzed"):
        return None
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO tracks
              (file_path, title, artist, album, duration, bpm, energy, danceability,
               has_vocals, vocalness, key_note, key_scale, mood, genre_top1, genre_top2, genre_scores, analyzed)
            VALUES
              (%(file_path)s, %(title)s, %(artist)s, %(album)s, %(duration)s, %(bpm)s,
               %(energy)s, %(danceability)s, %(has_vocals)s, %(vocalness)s, %(key_note)s, %(key_scale)s,
               %(mood)s, %(genre_top1)s, %(genre_top2)s, %(genre_scores)s::jsonb, %(analyzed)s)
            ON CONFLICT (file_path) DO UPDATE SET
              bpm=EXCLUDED.bpm, energy=EXCLUDED.energy, danceability=EXCLUDED.danceability,
              has_vocals=EXCLUDED.has_vocals, vocalness=EXCLUDED.vocalness, mood=EXCLUDED.mood,
              genre_top1=EXCLUDED.genre_top1, genre_top2=EXCLUDED.genre_top2,
              genre_scores=EXCLUDED.genre_scores::jsonb, analyzed=EXCLUDED.analyzed,
              key_note=EXCLUDED.key_note, key_scale=EXCLUDED.key_scale, updated_at=NOW()
            RETURNING id
        """, {**data, "genre_scores": data.get("genre_scores", "{}"),
              "vocalness": data.get("vocalness")})
        track_id = cur.fetchone()[0]
    conn.commit()

    # Sync az_id depuis AzuraCast.
    # Plusieurs file_path peuvent viser le MÊME fichier AzuraCast (doublons hérités des
    # migrations/ré-imports) → l'UPDATE violait `tracks_az_id_key` (contrainte unique) et
    # l'exception remontait jusqu'à l'appelant, tuant tout le lot du backfill (1 seul titre
    # drainé par cycle au lieu de 12, pendant des jours). La synchro az_id est un CONFORT :
    # elle ne doit jamais faire échouer l'analyse. On n'écrit donc que si l'az_id est libre,
    # et on avale l'échec proprement.
    if data.get("analyzed") and AZ_KEY:
        try:
            az_file = find_file_by_path(data["file_path"])
            if az_file:
                az_id = az_file.get("id")
                with conn.cursor() as cur:
                    cur.execute("""UPDATE tracks SET az_id=%s WHERE id=%s
                                   AND NOT EXISTS (SELECT 1 FROM tracks t2
                                                   WHERE t2.az_id=%s AND t2.id<>%s)""",
                                (az_id, track_id, az_id, track_id))
                conn.commit()
        except Exception as eaz:
            conn.rollback()
            print(f"  ⚠ sync az_id ignorée ({os.path.basename(data['file_path'])}): {eaz}", flush=True)

    return track_id


def main():
    print("🎵 Analyzer Gaiverland démarré")
    download_models()
    load_models()

    conn = get_conn()
    # Migration : colonne vocalness (0→1, proba « voix ») pour la préférence mainstage vocale.
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE tracks ADD COLUMN IF NOT EXISTS vocalness DOUBLE PRECISION")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT file_path FROM tracks WHERE analyzed=TRUE")
        known = {r[0] for r in cur.fetchall()}

    def should_skip(filename: str) -> bool:
        return filename.startswith(SKIP_PREFIXES)

    # Scan initial
    count = 0
    for root, _, files in os.walk(WATCH_DIR):
        for f in files:
            if os.path.splitext(f)[1].lower() in AUDIO_EXTS and not should_skip(f):
                fp = os.path.join(root, f)
                if fp not in known:
                    print(f"  → {os.path.basename(fp)}")
                    save_track(conn, analyze_file(fp))
                    count += 1
    print(f"  ✓ {count} fichiers analysés au démarrage")

    # Surveillance inotify — dict wd→dossier pour reconstituer le chemin complet
    inotify = inotify_simple.INotify()
    wd_to_dir = {}
    for root, _, _ in os.walk(WATCH_DIR):
        wd = inotify.add_watch(root, inotify_simple.flags.CLOSE_WRITE | inotify_simple.flags.MOVED_TO)
        wd_to_dir[wd] = root
    print("  ✓ Surveillance temps réel active")

    # Rescan périodique : l'inotify ci-dessus ne surveille que les dossiers existant AU
    # DÉMARRAGE. Les bacs thématiques music/<theme>/ (phonk, lofi, synthwave…) sont créés
    # PLUS TARD par le downloader → leurs fichiers ne déclenchent aucun event. Toutes les
    # RESCAN_INTERVAL s on re-parcourt l'arbre : on ajoute un watch aux nouveaux dossiers
    # et on analyse tout fichier encore absent de la base. Indispensable pour que les bacs
    # se remplissent ET s'analysent sans redéploiement.
    watched = set(wd_to_dir.values())
    last_rescan = time.time()
    RESCAN_INTERVAL = int(os.environ.get("ANALYZER_RESCAN_S", "120"))
    BACKFILL_BATCH  = int(os.environ.get("VOCALNESS_BACKFILL_BATCH", "12"))

    while True:
        events = inotify.read(timeout=5000)
        for event in events:
            name = event.name.decode() if isinstance(event.name, bytes) else event.name
            if os.path.splitext(name)[1].lower() in AUDIO_EXTS and not should_skip(name):
                try:
                    conn.cursor().execute("SELECT 1")
                except Exception:
                    conn = get_conn()
                # Reconstruire le chemin complet avec le dossier de l'event
                event_dir = wd_to_dir.get(event.wd, WATCH_DIR)
                fp = os.path.join(event_dir, name)
                # AzuraCast réécrit les mp3 déjà en place (ReplayGain, métadonnées, pochette)
                # → CLOSE_WRITE sur des titres DÉJÀ analysés. Sans ce garde-fou, chaque
                # réécriture relançait une analyse Essentia complète (lourde) pour rien.
                # Les vrais nouveaux fichiers, eux, ne sont pas dans `known`.
                if fp in known:
                    continue
                print(f"  → Nouveau : {fp}", flush=True)
                time.sleep(1)
                save_track(conn, analyze_file(fp))
                known.add(fp)
                print(f"  ✓ Analysé : {name}")

        # Rescan périodique (nouveaux bacs thématiques + rattrapage)
        if time.time() - last_rescan >= RESCAN_INTERVAL:
            last_rescan = time.time()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT file_path FROM tracks WHERE analyzed=TRUE")
                    known = {r[0] for r in cur.fetchall()}
            except Exception:
                conn = get_conn()
                known = set()
            for root, _, files in os.walk(WATCH_DIR):
                if root not in watched:
                    try:
                        wd = inotify.add_watch(root, inotify_simple.flags.CLOSE_WRITE | inotify_simple.flags.MOVED_TO)
                        wd_to_dir[wd] = root
                        watched.add(root)
                        print(f"  ✓ Nouveau dossier surveillé : {root}", flush=True)
                    except Exception:
                        pass
                for f in files:
                    if os.path.splitext(f)[1].lower() in AUDIO_EXTS and not should_skip(f):
                        fp = os.path.join(root, f)
                        if fp not in known:
                            print(f"  → (rescan) {os.path.basename(fp)}", flush=True)
                            save_track(conn, analyze_file(fp))
                            known.add(fp)

            # ── Backfill vocalness : ré-analyse throttlée des titres analysés AVANT le
            # modèle voix (vocalness NULL). Petit lot par cycle → CPU tranquille, l'UPSERT
            # met à jour la ligne existante.
            #
            # RÈGLE D'OR : un titre doit TOUJOURS sortir du pool NULL, quoi qu'il arrive.
            # Sinon il est re-sélectionné à chaque cycle et ré-analysé indéfiniment (chaque
            # analyse = du CPU + des milliers de lignes de log). C'était le cas : une erreur
            # sur le 1er titre faisait sauter tout le lot (le try englobait la boucle), donc
            # le même titre repassait toutes les 120 s… pendant 6 jours.
            # Donc : (1) chaque titre est isolé dans son propre try — un échec n'emporte plus
            # les autres ; (2) tout titre qui ne produit pas de vocalness exploitable (fichier
            # absent, analyse ratée, modèle muet) est marqué 0.0 = neutre, et sort du pool.
            if _voice_model is not None:
                try:
                    with conn.cursor() as cur:
                        cur.execute("""SELECT file_path FROM tracks
                                       WHERE analyzed=TRUE AND vocalness IS NULL
                                       ORDER BY updated_at DESC NULLS LAST LIMIT %s""",
                                    (BACKFILL_BATCH,))
                        todo = [r[0] for r in cur.fetchall()]
                except Exception as eb:
                    print(f"  ⚠ backfill vocalness (sélection): {eb}", flush=True)
                    todo = []

                for fp in todo:
                    got = None
                    try:
                        if os.path.exists(fp):
                            print(f"  → (backfill vocalness) {os.path.basename(fp)}", flush=True)
                            data = analyze_file(fp)
                            if data.get("analyzed"):
                                save_track(conn, data)
                                got = data.get("vocalness")
                    except Exception as eb:
                        conn.rollback()
                        print(f"  ⚠ backfill vocalness ({os.path.basename(fp)}): {eb}", flush=True)

                    if got is None:   # fichier absent, analyse ratée ou modèle muet → neutre
                        try:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE tracks SET vocalness=0.0 WHERE file_path=%s", (fp,))
                            conn.commit()
                        except Exception:
                            conn.rollback()


if __name__ == "__main__":
    main()
