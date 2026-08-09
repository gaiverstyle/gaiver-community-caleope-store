# Fluxer-Discord Bridge — variante « voix » (Le papa de kiki)
#
# L'image amont est un pont TEXTE pur : ni ffmpeg, ni @discordjs/voice.
# Cette variante ajoute de quoi parler dans un salon vocal, et embarque le
# Bot.js modifié (keep-alive vocal + easter egg « brdg;parle »).
#
# POURQUOI CE FICHIER EXISTE :
# la variante n'a longtemps existé QUE sous forme d'artefact — une image
# construite à la main le 2026-07-20 sur l'ancien serveur, sans Dockerfile.
# Pire : le Bot.js réellement exécuté n'était même pas DANS l'image, il avait
# été copié à chaud dans le conteneur. Réinstaller l'app depuis le magasin
# ramenait l'image standard et la voix disparaissait sans un seul message
# d'erreur ; un `docker rm` aurait effacé le code définitivement.
# Tout est désormais reconstructible depuis cette source.
#
# Construction manuelle :
#   docker build -t caleope-fluxer-discord-bridge-voice:1.0.0 .
# (le setup.sh du paquet la lance tout seul si l'image manque)

# Base épinglée par empreinte : le tag `latest` de l'amont bouge, et une
# reconstruction silencieusement différente est exactement ce qu'on cherche
# à éviter ici. Empreinte relevée le 2026-08-09 sur rillabel/…:latest.
FROM rillabel/fluxer-discord-bridge@sha256:b1f282cea9b3c08ba8aa8145cf12dc148efd3a56a3f531922f4747eee9e7096c

# ffmpeg est OBLIGATOIRE : le bot encode lui-même en Opus (`-c:a libopus`)
# et pousse un flux OggOpus vers Discord. Sans lui, le bot se connecte, dit
# « Bridges loaded », et reste muet — la panne est totalement silencieuse.
# python3 + curl servent à yt-dlp.
RUN (command -v apk > /dev/null 2>&1 && apk add --no-cache ffmpeg python3 curl) \
 || (apt-get update \
     && apt-get install -y --no-install-recommends ffmpeg python3 curl \
     && rm -rf /var/lib/apt/lists/*)

# yt-dlp : le son source est téléchargé au premier appel s'il n'est pas déjà
# dans /Bot/db (volume de données), puis découpé en 8 variantes de vitesse.
RUN curl -sSL https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
      -o /usr/local/bin/yt-dlp \
 && chmod +x /usr/local/bin/yt-dlp

WORKDIR /Bot

# Un seul module à ajouter. Pas d'encodeur Opus npm (@discordjs/opus,
# opusscript) : ffmpeg sort déjà de l'OggOpus. Pas de lib de chiffrement
# (libsodium, tweetnacl) non plus : depuis Node 22, @discordjs/voice utilise
# l'AES-256-GCM natif de Node — et la base est en Node 25.
RUN npm install --no-audit --no-fund @discordjs/voice@0.19.2

# Le bot patché, versionné à côté de ce Dockerfile.
COPY src/Bot.js /Bot/Bot.js

CMD ["node", "Bot.js"]
