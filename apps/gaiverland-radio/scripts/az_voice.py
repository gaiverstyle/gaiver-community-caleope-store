"""
Voix Rebexis vs crossfade (Yann).

Le fondu de la station s'applique à TOUTES les transitions, y compris l'entrée et la
sortie des annonces de Rebexis. Il faut un réglage ASYMÉTRIQUE :

  - ENTRÉE (fade_in ≈ 0.1s) : la voix arrive quasi à plein volume dès le premier mot,
    donc le morceau sortant ne « mange » pas l'intro.
  - SORTIE (fade_out ≈ 1.5s) : la voix se fond en douceur pendant que la musique
    remonte dessous → PAS de coupe nette à la fin (bug remonté par Cassy/Régis :
    un fade_out trop court = punch abrupt jingle→musique).

Réglé par fichier via l'API AzuraCast, dans l'objet imbriqué `extra_metadata`
(vérifié prod : un PUT à plat est ignoré ; la valeur EXACTEMENT 0 = « pas d'override »
→ null → défaut station, donc on garde une valeur non-nulle pour fade_in).

Idempotent : ne PUT que les fichiers dont fade_in/fade_out ne sont pas déjà réglés.
Appelé au démarrage du scheduler (couvre les nouveaux jingles à chaque boot).
"""
import os
import httpx
import az_utils as A

VOICE_PLAYLIST = os.environ.get("GCS_VOICE_PLAYLIST", "Rebexis")
VOICE_FADE_IN = float(os.environ.get("GCS_VOICE_FADE_IN", "0.1"))    # intro non mangée
VOICE_FADE_OUT = float(os.environ.get("GCS_VOICE_FADE_OUT", "1.5"))  # outro douce vers la musique


def _is_voice(f: dict) -> bool:
    return any((pl.get("name") == VOICE_PLAYLIST) for pl in (f.get("playlists") or []))


def _ok_set(em: dict) -> bool:
    fi, fo = em.get("fade_in"), em.get("fade_out")
    return (fi is not None and fo is not None
            and abs(fi - VOICE_FADE_IN) < 0.05 and abs(fo - VOICE_FADE_OUT) < 0.1)


def ensure_voice_no_fade() -> int:
    """Pose fade_in court + fade_out doux sur les fichiers voix qui ne l'ont pas déjà.
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
        if _ok_set(f.get("extra_metadata") or {}):
            continue  # déjà réglé
        try:
            r = httpx.put(f"{A.AZ_URL}/api/station/{A.AZ_STATION}/file/{f['id']}",
                          headers=A._headers(),
                          json={"extra_metadata": {"fade_in": VOICE_FADE_IN,
                                                   "fade_out": VOICE_FADE_OUT}},
                          timeout=15)
            if r.status_code in (200, 204):
                fixed += 1
        except Exception:
            pass
    if fixed:
        print(f"  ✓ voix Rebexis : fade_in={VOICE_FADE_IN}s / fade_out={VOICE_FADE_OUT}s posé sur "
              f"{fixed} fichier(s) → intro non mangée + outro douce (plus de coupe nette)")
    return fixed


if __name__ == "__main__":
    ensure_voice_no_fade()
