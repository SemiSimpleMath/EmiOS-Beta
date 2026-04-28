from datetime import datetime
from typing import Any

def convert_datetimes(obj: Any) -> Any:
    """
    Recursively converts all datetime objects in the input to ISO-formatted strings.
    """
    if isinstance(obj, dict):
        return {k: convert_datetimes(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [convert_datetimes(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(convert_datetimes(item) for item in obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    else:
        return obj
