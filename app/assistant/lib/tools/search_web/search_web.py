from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.search_web.utils import web_search
from app.assistant.utils.pydantic_classes import (
    ToolMessage,
    ToolResult
)

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

class SearchWeb(BaseTool):
    """
    Tool to perform web search and return structured results.
    """
    requires_approval = False

    def __init__(self):
        super().__init__('search_web')
        self.agent_factory = None

    def execute(self, tool_message: 'ToolMessage') -> ToolResult:
        """
        Executes the Search tool to perform a web search.

        Parameters:
        - tool_message (ToolMessage): The message triggering the tool execution.

        Returns:
        - ToolResult: Contains search results and extracted links.
        """

        arguments = tool_message.tool_data.get('arguments', {})
        search_query = arguments.get('query')

        if not search_query:
            logger.error("Error: 'query' argument is required for search_web tool.")
            return ToolResult(
                result_type="error",
                content="Error: 'query' argument is required for search_web tool."
            )

        try:
            # Perform the web search
            search_results = web_search.execute_search(search_query)

            if not search_results:
                return ToolResult(
                    result_type="search_web",
                    content="No search results found.",
                    data_list=[]
                )

            logger.info(f"SearchWeb: Received {len(search_results)} search results.")

            # Format all search results into a single structured string
            formatted_results = "\n\n".join(
                [f"Result {idx}:\nTitle: {res.get('title', 'Unknown Title')}\n"
                 f"URL: {res.get('url', 'No URL')}\nSnippet: {res.get('snippet', 'No snippet available.')}"
                 for idx, res in enumerate(search_results, start=1)]
            )

            # Return the structured search results directly to the planner. web_parser (an LLM
            # pre-digest of these same title/url/snippet results) was removed: the planner is itself
            # an LLM that reads/ranks these directly, and the parser only added an extra LLM call per
            # search plus a hardcoded science/space domain bias — without any new information.
            logger.info(f"SearchWeb: returning {len(search_results)} structured results to caller.")
            return ToolResult(
                result_type="search_web",
                content=formatted_results,
                data_list=search_results,
            )

        except Exception as e:
            logger.error(f"Error in execute search_web: {e}")
            return ToolResult(
                result_type="error",
                content=f"Error in execute search_web: {e}"
            )

def get_tool_class():
    """
    Returns an instance of SearchWeb tool.
    This function is required by the tool registry.
    """
    return SearchWeb

# === TEST BLOCK ===
if __name__ == "__main__":
    from app.assistant.ServiceLocator.service_locator import ServiceLocator
    from app.assistant.agent_registry.agent_factory import AgentFactory
    from app.assistant.utils.pydantic_classes import ToolMessage


    # Register dependencies
    ServiceLocator.register("agent_factory", AgentFactory)


    # Create a mock ToolMessage for testing
    mock_message = ToolMessage(
        data_type="tool_request",
        sender="test_user",
        receiver="SearchWebTool",
        task="search web",
        tool_name="search_web",
        tool_data={
            "tool_name": "search_web",
            "arguments": {
                "query": "History of AI development",
                "task": "Find the most detailed timeline of AI advancements."
            }
        }
    )

    # Instantiate the tool and execute the search
    search_web = SearchWeb()
    result = search_web.execute(mock_message)

    # Print results
    print(result.content)
    for entry in result.data_list:
        print(entry)
