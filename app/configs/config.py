# config.py
import os

# Import the unified database configuration
try:
    from app.models.base import get_database_uri
except ImportError as _e:
    raise ImportError(
        "Could not import get_database_uri from app.models.base. "
        "The database model module is missing or has an import error. "
        f"Original error: {_e}"
    ) from _e

class Config:
    _secret_key = os.environ.get('FLASK_SECRET_KEY')
    if not _secret_key:
        raise RuntimeError("FLASK_SECRET_KEY environment variable is not set. Set it in your .env file.")
    SECRET_KEY = _secret_key
    OPEN_AI = os.environ.get('OPENAI_API_KEY')
    _jwt_secret = os.environ.get('JWT_SECRET_KEY')
    if not _jwt_secret:
        raise RuntimeError("JWT_SECRET_KEY environment variable is not set. Set it in your .env file.")
    JWT_SECRET_KEY = _jwt_secret

class DevelopmentConfig(Config):
    DEBUG = True
    TEMP_FOLDER_AUDIO = os.path.join('app', 'static', 'temp_audio')
    TEMP_FOLDER_AUDIO_WEB = "temp_audio/"
    AUDIO_FILE_SUFFIX = '.mp3'
    TEMP_FOLDER_IMAGE = os.path.join('app', 'static', 'temp_image')
    # Database configuration - uses unified source of truth
    SQLALCHEMY_DATABASE_URI = get_database_uri()

class TestConfig(Config):
    TESTING = True
    # Database configuration - uses unified source of truth
    SQLALCHEMY_DATABASE_URI = get_database_uri()
    TEMP_FOLDER_AUDIO = os.path.join('app', 'static', 'temp_audio')
    TEMP_FOLDER_AUDIO_WEB = "/static/temp_audio/"
    AUDIO_FILE_SUFFIX = '.mp3'
    TEMP_FOLDER_IMAGE = os.path.join('app', 'static', 'temp_image')

class ProductionConfig(Config):
    DEBUG = False

class HerokuConfig(Config):
    DEBUG = True
    TEMP_FOLDER_AUDIO = os.path.join('app', 'static', 'temp_audio')
    TEMP_FOLDER_AUDIO_WEB = "/static/temp_audio/"
    AUDIO_FILE_SUFFIX = '.mp3'
    TEMP_FOLDER_IMAGE = os.path.join('app', 'static', 'temp_image')
    uri = os.getenv("DATABASE_URL")  # or other relevant config var
    if uri and uri.startswith("postgres://"):
        uri = uri.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = uri