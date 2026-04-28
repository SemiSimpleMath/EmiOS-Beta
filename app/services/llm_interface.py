from abc import ABC, abstractmethod
from typing import List, Dict, Any

class LLMProvider(ABC):
    @abstractmethod
    def send_query(self, messages: List[Dict[str, Any]], **send_request) -> Any:
        """Send a query to the LLM and return the response."""
        pass

    @abstractmethod
    def send_function_query(self, messages: List[Dict[str, Any]], **send_request) -> Any:
        """Send a function query to the LLM and return the response."""
        pass

    @abstractmethod
    def build_messages(self, **send_params) -> List[Dict[str, Any]]:
        """Build messages for the LLM query."""
        pass

    @abstractmethod
    def stream_response_to_socket(self, messages: List[Dict[str, Any]], socket_id: str, user_id: str, audio_output: bool, **kwargs) -> None:
        """Stream the LLM response to a socket."""
        pass

    @abstractmethod
    def structured_output(self, messages: List[Dict[str, Any]], **send_params) -> Any:
        """Get structured output from the LLM."""
        pass

    @abstractmethod
    def structured_output_json(self, messages: List[Dict[str, Any]], **send_params) -> Any:
        """Get structured JSON output from the LLM."""
        pass