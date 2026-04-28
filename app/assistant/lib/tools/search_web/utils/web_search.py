import os
import requests
import json
from typing import List, Dict, Any

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

# ---UNCHANGED: These environment variables are still needed---
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')
CSE_ID = os.getenv('GOOGLE_CSE_ID')

# ---UNCHANGED: This function is the core search API call---
def google_search(query: str, api_key: str, cse_id: str, num_results: int = 10) -> Dict[str, Any]:
    """
    Performs a search using the Google Custom Search JSON API.
    """
    url = 'https://customsearch.googleapis.com/customsearch/v1'
    params = {
        'key': api_key,
        'cx': cse_id,
        'q': query,
        'num': min(num_results, 10) # API allows a max of 10 results per call
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            return response.json()
        else:
            logger.error(f"Google search API error: {response.status_code} - {response.text}")
            return {"error": f"Google search failed with status {response.status_code}"}
    except requests.Timeout:
        logger.error("Google search API request timed out.")
        return {"error": "Google search request timed out"}
    except Exception as e:
        logger.error(f"Google search API exception: {e}")
        return {"error": "Google search encountered an exception"}

# ---REFACTORED: This is the new, intelligent search tool function---
def execute_search(query: str, max_results: int = 5) -> Any:
    """
    Executes a Google search and returns a clean list of results for an AI to analyze.
    This function no longer performs any scraping.

    Parameters:
    - query (str): The search query.
    - max_results (int): The number of search results to return (up to 10).

    Returns:
    - List[Dict[str, Any]]: A list of dictionaries, each containing the 'title', 'snippet', and 'url'.
    """
    try:
        logger.info(f"Executing search for query: {query}")
        search_results_json = google_search(query, GOOGLE_API_KEY, CSE_ID, num_results=max_results)

        if "error" in search_results_json:
            logger.error(f"Search failed: {search_results_json.get('error')}")
            return search_results_json

        items = search_results_json.get("items", [])
        if not items:
            logger.warning(f"No search results found for query: {query}")
            return []

        # Process ALL results returned by the API, not just the first one
        all_results = []
        for item in items:
            all_results.append({
                "title": item.get("title", "No Title"),
                "snippet": item.get("snippet", "No Snippet"),
                "url": item.get("link", "#")
            })

        return all_results

    except Exception as e:
        logger.error(f"Error in execute_search: {e}")
        return {"error": f"Error: Failed to execute search due to {e}"}

if __name__ == "__main__":
    test_query = 'Meadow Park Elementary attendance Lakeville'
    results = execute_search(test_query, max_results=5)
    print(json.dumps(results, indent=2))