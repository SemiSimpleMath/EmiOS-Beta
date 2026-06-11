from __future__ import annotations

import importlib


def load_class_from_prefix(prefix: str, class_name: str):
    """Import module ``<prefix>.<class_name>`` and return its ``class_name`` attribute.

    Used by factories whose convention is one module per class, with the
    module named after the class (e.g. ``app.assistant.manager_classes.FooManager``).
    """
    try:
        module = importlib.import_module(f"{prefix}.{class_name}")
        return getattr(module, class_name)
    except Exception as e:
        raise ImportError(f"❌ Could not import class '{class_name}' from '{prefix}': {e}")
