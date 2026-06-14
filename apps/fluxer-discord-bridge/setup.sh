#!/bin/bash
set -euo pipefail

CONFIG_DIR="${CALEOPE_BASE_DIR}/app-config/${CALEOPE_APP_ID}"
mkdir -p "${CONFIG_DIR}"
mkdir -p "${CALEOPE_BASE_DIR}/app-data/fluxer-discord-bridge/db"

# Les tokens sont fournis interactivement par le CLI via params.json
# et passés ici en tant que CALEOPE_PARAM_DISCORD_TOKEN, etc.
cat > "${CONFIG_DIR}/secrets.env" << EOF
DISCORD_TOKEN=${CALEOPE_PARAM_DISCORD_TOKEN:-}
FLUXER_TOKEN=${CALEOPE_PARAM_FLUXER_TOKEN:-}
CMD_PREFIX=${CALEOPE_PARAM_CMD_PREFIX:-brdg;}
EOF
chmod 600 "${CONFIG_DIR}/secrets.env"

# Vérification que les tokens ont bien été fournis
MISSING=()
[ -z "${CALEOPE_PARAM_DISCORD_TOKEN:-}" ] && MISSING+=("DISCORD_TOKEN")
[ -z "${CALEOPE_PARAM_FLUXER_TOKEN:-}"  ] && MISSING+=("FLUXER_TOKEN")

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
