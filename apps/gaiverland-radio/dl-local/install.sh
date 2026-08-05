#!/bin/bash
# Installateur gaiverland-dl — Mac / Linux.
# À relancer sur chaque nouvelle machine : il vérifie python3 + yt-dlp + ffmpeg
# et écrit la configuration (hôte + jeton de la régie).
set -e
cd "$(dirname "$0")"
echo "── gaiverland-dl : installation ──"

command -v python3 >/dev/null || { echo "python3 manquant — installe Python 3 d'abord"; exit 1; }
echo "python3 : $(python3 --version)"

# yt-dlp en module utilisateur. Les Python récents (brew/Debian) refusent pip hors
# venv (PEP 668) → on retente avec le drapeau prévu pour ça.
python3 -m pip install --user -q -U yt-dlp 2>/dev/null \
  || python3 -m pip install --user -q -U --break-system-packages yt-dlp
python3 -c "import yt_dlp" || { echo "installation de yt-dlp impossible"; exit 1; }
echo "yt-dlp : ok"

if command -v ffmpeg >/dev/null; then
  echo "ffmpeg : ok"
else
  case "$(uname -s)" in
    Darwin) echo "⚠ ffmpeg manquant → brew install ffmpeg" ;;
    *)      echo "⚠ ffmpeg manquant → sudo apt install ffmpeg (ou equivalent)" ;;
  esac
fi

if [ ! -f config.json ]; then
  read -r -p "Hôte [https://gaiverland.gaiver-it.fr] : " HOTE
  HOTE=${HOTE:-https://gaiverland.gaiver-it.fr}
  read -r -p "Jeton de la régie (le k= de ton lien /regie) : " JETON
  printf '{"hote": "%s", "jeton": "%s"}\n' "$HOTE" "$JETON" > config.json
  chmod 600 config.json
  echo "config.json écrit (droits 600 — le jeton reste chez toi)"
else
  echo "config.json déjà présent, conservé"
fi
echo
echo "Terminé. Utilisation :  python3 gaiverland-dl.py --liste   puis sans option pour tout traiter."
