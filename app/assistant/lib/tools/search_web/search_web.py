from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.lib.tools.search_web.utils import web_search
from app.assistant.utils.pydantic_classes import (
    ToolMessage,
    Message, ToolResult
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
        task_description = arguments.get('task', "Analyze the following search results for relevant information.")

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

            # Instantiate `web_parser` agent
            web_parser_agent = DI.agent_factory.create_agent('web_parser')

            if web_parser_agent is None:
                logger.error("No web_parser agent found!")
                exit(1)

            # Send the entire formatted search results to the `web_parser` agent, along with the task description
            parse_message = Message(
                agent_input=f"Task: {task_description}\n\nQuery: {search_query}\n\n{formatted_results}"
            )
            parsed_result = web_parser_agent.action_handler(parse_message)

            # Agent.action_handler returns a ToolResult with the actual dict in .data field
            if isinstance(parsed_result, ToolResult):
                result_data = parsed_result.data
                content = result_data.get('content', 'No content returned from web_parser')
                links = result_data.get('links', [])
            elif isinstance(parsed_result, dict):
                # Fallback for dict response
                content = parsed_result.get('content', 'No content returned from web_parser')
                links = parsed_result.get('links', [])
            else:
                # Unexpected type
                content = str(parsed_result)
                links = []
                logger.warning(f"web_parser returned unexpected type: {type(parsed_result)}")

            # Log for debugging
            logger.info(f"web_parser returned content length: {len(content)}, links count: {len(links)}")

            # Return structured search results as ToolResult
            # This will go through tool_result_handler and appear in planner's recent_history
            return ToolResult(
                result_type="search_web",
                content=content,
                data_list=links
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
