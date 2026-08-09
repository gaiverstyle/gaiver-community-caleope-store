#!/bin/bash
set -euo pipefail

CONFIG_DIR="${CALEOPE_BASE_DIR}/app-config/${CALEOPE_APP_ID}"
mkdir -p "${CONFIG_DIR}"
mkdir -p "${CALEOPE_BASE_DIR}/app-data/fluxer-discord-bridge/db"

# Les tokens sont fournis interactivement par le CLI via params.json
# et passés ici en tant que CALEOPE_PARAM_DISCORD_TOKEN, etc.
#
# ⚠️ ORDRE DE PRIORITÉ : paramètre fourni > valeur DÉJÀ EN PLACE > défaut.
# `caleope install --force` rejoue ce script SANS redemander les paramètres :
# écrire `DISCORD_TOKEN=${CALEOPE_PARAM_DISCORD_TOKEN:-}` sans garde VIDE le
# jeton et met le bot en boucle de crash sur MISSING_TOKENS. C'est arrivé le
# 09/08 pendant une simple mise à jour d'image — et le 14/07 sur le bot radio,
# ce qui avait déjà motivé ce motif ailleurs dans le magasin.
SECRETS="${CONFIG_DIR}/secrets.env"
_prev() { [ -f "${SECRETS}" ] && grep "^$1=" "${SECRETS}" 2>/dev/null | head -1 | cut -d= -f2- || true; }
_keep() { # $1=clé  $2=paramètre fourni  $3=défaut
    local cur; cur="$(_prev "$1")"
    if   [ -n "${2:-}" ];   then echo "$1=$2"
    elif [ -n "${cur}" ];   then echo "$1=${cur}"
    else                         echo "$1=${3:-}"
    fi
}

{
    _keep DISCORD_TOKEN "${CALEOPE_PARAM_DISCORD_TOKEN:-}" ""
    _keep FLUXER_TOKEN  "${CALEOPE_PARAM_FLUXER_TOKEN:-}"  ""
    _keep CMD_PREFIX    "${CALEOPE_PARAM_CMD_PREFIX:-}"    "brdg;"
} > "${SECRETS}.new"
mv -f "${SECRETS}.new" "${SECRETS}"
chmod 600 "${SECRETS}"

# Vérification que les tokens sont présents APRÈS préservation : ce qui compte
# est ce qui se trouve dans le fichier, pas ce que l'utilisateur vient de saisir.
MISSING=()
[ -z "$(_prev DISCORD_TOKEN)" ] && MISSING+=("DISCORD_TOKEN")
[ -z "$(_prev FLUXER_TOKEN)"  ] && MISSING+=("FLUXER_TOKEN")

if [ ${#MISSING[@]} -gt 0 ]; then
    echo "  ⚠ Tokens manquants : ${MISSING[*]}"
    echo "  Édite ${CONFIG_DIR}/secrets.env"
    echo "  puis : caleope restart ${CALEOPE_APP_ID}"
fi

cat > "${CONFIG_DIR}/post-install.txt" << EOF

  ┌──────────────────────────────────────────────────────────────┐
  │          Fluxer-Discord Bridge — Démarré                     │
  ├──────────────────────────────────────────────────────────────┤
  │  Teste dans Discord ou Fluxer :                              │
  │    ${CALEOPE_PARAM_CMD_PREFIX:-brdg;}help                    │
  │                                                              │
  │  Commandes utiles :                                          │
  │    caleope logs fluxer-discord-bridge                        │
  │    caleope restart fluxer-discord-bridge                     │
  │                                                              │
  │  Config : ${CONFIG_DIR}/secrets.env  │
  └──────────────────────────────────────────────────────────────┘
EOF

echo "✓ Fluxer-Discord Bridge configuré"
