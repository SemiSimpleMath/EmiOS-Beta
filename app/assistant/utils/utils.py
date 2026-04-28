import json
import unicodedata
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


def normalize_to_ascii(text: str) -> str:
    """
    Convert a string to ASCII by replacing non-ASCII characters with their ASCII equivalents.
    Uses smart replacement for common unicode characters (em dash, quotes, etc.)
    
    Args:
        text (str): Input string that may contain non-ASCII characters
        
    Returns:
        str: ASCII-only string with non-ASCII characters converted or replaced
        
    Examples:
        >>> normalize_to_ascii("Café")
        "Cafe"
        >>> normalize_to_ascii(""Hello"")
        '"Hello"'
        >>> normalize_to_ascii("6–8 words")
        "6-8 words"
        >>> normalize_to_ascii("It's")
        "It's"
    """
    if not isinstance(text, str):
        return str(text)
    
    # First pass: Replace common unicode characters with ASCII equivalents
    # This prevents "6–8" from becoming "68"
    replacements = {
        # Dashes
        '\u2013': '-',  # en dash
        '\u2014': '-',  # em dash
        '\u2015': '-',  # horizontal bar
        '\u2212': '-',  # minus sign
        # Quotes
        '\u2018': "'",  # left single quote
        '\u2019': "'",  # right single quote
        '\u201a': "'",  # single low quote
        '\u201b': "'",  # single high reversed quote
        '\u201c': '"',  # left double quote
        '\u201d': '"',  # right double quote
        '\u201e': '"',  # double low quote
        '\u201f': '"',  # double high reversed quote
        '\u2032': "'",  # prime
        '\u2033': '"',  # double prime
        # Spaces
        '\u00a0': ' ',  # non-breaking space
        '\u2009': ' ',  # thin space
        '\u200a': ' ',  # hair space
        '\u202f': ' ',  # narrow no-break space
        # Other common characters
        '\u2026': '...',  # ellipsis
        '\u00b7': '*',   # middle dot
        '\u2022': '*',   # bullet
        '\u2023': '*',   # triangular bullet
        '\u2043': '-',   # hyphen bullet
        '\u00d7': 'x',   # multiplication sign
        '\u00f7': '/',   # division sign
        '\u2190': '<-',  # leftwards arrow
        '\u2192': '->',  # rightwards arrow
        '\u2194': '<->', # left right arrow
    }
    
    for unicode_char, ascii_char in replacements.items():
        text = text.replace(unicode_char, ascii_char)
    
    # Second pass: Normalize unicode characters (e.g., decompose combined characters like é → e)
    # This handles accented characters: Café → Cafe
    normalized = unicodedata.normalize('NFD', text)
    
    # Third pass: Remove any remaining non-ASCII characters
    # At this point, only truly untranslatable characters remain
    ascii_text = normalized.encode('ascii', 'ignore').decode('ascii')
    
    return ascii_text

