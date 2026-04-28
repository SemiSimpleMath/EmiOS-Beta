"""
Highly Visible Error Logging Utilities
=======================================

Ensures critical errors cannot be missed in logs.
"""

import traceback
from typing import Optional
from app.assistant.utils.logging_config import get_logger

logger = get_logger(__name__)


def log_critical_error(
    message: str,
    exception: Optional[Exception] = None,
    context: Optional[str] = None,
    include_traceback: bool = True
):
    """
    Log an error with a highly visible format that cannot be missed.
    
    Args:
        message: The error message
        exception: Optional exception object
        context: Optional context (e.g., "PhysicalStatusManager._write_sleep_data")
        include_traceback: Whether to include full traceback
    """
    banner = "=" * 80
    stars = "*" * 80
    
    error_lines = [
        "",
        stars,
        stars,
        f"{'ERROR':^80}",
        stars,
        stars,
        banner,
    ]
    
    if context:
        error_lines.append(f"CONTEXT: {context}")
        error_lines.append(banner)
    
    error_lines.append(f"MESSAGE: {message}")
    
    if exception:
        error_lines.append(f"EXCEPTION TYPE: {type(exception).__name__}")
        error_lines.append(f"EXCEPTION: {str(exception)}")
    
    if include_traceback and exception:
        error_lines.append(banner)
        error_lines.append("TRACEBACK:")
        error_lines.append(traceback.format_exc())
    
    error_lines.extend([
        banner,
        stars,
        stars,
        ""
    ])
    
    # Log each line separately to ensure it all gets captured
    for line in error_lines:
        logger.error(line)
    
    # Also print to console for immediate visibility
    print("\n".join(error_lines))


def log_warning_banner(message: str, context: Optional[str] = None):
    """
    Log a warning with a visible banner.
    
    Args:
        message: The warning message
        context: Optional context
    """
    banner = "=" * 80
    warning_lines = [
        "",
        "!" * 80,
        f"{'WARNING':^80}",
        "!" * 80,
        banner,
    ]
    
    if context:
        warning_lines.append(f"CONTEXT: {context}")
        warning_lines.append(banner)
    
    warning_lines.append(f"MESSAGE: {message}")
    warning_lines.extend([
        banner,
        "!" * 80,
        ""
    ])
    
    for line in warning_lines:
        logger.warning(line)
    
    print("\n".join(warning_lines))

