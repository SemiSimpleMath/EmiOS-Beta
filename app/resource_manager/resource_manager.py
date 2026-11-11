from pathlib import Path
from typing import Dict, Any, Optional, Callable
from jinja2 import Template, StrictUndefined
import threading

from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.utils.logging_config import get_logger
from app.assistant.utils import resource_files
from app.assistant.utils import scope_gate

logger = get_logger(__name__)


class ResourceManager:
    """
    Manages shared resources backed by files with an in-process cache.

    Files are read, listed and written only through ``resource_files``: the owner's
    file in the user root wins over the shipped template or text in the code root, and
    the owner's ``_personal`` add-on is appended.

    - Load on startup.
    - Read through ResourceManager scoped API.
    - Optional persistent updates from agents (learned preferences).
    """

    def __init__(self):
        # resource_id -> path relative to the resources root
        self._resource_files: Dict[str, Path] = {}
        # resource_id -> concrete current value (source of truth for reads)
        self._resource_values: Dict[str, Any] = {}
        # resource_id -> newest mtime of the resource's files at cache-time.
        # Used for staleness detection: get_resource compares this against the
        # files' current mtime and auto-reloads if newer. Entries with no file
        # backing (synthetic updates) are absent from this map.
        self._cached_mtimes: Dict[str, float] = {}
        # resource_id -> provider fn(scope_dict) -> value (DYNAMIC/computed resources)
        self._resource_providers: Dict[str, Callable[[dict], Any]] = {}
        # resource_id -> requires_scope lock (OPTIONAL; absent = free, shared with skills)
        self._resource_locks: Dict[str, dict] = {}
        self._lock = threading.RLock()

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

    def _set_cached_resource(self, *, resource_id: str, value: Any, rel: Optional[Path]) -> None:
        """Cache a value. ``rel`` names the resource's files for staleness tracking; None
        for a value with no file behind it (an in-memory update)."""
        self._assert_concrete_resource(resource_id, value, rel if rel is not None else f"<update:{resource_id}>")
        with self._lock:
            self._resource_values[resource_id] = value
            if rel is not None and resource_files.exists(rel):
                try:
                    self._cached_mtimes[resource_id] = resource_files.mtime(rel)
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
            rel = self._resource_files.get(resource_id)
            cached_mtime = self._cached_mtimes.get(resource_id)

        if cached_value is None or rel is None or cached_mtime is None:
            return cached_value

        try:
            current_mtime = resource_files.mtime(rel)
        except OSError:
            # File vanished or unreadable — fall back to cached value.
            return cached_value

        if current_mtime <= cached_mtime:
            return cached_value

        # File is newer than what we cached: reload.
        try:
            fresh_value = resource_files.read(rel)
        except Exception as e:
            logger.warning(
                "Resource '%s' mtime advanced but reload failed; keeping stale cached value. error=%s",
                resource_id, e,
            )
            logger.debug("resource reload exception details", exc_info=True)
            return cached_value

        self._set_cached_resource(resource_id=resource_id, value=fresh_value, rel=rel)
        self._publish_to_global_blackboard(resource_id, fresh_value)
        logger.info(
            "Resource '%s' auto-reloaded after file mtime advanced (cached=%.3f current=%.3f)",
            resource_id, cached_mtime, current_mtime,
        )
        return fresh_value

    def _read_resource_from_disk_if_known(self, resource_id: str) -> Any:
        with self._lock:
            rel = self._resource_files.get(resource_id)
        if rel is None:
            return None
        if not resource_files.exists(rel):
            logger.warning("Known resource is missing for '%s': %s", resource_id, rel)
            return None
        value = resource_files.read(rel)
        self._set_cached_resource(resource_id=resource_id, value=value, rel=rel)
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

    def compile_templates(self) -> int:
        """
        Compile Jinja templates under the resources root's ``templates/`` into concrete
        resources.

        Source:  templates/<rel>    (either root, via resource_files)
        Output:  <rel>              (written to the user root)
        """
        template_rels = [
            rel for rel in resource_files.list_resources("templates")
            if not rel.name.startswith(".")
            and (rel.suffix.lower() in (".md", ".txt") or rel.suffix == "")
        ]
        if not template_rels:
            logger.info("No resource templates found. Skipping template compile.")
            return 0

        # Build render context from already-cached concrete resources.
        context: Dict[str, Any] = {}
        with self._lock:
            resource_ids = list(self._resource_files.keys())
        for res_id in resource_ids:
            val = self._resource_values.get(res_id)
            if val is not None:
                context[res_id] = val

        compiled_count = 0
        for template_rel in template_rels:
            output_rel = template_rel.relative_to("templates")
            resource_id = output_rel.stem

            raw_template = resource_files.read_base(template_rel)
            if not isinstance(raw_template, str):
                logger.error("Template '%s' did not load as text.", template_rel)
                raise TypeError(f"Template '{template_rel}' did not load as text.")

            try:
                rendered = Template(raw_template, undefined=StrictUndefined).render(**context)
            except Exception as e:
                logger.error(
                    "Template compile failed for '%s' -> '%s': %s",
                    template_rel,
                    output_rel,
                    e,
                )
                raise

            self._assert_concrete_resource(resource_id, rendered, output_rel)

            # Persist compiled output and update local cache.
            resource_files.write(output_rel, rendered)
            with self._lock:
                self._resource_files[resource_id] = output_rel
            self._set_cached_resource(resource_id=resource_id, value=rendered, rel=output_rel)
            self._publish_to_global_blackboard(resource_id, rendered)
            compiled_count += 1
            logger.info("✅ Compiled template '%s' -> '%s' (%s)", template_rel, output_rel, resource_id)

        logger.info("🧩 Template compile complete: %d compiled", compiled_count)
        return compiled_count

    def load_all(self) -> None:
        """
        Load every resource from the code and user roots (via resource_files) into the
        cache and global_blackboard.

        Uses the filename (without extension) as the resource_id.
        Example: instructions/email_instructions.md -> resource_id: "email_instructions"

        Loads in three phases:
        1. Load all JSON files (context/source data)
        2. Compile templates/* into concrete resources
        3. Load concrete text resources (.md/.txt/no-ext)
        """
        logger.info(
            "🔧 Starting resource loading (code root %s, user root %s)...",
            resource_files.code_root(), resource_files.user_root(),
        )
        loaded_count = 0
        skipped_count = 0

        # Avoid noisy collisions (multiple README.md files in subfolders).
        skip_names = {"README", "README.md", "README.txt"}
        skip_dir_names = {"tmp", "tests", "__pycache__", "templates", "day_context", "pointers"}

        all_rels: list[Path] = []
        for rel in resource_files.list_resources():
            if rel.name.startswith("."):
                continue
            # Skip hidden/ignored directories anywhere in the path
            if any(part.startswith(".") for part in rel.parts):
                continue
            if any(part in skip_dir_names for part in rel.parts):
                continue
            if rel.name in skip_names:
                continue
            all_rels.append(rel)

        def _is_root_level(rel: Path) -> bool:
            return len(rel.parts) == 1

        json_rels: list[Path] = []
        text_rels: list[Path] = []
        # Prefer canonical subfolders over the resources root.
        # This ensures that any regenerated root-level stragglers (old paths) don't win collisions.
        for rel in sorted(all_rels, key=lambda r: (_is_root_level(r), str(r).lower())):
            with self._lock:
                known_rels = set(self._resource_files.values())
            if rel in known_rels:
                skipped_count += 1
                continue

            if rel.suffix.lower() == '.json':
                json_rels.append(rel)
            elif rel.suffix.lower() in ['.md', '.txt'] or rel.suffix == '':
                # Include .md, .txt, and files with no extension as concrete resources
                text_rels.append(rel)

        # PASS 1: Load all JSON files first (these provide context)
        for rel in json_rels:
            resource_id = rel.stem
            try:
                # Prevent accidental overrides when two subfolders contain same stem.
                with self._lock:
                    existing_rel = self._resource_files.get(resource_id)
                if existing_rel and existing_rel != rel:
                    if resource_id == "resource_weather":
                        logger.error(
                            "Duplicate canonical weather resources found: '%s' and '%s'. "
                            "Only one resource_weather.json is allowed.",
                            existing_rel,
                            rel,
                        )
                        raise ValueError(
                            "Duplicate resource_weather.json files found. "
                            "Keep exactly one canonical weather resource file."
                        )
                    # If we previously loaded a root-level straggler, allow the subfolder
                    # canonical file to override it.
                    if _is_root_level(existing_rel) and not _is_root_level(rel):
                        logger.warning(
                            f"⚠️ Duplicate resource_id '{resource_id}': overriding root-level "
                            f"{existing_rel} with canonical {rel}"
                        )
                    else:
                        logger.warning(
                            f"⚠️ Duplicate resource_id '{resource_id}' from {rel}; "
                            f"already loaded from {existing_rel}. Skipping duplicate."
                        )
                        skipped_count += 1
                        continue

                content = resource_files.read(rel)
                with self._lock:
                    self._resource_files[resource_id] = rel
                self._set_cached_resource(resource_id=resource_id, value=content, rel=rel)
                self._publish_to_global_blackboard(resource_id, content)
                loaded_count += 1
                logger.info(f"✅ Loaded JSON resource '{resource_id}' from {resource_files.base_path(rel)}")
            except Exception as e:
                logger.error(f"Failed to load JSON resource '{resource_id}' from {rel}: {e}")
                logger.debug("JSON resource load exception details", exc_info=True)
                skipped_count += 1

        # PHASE 2: Compile templates to concrete resources.
        self.compile_templates()

        # PHASE 3: Load concrete text resources.
        for rel in text_rels:
            resource_id = rel.stem
            try:
                with self._lock:
                    existing_rel = self._resource_files.get(resource_id)
                if existing_rel and existing_rel != rel:
                    if _is_root_level(existing_rel) and not _is_root_level(rel):
                        logger.warning(
                            f"⚠️ Duplicate resource_id '{resource_id}': overriding root-level "
                            f"{existing_rel} with canonical {rel}"
                        )
                    else:
                        logger.warning(
                            f"⚠️ Duplicate resource_id '{resource_id}' from {rel}; "
                            f"already loaded from {existing_rel}. Skipping duplicate."
                        )
                        skipped_count += 1
                        continue

                # The owner's `_personal` add-on, if any, is appended by resource_files.read
                # (later instructions win at LLM read time).
                content = resource_files.read(rel)
                self._assert_concrete_resource(resource_id, content, rel)
                with self._lock:
                    self._resource_files[resource_id] = rel
                self._set_cached_resource(resource_id=resource_id, value=content, rel=rel)
                self._publish_to_global_blackboard(resource_id, content)
                loaded_count += 1
                addon = resource_files.addon_path(rel)
                logger.info(
                    f"✅ Loaded text resource '{resource_id}' from {resource_files.base_path(rel)}"
                    + (f" + add-on {addon}" if addon is not None else "")
                )
            except Exception as e:
                logger.error(f"Failed to load text resource '{resource_id}' from {rel}: {e}")
                logger.debug("Text resource load exception details", exc_info=True)
                skipped_count += 1

        logger.info(f"📦 Resources loading complete: {loaded_count} loaded, {skipped_count} skipped")

        # Memory tag catalog (tag_router) archived — belief engine is the write path now.

    def update_resource(self, resource_id: str, value: Any, persist: bool = True) -> None:
        """
        Update a shared resource both in memory and on disk (the user root).

        Use case:
          email agent learns "ignore chess.com newsletters" and updates
          'email_instructions' or 'newsletter_prefs'.
        """
        rid = str(resource_id or "").strip()
        if not rid:
            raise ValueError("resource_id must be a non-empty string.")
        self._set_cached_resource(resource_id=rid, value=value, rel=None)
        self._publish_to_global_blackboard(rid, value)

        with self._lock:
            rel = self._resource_files.get(rid)
        if persist and rel is not None:
            try:
                written = resource_files.write(rel, value)
                logger.info(f"Persisted shared resource '{rid}' to {written}")
                # After writing, re-anchor the cache to the files' new mtime so
                # get_resource's staleness check can detect FUTURE external
                # overwrites. Without this the cached_mtime stays absent and
                # subsequent third-party writes are invisible to us.
                try:
                    with self._lock:
                        self._cached_mtimes[rid] = resource_files.mtime(rel)
                except OSError:
                    pass
            except Exception as e:
                logger.error(f"Failed to persist resource '{rid}' to {rel}: {e}")
                logger.debug("update_resource persist exception details", exc_info=True)

    def refresh_resource(self, resource_id: str) -> None:
        """
        Reload a resource from disk into the resource manager cache.
        """
        rid = str(resource_id or "").strip()
        if not rid:
            raise ValueError("resource_id must be a non-empty string.")

        with self._lock:
            rel = self._resource_files.get(rid)
        if rel is None:
            candidate = Path(f"{rid}.json")
            if resource_files.exists(candidate):
                rel = candidate
                with self._lock:
                    self._resource_files[rid] = candidate
            else:
                logger.warning(f"Resource file not found for refresh: {rid}")
                return

        try:
            content = resource_files.read(rel)
            self._set_cached_resource(resource_id=rid, value=content, rel=rel)
            self._publish_to_global_blackboard(rid, content)
        except Exception as e:
            logger.error(f"Failed to refresh resource '{rid}' from {rel}: {e}")
            logger.debug("refresh_resource exception details", exc_info=True)
