# Fluxer-Discord Bridge — variante « voix »

Pont Discord ↔ Fluxer, **plus la voix** (« Le papa de kiki »).

## Ce que fait la partie vocale

| Déclencheur | Comportement |
|---|---|
| Le **dernier humain quitte** un salon vocal | Le bot rejoint, muet et sourd, pour tenir le timer d'occupation du salon (*keep-alive*). |
| Quelqu'un **revient** dans ce salon | Le bot enchaîne des `AUGGH` à vitesses aléatoires pendant **10 secondes**, puis repart. |
| `brdg;parle` | Le bot rejoint le salon de l'auteur du message et parle jusqu'à `brdg;tais`. Il tient au reset (il revient tout seul après un redémarrage). |
| `brdg;tais` | Il se tait et quitte. |
| `brdg;volume <0-200>` | Volume de la voix, en pourcentage. |

Le son est **téléchargé et découpé au premier usage** (yt-dlp + ffmpeg) dans
`app-data/fluxer-discord-bridge/db/` : `augghh.mp3` plus 8 variantes de vitesse
(`augghh_055` … `augghh_20`). Elles persistent ensuite dans le volume de données.

## Pourquoi une image maison

L'image amont `rillabel/fluxer-discord-bridge` est un pont **texte pur** : ni
ffmpeg, ni `@discordjs/voice`. Le `Dockerfile` de ce dossier ajoute les deux et
embarque le `Bot.js` modifié (`src/Bot.js`).

`setup.sh` construit l'image automatiquement si elle manque. À la main :

```bash
docker build -t caleope-fluxer-discord-bridge-voice:1.0.0 .
```

## ⚠️ Deux pièges déjà payés

1. **La voix peut disparaître sans aucune erreur.** Sans ffmpeg, le bot se
   connecte, journalise « Bridges loaded », et se tait. C'est arrivé le
   2026-08-09 : une réinstallation depuis le magasin a ramené l'image amont.
   D'où l'échec explicite du `setup.sh` si l'image vocale ne peut pas être
   construite — plutôt qu'un repli silencieux.

2. **Le code n'était nulle part.** Le `Bot.js` réellement exécuté avait été
   copié **à chaud** dans le conteneur : absent de l'image, absent de git,
   récupérable uniquement tant que ce conteneur existait. Il a été extrait le
   2026-08-09 avant la destruction de l'ancien serveur et vit maintenant dans
   `src/Bot.js`. **Ne plus jamais corriger ce bot par `docker cp`** — éditer
   `src/Bot.js`, pousser, puis `caleope update && caleope install
   fluxer-discord-bridge --force`.

## Jetons

`DISCORD_TOKEN` et `FLUXER_TOKEN` vivent dans
`app-config/fluxer-discord-bridge/secrets.env` (chmod 600, jamais dans git).
Le `setup.sh` les **préserve** lors d'un `install --force` (motif `_keep` :
paramètre fourni > valeur en place > défaut).
