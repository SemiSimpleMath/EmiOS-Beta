# config.py
import os
 
# Import the unified database configuration
try:
    from app.models.base import get_database_uri
except ImportError:
    # Fallback for when app.models.base is not available
    def get_database_uri():
        if os.environ.get('USE_TEST_DB') == 'true':
            # Test database configuration - prefer explicit TEST_DATABASE_URI_EMI
            test_db_uri = os.environ.get('TEST_DATABASE_URI_EMI')
            if not test_db_uri:
                # Fallback to default test database name
                dev_uri = os.environ.get('DEV_DATABASE_URI_EMI', '')
                test_db_name = os.environ.get('TEST_DB_NAME', 'test_emidb')
                test_db_uri = dev_uri.rsplit('/', 1)[0] + '/' + test_db_name
            return test_db_uri
        else:
            # Production/development database
            return os.environ.get('DEV_DATABASE_URI_EMI')

class Config:
    _secret_key = os.environ.get('FLASK_SECRET_KEY')
    if not _secret_key:
        import secrets as _s
        _secret_key = _s.token_hex(32)
        os.environ['FLASK_SECRET_KEY'] = _secret_key
    SECRET_KEY = _secret_key
    OPEN_AI = os.environ.get('OPENAI_API_KEY')
    # 30 MB upload limit — matches the client-side 25 MB blob cap with headroom for form fields
    MAX_CONTENT_LENGTH = 30 * 1024 * 1024

class DevelopmentConfig(Config):
    DEBUG = True
    # Base directory of the project
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    # Directory where audio files will be temporarily stored (absolute path)
    TEMP_FOLDER_AUDIO = os.path.join(BASE_DIR, 'app', 'static', 'temp_audio')
    # Web-accessible path for audio files (relative to 'static', ensure it ends with a slash)
    TEMP_FOLDER_AUDIO_WEB = 'temp_audio/'
    # Audio file suffix
    AUDIO_FILE_SUFFIX = '.mp3'
    # Directory for temporary images (absolute path)
    TEMP_FOLDER_IMAGE = os.path.join(BASE_DIR, 'app', 'static', 'temp_image')
    # Database configuration - uses unified source of truth
    SQLALCHEMY_DATABASE_URI = get_database_uri()
    # Language Model Provider
    llm_provider = "openai"

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
