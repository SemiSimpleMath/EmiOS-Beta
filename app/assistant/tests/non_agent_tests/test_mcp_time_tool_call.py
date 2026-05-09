import sys
from pathlib import Path

# Ensure repo root is on sys.path (when running directly).
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.assistant.agent_classes.ToolArguments import ToolArguments
from app.assistant.control_nodes.tool_caller import ToolCaller
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
from app.assistant.utils.pydantic_classes import Message
from app.assistant.utils.pipeline_state import set_pending_tool, get_pending_tool


class _StubLLMInterface:
    def __init__(self, result_dict: dict):
        self._result_dict = result_dict

    def structured_output(self, messages, use_json=False, **params):
        return self._result_dict


class _AgentRegistryStub:
    """
    Minimal agent registry stub for ToolArguments + ToolCaller tests.
    """

    def __init__(self, tool_result_handler=None):
        self._tool_result_handler = tool_result_handler

    def get_agent_config(self, name: str):
        # Provide minimal prompt templates used by ToolArguments
        if name == "shared::tool_arguments":
            return {
                "prompts": {
                    "system": "You generate tool arguments.",
                    "user": "{{ tool_description }}\n\n{{ tool_args }}",
                },
                "system_context_items": [],
                "user_context_items": [],
                "llm_params": {"llm_provider": "openai", "engine": "gpt-5-mini", "temperature": 1},
            }
        return {}

    def get_agent_input_form(self, agent_name: str):
        return None

    def get_agent_instance(self, name: str):
        if name == "tool_result_handler":
            return self._tool_result_handler
        return None


class _ToolResultHandlerStub:
    def __init__(self):
        self.last_tool_result = None

    def process_tool_result_direct(self, tool_result=None):
        self.last_tool_result = tool_result
        return


def test_mcp_time_tool_appears_in_tool_descriptions_and_executes():
    bb = Blackboard()

    tool_registry = ToolRegistry()
    tool_registry.load_mcp_servers()

    # Ensure we can inject a fake stdio launch option (no external installs required).
    server_id = "io.modelcontextprotocol/time"
    entry = tool_registry.get_mcp_server_entry(server_id)
    assert entry is not None

    fake_server = Path(__file__).resolve().parent / "fake_mcp_servers" / "fake_time_server.py"
    assert fake_server.exists()

    # Prefer fake server for this test.
    entry["launch_options"] = [
        {
            "id": "test_fake",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(fake_server)],
        }
    ]

    # Write a minimal cache so ToolRegistry can load tool metadata (names + schemas).
    from app.assistant.lib.tool_registry.mcp_tool_cache import write_mcp_tool_cache

    write_mcp_tool_cache(
        server_id,
        tools=[
            {
                "name": "get_current_time",
                "description": "Get current time in a specific timezone.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"timezone": {"type": "string"}},
                    "required": ["timezone"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "convert_time",
                "description": "Convert time between timezones.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "source_timezone": {"type": "string"},
                        "time": {"type": "string"},
                        "target_timezone": {"type": "string"},
                    },
                    "required": ["source_timezone", "time", "target_timezone"],
                    "additionalProperties": False,
                },
            },
        ],
        retrieved_at="2026-02-01T00:00:00Z",
    )

    tool_registry.load_mcp_tool_cache(enabled_only=True)

    namespaced = "mcp::io.modelcontextprotocol/time::get_current_time"
    descs = tool_registry.get_tool_descriptions([namespaced])
    assert namespaced in descs
    assert "current time" in (descs[namespaced] or "").lower()

    # --- ToolArguments agent generates correct arguments (stubbed LLM) ---
    set_pending_tool(bb, name=namespaced, calling_agent="test_agent", action_input=None, arguments=None, kind="tool")

    handler_stub = _ToolResultHandlerStub()
    agent_registry = _AgentRegistryStub(tool_result_handler=handler_stub)
    tool_args_agent = ToolArguments(
        name="shared::tool_arguments",
        blackboard=bb,
        agent_registry=agent_registry,
        tool_registry=tool_registry,
    )
    tool_args_agent.llm_interface = _StubLLMInterface(
        {"tool_name": namespaced, "arguments": {"timezone": "UTC"}}
    )
    tool_args_agent.action_handler(Message(data_type="agent_activation"))

    tool_args = get_pending_tool(bb)
    assert isinstance(tool_args, dict)
    assert tool_args.get("arguments", {}).get("timezone") == "UTC"

    # --- ToolCaller executes MCP tool ---
    tool_caller = ToolCaller(
        name="tool_caller",
        blackboard=bb,
        agent_registry=agent_registry,
        tool_registry=tool_registry,
    )
    tool_caller.action_handler(Message(data_type="agent_activation"))

    tool_result = handler_stub.last_tool_result
    assert tool_result is not None
    assert "2026-01-01" in (tool_result.content or "")

