from typing import List, Dict

import requests
from datetime import datetime

BASE_URL = "https://api.open-meteo.com/v1/forecast"

WEATHER_CODE_DESCRIPTION = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Drizzle: Light intensity",
    53: "Drizzle: Moderate intensity",
    55: "Drizzle: Dense intensity",
    56: "Freezing Drizzle: Light intensity",
    57: "Freezing Drizzle: Dense intensity",
    61: "Rain: Slight intensity",
    63: "Rain: Moderate intensity",
    65: "Rain: Heavy intensity",
    66: "Freezing Rain: Light intensity",
    67: "Freezing Rain: Heavy intensity",
    71: "Snow fall: Slight intensity",
    73: "Snow fall: Moderate intensity",
    75: "Snow fall: Heavy intensity",
    77: "Snow grains",
    80: "Rain showers: Slight intensity",
    81: "Rain showers: Moderate intensity",
    82: "Rain showers: Violent intensity",
    85: "Snow showers: Slight intensity",
    86: "Snow showers: Heavy intensity"
}


def fetch_open_meteo_data(params):
    """Fetch data from the Open-Meteo API."""
    response = requests.get(BASE_URL, params=params)
    if response.status_code == 200:
        return response.json()
    else:
        raise Exception(f"Error {response.status_code}: {response.text}")


def get_current_weather(latitude: float, longitude: float, location_name: str, country_code: str) -> dict:
    """
    Fetch and structure current weather with high/low temperatures from Open-Meteo API.

    Returns a dictionary formatted to match the expected ToolResult structure.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current_weather": True,
        "daily": "temperature_2m_max,temperature_2m_min,sunrise,sunset",
        "temperature_unit": "fahrenheit",
        "timezone": "auto"
    }
    data = fetch_open_meteo_data(params)

    # Extract current weather
    current_weather = data.get("current_weather", {})
    if not current_weather:
        raise Exception("No current weather data available.")

    # Extract daily data for high and low temperatures and sun_times
    daily = data.get("daily", {})
    sunrise = daily.get("sunrise", [None])[0]
    sunset = daily.get("sunset", [None])[0]
    max_temp = daily.get("temperature_2m_max", [None])[0]
    min_temp = daily.get("temperature_2m_min", [None])[0]

    weather_code = current_weather.get("weathercode")
    condition = WEATHER_CODE_DESCRIPTION.get(weather_code, "Unknown condition")

    # Construct the weather object
    weather_object = {
        "data_type": "current_weather",
        "location": {
            "name": location_name,
            "country": country_code,
            "coordinates": {
                "lat": latitude,
                "lon": longitude
            }
        },
        "weather": {
            "temperature": current_weather.get("temperature"),
            "feels_like": None,  # Open-Meteo does not provide 'feels_like' data
            "min_temperature": min_temp,
            "max_temperature": max_temp,
            "description": condition,
            "humidity": None  # Open-Meteo does not provide 'humidity' data
        },
        "wind": {
            "speed": current_weather.get("windspeed"),
            "direction": current_weather.get("winddirection")
        },
        "sun_times": {
            "sunrise": sunrise,
            "sunset": sunset
        }
    }

    return weather_object

def get_five_day_forecast(latitude: float, longitude: float, location_name: str, country_code: str) -> List[Dict]:
    """
    Fetch and structure a 5-day weather forecast from Open-Meteo API.

    Returns a list of dictionaries formatted to match the expected ToolResult data_list structure.

    Args:
        latitude (float): Latitude of the location.
        longitude (float): Longitude of the location.
        location_name (str): Name of the location (e.g., city).
        country_code (str): Country code (e.g., "US").

    Returns:
        List[Dict]: List of structured forecast data for each day.
    """
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "daily": "temperature_2m_max,temperature_2m_min,windspeed_10m_max,winddirection_10m_dominant,weathercode",
        "forecast_days": 5,
        "temperature_unit": "fahrenheit",
        "timezone": "auto"
    }
    data = fetch_open_meteo_data(params)

    # Extract daily data
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    max_temps = daily.get("temperature_2m_max", [])
    min_temps = daily.get("temperature_2m_min", [])
    weather_codes = daily.get("weathercode", [])
    wind_speeds = daily.get("windspeed_10m_max", [])
    wind_directions = daily.get("winddirection_10m_dominant", [])

    if not dates:
        raise Exception("No daily forecast data available.")

    forecast_list = []
    for i in range(len(dates)):
        date = dates[i]
        max_temp = max_temps[i] if i < len(max_temps) else None
        min_temp = min_temps[i] if i < len(min_temps) else None
        weather_code = weather_codes[i] if i < len(weather_codes) else None
        condition = WEATHER_CODE_DESCRIPTION.get(weather_code, "Unknown condition")
        wind_speed = wind_speeds[i] if i < len(wind_speeds) else None
        wind_direction = wind_directions[i] if i < len(wind_directions) else None

        forecast_obj = {
            "data_type": "forecast",
            "date": date,
            "weather": {
                "min_temperature": min_temp,
                "max_temperature": max_temp,
                "description": condition,
                "humidity": None  # Open-Meteo does not provide daily humidity
            },
            "wind": {
                "speed": wind_speed,
                "direction": wind_direction
            }
        }

        forecast_list.append(forecast_obj)

    return forecast_list


if __name__ == "__main__":
    # Example usage
    latitude = 33.6846
    longitude = -117.8265
    location_name = "Lakeville"
    country_code = "US"

    print("\n\nFetching Current Weather with High/Low...")
    try:
        current_weather = get_current_weather(latitude, longitude, location_name, country_code)
        print(current_weather)
    except Exception as e:
        print(f"Error fetching current weather: {e}")
