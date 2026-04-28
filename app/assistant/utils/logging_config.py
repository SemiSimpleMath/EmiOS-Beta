import sys
import os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

import logging
import queue
import threading
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
import platform

log_queue = queue.Queue()
log_listener = None
log_lock = threading.Lock()

# Handlers created inside the queue listener (captured so we can adjust levels at runtime)
_console_handler = None
_file_handler = None

# Runtime logging controls (persisted via UserSettingsManager)
_pending_console_level = None  # used if settings applied before first logger initializes handlers
_baseline_logger_state = {}    # name -> {"level": int, "disabled": bool}
_baseline_console_level = None
_applied_overrides = set()     # logger names we have overridden during this process
_saved_controls_applied_once = False
_current_overrides = {}        # last applied override map (name -> level string)
_current_console_level = None  # last applied console threshold (string)
_console_verbose = False       # when True, console filter is removed (all logs to terminal)


def _apply_override_to_logger(logger_name: str, logger_obj: logging.Logger, level_name: str):
    """
    Apply one override to one logger (without touching others).
    """
    if logger_name not in _baseline_logger_state:
        _baseline_logger_state[logger_name] = {
            "level": logger_obj.level,
            "disabled": bool(getattr(logger_obj, "disabled", False)),
        }

    level = _normalize_level(level_name)
    if level is None:
        logger_obj.disabled = True
    else:
        logger_obj.disabled = False
        logger_obj.setLevel(level)

    _applied_overrides.add(logger_name)

# ========================
# 1. Define Custom Levels
# ========================
MODEL_SYSTEM_LEVEL = 25
MODEL_USER_LEVEL = 26
MODEL_OUTPUT_LEVEL = 27

logging.addLevelName(MODEL_SYSTEM_LEVEL, "MODEL_SYSTEM")
logging.addLevelName(MODEL_USER_LEVEL, "MODEL_USER")
logging.addLevelName(MODEL_OUTPUT_LEVEL, "MODEL_OUTPUT")

class SafeFormatter(logging.Formatter):
    def format(self, record):
        # If the record does not have 'parent_name', set it to a default value.
        if 'parent_name' not in record.__dict__:
            record.__dict__['parent_name'] = "N/A"
        return super().format(record)

def model_system(self, message, *args, **kwargs):
    if self.isEnabledFor(MODEL_SYSTEM_LEVEL):
        self._log(MODEL_SYSTEM_LEVEL, message, args, **kwargs)

def model_user(self, message, *args, **kwargs):
    if self.isEnabledFor(MODEL_USER_LEVEL):
        self._log(MODEL_USER_LEVEL, message, args, **kwargs)

def model_output(self, message, *args, **kwargs):
    if self.isEnabledFor(MODEL_OUTPUT_LEVEL):
        self._log(MODEL_OUTPUT_LEVEL, message, args, **kwargs)

logging.Logger.model_system = model_system
logging.Logger.model_user = model_user
logging.Logger.model_output = model_output

# ========================
# 2. Configure Logger
# ========================
GLOBAL_LOG_LEVEL = logging.DEBUG  # Debug level to see all error messages

# Use SafeFormatter to include %(parent_name)s in our format string.
LOG_FORMATTER = SafeFormatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(parent_name)s - %(message)s'
)

class UnicodeStreamHandler(logging.StreamHandler):
    """Custom StreamHandler that explicitly handles Unicode characters"""
    def __init__(self, stream=None):
        super().__init__(stream)
        self.encoding = 'utf-8'

    def emit(self, record):
        try:
            msg = self.format(record)
            stream = self.stream
            # Write the message in utf-8 encoding
            if hasattr(stream, 'buffer'):
                # For sys.stdout/stderr
                stream.buffer.write((msg + self.terminator).encode(self.encoding))
                stream.buffer.flush()
            else:
                # Fallback
                stream.write(msg + self.terminator)
            self.flush()
        except Exception:
            self.handleError(record)

def ensure_logs_directory():
    """Ensure the logs directory exists"""
    logs_dir = os.path.join(os.getcwd(), 'logs')
    if not os.path.exists(logs_dir):
        os.makedirs(logs_dir)
    return logs_dir

def setup_logger(name, level=None, log_file="project.log"):
    global log_listener, _console_handler, _file_handler, _baseline_console_level, _saved_controls_applied_once

    # Apply persisted logging controls as early as possible (works for test harness too).
    # Safe to call repeatedly; we guard it to run once per process.
    if not _saved_controls_applied_once:
        try:
            apply_saved_logging_controls_from_user_settings()
        finally:
            _saved_controls_applied_once = True

    # Ensure logs directory exists and update log file path
    logs_dir = ensure_logs_directory()
    if not os.path.isabs(log_file):
        # Add process-specific suffix to avoid file locking conflicts when multiple processes run
        # e.g., emi_logs.log -> emi_logs_12345.log (where 12345 is the process ID)
        base_name, ext = os.path.splitext(log_file)
        process_id = os.getpid()
        log_file = os.path.join(logs_dir, f"{base_name}_{process_id}{ext}")

    logger = logging.getLogger(name)
    logger.setLevel(level if level is not None else GLOBAL_LOG_LEVEL)
    logger.propagate = False

    if not logger.handlers:
        queue_handler = QueueHandler(log_queue)
        logger.addHandler(queue_handler)

    with log_lock:
        if log_listener is None:
            # Use larger file size on Windows to avoid rotation issues
            if platform.system() == 'Windows':
                file_handler = RotatingFileHandler(log_file, maxBytes=10485760, backupCount=2, encoding='utf-8', delay=True)
            else:
                file_handler = RotatingFileHandler(log_file, maxBytes=1048576, backupCount=3, encoding='utf-8', delay=True)
            file_handler.setFormatter(LOG_FORMATTER)

            # Agent-only log file — receives only MODEL_SYSTEM / MODEL_USER / MODEL_OUTPUT.
            agent_log_file = log_file.replace(".log", "_agents.log")
            if platform.system() == 'Windows':
                agent_file_handler = RotatingFileHandler(agent_log_file, maxBytes=10485760, backupCount=2, encoding='utf-8', delay=True)
            else:
                agent_file_handler = RotatingFileHandler(agent_log_file, maxBytes=1048576, backupCount=3, encoding='utf-8', delay=True)
            agent_file_handler.setFormatter(LOG_FORMATTER)
            agent_file_handler.addFilter(AgentOnlyFilter())

            console_handler = UnicodeStreamHandler(sys.stdout)
            console_handler.setFormatter(LOG_FORMATTER)
            # Console shows only agent prompts/results by default.
            console_handler.addFilter(AgentOnlyFilter())
            # Apply pending console level (if set before listener initialized)
            if _pending_console_level is not None:
                console_handler.setLevel(_pending_console_level)

            _file_handler = file_handler
            _console_handler = console_handler
            if _baseline_console_level is None:
                _baseline_console_level = console_handler.level

            log_listener = QueueListener(log_queue, file_handler, agent_file_handler, console_handler)
            log_listener.start()
        elif not log_listener._thread.is_alive():
            log_listener.start()

    # IMPORTANT:
    # setup_logger always sets a default level (GLOBAL_LOG_LEVEL). If we have a saved per-logger
    # custom level, apply it again here so it isn't clobbered by the default.
    try:
        override_level = _current_overrides.get(name)
        if override_level:
            _apply_override_to_logger(name, logger, override_level)
    except Exception:
        pass

    return logger


def get_logger(name: str, level=None, log_file="emi_logs.log"):
    """
    Returns a module-specific logger that adheres to the global configuration.
    All log files will be written to the /logs subfolder.
    """
    return setup_logger(name, level, log_file)


# ========================
# 6. Runtime Logging Controls (Dev UI)
# ========================
def _normalize_level(level_name: str):
    """
    Convert a string level to logging level int.
    Special case: "OFF" returns None.
    """
    if level_name is None:
        raise ValueError("level_name cannot be None")

    if isinstance(level_name, int):
        return level_name

    s = str(level_name).strip().upper()
    if s in ("OFF", "DISABLED", "NONE"):
        return None
    if s in ("CRITICAL", "FATAL"):
        return logging.CRITICAL
    if s == "ERROR":
        return logging.ERROR
    if s in ("WARN", "WARNING"):
        return logging.WARNING
    if s == "INFO":
        return logging.INFO
    if s == "DEBUG":
        return logging.DEBUG
    if s == "NOTSET":
        return logging.NOTSET
    raise ValueError(f"Unknown log level: {level_name!r}")


def list_runtime_loggers(prefix: str | None = None):
    """
    Return a snapshot of known runtime loggers.
    Note: This lists loggers that have been created/imported in this process.
    """
    result = []

    # root logger
    root = logging.getLogger()
    result.append({
        "name": "root",
        "level": logging.getLevelName(root.level),
        "effective_level": logging.getLevelName(root.getEffectiveLevel()),
        "disabled": bool(getattr(root, "disabled", False)),
        "propagate": bool(getattr(root, "propagate", False)),
        "has_override": "root" in _applied_overrides,
    })

    logger_dict = getattr(logging.root.manager, "loggerDict", {}) or {}
    for name, obj in logger_dict.items():
        if prefix and not name.startswith(prefix):
            continue

        # obj can be a Logger or a PlaceHolder (for hierarchical names)
        if isinstance(obj, logging.Logger):
            logger = obj
            result.append({
                "name": name,
                "level": logging.getLevelName(logger.level),
                "effective_level": logging.getLevelName(logger.getEffectiveLevel()),
                "disabled": bool(getattr(logger, "disabled", False)),
                "propagate": bool(getattr(logger, "propagate", False)),
                "has_override": name in _applied_overrides,
                "placeholder": False,
            })
            continue

        # Show placeholders too (these can be “invisible” otherwise)
        if isinstance(obj, logging.PlaceHolder):
            result.append({
                "name": name,
                "level": "PLACEHOLDER",
                "effective_level": "PLACEHOLDER",
                "disabled": False,
                "propagate": True,
                "has_override": name in _applied_overrides,
                "placeholder": True,
            })
            continue

        # Unknown type, ignore safely

    # Ensure any overridden logger appears even if it wasn't present yet
    for name in sorted(_applied_overrides):
        if prefix and not name.startswith(prefix):
            continue
        if any(r["name"] == name for r in result):
            continue
        logger = logging.getLogger(name if name != "root" else "")
        result.append({
            "name": name,
            "level": logging.getLevelName(logger.level),
            "effective_level": logging.getLevelName(logger.getEffectiveLevel()),
            "disabled": bool(getattr(logger, "disabled", False)),
            "propagate": bool(getattr(logger, "propagate", False)),
            "has_override": True,
            "placeholder": False,
        })

    # stable ordering for UI
    result.sort(key=lambda x: x["name"])
    return result


def set_console_level(level_name: str):
    """
    Set the console handler threshold at runtime.
    If handlers aren't initialized yet, stores a pending level.
    """
    global _pending_console_level, _baseline_console_level
    level = _normalize_level(level_name)
    if level is None:
        # "OFF" doesn't make sense for handler level; treat as CRITICAL+1
        level = logging.CRITICAL + 1

    with log_lock:
        if _console_handler is None:
            _pending_console_level = level
            return

        if _baseline_console_level is None:
            _baseline_console_level = _console_handler.level

        _console_handler.setLevel(level)


def set_console_verbose(enabled: bool) -> bool:
    """Toggle between agent-only console output and all-logs-to-terminal."""
    global _console_verbose
    with log_lock:
        if _console_handler is None:
            _console_verbose = enabled
            return enabled

        if enabled:
            for f in list(_console_handler.filters):
                if isinstance(f, AgentOnlyFilter):
                    _console_handler.removeFilter(f)
            _console_handler.setLevel(logging.DEBUG)
        else:
            if not any(isinstance(f, AgentOnlyFilter) for f in _console_handler.filters):
                _console_handler.addFilter(AgentOnlyFilter())
            if _baseline_console_level is not None:
                _console_handler.setLevel(_baseline_console_level)

        _console_verbose = enabled
    return enabled


def get_console_verbose() -> bool:
    return _console_verbose


def apply_logger_overrides(overrides: dict):
    """
    Apply per-logger overrides at runtime.

    overrides format:
      { "logger.name": "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL" | "OFF", ... }

    Behavior:
    - "OFF": logger.disabled=True (hard off)
    - Other levels: logger.disabled=False and logger.setLevel(level)
    - Removing an override restores the baseline logger state captured when first overridden
    """
    global _applied_overrides, _current_overrides

    if overrides is None:
        overrides = {}

    # Cache the latest override map for future logger creations
    try:
        _current_overrides = dict(overrides)
    except Exception:
        _current_overrides = {}

    # Restore removed overrides first
    to_restore = [name for name in _applied_overrides if name not in overrides]
    for name in to_restore:
        logger = logging.getLogger(name if name != "root" else "")
        baseline = _baseline_logger_state.get(name)
        if baseline:
            logger.setLevel(baseline["level"])
            logger.disabled = baseline["disabled"]
        else:
            logger.disabled = False
            logger.setLevel(GLOBAL_LOG_LEVEL)
        _applied_overrides.discard(name)

    # Apply current overrides
    for name, level_name in overrides.items():
        logger = logging.getLogger(name if name != "root" else "")
        if name not in _baseline_logger_state:
            _baseline_logger_state[name] = {"level": logger.level, "disabled": bool(getattr(logger, "disabled", False))}

        try:
            _apply_override_to_logger(name, logger, level_name)
        except Exception:
            continue


def apply_logging_controls(console_level: str | None = None, overrides: dict | None = None):
    """
    Convenience: apply both console threshold + logger overrides.
    """
    global _current_console_level
    if console_level:
        _current_console_level = str(console_level).upper()
        set_console_level(console_level)
    if overrides is not None:
        apply_logger_overrides(overrides)


def apply_saved_logging_controls_from_user_settings():
    """
    Load persisted logging controls from UserSettingsManager and apply them.
    Safe to call multiple times.
    """
    try:
        from app.assistant.user_settings_manager.user_settings import get_settings_manager
        settings = get_settings_manager()
        console_level = settings.get("logging.console_level", "WARNING")
        overrides = settings.get("logging.overrides", {}) or {}
        apply_logging_controls(console_level=console_level, overrides=overrides)
    except Exception:
        # Never allow logging UI controls to break app startup
        return


def get_maintenance_logger(name: str, level=None):
    """
    Returns a logger specifically for maintenance tasks.
    Logs only to maintenance.log file (not to console) for cleaner terminal output.
    All maintenance logs will be written to the /logs subfolder.
    """
    logger = logging.getLogger(f"maintenance.{name}")
    logger.setLevel(level if level is not None else GLOBAL_LOG_LEVEL)
    logger.propagate = False
    
    # Only add handler if not already present
    if not logger.handlers:
        logs_dir = ensure_logs_directory()
        process_id = os.getpid()
        log_file = os.path.join(logs_dir, f"maintenance_{process_id}.log")
        
        # Create file handler - on Windows, use larger file size to avoid frequent rotation issues
        # Windows has aggressive file locking that prevents rotation when multiple threads are writing
        if platform.system() == 'Windows':
            # Use 10MB on Windows to reduce rotation frequency (file locking issues)
            file_handler = RotatingFileHandler(
                log_file, 
                maxBytes=10485760,  # 10MB (10x larger to avoid frequent rotation)
                backupCount=2,      # Keep fewer backups
                encoding='utf-8',
                delay=True
            )
        else:
            # Use 1MB on Unix-like systems (no file locking issues)
            file_handler = RotatingFileHandler(
                log_file, 
                maxBytes=1048576,  # 1MB
                backupCount=3, 
                encoding='utf-8',
                delay=True
            )
        file_handler.setFormatter(LOG_FORMATTER)
        logger.addHandler(file_handler)
    
    return logger

# ========================
# 3. Logging Configuration
# ========================
LOGGING_CONFIG = {
    "log_model_system": True,
    "log_model_user": True,
    "log_model_output": True,
    # Optionally, add other event types if needed.
}

# ========================
# 4. Custom Filters
# ========================
class CustomLevelFilter(logging.Filter):
    """
    Filters log records based on the custom logging configuration.
    """
    def __init__(self, config):
        super().__init__()
        self.config = config

    def filter(self, record):
        if record.levelno not in [MODEL_SYSTEM_LEVEL, MODEL_USER_LEVEL, MODEL_OUTPUT_LEVEL]:
            return True  # Allow normal logs
        key = f"log_{record.levelname.lower()}"
        return self.config.get(key, False)

class DefaultParentNameFilter(logging.Filter):
    """
    Ensures that every log record has a 'parent_name' attribute.
    """
    def filter(self, record):
        if not hasattr(record, 'parent_name'):
            record.parent_name = "N/A"
        return True


_AGENT_LOG_LEVELS = frozenset({MODEL_SYSTEM_LEVEL, MODEL_USER_LEVEL, MODEL_OUTPUT_LEVEL})


class AgentOnlyFilter(logging.Filter):
    """
    Passes MODEL_SYSTEM / MODEL_USER / MODEL_OUTPUT records plus
    WARNING and ERROR so that problems surface in the console alongside
    agent prompts/results.
    Everything else (DEBUG / INFO) goes to the main log file only.
    """
    def filter(self, record):
        return record.levelno in _AGENT_LOG_LEVELS or record.levelno >= logging.WARNING


class ExcludeAgentFilter(logging.Filter):
    """
    Inverse of AgentOnlyFilter — passes everything EXCEPT the three
    MODEL_* levels.  Applied to nothing by default but available if
    you ever want a 'system-only' file.
    """
    def filter(self, record):
        return record.levelno not in _AGENT_LOG_LEVELS

# ========================
# 5. Logger Adapter
# ========================
class ContextLoggerAdapter(logging.LoggerAdapter):
    """
    Extends LoggerAdapter to include additional context like parent_name and expose custom logging methods.
    """
    def __init__(self, logger, extra=None):
        if extra is None or not isinstance(extra, dict):
            extra = {}
        extra.setdefault('parent_name', 'N/A')
        super().__init__(logger, extra)

    def process(self, msg, kwargs):
        extra = kwargs.get('extra', {})
        if not isinstance(extra, dict):
            extra = {}
        extra.update(self.extra)
        kwargs['extra'] = extra
        return msg, kwargs

    def model_system(self, message, *args, **kwargs):
        message, kwargs = self.process(message, kwargs)
        self.logger.model_system(message, *args, **kwargs)

    def model_user(self, message, *args, **kwargs):
        message, kwargs = self.process(message, kwargs)
        self.logger.model_user(message, *args, **kwargs)

    def model_output(self, message, *args, **kwargs):
        message, kwargs = self.process(message, kwargs)
        self.logger.model_output(message, *args, **kwargs)



# ========================
# 7. Logging Functions
# ========================
def log_event(logger, event_type, message, extra_context=None):
    """
    Logs an event based on its type.
    """
    level_mapping = {
        "model_system": MODEL_SYSTEM_LEVEL,
        "model_user": MODEL_USER_LEVEL,
        "model_output": MODEL_OUTPUT_LEVEL,
    }
    if event_type in level_mapping:
        getattr(logger, event_type)(message, extra=extra_context)
    else:
        logger.info(message, extra=extra_context)

# ========================
# 8. Example Usage
# ========================
if __name__ == "__main__":
    # Initialize logger and listener *once*, in the correct place
    base_logger = setup_logger("MyProject")

    # Now that the logger has handlers, apply the filter
    custom_filter = CustomLevelFilter(LOGGING_CONFIG)
    for handler in base_logger.handlers:
        handler.addFilter(custom_filter)

    adapter = ContextLoggerAdapter(base_logger, {'parent_name': 'MainModule'})
    log_event(adapter, "model_system", "System level log for debugging.")
    log_event(adapter, "model_user", "User level log for tracking interactions.")
    log_event(adapter, "model_output", "Output level log for model responses.")
    log_standout_text(adapter, "This is standout output.", title="PlannerAgent Output")
    dummy_handler = "<dummy_handler>"
    adapter.info(f"📨 Dispatching message to handler {dummy_handler}")

    print("Logging test complete - Unicode emoji test: 📨 ✅ 🔍")
