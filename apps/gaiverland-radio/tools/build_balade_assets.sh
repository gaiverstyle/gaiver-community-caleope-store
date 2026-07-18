#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# build_balade_assets.sh — prépare la galerie « balade Toulon→Hyères » du site.
#
# POURQUOI : les JPG source sont du 4K plein format (~1,3–1,9 Mo pièce). On ne
# sert JAMAIS ça brut sur la home. Ce script produit deux tailles web depuis le
# dossier source et les dépose dans scripts/assets/balade/ (versionné dans le
# store, copié vers l'hôte par setup.sh, monté /app/assets dans le conteneur).
#
#   - assets/balade/<nom>.jpg          → 1920px, q82  (affiché en plein écran / lightbox)
#   - assets/balade/thumbs/<nom>.jpg   →  800px, q75  (vignette de la grille, lazy-load)
#
# QUAND LE RELANCER : à chaque fois que le dossier source change (le chef ajoute
# des recadrages). "Prends tout le dossier tel quel" → on scanne TOUS les *.jpg.
# Idempotent : re-génère tout à chaque passage. Aucune retouche, aucun réglage.
#
# DÉPENDANCE : `sips` (fourni par macOS). Se lance sur le Mac, AVANT le push.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SRC="${1:-/Users/ewen/Documents/03_claude/Projet Radio Gaiverland/photos-balade-toulon}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DST="${HERE}/../scripts/assets/balade"
THUMBS="${DST}/thumbs"

if [ ! -d "${SRC}" ]; then
  echo "✗ Dossier source introuvable : ${SRC}" >&2
  exit 1
fi

# On repart propre : évite de laisser traîner une photo que le chef aurait retirée.
rm -rf "${DST}"
mkdir -p "${DST}" "${THUMBS}"

shopt -s nullglob nocaseglob
count=0
for f in "${SRC}"/*.jpg "${SRC}"/*.jpeg; do
  base="$(basename "$f")"
  name="${base%.*}"
  out="${DST}/${name}.jpg"
  thumb="${THUMBS}/${name}.jpg"

  # Plein format web : borne la plus grande dimension à 1920px, q82.
  sips -Z 1920 -s format jpeg -s formatOptions 82 "$f" --out "$out" >/dev/null
  # Vignette grille : 800px, q75.
  sips -Z 800  -s format jpeg -s formatOptions 75 "$f" --out "$thumb" >/dev/null

  count=$((count + 1))
  printf '  ✓ %-40s %6sK → %5sK (thumb %4sK)\n' "$base" \
    "$(( ($(stat -f%z "$f")     + 512) / 1024 ))" \
    "$(( ($(stat -f%z "$out")   + 512) / 1024 ))" \
    "$(( ($(stat -f%z "$thumb") + 512) / 1024 ))"
done

echo "✓ ${count} photos optimisées → scripts/assets/balade/ (+ thumbs/)"
[ "${count}" -gt 0 ] || { echo "✗ Aucune photo trouvée dans ${SRC}" >&2; exit 1; }
