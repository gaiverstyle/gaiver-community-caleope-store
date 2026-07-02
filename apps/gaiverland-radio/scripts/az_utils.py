"""
Client AzuraCast 0.23.x — utilisé par analyzer, tts_worker, scheduler.
"""
import os, httpx
from typing import Optional

AZ_URL = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))
_TIMEOUT = 15


def _headers():
    return {"X-API-Key": AZ_KEY, "Content-Type": "application/json"}


def _ok():
    return bool(AZ_KEY) and AZ_KEY not in ("", "__CONFIGURE__")


def get_station() -> Optional[dict]:
    if not _ok():
        return None
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}", headers=_headers(), timeout=_TIMEOUT)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def list_playlists() -> list:
    if not _ok():
        return []
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/playlists", headers=_headers(), timeout=_TIMEOUT)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def create_playlist(name: str, pl_type: str = "default", weight: int = 3,
                    play_per_songs: int = 0) -> Optional[dict]:
    """Crée une playlist AzuraCast et retourne son objet."""
    if not _ok():
        return None
    body = {"name": name, "type": pl_type, "weight": weight, "is_enabled": True}
    if pl_type == "once_per_x_songs":
        body["play_per_songs"] = play_per_songs
    try:
        r = httpx.post(f"{AZ_URL}/api/station/{AZ_STATION}/playlists",
                       headers=_headers(), json=body, timeout=_TIMEOUT)
        return r.json() if r.status_code in (200, 201) else None
    except Exception:
        return None


def get_or_create_playlist(name: str, **kwargs) -> Optional[int]:
    """Retourne l'ID d'une playlist existante ou la crée."""
    for pl in list_playlists():
        if pl.get("name") == name:
            return pl["id"]
    pl = create_playlist(name, **kwargs)
    return pl["id"] if pl else None


def set_playlist_order(playlist_id: int, file_ids: list) -> bool:
    """Définit l'ordre de lecture d'une playlist (AzuraCast 0.20+)."""
    if not _ok() or not file_ids:
        return False
    try:
        r = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/playlist/{playlist_id}/order",
                      headers=_headers(), json={"order": file_ids}, timeout=30)
        return r.status_code in (200, 204)
    except Exception:
        return False


def batch_assign_playlist(file_ids: list, playlist_ids: list) -> bool:
    """Assigne des fichiers (az_id) à des playlists (AzuraCast 0.23+).
    Le batch endpoint attend des chemins de fichiers, pas des IDs."""
    if not _ok() or not file_ids:
        return False
    try:
        # 1. Récupérer les chemins correspondant aux IDs
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/files",
                      headers=_headers(), params={"rowsPerPage": 500}, timeout=30)
        if r.status_code != 200:
            return False
        rows = r.json()
        if isinstance(rows, dict):
            rows = rows.get("rows", [])
        id_to_path = {row["id"]: row["path"] for row in rows if "id" in row and "path" in row}
        paths = [id_to_path[fid] for fid in file_ids if fid in id_to_path]
        if not paths:
            return False
        # 2. Batch assign avec chemins + do=playlist
        r2 = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/files/batch",
                       headers=_headers(),
                       json={"do": "playlist", "files": paths,
                             "playlists": [str(pid) for pid in playlist_ids]},
                       timeout=30)
        d = r2.json() if r2.status_code in (200, 201) else {}
        return bool(d.get("success", False))
    except Exception as e:
        print(f"  ⚠ batch_assign_playlist: {e}")
        return False


def find_file_by_path(path: str) -> Optional[dict]:
    """Cherche un fichier AzuraCast par son nom."""
    if not _ok():
        return None
    name = os.path.basename(path)
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/files",
                      headers=_headers(),
                      params={"searchPhrase": name, "pageSize": 5},
                      timeout=_TIMEOUT)
        data = r.json() if r.status_code == 200 else {}
        rows = data.get("rows", data) if isinstance(data, dict) else data
        if rows:
            return rows[0]
        return None
    except Exception:
        return None


def upload_file(local_path: str, title: str) -> Optional[dict]:
    """Upload un fichier audio dans AzuraCast (format JSON base64), retourne l'objet créé."""
    if not _ok():
        return None
    try:
        import base64
        with open(local_path, "rb") as f:
            content_b64 = base64.b64encode(f.read()).decode()
        dest_path = f"gaiverland/{os.path.basename(local_path)}"
        r = httpx.post(
            f"{AZ_URL}/api/station/{AZ_STATION}/files",
            headers={"X-API-Key": AZ_KEY, "Content-Type": "application/json"},
            json={"path": dest_path, "file": content_b64},
            timeout=120,
        )
        return r.json() if r.status_code in (200, 201) else None
    except Exception as e:
        print(f"  ⚠ Upload AzuraCast: {e}")
        return None


def now_playing() -> Optional[dict]:
    """Retourne les infos du morceau en cours."""
    try:
        r = httpx.get(f"{AZ_URL}/api/nowplaying/{AZ_STATION}", timeout=5)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def get_queue(limit: int = 6) -> list:
    """Retourne les prochaines pistes planifiées dans la queue AzuraCast."""
    if not _ok():
        return []
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/queue",
                      headers=_headers(), timeout=5)
        items = r.json() if r.status_code == 200 else []
        return items[:limit] if isinstance(items, list) else []
    except Exception:
        return []


def update_playlist(playlist_id: int, **kwargs) -> bool:
    """Met à jour les propriétés d'une playlist AzuraCast (weight, is_enabled, etc.)."""
    if not _ok():
        return False
    try:
        r = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/playlist/{playlist_id}",
                      headers=_headers(), json=kwargs, timeout=_TIMEOUT)
        return r.status_code < 400
    except Exception as e:
        print(f"  warning update_playlist: {e}")
        return False


def _get_all_files() -> list:
    """Récupère tous les fichiers de la station (max 1000)."""
    try:
        r = httpx.get(f"{AZ_URL}/api/station/{AZ_STATION}/files",
                      headers=_headers(), params={"rowsPerPage": 1000}, timeout=30)
        rows = r.json() if r.status_code == 200 else []
        return rows.get("rows", rows) if isinstance(rows, dict) else rows
    except Exception:
        return []


def replace_playlist(file_ids: list, playlist_id: int,
                     prev_az_ids: list = None) -> bool:
    """
    Remplace le contenu de la playlist par file_ids.
    - Retire prev_az_ids de la playlist via PUT individuel (si fournis)
    - Ajoute les nouveaux via batch assign
    Note: le batch AzuraCast (do=playlist) est additif. Le retrait se fait fichier par fichier.
    """
    if not _ok() or not file_ids:
        return False
    try:
        all_rows = _get_all_files()
        id_to_path = {row["id"]: row["path"] for row in all_rows if "id" in row and "path" in row}
        path_to_playlists = {row["path"]: [p["id"] for p in row.get("playlists", [])] for row in all_rows}

        # Retirer prev_az_ids de la playlist si c'est une rotation
        to_remove = [fid for fid in (prev_az_ids or []) if fid not in file_ids and fid in id_to_path]
        for fid in to_remove[:20]:  # max 20 suppressions par cycle pour limiter la charge
            path = id_to_path[fid]
            current_pls = [p for p in path_to_playlists.get(path, []) if p != playlist_id]
            try:
                httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/file/{fid}",
                          headers=_headers(), json={"playlists": current_pls}, timeout=10)
            except Exception:
                pass

        # Ajouter les nouveaux
        new_paths = [id_to_path[fid] for fid in file_ids if fid in id_to_path]
        if not new_paths:
            return False
        r2 = httpx.put(f"{AZ_URL}/api/station/{AZ_STATION}/files/batch",
                       headers=_headers(),
                       json={"do": "playlist", "files": new_paths,
                             "playlists": [str(playlist_id)]},
                       timeout=30)
        d = r2.json() if r2.status_code in (200, 201) else {}
        return bool(d.get("success", False))
    except Exception as e:
        print(f"  ⚠ replace_playlist: {e}")
        return False
