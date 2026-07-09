"""
Voix Rebexis vs crossfade (Yann).

Le fondu de la station (crossfade) s'applique à TOUTES les transitions, y compris
l'entrée/sortie des annonces vocales de Rebexis → l'intro (et parfois l'outro) de
la voix se fait « manger » par le fondu (rampe de ~2s = premiers mots inaudibles).

Fix : forcer un fondu quasi nul sur les fichiers de la playlist voix (« Rebexis »),
via l'API AzuraCast. La voix entre alors à plein volume dès le premier mot (le
morceau sortant se fond DESSOUS). La musique garde son beatmatch — seule la voix
est protégée.

⚠️ Détails API AzuraCast (vérifiés en prod) :
  - Les fades sont dans l'objet imbriqué `extra_metadata` (PAS à plat) :
      PUT /file/{id}  {"extra_metadata": {"fade_in": x, "fade_out": x}}
  - La valeur EXACTEMENT 0 est interprétée comme « pas d'override » → stockée en
    null → le fichier reprend le fondu par défaut (2s). On utilise donc une valeur
    infime non-nulle (0.1s = imperceptible mais persistée comme override).

Idempotent : ne PUT que les fichiers dont le fondu n'est pas déjà quasi nul.
Appelé au démarrage du scheduler (couvre les nouveaux jingles à chaque boot).
"""
import os
import httpx
import az_utils as A

VOICE_PLAYLIST = os.environ.get("GCS_VOICE_PLAYLIST", "Rebexis")
VOICE_FADE = float(os.environ.get("GCS_VOICE_FADE", "0.1"))  # ~instantané, non-nul


def _is_voice(f: dict) -> bool:
    return any((pl.get("name") == VOICE_PLAYLIST) for pl in (f.get("playlists") or []))


def ensure_voice_no_fade() -> int:
    """Pose un fondu quasi nul sur les fichiers voix qui ne l'ont pas encore.
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
        fi = (f.get("extra_metadata") or {}).get("fade_in")
        if fi is not None and fi <= 0.2:
            continue  # déjà réglé (fondu quasi nul)
        try:
            r = httpx.put(f"{A.AZ_URL}/api/station/{A.AZ_STATION}/file/{f['id']}",
                          headers=A._headers(),
                          json={"extra_metadata": {"fade_in": VOICE_FADE, "fade_out": VOICE_FADE}},
                          timeout=15)
            if r.status_code in (200, 204):
                fixed += 1
        except Exception:
            pass
    if fixed:
        print(f"  ✓ voix Rebexis : fondu quasi nul (fade={VOICE_FADE}s) posé sur {fixed} "
              f"fichier(s) → intro/outro plus mangées par le crossfade")
    return fixed


if __name__ == "__main__":
    ensure_voice_no_fade()
