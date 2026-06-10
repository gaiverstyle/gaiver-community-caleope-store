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

## Synthèse vocale (Kokoro TTS)

- [x] Moteur : **Kokoro TTS** (voix française `ff_siwis` — expressive, non robotique)
- [x] Post-traitement radio automatique via ffmpeg :
  - Highpass 80Hz (nettoyage basses inutiles)
  - EQ -2dB @ 200Hz (anti-nasillard), +4dB @ 2.5kHz (présence voix), +2dB @ 10kHz (air)
  - Compresseur broadcast (threshold -20dB, ratio 4:1, makeup +5dB)
  - Limiteur de crête (0.95)
  - Loudnorm EBU R128 (-14 LUFS, TP -1.5dB — standard streaming)
- [x] Cache des fichiers audio (pas de re-génération si texte identique)
- [x] Pré-génération à l'avance — jamais en temps réel (charge CPU stable)

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
| RAM       | 4 Go | 8 Go (12 Go si Ollama) |
| Stockage  | 2 Go (cache + DB) | 10 Go+ (bibliothèque musicale) |
| GPU       | Non requis | Non requis |

---

## Ce qui reste intentionnellement manuel

1. **Clé API AzuraCast** : créer dans l'UI AzuraCast, renseigner dans `services.env`
2. **Modèle Ollama** : `docker exec gaiverland-ollama ollama pull llama3.2:3b` (si mode `ollama`)
3. **Bibliothèque musicale** : s'ajoute via SFTP ou l'interface AzuraCast — l'analyseur scan automatiquement
4. **Ouverture du port Icecast** dans le pare-feu du serveur (port alloué dynamiquement)
