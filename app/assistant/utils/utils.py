import json
from datetime import datetime

# Custom JSON Encoder for datetime objects
class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            # Convert datetime object to ISO format string
            return obj.isoformat()
        return super().default(obj)

# Serialize data to JSON string
# json_string = json.dumps(data, cls=DateTimeEncoder, indent=4)
