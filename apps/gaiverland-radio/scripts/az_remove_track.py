#!/usr/bin/env python3
"""One-shot : recherche et supprime un titre d'AzuraCast par son nom."""
import sys, os, httpx

AZ_URL     = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY     = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))
QUERY      = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else ""

if not QUERY:
    print("Usage: python3 az_remove_track.py <search phrase>")
    sys.exit(1)

if not AZ_KEY:
    print("ERROR: AZURACAST_API_KEY not set")
    sys.exit(1)

headers = {"X-API-Key": AZ_KEY, "Content-Type": "application/json"}


def search(phrase):
    r = httpx.get(
        f"{AZ_URL}/api/station/{AZ_STATION}/files",
        headers=headers,
        params={"searchPhrase": phrase, "rowsPerPage": 50},
        timeout=15,
    )
    if r.status_code != 200:
        print(f"Search error: HTTP {r.status_code} — {r.text[:200]}")
        return []
    data = r.json()
    return data.get("rows", data) if isinstance(data, dict) else data


def delete_track(track):
    tid = track.get("id")
    r = httpx.delete(
        f"{AZ_URL}/api/station/{AZ_STATION}/file/{tid}",
        headers=headers,
        timeout=15,
    )
    return r.status_code in (200, 204)


print(f"Searching AzuraCast for: '{QUERY}'")
rows = search(QUERY)

if not rows:
    print("No matching tracks found.")
    sys.exit(0)

print(f"Found {len(rows)} track(s):")
for t in rows:
    print(f"  [{t.get('id')}] {t.get('artist','?')} — {t.get('title','?')}  ({t.get('path','?')})")

print()
for t in rows:
    if delete_track(t):
        print(f"DELETED: {t.get('artist','?')} — {t.get('title','?')}")
    else:
        print(f"ERROR deleting [{t.get('id')}] {t.get('title','?')}")

print("Done.")
