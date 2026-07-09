"""
Voix Rebexis vs crossfade (Yann).

Le fondu de la station (crossfade) s'applique à TOUTES les transitions, y compris
l'entrée/sortie des annonces vocales de Rebexis → l'intro (et parfois l'outro) de
la voix se fait « manger » par le fondu du morceau précédent/suivant.

Fix : forcer `fade_in = 0` (et `fade_out = 0`) sur les fichiers de la playlist voix
(« Rebexis ») via l'API AzuraCast. La voix entre alors à plein volume dès le premier
mot (le morceau sortant se fond DESSOUS, la voix reste claire). La musique garde son
beatmatch — seule la voix est exclue du fondu.

Idempotent : ne PUT que les fichiers pas encore réglés. Appelé au démarrage du
scheduler + périodiquement (couvre les nouveaux jingles générés).
"""
import os
import httpx
import az_utils as A

VOICE_PLAYLIST = os.environ.get("GCS_VOICE_PLAYLIST", "Rebexis")


def _is_voice(f: dict) -> bool:
    return any((pl.get("name") == VOICE_PLAYLIST) for pl in (f.get("playlists") or []))


def ensure_voice_no_fade() -> int:
    """Pose fade_in=0/fade_out=0 sur les fichiers voix qui ne l'ont pas encore.
    Retourne le nombre de fichiers corrigés (0 si rien à faire / indisponible)."""
    if not A._ok():
        return 0
    try:
        files = httpx.get(f"{A.AZ_URL}/api/station/{A.AZ_STATION}/files",
                          headers=A._headers(), timeout=30).json()
    except Exception as e:
        print(f"  ⚠ voix fade : liste fichiers KO ({e})")
        return 0
    if not isinstance(files, list):
        return 0
    fixed = 0
    for f in files:
        if not _is_voice(f):
            continue
        if f.get("fade_in") == 0 and f.get("fade_out") == 0:
            continue  # déjà réglé
        try:
            r = httpx.put(f"{A.AZ_URL}/api/station/{A.AZ_STATION}/file/{f['id']}",
                          headers=A._headers(), json={"fade_in": 0, "fade_out": 0}, timeout=15)
            if r.status_code in (200, 204):
                fixed += 1
        except Exception:
            pass
    if fixed:
        print(f"  ✓ voix Rebexis : fade_in/out=0 posé sur {fixed} fichier(s) "
              f"(intro/outro plus mangées par le fondu)")
    return fixed


if __name__ == "__main__":
    ensure_voice_no_fade()
