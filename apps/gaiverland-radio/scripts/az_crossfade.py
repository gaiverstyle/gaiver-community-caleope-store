"""
Beatmatch niveau 2 — câblage audio (Yann).

Active le fondu « smart » de Liquidsoap sur la station AzuraCast, de façon
idempotente. La logique musicale (enchaînement par BPM proche) est faite en amont
par playlist.py (Régis) ; ici on règle uniquement le crossfade intelligent qui
beatmatche les tempos rapprochés. CPU négligeable : c'est un réglage de station,
pas du traitement temps réel.

Réglage via l'API admin AzuraCast (backend_config.crossfade_type / crossfade).
Appelé au démarrage du scheduler ; exécutable seul pour un one-shot.
"""
import os
import httpx
import az_utils as A

# Cibles du fondu. Surchargables par env sans toucher au code.
CROSSFADE_TYPE = os.environ.get("GCS_CROSSFADE_TYPE", "smart")   # smart | normal | none
CROSSFADE_SECS = float(os.environ.get("GCS_CROSSFADE_SECS", "2"))


def ensure_smart_crossfade():
    """
    GET la config backend de la station -> règle le crossfade -> PUT (idempotent).
    Ne touche qu'aux clés crossfade : tout le reste de backend_config est préservé.
    Retourne le backend_config appliqué, ou None si indisponible.
    """
    if not A._ok():
        print("  ⚠ crossfade : pas de clé AzuraCast utilisable, skip")
        return None

    admin = f"{A.AZ_URL}/api/admin/station/{A.AZ_STATION}"
    try:
        r = httpx.get(admin, headers=A._headers(), timeout=A._TIMEOUT)
        if r.status_code != 200:
            print(f"  ⚠ crossfade : GET admin station HTTP {r.status_code} "
                  f"(la clé a-t-elle les droits admin ?)")
            return None

        bc = (r.json() or {}).get("backend_config") or {}
        cur_type = bc.get("crossfade_type", "") or ""
        try:
            cur_secs = float(bc.get("crossfade") or 0)
        except (TypeError, ValueError):
            cur_secs = 0.0

        if cur_type == CROSSFADE_TYPE and abs(cur_secs - CROSSFADE_SECS) < 0.01:
            print(f"  ✓ crossfade déjà réglé : type={cur_type} durée={cur_secs}s")
            return bc

        bc["crossfade_type"] = CROSSFADE_TYPE
        bc["crossfade"] = CROSSFADE_SECS
        r2 = httpx.put(admin, headers=A._headers(),
                       json={"backend_config": bc}, timeout=A._TIMEOUT + 5)
        if r2.status_code not in (200, 204):
            print(f"  ⚠ crossfade : PUT HTTP {r2.status_code} — {r2.text[:160]}")
            return None

        print(f"  ✓ crossfade appliqué : type={CROSSFADE_TYPE} durée={CROSSFADE_SECS}s "
              f"(était type='{cur_type}' {cur_secs}s) — les tempos proches sont "
              f"maintenant beatmatchés par Liquidsoap")
        return bc
    except Exception as e:
        print(f"  ⚠ crossfade : {e}")
        return None


if __name__ == "__main__":
    ensure_smart_crossfade()
