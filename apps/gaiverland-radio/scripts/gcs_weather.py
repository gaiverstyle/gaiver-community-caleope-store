"""
GCS Weather Service — Priority 6.
Météo réelle via open-meteo.com (gratuit, sans clé API).
Pousse les données au gcs-state-engine toutes les 30 minutes.
Influence uniquement l'ambiance Rebexis — pas la sélection musicale.
"""
import os, sys, subprocess, time, threading

def _install():
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "fastapi", "uvicorn[standard]", "httpx"], check=True)

try:
    import fastapi, uvicorn, httpx
except ImportError:
    _install()
    import fastapi, uvicorn, httpx

from fastapi import FastAPI

STATE_URL     = os.environ.get("GCS_STATE_ENGINE_URL", "http://gcs-state-engine:8091")
GCS_CITY      = os.environ.get("GCS_CITY", "Toulon")
POLL_INTERVAL = int(os.environ.get("GCS_WEATHER_INTERVAL_MIN", "30")) * 60

# City → coordinates. Ajouter de nouvelles villes ici si GCS_CITY change.
CITY_COORDS = {
    "toulon":    (43.1242, 5.9280),
    "marseille": (43.2965, 5.3698),
    "paris":     (48.8566, 2.3522),
    "lyon":      (45.7640, 4.8357),
    "nice":      (43.7102, 7.2620),
    "bordeaux":  (44.8378, -0.5792),
    "toulouse":  (43.6047, 1.4442),
}

# WMO weather codes → condition + weather_mood
WMO_CONDITION = {
    0:  ("clear sky", "sunny"),
    1:  ("mainly clear", "sunny"), 2: ("partly cloudy", "cloudy"), 3: ("overcast", "cloudy"),
    45: ("fog", "cloudy"), 48: ("fog", "cloudy"),
    51: ("drizzle", "rain"), 53: ("drizzle", "rain"), 55: ("drizzle", "rain"),
    61: ("rain", "rain"), 63: ("rain", "rain"), 65: ("heavy rain", "rain"),
    71: ("snow", "cold"), 73: ("snow", "cold"), 75: ("snow", "cold"),
    80: ("rain showers", "rain"), 81: ("rain showers", "rain"), 82: ("violent rain", "storm"),
    95: ("thunderstorm", "storm"), 96: ("thunderstorm", "storm"), 99: ("thunderstorm", "storm"),
}

def wmo_to_condition(code: int) -> tuple[str, str]:
    return WMO_CONDITION.get(code, ("unknown", "calm"))

def temp_to_feel(temp: float) -> str:
    if temp >= 28:  return "hot"
    if temp >= 22:  return "warm"
    if temp >= 12:  return "cool"
    return "cold"

def condition_to_mood(condition: str, temp: float) -> str:
    """Map condition + temperature to the weather_mood values state engine understands."""
    if condition == "storm":    return "storm"
    if condition == "rain":     return "rain"
    if condition == "sunny" and temp >= 22: return "warm"
    if condition == "cold" or temp < 5:     return "cold"
    if temp >= 18:              return "calm"
    return "calm"

app = FastAPI(title="GCS Weather Service")
_last_weather: dict = {}
_last_fetch_ts: float = 0.0


def get_coords(city: str) -> tuple[float, float]:
    return CITY_COORDS.get(city.lower(), CITY_COORDS["toulon"])


def fetch_weather(city: str) -> dict:
    lat, lon = get_coords(city)
    try:
        r = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat, "longitude": lon,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "timezone": "Europe/Paris",
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"  ⚠ weather API HTTP {r.status_code}")
            return {}
        data = r.json().get("current", {})
        temp         = float(data.get("temperature_2m", 20))
        apparent     = float(data.get("apparent_temperature", temp))
        wind         = float(data.get("wind_speed_10m", 0))
        code         = int(data.get("weather_code", 0))
        condition_str, raw_condition = wmo_to_condition(code)
        weather_mood = condition_to_mood(raw_condition, temp)
        feel         = temp_to_feel(apparent)

        # Wind override
        if wind > 40 and weather_mood not in ("storm", "rain"):
            weather_mood = "windy"

        result = {
            "city":         city,
            "temperature":  round(temp, 1),
            "apparent":     round(apparent, 1),
            "wind_kmh":     round(wind, 1),
            "condition":    condition_str,
            "weather_code": code,
            "feel":         feel,
            "weather_mood": weather_mood,
        }
        print(f"  🌤 weather [{city}]: {temp}°C {condition_str} → mood={weather_mood}")
        return result
    except Exception as e:
        print(f"  ⚠ weather fetch: {e}")
        return {}


def push_to_state(weather: dict):
    if not weather:
        return
    try:
        httpx.post(f"{STATE_URL}/state/weather", json={
            "weather_mood": weather["weather_mood"],
            "weather_data": weather,
        }, timeout=5)
    except Exception as e:
        print(f"  ⚠ weather → state: {e}")


def weather_loop():
    global _last_weather, _last_fetch_ts
    print(f"🌤 GCS Weather — city={GCS_CITY} poll={POLL_INTERVAL//60}min")
    while True:
        weather = fetch_weather(GCS_CITY)
        if weather:
            _last_weather = weather
            _last_fetch_ts = time.time()
            push_to_state(weather)
        time.sleep(POLL_INTERVAL)


@app.on_event("startup")
def startup():
    # Fetch immediately on startup
    threading.Thread(target=weather_loop, daemon=True).start()


@app.get("/health")
def health():
    return {
        "status": "ok",
        "city": GCS_CITY,
        "last_weather": _last_weather,
        "last_fetch_ts": _last_fetch_ts,
    }


@app.get("/weather")
def get_weather():
    """Current weather snapshot."""
    return _last_weather or {"status": "not_yet_fetched", "city": GCS_CITY}


@app.post("/weather/refresh")
def force_refresh():
    """Force immediate weather refresh."""
    weather = fetch_weather(GCS_CITY)
    if weather:
        global _last_weather, _last_fetch_ts
        _last_weather = weather
        _last_fetch_ts = time.time()
        push_to_state(weather)
    return weather or {"error": "fetch_failed"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8098)
