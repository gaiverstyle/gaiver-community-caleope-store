# Gaiverland Radio IA — Exigences d'automatisation

> Tout ce qui est listé ici doit être configuré **automatiquement** au premier démarrage,
> sans aucune intervention manuelle, sauf les exceptions explicitement marquées *(manuel)*.

---

## Détection AzuraCast (automatique)

- [x] `setup.sh` détecte si le container `azuracast` tourne déjà
- [x] Mode **addon** : connexion à l'instance existante via `azuracast-internal`
- [x] Mode **autonome** : déploiement AzuraCast 0.23.4 complet si absent (profil `with-azuracast`)
- [x] Réseau `azuracast-internal` créé automatiquement si nécessaire
- *(manuel)* Clé API AzuraCast à créer dans AzuraCast → Administration → API Keys (Read + Write)

---

## Analyse musicale (Essentia + Discogs)

- [x] Téléchargement automatique des modèles Essentia au premier démarrage (~50 Mo)
  - `discogs-effnet-bs64-1.pb` — embeddings EffNet entraîné sur Discogs
  - `discogs_multi_embeddings-effnet-1.pb` — classificateur 400 genres
- [x] Scan initial de toute la bibliothèque AzuraCast existante
- [x] Surveillance temps réel des nouveaux fichiers (inotify)
- [x] Extraction par titre : BPM précis, énergie, danceability, tonalité, présence vocale
- [x] Classification genre parmi 400 classes Discogs (hardstyle, melodic techno, progressive, etc.)
- [x] Mapping genre → mood Gaiverland (festival / intense / energique / melodique / nocturne)
- [x] Synchronisation `az_id` avec AzuraCast (lien entre notre DB et les fichiers AzuraCast)

---

## Moteur de playlist IA

- [x] API FastAPI disponible sur le port alloué dynamiquement
- [x] Cohérence énergétique (glissement progressif ±0.35)
- [x] Graphe de transitions d'états émotionnels
- [x] Anti-répétition artiste sur 3 titres consécutifs
- [x] Anti-répétition titre sur 2 heures
- [x] Ratio découverte configurable (défaut 20%)

---

## Playlists AzuraCast (API 0.23.4)

- [x] Création automatique de la playlist **"Gaiverland IA"** (type: default, weight: 3)
- [x] Création automatique de la playlist **"Rebexis"** (type: once_per_x_songs, 1 jingle/8 morceaux)
- [x] Mise à jour de la playlist "Gaiverland IA" toutes les 5 min avec les titres mood-appropriés
- [x] Upload et assignation automatique des fichiers audio Rebexis à la playlist "Rebexis"

---

## Rebexis (animatrice IA)

- [x] 3 modes au choix : `template` (rapide), `ollama` (LLM local CPU), `api` (LLM externe)
- [x] Mémoire des 5 dernières interventions (anti-répétition)
- [x] Intervalle min/max configurable (défaut 15–30 min)
- [x] Détection du titre en cours via AzuraCast API pour contextualiser l'intervention
- *(manuel)* Mode `ollama` : `docker exec gaiverland-ollama ollama pull llama3.2:3b`
- *(manuel)* Mode `api` : renseigner `REBEXIS_API_KEY` dans `rebexis.env`

---

## Synthèse vocale (XTTS v2 — Coqui TTS)

- [x] Moteur : **XTTS v2** (Coqui TTS) — qualité quasi-humaine, non robotique
- [x] Modèle `tts_models/multilingual/multi-dataset/xtts_v2` (~2.4 Go, stocké sur NAS)
- [x] Mode **locuteur intégré** (défaut) : voix féminine "Ana Florence", excellent français
- [x] Mode **clonage vocal** (optionnel) : déposer un WAV de 6–30s dans  
  `app-config/gaiverland-radio/rebexis-voice.wav` pour cloner la voix voulue
- [x] Post-traitement radio automatique via ffmpeg :
  - Highpass 90Hz (nettoyage basses inutiles)
  - EQ -3dB @ 180Hz (anti-nasillard), +5dB @ 2.5kHz (présence voix), +3dB @ 10kHz (air)
  - Compresseur broadcast agressif (threshold -22dB, ratio 6:1, makeup +7dB)
  - Saturation harmonique (aexciter — chaleur + agressivité festival)
  - Limiteur brick-wall (0.92)
  - Loudnorm EBU R128 (-13 LUFS, TP -1.0dB)
- [x] Cache des fichiers audio (pas de re-génération si texte identique)
- [x] Pré-génération à l'avance — jamais en temps réel (~2-3 min/clip CPU, acceptable)

---

## Credentials et secrets

- [x] Mot de passe PostgreSQL généré aléatoirement
- [x] Secrets AzuraCast (si mode autonome) générés aléatoirement
- [x] Tous les secrets stockés en chmod 600 dans `app-config/gaiverland-radio/`
- [x] `post-install.txt` avec les informations de connexion et les étapes suivantes

---

## Infrastructure requise

| Ressource | Minimum | Recommandé |
|-----------|---------|------------|
| CPU       | 4 cœurs x86-64 | Ryzen 5 3600+ |
| RAM       | 8 Go | 12 Go (16 Go si Ollama) |
| Stockage  | 2 Go (cache + DB) | 10 Go+ (bibliothèque musicale) |
| GPU       | Non requis | Non requis |

---

## Ce qui reste intentionnellement manuel

1. **Clé API AzuraCast** : créer dans l'UI AzuraCast, renseigner dans `services.env`
2. **Modèle Ollama** : `docker exec gaiverland-ollama ollama pull llama3.2:3b` (si mode `ollama`)
3. **Bibliothèque musicale** : s'ajoute via SFTP ou l'interface AzuraCast — l'analyseur scan automatiquement
4. **Ouverture du port Icecast** dans le pare-feu du serveur (port alloué dynamiquement)
