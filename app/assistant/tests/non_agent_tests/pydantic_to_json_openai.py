import json
from pydantic import BaseModel

def generate_openai_tool_schema(
        model_class: type[BaseModel],
        tool_name: str | None = None,
        description: str = "",
        dump: bool = True
) -> dict:
    """
    Generates an OpenAI tool/function-compatible schema from a Pydantic model.

    Args:
        model_class: The Pydantic model class
        tool_name: Optional override for the tool name (defaults to class name)
        description: Description of the tool/function
        dump: If True, prints the resulting schema

    Returns:
        A dictionary in OpenAI tool schema format
    """
    if not issubclass(model_class, BaseModel):
        raise TypeError("Expected a Pydantic BaseModel subclass")

    model_schema = model_class.model_json_schema()
    parameters = {
        "type": "object",
        "properties": model_schema.get("properties", {}),
        "required": model_schema.get("required", []),
    }

    tool_schema = {
        "type": "function",
        "function": {
            "name": tool_name or model_class.__name__,
            "description": description,
            "parameters": parameters
        }
    }

    if dump:
        print(json.dumps(tool_schema, indent=2))

    return tool_schema


from pydantic import BaseModel, Field

class WeatherQuery(BaseModel):
    city: str = Field(..., description="City to check weather for")
    units: str = Field("metric", description="Unit system (metric or imperial)")

generate_openai_tool_schema(WeatherQuery, description="Get the weather for a city")
