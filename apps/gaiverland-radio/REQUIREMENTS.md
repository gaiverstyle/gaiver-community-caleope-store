# Gaiverland Radio IA — Exigences d'automatisation

> Tout ce qui est listé ici doit être configuré **automatiquement** au premier démarrage,
> sans aucune intervention manuelle, sauf les exceptions explicitement marquées *(manuel)*.

---

## Prérequis obligatoire

- Package **`azuracast`** Caleope déjà installé et fonctionnel
- Une **clé API AzuraCast** avec droits Read + Write *(manuel — créer dans AzuraCast → Admin → API Keys)*

---

## Démarrage des services IA

- [x] Base de données PostgreSQL initialisée avec le schéma complet (tables `tracks`, `play_history`, `rebexis_sessions`, `radio_state`)
- [x] Analyseur musical démarré et scan initial de la bibliothèque AzuraCast
- [x] Surveillance temps réel des nouveaux fichiers ajoutés (inotify)
- [x] API Playlist Engine disponible sur le port alloué dynamiquement
- [x] Rebexis Engine démarré avec le mode sélectionné (template / ollama / api)
- [x] TTS Worker démarré avec le modèle de voix sélectionné
- [x] Scheduler orchestrateur actif (cycle toutes les 5 minutes)
- *(manuel)* Téléchargement du modèle Ollama si mode `ollama` sélectionné

---

## Connexion à AzuraCast

- [x] Connexion réseau via `azuracast-internal` (réseau Docker existant du package `azuracast`)
- [x] URL et station ID configurés depuis les paramètres d'installation
- [x] Chemin du dossier `stations` monté en lecture seule pour l'analyseur
- *(manuel)* Clé API AzuraCast à renseigner dans `services.env` si non fournie à l'installation

---

## Rebexis (animatrice IA)

- [x] Templates d'intervention pré-configurés (4 modes : normal, hype, peak, flow)
- [x] Mémoire des interventions précédentes (anti-répétition)
- [x] Respect de l'intervalle min/max configuré entre interventions
- [x] Fichiers audio générés à l'avance — jamais en temps réel (charge CPU stable)
- [x] Upload automatique des fichiers audio dans AzuraCast via son API
- *(manuel)* Si mode `api` : renseigner la clé API LLM dans `rebexis.env`
- *(manuel)* Si mode `ollama` : `docker exec gaiverland-ollama ollama pull <modèle>` après démarrage

---

## Modèle TTS (Piper)

- [x] Téléchargement automatique du modèle Piper depuis HuggingFace au premier appel (~70 Mo)
- [x] Cache des fichiers audio générés (pas de re-génération si texte identique)
- [x] Conversion WAV → MP3 via ffmpeg intégré

---

## Credentials et secrets

- [x] Mot de passe PostgreSQL généré aléatoirement par `setup.sh`
- [x] Stockés dans `app-config/gaiverland-radio/db.env` et `services.env` (chmod 600)
- [x] `post-install.txt` affiché après installation avec les instructions de configuration

---

## Accès

| Élément           | Valeur                                              |
|-------------------|-----------------------------------------------------|
| Playlist API      | `http://<IP-serveur>:<port-api>/` (port dynamique)  |
| État radio (GET)  | `http://<IP-serveur>:<port-api>/state`              |
| Changer le mood   | `POST /state/mood?mood=festival`                    |
| Forcer Rebexis    | `POST http://<IP-serveur>:8081/generate?force=true` |

---

## Infrastructure requise

- **CPU** : x86-64, 4 cœurs minimum (Ryzen 5 3600 ou équivalent recommandé)
- **RAM** : 4 Go minimum · 8 Go recommandé · 12 Go si mode Ollama activé
- **Stockage** : 1 Go minimum pour le cache TTS + base de données
- **GPU** : Non requis — tous les services tournent sur CPU uniquement

---

## Ce qui reste intentionnellement manuel

1. **Clé API AzuraCast** : créer dans l'UI AzuraCast puis renseigner dans `services.env`
2. **Modèle Ollama** : `docker exec gaiverland-ollama ollama pull llama3.2:3b` (si mode `ollama`)
3. **Clé API LLM externe** : renseigner dans `rebexis.env` (si mode `api`)
4. **Bibliothèque musicale** : l'analyseur scanne l'existant, mais la musique s'ajoute via AzuraCast
