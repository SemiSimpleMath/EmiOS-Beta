import os
import requests
import json
from typing import List, Dict, Any

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

# API key + CSE id are resolved at CALL time (see execute_search), NOT captured at
# import. The live app loads .env before importing this module, but other entrypoints
# (tests/scenarios) may load .env AFTER import — a module-level capture would bind a
# stale/missing value. GOOGLE_SEARCH_API_KEY lets web search use its own
# Custom-Search-enabled key, distinct from the Gemini GOOGLE_API_KEY.

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
        # 400/403 mean the key is not enabled for Custom Search (or the CSE id is wrong) — a
        # CONFIG error, not a transient one. Surface it clearly and mark it non-retryable: a
        # blocked key can never succeed, so retrying just thrashes the calling agent.
        if response.status_code in (400, 403):
            msg = (f"Google Custom Search rejected the request (HTTP {response.status_code}): the API "
                   f"key is not enabled for Custom Search. Set GOOGLE_SEARCH_API_KEY to a "
                   f"Custom-Search-enabled key (the Gemini GOOGLE_API_KEY is normally blocked for it) "
                   f"and a valid GOOGLE_CSE_ID.")
            logger.error(f"Google search config error: {response.status_code} - {response.text}")
            return {"error": msg, "config_error": True}
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
        # Prefer the dedicated Custom-Search key; also accept GOOGLE_API_KEY for single-key
        # setups where that key is itself Custom-Search-enabled. A missing or blocked key now
        # fails LOUDLY (config_error) below instead of looking like a flaky search.
        api_key = os.getenv('GOOGLE_SEARCH_API_KEY') or os.getenv('GOOGLE_API_KEY')
        cse_id = os.getenv('GOOGLE_CSE_ID')
        if not api_key or not cse_id:
            missing = " and ".join(
                name for name, val in (("GOOGLE_SEARCH_API_KEY", api_key), ("GOOGLE_CSE_ID", cse_id)) if not val
            )
            msg = (f"Web search is not configured: {missing} not set. Set GOOGLE_SEARCH_API_KEY to a "
                   f"Custom-Search-enabled key (distinct from the Gemini GOOGLE_API_KEY) and GOOGLE_CSE_ID.")
            logger.error(msg)
            return {"error": msg, "config_error": True}
        search_results_json = google_search(query, api_key, cse_id, num_results=max_results)

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