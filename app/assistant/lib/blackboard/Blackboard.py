import threading
from typing import List
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class Blackboard:
    """Shared-state substrate for a manager loop: a stack of scope dicts + a
    message log.

    CONCURRENCY CONTRACT (this class is deliberately lock-free — read before
    changing how an instance is shared):

    - Scope stacks are MANAGER-LOCAL and SINGLE-THREADED. Each MultiAgentManager
      owns its own Blackboard and runs on one thread, so push_call_context /
      pop_call_context / update_state_value on a manager's blackboard never
      race. NEVER call push_call_context / pop_call_context on a blackboard
      shared across threads — the scope stack is not thread-safe and a
      concurrent push/pop would corrupt it.

    - DI.global_blackboard is a SHARED, LOCK-FREE FLAT CACHE. Pipeline threads,
      tools, HTTP routes, and event handlers write resource snapshots and
      messages to it concurrently. dict writes are GIL-atomic (no corruption),
      but same-key update_state_value is last-writer-wins (a concurrent write
      to the same key is silently lost) — fine for cache snapshots, not for
      state that must accumulate. The global instance is NEVER scope-stacked,
      which is what keeps it safe. add_msg's history-id assignment IS locked
      (below) so concurrent add_msg on the shared instance can't collide ids.
    """

    # Serializes the history_id read-modify-write in add_msg so concurrent
    # add_msg on the SHARED global blackboard can't hand two messages the same
    # id or lose an increment (Blackboard audit B2). Class-level: manager-local
    # blackboards are single-threaded so the lock is uncontended there.
    _history_id_lock = threading.Lock()

    def __init__(self):
        """Initialize blackboard with a stack of scopes for state and a global message log."""
        # State is now a stack of dictionaries (scopes).
        # The first scope (index 0) is the global scope.
        self.scopes: List[dict] = [{
            "task": "",
            "information": "",
            "request_id": None,
            "num_cycles": 0,
            "history_next_id": 1,
        }]

        # The call stack is a top-level attribute for managing scopes.
        # Each entry is a tuple: (calling_agent, called_agent, scope_id)
        self.call_stack: List[tuple] = []

        # Messages are a single, global log for the entire task.
        self.messages: List[Message] = []
        self.results = []
        self.tool_results = []
        self.history = []
        self.request_id = None

    def get_messages_for_scope(self, scope_id: str) -> List[Message]:
        """Get all messages that match a specific scope_id."""
        if not scope_id:
            return []
        return [msg for msg in self.messages if msg.scope_id == scope_id]

    def get_task(self):
        return self.get_state_value("task")

    def get_cycles(self):
        return self.get_state_value("num_cycles", 0)


    def reset_blackboard(self):
        """Resets the blackboard to its initial state, including scopes and call stack."""
        # Clear message logs
        self.messages = []
        self.results = []
        self.tool_results = []
        self.history = []
        self.request_id = None
        self.last_agent = None
        self.next_agent = None
        
        # Reset the NEW scope-based system (critical for manager reuse!)
        self.scopes = [{
            "task": "",
            "information": "",
            "discovered_info": [],
            "summary": [],
            "links": {},
            "visited_links": {},
            "final_answer_content": [],
            "checklist": [],
            "progress": [],
            "request_id": None,
            "last_agent": None,
            "num_cycles": 0,
            "final_result": None,
            "exit": False,  # Explicitly clear exit flag for reused manager instances
            "error": False,  # Explicitly clear error flag
            "current_agent": None,
            "history_next_id": 1,
        }]
        
        # Reset the call stack (critical for manager reuse!)
        self.call_stack = []

    def add_request_id(self, request_id):
        # Write BOTH the instance attribute AND the global scope key. The two
        # were a split source of truth: get_request_id() reads the attr (main
        # tool_caller path, chat_task_router), while _tool_caller_util and
        # approval_gateway._resolve_reply_to read get_state_value("request_id")
        # — the scope key, which nothing else ever wrote, so those readers
        # always saw None (the approval gateway's reply-route lookup was dead:
        # None request_id → it never called reply_router). Seeding both makes
        # every reader style converge on the same id (Blackboard audit B1).
        self.request_id = request_id
        self.update_global_state_value("request_id", request_id)

    def get_request_id(self):
        return self.request_id

    def get_messages(self, n=None):
        if n:
            return self.messages[-n:]
        else:
            return self.messages

    def get_all_messages(self):
        return self.messages

    def get_current_scope_id(self) -> str | None:
        """Returns the scope_id from the top of the call stack."""
        if self.call_stack:
            return self.call_stack[-1][2] # (caller, callee, scope_id)
        return None

    def clear_messages(self):
        self.messages = []

    def get_state_value(self, key, default=None):
        """Retrieve a value by searching from the top (local) scope down to global."""
        for scope in reversed(self.scopes):
            if key in scope:
                return scope[key]
        return default

    def update_state_value(self, key, value):
        """Update a value in the CURRENT (top) local scope."""
        self.scopes[-1][key] = value

    def update_global_state_value(self, key, value):
        """Update a value in the GLOBAL (bottom) scope."""
        self.scopes[0][key] = value

    def append_state_value(self, key, value):
        """
        Append semantics for agent outputs.

        - If the stored value is not a list, initialize it to [].
        - If `value` is a list, EXTEND (so append_fields can return list deltas without nesting).
        - Otherwise, APPEND a single item.
        """
        if key not in self.scopes[-1] or not isinstance(self.scopes[-1].get(key), list):
            self.scopes[-1][key] = []
        if isinstance(value, list):
            self.scopes[-1][key].extend(value)
        else:
            self.scopes[-1][key].append(value)

    def append_global_state_value(self, key, value):
        """
        Append semantics for global outputs.

        Behaves like append_state_value, but targets the GLOBAL (bottom) scope.
        """
        if key not in self.scopes[0] or not isinstance(self.scopes[0].get(key), list):
            self.scopes[0][key] = []
        if isinstance(value, list):
            self.scopes[0][key].extend(value)
        else:
            self.scopes[0][key].append(value)

    def add_msg(self, msg: Message):
        """Adds a message to the log, auto-tagging it with the current scope_id."""
        current_scope_id = self.get_current_scope_id()
        if hasattr(msg, 'scope_id') and msg.scope_id is None and current_scope_id:
            msg.scope_id = current_scope_id
        meta = getattr(msg, "metadata", None)
        if not isinstance(meta, dict):
            meta = {}
        history_id = meta.get("history_id")
        if not isinstance(history_id, int) or history_id <= 0:
            # Read-modify-write, serialized: two concurrent add_msg on the
            # SHARED global blackboard must not hand out the same id or lose an
            # increment (audit B2). Uncontended on a manager-local blackboard.
            with self._history_id_lock:
                next_id = int(self.get_state_value("history_next_id", 1) or 1)
                if next_id <= 0:
                    raise ValueError("history_next_id must be > 0")
                meta["history_id"] = next_id
                self.update_global_state_value("history_next_id", next_id + 1)
        # Canonical message lifecycle flags used by summary agent actions.
        if "history_deleted" not in meta:
            meta["history_deleted"] = False
        if "history_hidden" not in meta:
            meta["history_hidden"] = False
        if "history_pinned" not in meta:
            meta["history_pinned"] = False
        if "history_summarized" not in meta:
            meta["history_summarized"] = False
        msg.metadata = meta
        self.messages.append(msg)



    # --- Scope-Aware Call Stack Management ---

    def push_call_context(self, calling_agent: str, called_agent: str, scope_id: str):
        """
        Pushes a new call context (including the scope_id) onto the stack
        and creates a new, empty local scope for the agent being called.
        """
        # The call stack now stores a tuple: (caller, callee, scope_id)
        self.call_stack.append((calling_agent, called_agent, scope_id))

        # A new, empty dictionary is pushed onto the scopes stack, creating the local workspace.
        self.scopes.append({})

        logger.info(f"[Blackboard] Pushed scope '{scope_id}' for call: {calling_agent} -> {called_agent}")
        logger.debug(f"[Blackboard] Call stack size: {len(self.call_stack)}, Scopes size: {len(self.scopes)}")

    def pop_call_context(self):
        """
        Pops the top call context from the stack and destroys the corresponding
        local scope, returning control to the previous agent.
        """
        if self.call_stack:
            # The context and the state scope are removed in a single, atomic operation.
            popped_context = self.call_stack.pop()
            self.scopes.pop()

            scope_id = popped_context[2]
            logger.info(f"[Blackboard] Popped scope '{scope_id}'")
            logger.debug(f"[Blackboard] Call stack size: {len(self.call_stack)}, Scopes size: {len(self.scopes)}")
            return popped_context

        logger.warning("[Blackboard] Attempted to pop from an empty call stack.")
        return None

    def get_current_call_context(self):
        """
        Peeks at the current call context (the top of the stack) without removing it.
        Returns the (caller, callee, scope_id) tuple.
        """
        if self.call_stack:
            return self.call_stack[-1]
        return None


