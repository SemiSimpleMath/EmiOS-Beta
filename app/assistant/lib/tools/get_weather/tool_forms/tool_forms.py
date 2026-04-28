from pydantic import BaseModel


class get_weather_args(BaseModel):
    city: str
    forecast_type: str


class get_weather_arguments(BaseModel):
    tool_name: str
    arguments: get_weather_args