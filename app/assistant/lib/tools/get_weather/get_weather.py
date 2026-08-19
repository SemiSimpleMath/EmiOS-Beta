import json
import os
from typing import Dict

import requests

from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.get_weather.utils import openmeteo
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_resources_dir
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.event_repository.event_repository import EventRepositoryManager

logger = get_logger(__name__)

OPENWEATHER_API_KEY = (os.getenv("OPENWEATHER_API_KEY") or "").strip()


def _get_user_location() -> Dict:
    """Resolve user's city for weather lookup.

    Priority:
    1. resource_current_location.json → current_location (dynamic)
    2. resource_user_data.json → home_city (permanent)
    """
    root = get_resources_dir()

    # 1. Dynamic current location.
    current_loc_path = root / "resource_current_location.json"
    if current_loc_path.exists():
        try:
            loc_data = json.loads(current_loc_path.read_text(encoding="utf-8"))
            loc = loc_data.get("current_location") or {}
            address = loc.get("address") or {}
            city = (address.get("city") or "").strip()
            if city:
                return {
                    "latitude": loc.get("latitude"),
                    "longitude": loc.get("longitude"),
                    "city": city,
                    "country": (address.get("country") or "").strip() or "US",
                }
        except Exception as e:
            logger.error("Error loading current location: %s", e)

    # 2. Permanent home_city.
    user_data_path = root / "user" / "resource_user_data.json"
    if user_data_path.exists():
        try:
            user_data = json.loads(user_data_path.read_text(encoding="utf-8"))
            city = (user_data.get("home_city") or "").strip()
            if city:
                country = (user_data.get("home_country") or "").strip() or "US"
                return {"latitude": None, "longitude": None, "city": city, "country": country}
        except Exception as e:
            logger.error("Error loading user data: %s", e)

    logger.error("No city configured. Set a home city in Settings or the setup wizard.")
    return {"latitude": None, "longitude": None, "city": "", "country": ""}


def _geocode_city(city: str) -> Dict:
    """Geocode a city name to lat/lon via OpenWeatherMap.

    Appends user's home country to disambiguate bare city names.
    Returns dict with latitude, longitude, name, country — or {"error": "..."}.
    """
    if not OPENWEATHER_API_KEY:
        return {"error": "OPENWEATHER_API_KEY not configured. Cannot geocode city."}

    # Append country if bare city name (no comma qualifier).
    if "," not in city:
        user_loc = _get_user_location()
        country = (user_loc.get("country") or "").strip()
        if country:
            city = f"{city},{country}"

    url = f"https://api.openweathermap.org/geo/1.0/direct?q={city}&limit=1&appid={OPENWEATHER_API_KEY}"
    response = requests.get(url, timeout=10)
    if response.status_code != 200:
        return {"error": f"Geocoding HTTP {response.status_code}: {response.text}"}
    data = response.json()
    if not data:
        return {"error": f"City '{city}' not found."}
    return {
        "latitude": data[0]["lat"],
        "longitude": data[0]["lon"],
        "name": data[0].get("name", "Unknown"),
        "country": data[0].get("country", "Unknown"),
    }


class GetWeather(BaseTool):
    def __init__(self):
        super().__init__("get_weather")
        self.repo_manager = EventRepositoryManager()

    def execute(self, tool_message: ToolMessage) -> ToolResult:
        tool_data = tool_message.tool_data.get("arguments", {})
        city = tool_data.get("city")
        forecast_type = tool_data.get("forecast_type", "current")

        user_location = _get_user_location()
        latitude = tool_data.get("latitude", user_location["latitude"])
        longitude = tool_data.get("longitude", user_location["longitude"])

        try:
            # Resolve location.
            if city:
                geo_data = _geocode_city(city)
                if "error" in geo_data:
                    raise ValueError(geo_data["error"])
                latitude, longitude = geo_data["latitude"], geo_data["longitude"]
                location_name = geo_data.get("name", "Unknown")
                country_code = geo_data.get("country", "Unknown")
            else:
                location_name = user_location["city"]
                country_code = user_location["country"]
                if not location_name:
                    raise ValueError("No city configured. Set a home city in Settings or the setup wizard.")
                if latitude is None or longitude is None:
                    geo_data = _geocode_city(f"{location_name},{country_code}")
                    if "error" in geo_data:
                        raise ValueError(geo_data["error"])
                    latitude, longitude = geo_data["latitude"], geo_data["longitude"]
                logger.info("Using location: %s, %s (%s, %s)", location_name, country_code, latitude, longitude)

            # Fetch weather data.
            data_list = []
            if forecast_type == "current":
                weather_data = openmeteo.get_current_weather(latitude, longitude, location_name, country_code)
                data_list.append(weather_data)
            elif forecast_type == "five_day":
                # get_five_day_forecast takes the resolved coordinates and returns entries already
                # shaped for data_list. The old call passed a single query string (TypeError) and
                # unpacked tuples that never existed — the branch had no caller until the
                # morning_briefing recompile hit it (2026-07-12 incident).
                data_list.extend(
                    openmeteo.get_five_day_forecast(latitude, longitude, location_name, country_code)
                )
            else:
                raise ValueError(
                    f"Unknown forecast_type {forecast_type!r} — valid values: 'current', 'five_day'."
                )

            if not data_list:
                raise RuntimeError(f"Weather fetch returned no data (forecast_type={forecast_type}).")

            # Persist only if we have real data.
            self.repo_manager.store_event(
                id="weather",
                event_data={"weather_data": data_list},
                data_type="weather",
            )
            self.repo_manager.sync_events_with_server(["weather"], "weather")
            self._write_weather_resource(data_list, location_name, country_code, latitude, longitude)

            return ToolResult(
                result_type="weather",
                content="Weather data fetched successfully.",
                data_list=data_list,
            )

        except Exception as e:
            logger.error("Weather tool execution failed: %s", e)
            logger.debug("weather tool execution exception details", exc_info=True)
            from app.assistant.lib.core_tools.tool_error_protocol import make_tool_error
            return make_tool_error(
                error_code="weather_fetch_failed",
                message=str(e),
                abort_policy="abort_tool",
                retryable=True,
                user_visible=True,
            )

    def _write_weather_resource(self, data_list: list, location_name: str, country_code: str, latitude, longitude) -> None:
        """Write weather data to resource_weather.json and update in-memory caches."""
        from datetime import datetime, timezone as tz
        from app.assistant.utils.time_utils import utc_to_local

        now_local = utc_to_local(datetime.now(tz.utc))

        weather_resource = {
            "location": {
                "city": location_name,
                "country": country_code,
                "latitude": latitude,
                "longitude": longitude,
            },
            "current": None,
            "forecast": [],
            "last_updated": now_local.strftime("%Y-%m-%d %I:%M %p"),
        }

        for entry in data_list:
            if not isinstance(entry, dict):
                continue
            data_type = entry.get("data_type", "")
            weather_block = entry.get("weather") if isinstance(entry.get("weather"), dict) else {}
            is_current = (
                data_type in ("current", "current_weather")
                or "temperature" in entry
                or (isinstance(weather_block, dict) and "temperature" in weather_block)
            )
            if is_current:
                weather_resource["current"] = {
                    "temperature": entry.get("temperature", weather_block.get("temperature")),
                    "feels_like": entry.get("feels_like", weather_block.get("feels_like")),
                    "humidity": entry.get("humidity", weather_block.get("humidity")),
                    "condition": entry.get(
                        "weather_description",
                        entry.get("condition", weather_block.get("description", "")),
                    ),
                    "wind_speed": entry.get("wind", {}).get("speed") if isinstance(entry.get("wind"), dict) else None,
                }
            elif data_type == "forecast":
                weather_resource["forecast"].append({
                    "date": entry.get("date"),
                    "weather": entry.get("weather"),
                })

        # Write to disk.
        resources_path = get_resources_dir() / "context" / "global" / "resource_weather.json"
        resources_path.parent.mkdir(parents=True, exist_ok=True)
        with open(resources_path, "w", encoding="utf-8") as f:
            json.dump(weather_resource, f, indent=2, ensure_ascii=False)

        # Remove legacy duplicate if present.
        legacy_path = get_resources_dir() / "resource_weather.json"
        if legacy_path.exists() and legacy_path.resolve() != resources_path.resolve():
            legacy_path.unlink()

        # Update in-memory caches so agents see the data immediately.
        DI.global_blackboard.update_state_value("resource_weather", weather_resource)
        if hasattr(DI, "resource_manager") and DI.resource_manager:
            DI.resource_manager.update_resource("resource_weather", weather_resource, persist=False)
        logger.info("Weather resource written: %s", resources_path)


def get_tool_class():
    return GetWeather
