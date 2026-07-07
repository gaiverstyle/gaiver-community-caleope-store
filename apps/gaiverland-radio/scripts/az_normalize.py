#!/usr/bin/env python3
"""Enable AzuraCast Liquidsoap normalization on the station."""
import sys, os, httpx, json

AZ_URL     = os.environ.get("AZURACAST_URL", "http://azuracast:80")
AZ_KEY     = os.environ.get("AZURACAST_API_KEY", "")
AZ_STATION = int(os.environ.get("AZURACAST_STATION_ID", "1"))
TARGET_DB  = float(sys.argv[1]) if len(sys.argv) > 1 else -14.0

if not AZ_KEY:
    print("ERROR: AZURACAST_API_KEY not set")
    sys.exit(1)

headers = {"X-API-Key": AZ_KEY, "Content-Type": "application/json"}


def get_station():
    r = httpx.get(f"{AZ_URL}/api/admin/stations/{AZ_STATION}",
                  headers=headers, timeout=10)
    if r.status_code != 200:
        print(f"GET station error: HTTP {r.status_code} — {r.text[:300]}")
        return None
    return r.json()


def put_station(payload):
    r = httpx.put(f"{AZ_URL}/api/admin/stations/{AZ_STATION}",
                  headers=headers, json=payload, timeout=15)
    return r.status_code, r.text[:300]


station = get_station()
if not station:
    sys.exit(1)

name = station.get("name", "?")
backend = station.get("backend_config", {})
current_norm = backend.get("normalize_levels", False)
current_db   = backend.get("normalization_level", -14.0)

print(f"Station: {name}")
print(f"Current normalization: {current_norm}  level={current_db} dBFS")

if current_norm and abs(float(current_db) - TARGET_DB) < 0.1:
    print(f"Normalization already enabled at {current_db} dBFS — nothing to do.")
    sys.exit(0)

# Build update payload — send full backend_config to avoid overwriting other settings
backend["normalize_levels"] = True
backend["normalization_level"] = TARGET_DB
station["backend_config"] = backend

status, body = put_station(station)
if status in (200, 201):
    print(f"Normalization ENABLED — target: {TARGET_DB} dBFS")
    print("Liquidsoap will restart automatically.")
else:
    print(f"ERROR HTTP {status}: {body}")
    # Try partial update (some AzuraCast versions only accept changed fields)
    payload = {"backend_config": {"normalize_levels": True,
                                  "normalization_level": TARGET_DB}}
    status2, body2 = put_station(payload)
    if status2 in (200, 201):
        print(f"Normalization ENABLED via partial update — target: {TARGET_DB} dBFS")
    else:
        print(f"Partial update also failed HTTP {status2}: {body2}")
