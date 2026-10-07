"""Weather from Open-Meteo (free, no API key): geocoding + current conditions + daily forecast."""

from __future__ import annotations

from typing import Any

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes.
WMO: dict[int, tuple[str, str]] = {
    0: ("שמיים בהירים", "clear sky"),
    1: ("בהיר ברובו", "mainly clear"),
    2: ("מעונן חלקית", "partly cloudy"),
    3: ("מעונן", "overcast"),
    45: ("ערפל", "fog"),
    48: ("ערפל קפוא", "depositing rime fog"),
    51: ("טפטוף קל", "light drizzle"),
    53: ("טפטוף", "drizzle"),
    55: ("טפטוף חזק", "dense drizzle"),
    56: ("טפטוף קפוא", "freezing drizzle"),
    57: ("טפטוף קפוא חזק", "dense freezing drizzle"),
    61: ("גשם קל", "light rain"),
    63: ("גשם", "rain"),
    65: ("גשם חזק", "heavy rain"),
    66: ("גשם קפוא", "freezing rain"),
    67: ("גשם קפוא חזק", "heavy freezing rain"),
    71: ("שלג קל", "light snow"),
    73: ("שלג", "snow"),
    75: ("שלג כבד", "heavy snow"),
    77: ("גרגרי שלג", "snow grains"),
    80: ("ממטרים קלים", "light showers"),
    81: ("ממטרים", "showers"),
    82: ("ממטרים עזים", "violent showers"),
    85: ("ממטרי שלג", "snow showers"),
    86: ("ממטרי שלג כבדים", "heavy snow showers"),
    95: ("סופת רעמים", "thunderstorm"),
    96: ("סופת רעמים עם ברד", "thunderstorm with hail"),
    99: ("סופת רעמים עם ברד כבד", "thunderstorm with heavy hail"),
}


def describe_code(code: int | None, lang: str) -> str:
    he, en = WMO.get(int(code) if code is not None else -1, ("מזג אוויר לא ידוע", "unknown conditions"))
    return he if lang == "he" else en


def geocode_query(name: str, lang: str) -> dict[str, str]:
    return {"name": name.strip(), "count": "1", "language": "he" if lang == "he" else "en", "format": "json"}


def forecast_query(lat: float, lon: float) -> dict[str, str]:
    return {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": "3",
    }


def format_weather(place: dict[str, Any], data: dict[str, Any], when: str, lang: str) -> tuple[str, dict[str, Any]]:
    """Human sentence + structured data for today/tomorrow."""
    name = place.get("name") or ""
    country = place.get("country") or ""
    where = f"{name}, {country}" if country and country != name else name
    current = data.get("current") or {}
    daily = data.get("daily") or {}
    idx = 1 if when == "tomorrow" else 0

    def day(key: str) -> Any:
        values = daily.get(key) or []
        return values[idx] if len(values) > idx else None

    hi, lo = day("temperature_2m_max"), day("temperature_2m_min")
    rain = day("precipitation_probability_max")
    code = day("weather_code")
    info = {
        "place": where, "latitude": place.get("latitude"), "longitude": place.get("longitude"), "when": when,
        "max_c": hi, "min_c": lo, "rain_chance": rain, "conditions": describe_code(code, lang),
        "current": {
            "temperature_c": current.get("temperature_2m"), "feels_like_c": current.get("apparent_temperature"),
            "humidity": current.get("relative_humidity_2m"), "wind_kmh": current.get("wind_speed_10m"),
            "conditions": describe_code(current.get("weather_code"), lang),
        },
    }
    if when == "tomorrow":
        text = (f"מחר ב{where}: {info['conditions']}, {_n(lo)}–{_n(hi)}°C, סיכוי לגשם {_n(rain)}%."
                if lang == "he" else
                f"Tomorrow in {where}: {info['conditions']}, {_n(lo)}–{_n(hi)}°C, {_n(rain)}% chance of rain.")
    else:
        cur = info["current"]
        text = (f"עכשיו ב{where}: {cur['conditions']}, {_n(cur['temperature_c'])}°C (מרגיש כמו {_n(cur['feels_like_c'])}°C). "
                f"היום {_n(lo)}–{_n(hi)}°C, סיכוי לגשם {_n(rain)}%."
                if lang == "he" else
                f"Now in {where}: {cur['conditions']}, {_n(cur['temperature_c'])}°C (feels like "
                f"{_n(cur['feels_like_c'])}°C). Today {_n(lo)}–{_n(hi)}°C, {_n(rain)}% chance of rain.")
    return text, info


def _n(value: Any) -> str:
    if value is None:
        return "?"
    if isinstance(value, float):
        return str(round(value))
    return str(value)
