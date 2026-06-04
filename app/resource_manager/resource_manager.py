import json
from pathlib import Path
from typing import Dict, Any, Optional, Callable
from jinja2 import Template, StrictUndefined
import threading
import tempfile
import os

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils.path_utils import get_repo_root
from app.assistant.utils import scope_gate

logger = get_logger(__name__)


class ResourceManager:
    """
    Manages shared resources backed by files with an in-process cache.

    - Load on startup from config.
    - Read through ResourceManager scoped API.
    - Optional persistent updates from agents (learned preferences).
    """

    def __init__(self, base_dir: Optional[Path] = None):
        # Base directory for relative resource paths
        self.base_dir = base_dir or get_repo_root()
        # resource_id -> absolute file path
        self._resource_files: Dict[str, Path] = {}
        # resource_id -> concrete current value (source of truth for reads)
        self._resource_values: Dict[str, Any] = {}
        # resource_id -> st_mtime (float) of the backing file at cache-time.
        # Used for staleness detection: get_resource compares this against the
        # file's current mtime and auto-reloads if newer. Entries with no file
        # backing (synthetic updates) are absent from this map.
        self._cached_mtimes: Dict[str, float] = {}
        # resource_id -> provider fn(scope_dict) -> value (DYNAMIC/computed resources)
        self._resource_providers: Dict[str, Callable[[dict], Any]] = {}
        # resource_id -> requires_scope lock (OPTIONAL; absent = free, shared with skills)
        self._resource_locks: Dict[str, dict] = {}
        self._lock = threading.RLock()
        self._file_locks: Dict[Path, threading.RLock] = {}

    def _get_file_lock(self, path: Path) -> threading.RLock:
        with self._lock:
            lock = self._file_locks.get(path)
            if lock is None:
                lock = threading.RLock()
                self._file_locks[path] = lock
            return lock

    @staticmethod
    def _has_template_tokens(value: Any) -> bool:
        if not isinstance(value, str):
            return False
        return ("{{" in value) or ("{%" in value)

    def _assert_concrete_resource(self, resource_id: str, value: Any, source_path: Path | str) -> None:
        """
        Enforce contract: injectable resources must be concrete values.
        """
        if self._has_template_tokens(value):
            logger.error(
                "Resource '%s' from %s contains template tokens. "
                "Templates must live under resources/templates and be compiled first.",
                resource_id,
                source_path,
            )
            raise ValueError(f"Resource '{resource_id}' is not concrete.")

    @staticmethod
    def _publish_to_global_blackboard(resource_id: str, value: Any) -> None:
        """
        Best-effort mirror for legacy readers.
        ResourceManager cache remains the source of truth.
        """
        try:
            global_bb = getattr(DI, "global_blackboard", None)
            if global_bb is None:
                return
            global_bb.update_state_value(resource_id, value)
        except Exception as e:
            logger.error("Failed mirroring resource '%s' to global_blackboard: %s", resource_id, e)
            logger.debug("resource mirror to global blackboard exception details", exc_info=True)

    def _set_cached_resource(self, *, resource_id: str, value: Any, source_path: Path | str) -> None:
        self._assert_concrete_resource(resource_id, value, source_path)
        with self._lock:
            self._resource_values[resource_id] = value
            # Record the file mtime at cache-time so get_resource can detect
            # external modifications. Synthetic source_paths (strings like
            # "<update:X>") are not real files — skip mtime tracking for them.
            if isinstance(source_path, Path) and source_path.exists():
                try:
                    self._cached_mtimes[resource_id] = source_path.stat().st_mtime
                except OSError:
                    self._cached_mtimes.pop(resource_id, None)
            else:
                self._cached_mtimes.pop(resource_id, None)

    def _get_cached_resource(self, resource_id: str) -> Any:
        """Return the cached value, auto-reloading if the backing file is newer.

        Staleness check: if the file's current mtime exceeds the cached mtime,
        re-read from disk and update the cache. This eliminates the class of
        bugs where a pipeline writes a resource file directly (bypassing
        update_resource) and agents keep reading the boot-time cached copy.

        Cost is one ``os.stat()`` per read — fast (microseconds) and scoped
        only to resources that have a known file path. Resources updated in
        memory (via update_resource with persist=False) skip the staleness
        check entirely.
        """
        with self._lock:
            cached_value = self._resource_values.get(resource_id)
            path = self._resource_files.get(resource_id)
            cached_mtime = self._cached_mtimes.get(resource_id)

        if cached_value is None or path is None or cached_mtime is None:
            return cached_value

        try:
            current_mtime = path.stat().st_mtime
        except OSError:
            # File vanished or unreadable — fall back to cached value.
            return cached_value

        if current_mtime <= cached_mtime:
            return cached_value

        # File is newer than what we cached: reload.
        try:
            fresh_value = self._read_file(path)
        except Exception as e:
            logger.warning(
                "Resource '%s' mtime advanced but reload failed; keeping stale cached value. error=%s",
                resource_id, e,
            )
            logger.debug("resource reload exception details", exc_info=True)
            return cached_value

        self._set_cached_resource(resource_id=resource_id, value=fresh_value, source_path=path)
        self._publish_to_global_blackboard(resource_id, fresh_value)
        logger.info(
            "Resource '%s' auto-reloaded after file mtime advanced (cached=%.3f current=%.3f)",
            resource_id, cached_mtime, current_mtime,
        )
        return fresh_value

    def _read_resource_from_disk_if_known(self, resource_id: str) -> Any:
        with self._lock:
            path = self._resource_files.get(resource_id)
        if path is None:
            return None
        if not path.exists():
            logger.warning("Known resource path is missing for '%s': %s", resource_id, path)
            return None
        value = self._read_file(path)
        self._set_cached_resource(resource_id=resource_id, value=value, source_path=path)
        self._publish_to_global_blackboard(resource_id, value)
        return value

    @staticmethod
    def _normalize_scope_context(scope_context: Any) -> dict:
        if hasattr(scope_context, "model_dump"):
            scope_context = scope_context.model_dump()
        if not isinstance(scope_context, dict):
            raise ValueError("scope_context must be a dict-like object.")
        return dict(scope_context)

    @staticmethod
    def _resolve_scope_resource_policy(scope_context: dict) -> tuple[set[str], set[str]]:
        resources = scope_context.get("resources")
        if not isinstance(resources, dict):
            raise ValueError("scope_context.resources must be an object.")

        allowed_raw = resources.get("allowed_global_resources")
        denied_raw = resources.get("denied_resources")
        if not isinstance(allowed_raw, list):
            raise ValueError("scope_context.resources.allowed_global_resources must be a list.")
        if denied_raw is None:
            denied_raw = []
        if not isinstance(denied_raw, list):
            raise ValueError("scope_context.resources.denied_resources must be a list when provided.")

        allowed = {str(x).strip() for x in allowed_raw if isinstance(x, str) and str(x).strip()}
        denied = {str(x).strip() for x in denied_raw if isinstance(x, str) and str(x).strip()}
        return allowed, denied

    def register_provider(self, resource_id: str, provider: Callable[[dict], Any], *,
                          requires_scope: Optional[dict] = None) -> None:
        """Register a DYNAMIC resource: its value is COMPUTED per read by
        `provider(scope_dict)` rather than read from a file. Optionally lock it with
        a `requires_scope` (the same shape skills use). Both ride the standard
        scope-gated `get_resource` path."""
        rid = str(resource_id or "").strip()
        if not rid:
            raise ValueError("resource_id must be a non-empty string.")
        self._resource_providers[rid] = provider
        if requires_scope:
            self._resource_locks[rid] = dict(requires_scope)
        logger.info("Registered dynamic resource provider '%s' (locked=%s)", rid, bool(requires_scope))

    def register_lock(self, resource_id: str, requires_scope: dict) -> None:
        """Attach a `requires_scope` lock to an existing (file-backed) resource —
        no lock = free; with a lock the scope must satisfy it (shared scope-gate)."""
        rid = str(resource_id or "").strip()
        if not rid:
            raise ValueError("resource_id must be a non-empty string.")
        if requires_scope:
            self._resource_locks[rid] = dict(requires_scope)

    def get_resource(self, *, scope_context: Any, resource_id: str, required: bool = False) -> Any:
        rid = str(resource_id or "").strip()
        if not rid:
            raise ValueError("resource_id must be a non-empty string.")

        scope_dict = self._normalize_scope_context(scope_context)
        allowed, denied = self._resolve_scope_resource_policy(scope_dict)
        if rid in denied:
            raise PermissionError(f"Resource '{rid}' is explicitly denied by scope policy.")
        if "all" not in allowed and rid not in allowed:
            logger.warning(
                "Resource '%s' blocked by scope policy. allowed_global_resources=%r (scope keys=%r)",
                rid, sorted(allowed), sorted(scope_dict.keys()),
            )
            raise PermissionError(f"Resource '{rid}' is not allowed by scope policy.")

        # Lock-optional thing-side gate, shared with skills. No lock = free.
        lock = self._resource_locks.get(rid)
        if lock and not scope_gate.scope_gate_passes(lock, scope_gate.scope_identity(scope=scope_dict)):
            raise PermissionError(f"Resource '{rid}' requires a scope its lock does not match.")

        # Value: a registered provider COMPUTES it from the scope (dynamic
        # resource); otherwise read the file-backed cache/disk.
        provider = self._resource_providers.get(rid)
        if provider is not None:
            value = provider(scope_dict)
        else:
            value = self._get_cached_resource(rid)
            if value is None:
                try:
                    value = self._read_resource_from_disk_if_known(rid)
                except Exception as e:
                    logger.error("Failed lazy-loading resource '%s' from disk: %s", rid, e)
                    logger.debug("resource_manager lazy-load exception details", exc_info=True)
                    raise

        if required and value is None:
            raise ValueError(f"Required resource '{rid}' is missing.")
        return value

    def get_resources(self, *, scope_context: Any, resource_ids: list[str], required: bool = False) -> Dict[str, Any]:
        if not isinstance(resource_ids, list):
            raise ValueError("resource_ids must be a list of resource IDs.")
        out: Dict[str, Any] = {}
        for rid in resource_ids:
            if not isinstance(rid, str) or not rid.strip():
                continue
            cleaned = rid.strip()
            out[cleaned] = self.get_resource(scope_context=scope_context, resource_id=cleaned, required=required)
        return out

    def compile_templates(self, resources_dir: Optional[str] = "resources") -> int:
        """
        Compile Jinja templates from resources/templates into concrete resources.

        Source:  resources/templates/**/<name>.<ext>
        Output:  resources/**/<name>.<ext>   (same relative path without 'templates/')
        """
        resources_path = (self.base_dir / (resources_dir or "resources")).resolve()
        templates_root = resources_path / "templates"
        if not templates_root.exists():
            logger.info("No templates directory found at %s. Skipping template compile.", templates_root)
            return 0
        if not templates_root.is_dir():
            logger.error("Templates root is not a directory: %s", templates_root)
            raise RuntimeError(f"Templates root is not a directory: {templates_root}")

        # Build render context from already-cached concrete resources.
        context: Dict[str, Any] = {}
        with self._lock:
            resource_ids = list(self._resource_files.keys())
        for res_id in resource_ids:
            val = self._resource_values.get(res_id)
            if val is not None:
                context[res_id] = val

        compiled_count = 0
        for template_path in templates_root.rglob("*"):
            if not template_path.is_file():
                continue
            if template_path.name.startswith("."):
                continue
            if template_path.suffix.lower() not in [".md", ".txt"] and template_path.suffix != "":
                continue

            rel = template_path.relative_to(templates_root)
            output_path = (resources_path / rel).resolve()
            resource_id = output_path.stem

            raw_template = self._read_file(template_path)
            if not isinstance(raw_template, str):
                logger.error("Template '%s' did not load as text.", template_path)
                raise TypeError(f"Template '{template_path}' did not load as text.")

            try:
                rendered = Template(raw_template, undefined=StrictUndefined).render(**context)
            except Exception as e:
                logger.error(
                    "Template compile failed for '%s' -> '%s': %s",
                    template_path,
                    output_path,
                    e,
                )
                raise

            self._assert_concrete_resource(resource_id, rendered, output_path)

            # Persist compiled output and update local cache.
            self._write_file(output_path, rendered)
            self._set_cached_resource(resource_id=resource_id, value=rendered, source_path=output_path)
            with self._lock:
                self._resource_files[resource_id] = output_path
            self._publish_to_global_blackboard(resource_id, rendered)
            compiled_count += 1
            logger.info("✅ Compiled template '%s' -> '%s' (%s)", template_path.name, output_path.name, resource_id)

        logger.info("🧩 Template compile complete: %d compiled", compiled_count)
        return compiled_count

    def load_from_config(self, config: Dict[str, Any]) -> None:
        """
        Config shape example:

        shared_resources:
          email_instructions: "resources/email_instructions.md"
          newsletter_prefs: "resources/newsletter_prefs.json"
        """
        shared = config.get("shared_resources", {})
        if not shared:
            return

        for resource_id, rel_path in shared.items():
            path = (self.base_dir / rel_path).resolve()
            with self._lock:
                self._resource_files[resource_id] = path

            try:
                if not path.exists():
                    logger.error(
                        "Resource file missing for '%s': %s — agents using this resource will receive None.",
                        resource_id, path,
                    )
                    self._set_cached_resource(resource_id=resource_id, value=None, source_path=path)
                    self._publish_to_global_blackboard(resource_id, None)
                    continue

                content = self._read_file(path)
                self._set_cached_resource(resource_id=resource_id, value=content, source_path=path)
                self._publish_to_global_blackboard(resource_id, content)
                logger.info(f"Loaded shared resource '{resource_id}' from {path}")
            except Exception as e:
                logger.error(
                    "Failed to load resource '%s' from %s: %s — agents using this resource will receive None.",
                    resource_id, path, e, exc_info=True,
                )
                self._set_cached_resource(resource_id=resource_id, value=None, source_path=path)
                self._publish_to_global_blackboard(resource_id, None)

    def _read_file(self, path: Path) -> Any:
        """Read file and return content - JSON as dict, text files as strings."""
        if path.suffix.lower() in [".json"]:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        else:
            with path.open("r", encoding="utf-8") as f:
                return f.read()

    def _write_file(self, path: Path, value: Any) -> None:
        # Atomic write + per-file lock to prevent concurrent writers from corrupting files.
        lock = self._get_file_lock(path)
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    if path.suffix.lower() in [".json"]:
                        json.dump(value, f, indent=2, ensure_ascii=False)
                    else:
                        if not isinstance(value, str):
                            value = str(value)
                        f.write(value)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_name, str(path))
            finally:
                try:
                    if os.path.exists(tmp_name):
                        os.remove(tmp_name)
                except Exception:
                    pass

    def load_all_from_directory(self, resources_dir: Optional[str] = "resources") -> None:
        """
        Automatically load all files from a resources directory into global_blackboard.
        
        Uses the filename (without extension) as the resource_id.
        Example: resources/email_instructions.md -> resource_id: "email_instructions"
        
        Loads in three phases:
        1. Load all JSON files (context/source data)
        2. Compile resources/templates/* into concrete resources/*
        3. Load concrete text resources (.md/.txt/no-ext)
        
        Args:
            resources_dir: Relative path to resources directory (default: "resources")
        """
        logger.info(f"🔧 Starting resource loading from '{resources_dir}'...")

        resources_path = (self.base_dir / resources_dir).resolve()
        logger.info(f"   Resources path: {resources_path}")
        logger.info(f"   Path exists: {resources_path.exists()}")
        
        if not resources_path.exists():
            logger.error(f"❌ Resources directory does not exist: {resources_path}")
            return
        
        logger.info(f"   Is directory: {resources_path.is_dir()}")
        if not resources_path.is_dir():
            logger.error(f"❌ Resources path is not a directory: {resources_path}")
            return

        logger.info(f"📂 Scanning resources directory (recursive): {resources_path}")
        loaded_count = 0
        skipped_count = 0

        # Collect files by type
        json_files = []
        text_files = []

        # Avoid noisy collisions (multiple README.md files in subfolders).
        skip_names = {"README", "README.md", "README.txt"}
        skip_dir_names = {"tmp", "tests", "__pycache__", "templates", "day_context", "pointers"}
        
        all_files: list[Path] = []
        for file_path in resources_path.rglob("*"):
            try:
                if not file_path.is_file():
                    continue
                if file_path.name.startswith("."):
                    continue
                # Skip hidden/ignored directories anywhere in the path
                if any(part.startswith(".") for part in file_path.parts):
                    continue
                if any(part in skip_dir_names for part in file_path.parts):
                    continue
                if file_path.name in skip_names:
                    continue
                # Personal overlays (*_personal.md / *_personal.txt) are NOT
                # standalone resources — they're appended onto their public
                # base by the text-resource loader (see PHASE 3 below). The
                # convention lets users keep personal preferences out of the
                # public repo (gitignore *_personal.md) while still benefiting
                # from a shared template as the baseline.
                if file_path.stem.endswith("_personal"):
                    continue
                all_files.append(file_path)
            except Exception as e:
                logger.error("Failed while scanning resource file candidate '%s': %s", file_path, e)
                logger.debug("resource scan exception details", exc_info=True)
                continue

        def _is_root_level(p: Path) -> bool:
            try:
                rel = p.relative_to(resources_path)
                return len(rel.parts) == 1
            except Exception:
                return False

        # Prefer canonical subfolders over resources/ root.
        # This ensures that any regenerated root-level stragglers (old paths) don't win collisions.
        for file_path in sorted(all_files, key=lambda p: (_is_root_level(p), str(p).lower())):
            
            with self._lock:
                known_values = set(self._resource_files.values())
            if file_path in known_values:
                skipped_count += 1
                continue

            if file_path.suffix.lower() == '.json':
                json_files.append(file_path)
            elif file_path.suffix.lower() in ['.md', '.txt'] or file_path.suffix == '':
                # Include .md, .txt, and files with no extension as concrete resources
                text_files.append(file_path)

        # PASS 1: Load all JSON files first (these provide context)
        for file_path in json_files:
            resource_id = file_path.stem
            try:
                # Prevent accidental overrides when two subfolders contain same stem.
                with self._lock:
                    existing_path = self._resource_files.get(resource_id)
                if existing_path and existing_path.resolve() != file_path.resolve():
                    if resource_id == "resource_weather":
                        logger.error(
                            "Duplicate canonical weather resources found: '%s' and '%s'. "
                            "Only one resource_weather.json is allowed.",
                            existing_path,
                            file_path,
                        )
                        raise ValueError(
                            "Duplicate resource_weather.json files found. "
                            "Keep exactly one canonical weather resource file."
                        )
                    # If we previously loaded a root-level straggler, allow the subfolder
                    # canonical file to override it.
                    if _is_root_level(existing_path) and not _is_root_level(file_path):
                        logger.warning(
                            f"⚠️ Duplicate resource_id '{resource_id}': overriding root-level "
                            f"{existing_path} with canonical {file_path}"
                        )
                    else:
                        logger.warning(
                            f"⚠️ Duplicate resource_id '{resource_id}' from {file_path}; "
                            f"already loaded from {existing_path}. Skipping duplicate."
                        )
                        skipped_count += 1
                        continue

                content = self._read_file(file_path)
                self._set_cached_resource(resource_id=resource_id, value=content, source_path=file_path)
                with self._lock:
                    self._resource_files[resource_id] = file_path
                self._publish_to_global_blackboard(resource_id, content)
                loaded_count += 1
                
                # Debug: Show what we're storing for user preference files
                if resource_id.startswith('resource_user_'):
                    if isinstance(content, str):
                        preview = content[:150].replace('\n', ' ')
                        logger.info(f"✅ Loaded JSON resource '{resource_id}' as MARKDOWN ({len(content)} chars): {preview}...")
                    else:
                        logger.info(f"✅ Loaded JSON resource '{resource_id}' as DICT with keys: {list(content.keys()) if isinstance(content, dict) else type(content)}")
                else:
                    logger.info(f"✅ Loaded JSON resource '{resource_id}' from {file_path.name}")
            except Exception as e:
                logger.error(f"Failed to load JSON resource '{resource_id}' from {file_path}: {e}")
                logger.debug("JSON resource load exception details", exc_info=True)
                skipped_count += 1

        # PHASE 2: Compile templates to concrete resources.
        self.compile_templates(resources_dir=resources_dir)

        # PHASE 3: Load concrete text resources.
        for file_path in text_files:
            resource_id = file_path.stem
            try:
                with self._lock:
                    existing_path = self._resource_files.get(resource_id)
                if existing_path and existing_path.resolve() != file_path.resolve():
                    if _is_root_level(existing_path) and not _is_root_level(file_path):
                        logger.warning(
                            f"⚠️ Duplicate resource_id '{resource_id}': overriding root-level "
                            f"{existing_path} with canonical {file_path}"
                        )
                    else:
                        logger.warning(
                            f"⚠️ Duplicate resource_id '{resource_id}' from {file_path}; "
                            f"already loaded from {existing_path}. Skipping duplicate."
                        )
                        skipped_count += 1
                        continue

                content = self._read_file(file_path)
                self._assert_concrete_resource(resource_id, content, file_path)

                # Personal overlay: append `<stem>_personal<suffix>` if a
                # sibling exists. Lets users keep personal preferences out
                # of the public repo (gitignore *_personal.md) while
                # preserving the public template as a safe baseline. The
                # personal content goes AFTER the base — later instructions
                # win at LLM read time, so personal directives override the
                # template defaults without having to delete from the
                # public file.
                personal_path = file_path.with_name(
                    f"{file_path.stem}_personal{file_path.suffix}"
                )
                if personal_path.exists() and personal_path.is_file():
                    try:
                        personal = self._read_file(personal_path)
                        if isinstance(content, str) and isinstance(personal, str) and personal.strip():
                            content = content.rstrip() + "\n\n" + personal.lstrip()
                            logger.info(
                                f"🔧 Applied personal overlay to '{resource_id}' "
                                f"from {personal_path.name} (+{len(personal)} chars)"
                            )
                    except Exception as e:
                        logger.warning(
                            "Personal overlay for '%s' could not be applied: %s",
                            resource_id, e,
                        )
                        logger.debug("personal overlay exception details", exc_info=True)

                self._set_cached_resource(resource_id=resource_id, value=content, source_path=file_path)
                with self._lock:
                    self._resource_files[resource_id] = file_path
                self._publish_to_global_blackboard(resource_id, content)
                loaded_count += 1
                logger.info(f"✅ Loaded text resource '{resource_id}' from {file_path.name}")
            except Exception as e:
                logger.error(f"Failed to load text resource '{resource_id}' from {file_path}: {e}")
                logger.debug("Text resource load exception details", exc_info=True)
                skipped_count += 1

        logger.info(f"📦 Resources loading complete: {loaded_count} loaded, {skipped_count} skipped")

        # Memory tag catalog (tag_router) archived — belief engine is the write path now.

    def update_resource(self, resource_id: str, value: Any, persist: bool = True) -> None:
        """
        Update a shared resource both in memory and on disk.

        Use case:
          email agent learns "ignore chess.com newsletters" and updates
          'email_instructions' or 'newsletter_prefs'.
        """
        rid = str(resource_id or "").strip()
        if not rid:
            raise ValueError("resource_id must be a non-empty string.")
        self._set_cached_resource(resource_id=rid, value=value, source_path=f"<update:{rid}>")
        self._publish_to_global_blackboard(rid, value)

        with self._lock:
            path = self._resource_files.get(rid)
        if persist and path is not None:
            try:
                self._write_file(path, value)
                logger.info(f"Persisted shared resource '{rid}' to {path}")
                # After writing, re-anchor the cache to the file's new mtime so
                # get_resource's staleness check can detect FUTURE external
                # overwrites. Without this the cached_mtime stays absent and
                # subsequent third-party writes are invisible to us.
                try:
                    with self._lock:
                        self._cached_mtimes[rid] = path.stat().st_mtime
                except OSError:
                    pass
            except Exception as e:
                logger.error(f"Failed to persist resource '{rid}' to {path}: {e}")
                logger.debug("update_resource persist exception details", exc_info=True)

    def refresh_resource(self, resource_id: str) -> None:
        """
        Reload a resource from disk into the resource manager cache.
        """
        rid = str(resource_id or "").strip()
        if not rid:
            raise ValueError("resource_id must be a non-empty string.")

        with self._lock:
            path = self._resource_files.get(rid)
        if path is None:
            candidate = (self.base_dir / "resources" / f"{rid}.json").resolve()
            if candidate.exists():
                path = candidate
                with self._lock:
                    self._resource_files[rid] = candidate
            else:
                logger.warning(f"Resource file not found for refresh: {rid}")
                return

        try:
            content = self._read_file(path)
            self._set_cached_resource(resource_id=rid, value=content, source_path=path)
            self._publish_to_global_blackboard(rid, content)
        except Exception as e:
            logger.error(f"Failed to refresh resource '{rid}' from {path}: {e}")
            logger.debug("refresh_resource exception details", exc_info=True)
