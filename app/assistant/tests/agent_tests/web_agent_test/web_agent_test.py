import app.assistant.tests.test_setup # This is just run for the import
import time
from app.assistant.lib.blackboard.Blackboard import Blackboard
from app.assistant.ServiceLocator.service_locator import DI
from app.assistant.agent_registry.agent_registry import AgentRegistry
from app.assistant.lib.tool_registry.tool_registry import ToolRegistry
from app.assistant.utils.pydantic_classes import Message
from app.assistant.agent_registry.agent_factory import AgentFactory

user_prompt = """

============================================================
USER PROMPT web::planner
============================================================
Task you have been given: Find the address of the Del Taco locations in or nearest to Irvine, CA. Return the closest one or a few options if they are nearby.
Additional information: User location: Irvine, CA. Search target: Del Taco.

Current accumulated progress_report (append-only; do NOT repeat items already listed here):
[{'kind': 'fact', 'summary': 'Identified official Del Taco site as primary source for store locator.', 'evidence_url': 'https://deltaco.com/', 'confidence': 'high', 'major_impact': False}, {'kind': 'evidence', 'summary': 'Reached Del Taco official locator landing page; need Irvine-specific store pages.', 'evidence_url': 'https://locations.deltaco.com/', 'confidence': 'high', 'major_impact': False}, {'kind': 'partial_answer', 'summary': 'Identified two official Irvine Del Taco addresses from the locator.', 'evidence_url': 'https://locations.deltaco.com/us/ca/irvine/4820-barranca-pkwy', 'confidence': 'high', 'major_impact': False}, {'kind': 'evidence', 'summary': 'Located Irvine locations index page on official Del Taco locator.', 'evidence_url': 'https://locations.deltaco.com/us/ca/irvine', 'confidence': 'high', 'major_impact': False}]
This is our previous checklist that you have to now update. You may also add sub-goals to the checklist. Do not get stuck thinking you must check off every single part. Often main goal can be achieved by abandoning some goals or sub-goals or deciding that a check item has been closely enough satisfied.
['IN PROGRESS: Search for Del Taco locations in Irvine', 'IN PROGRESS: Confirm addresses via official locator or reliable source', 'TODO: List 1-3 closest options with addresses', 'TODO: Stop once credible sources found']
Things that happened since last time you made a plan from oldest to latest. Last thing is most recent tool result that you have to evaluate:
[01] 13:19 - search_web
Args: Calling tool search_web with arguments {"tool_name": "search_web", "arguments": {"query": "Del Taco locations in or near Irvine, CA current addresses official store locator or reliable map listings closest options", "task": "Find current addresses of Del Taco locations in or near Irvine, CA; prefer official store locator or reliable map listings; list closest options."}}
Result ID: 72aa878770634342b95b449beaefe1be
Result: {
    "search_results": "Most relevant result: Del Taco - Home (official site). This is the official Del Taco website and will contain the store locator (recommended for current addresses). The other provided results are not relevant to Del Taco locations in/near Irvine, CA.",
    "sources": [
        {
            "url": "https://deltaco.com/",
            "summary": "Official Del Taco website \u2014 use the site\u2019s Locations / Find a Location (store locator) feature to search for current Del Taco addresses in or near Irvine, CA."
        }
    ]
}
[02] 13:20 - scrape
Args: Calling tool scrape_url with arguments {"tool_name": "scrape_url", "arguments": {"query": "", "url": "https://locations.deltaco.com/"}}
Result ID: 986d732f57d2496988c6c427e613cf9c
Summary: We have the official Del Taco locator page URL but no Irvine-specific addresses yet.
[03] 13:20 - search_web
Args: Calling tool search_web with arguments {"tool_name": "search_web", "arguments": {"query": "Del Taco Irvine", "task": "search"}}
Result ID: 49a64d2faeae4f0e9f0c84162a048b97
Summary: Found two official Del Taco location pages in Irvine with addresses: 4780 Irvine Blvd, Irvine, CA 92620 and 4820 Barranca Pkwy, Irvine, CA 92604. URLs: https://locations.deltaco.com/us/ca/irvine/4780-irvine-blvd and https://locations.deltaco.com/us/ca/irvine/4820-barranca-pkwy .
[04] 13:20 - search_web
Args: Calling tool search_web with arguments {"tool_name": "search_web", "arguments": {"query": "site:locations.deltaco.com Irvine CA Del Taco", "task": "search"}}
Result ID: d204e6e2798646539a54fe222f4ef599
Summary: From search: Official Del Taco site (https://deltaco.com/) and locator (https://locations.deltaco.com/). Found two Irvine store pages with addresses: 1) https://locations.deltaco.com/us/ca/irvine/4820-barranca-pkwy (address noted as 4820 Barranca Pkwy, Irvine, CA 92604) and 2) https://locations.deltaco.com/us/ca/irvine/4780-irvine-blvd (address noted as 4780 Irvine Blvd, Irvine, CA 92620). Also found the Irvine locations index: https://locations.deltaco.com/us/ca/irvine .
[05] 13:21 - scrape
Args: Calling tool scrape_url with arguments {"tool_name": "scrape_url", "arguments": {"query": "address Irvine", "url": "https://locations.deltaco.com/us/ca/irvine"}}
Result ID: 99e8d952841b49d18e9457fd2be23a4f
Result: {
    "scraped_content": "Scraped URL: https://locations.deltaco.com/us/ca/irvine\nQuery: address Irvine\nRelevant content:\n... < See all locations Location > United States > California > Irvine ...\n\nDel Taco Locations in irvine, ca\n\nIrvine 4780 Irvine Blvd. Irvine, California 92620 (714) 838-7975\nIrvine 16211 Lake Forest Dr. Irvine, California 92618 (949) 453-0281\nIrvine 4547 Campus Dr Irvine, California 92612 (949) 854-7102\nIrvine 4820 Barranca Pkwy Irvine, California 92604 (949) 556-4661\nIrvine 59 Technology Drive Irvine, California 92618 (949) 453-1600\n\n...",
    "links": [
        {
            "url": "https://www.deltaco.com/locations",
            "description": "Locations \u2014 likely a page listing Del Taco restaurant locations (context: multiple 'Locations' mentions and the displayed Irvine addresses)."
        },
        {
            "url": "https://www.deltaco.com/menu",
            "description": "Menu \u2014 likely contains the Del Taco food menu (context: 'Menu' in the site navigation)."
        },
        {
            "url": "https://www.deltaco.com/order",
            "description": "Order ahead or delivery \u2014 likely the ordering/delivery page (context: repeated 'Order ahead or delivery' in the header)."
        },
        {
            "url": "https://www.deltaco.com/specials",
            "description": "Specials \u2014 likely lists current promotions and special offers (context: 'Specials' in nav)."
        },
        {
            "url": "https://www.deltaco.com/gift-cards",
            "description": "Buy Gift Card \u2014 likely the webstore or gift card purchase page (context: 'Buy Gift Card', 'Webstore', 'Gift Cards')."
        },
        {
            "url": "https://www.deltaco.com/gift-card-balance",
            "description": "Check Gift Card Balance \u2014 likely a page to check gift card balances (context: 'Check Gift Card Balance')."
        },
        {
            "url": "https://www.deltaco.com/careers",
            "description": "Careers \u2014 likely job openings and career information (context: 'Careers' in navigation)."
        },
        {
            "url": "https://www.deltaco.com/franchising",
            "description": "Franchising \u2014 likely information for prospective franchisees (context: 'Franchising' in nav)."
        },
        {
            "url": "https://www.deltaco.com/contact",
            "description": "Contact Us \u2014 likely contact information or a contact form for Del Taco (context: 'Contact Us' in header)."
        },
        {
            "url": "https://www.deltaco.com/terms-of-use",
            "description": "Terms of use \u2014 site terms and legal information (context: 'Terms of use' in footer)."
        },
        {
            "url": "https://www.deltaco.com/privacy-policy",
            "description": "Privacy Policy \u2014 privacy practices and data handling (context: 'Privacy Policy' in footer)."
        },
        {
            "url": "https://www.deltaco.com/do-not-sell-my-info",
            "description": "Don't sell my info \u2014 likely information or opt-out related to California privacy rights (context: 'Don't sell my info' in footer)."
        }
    ]
}
We now have tool result, take verbatim most important things and save in summary.
**Action Count:** 5/15 (You have 10 actions remaining)

============================================================

"""

system_prompt ="""
============================================================
SYSTEM PROMPT web::planner
============================================================
You are an expert search agent specializing in finding accurate and up-to-date information.
## Search Strategy
1. **Use short search terms. Start broad and then get more specific.** First search term regardless of what you are trying to do should be shortest possible. eg.
You are trying to find out about Thomas Jefferson's birth place. Search 'Thomas Jefferson'.  Next you can search 'Thomas Jefferson birth place' if needed
Wikipedia is authorotative source.  So when you are trying to find any information you should always check 'Thomas Jefferson wikidpea'. If the information is found in Wikipedia you do not
need to look any further.
2. **Perform a Web Search if Necessary**
   - If no direct source is found, use a search engine.
   - Prioritize sources that are known for accuracy and reliability.
3. **Split task into sub-tasks. Do not get stuck on any part for too long. Move on or try new approach.**
4. **Be pragmatic - good enough is good enough!**
   - Wikipedia is a reliable source for basic biographical facts (dates, names, etc.)
   - Once you have answered the core question with a credible source, or two anecdotal source, STOP searching
   - Don't endlessly seek "perfect" sources when you already have the answer
   - Respect your action limit - use actions wisely
   - Respect that you will not always get a perfect up to date answer.  If you are asked for upto date fact, it may not exist and you have to settle for closest
   known date.  Use similar common sense in other matters to determine when close enough is good enough.
You have access to the following resources:
Tools:
- **search_web**: search_web is a basic search engine.  It requires a query and context.  Short queries of 2-3 words are best.  Consider longer
queries only if there is no success.  Context is used to guide the summary of the search results.  You need to explain to the search tool what it is that we are looking for.
While the search_web tool returns summaries, there is no reason to distrust that the facts reported are on these pages.  You can question the source, but not the fact that the results are as they appear.
- **scrape_url**: Scrape tool fetches content from a webpage. Arguments are URL and optional "query" string.
- If query is provided: extracts only content relevant to the query
- If query is empty or omitted: returns the entire page content
Can only scrape HTML/web pages. Cannot scrape PDF files.
Agents:
- **shared::final_answer_lite**: 
- **shared::mathematician**: mathematician - Sometimes you may need to have to do a calculation.  You can ask the math guy.
    Action: mathematician
    Action Input: Whatever you want to ask.
Action to use a tool or agent is always just the name for the tool or agent.
Action Input for tool is your best assessment of what is needed to run the tool. You don't need to get arguments exactly right since a tool agent will take your input and generate the parameters for actual tool call.
## Output Fields
- what_i_am_thinking: Write out your reasoning before you create your plan. Use this section to brainstorm, refine the problem, or reflect on previous actions.
- summary: Summarize the most recent tool result. Extract and record the most important information from the tool output - include URLs, key facts, and specific data. WHATEVER YOU DO NOT RECORD WILL BE LOST FOREVER! This is your only chance to save information from each tool call.
- checklist: Track progress. Use: DONE for completed, IN PROGRESS for current, ABANDONED for error states or unfruitful directions. Nothing for un-attempted items.
- action: tool to use or agent to call (search, scrape, shared::writer, etc.)
- action_input: The input required to perform the action
#### When the problem has been solved and the final answer is known to you or task is solved best as can be, set action to return_control ####
**IMPORTANT: You have a maximum of 15 actions to complete your task. After 15 actions, you MUST wrap up and set action to return_control. Use your actions efficiently.**
**CRITICAL: Once you have factually answered the user's question with credible sources, STOP and set action to return_control. Do NOT keep searching for "better" sources when you already have the answer. Wikipedia is acceptable for basic facts. Be efficient.**
** CRITICAL: DO NOT SCRAPE REDDIT YOU WILL GET BANNED.  DO NOT SCRAPE OLD REDDIT **
system_wide_tasks is unused and you should never set this field equal to anything.
Notes on checklist:
Your checklist is a living strategy, not a binding contract.
If you find Tier 1 evidence (e.g., Wikipedia for bio, Official News for events), you are required to check off all related research sub-tasks as 'DONE'
 immediately. Do not 'double-check' or 'corroborate' unless the source is explicitly conflicting or low-confidence.
  If the core answer is found, bypass the remaining plan and exit.
You are allowed to adjust your checklist as you go along.  It is there to give you some memory of what has been done and what has not, but it don't feel like
every step must be completed exactly as written.  You can edit the check list to remove steps or write them better as you progress.  eg. you might write get 3 sources, but if you find one very good one you
can mark yourself as done.2026-02-13 13:21:41,814 - tool_result_handler - DEBUG - N/A - [tool_result_handler] Returning control to calling agent: web::planner

============================================================


"""

def main():
    # Create the agent
    agent = DI.agent_factory.create_agent("test_agent")
    if not agent:
        print("❌ Agent creation failed!")
        return


    #fmt_input = {"text": "['Jukka wants the Emi open-source community to refactor Emi's codebase as a first priority'., 'Jukka wants the Emi open-source community to strengthen Emi's codebase as a first priority.', 'Jukka wants the Emi open-source community to prioritize security.', 'Jukka wants the Emi open-source community to prioritize databases.', 'Jukka wants the Emi open-source community to prioritize multi-threading.']"}
    fmt_input = {"test_system_prompt": system_prompt, "test_user_prompt": user_prompt}
    msg = Message(agent_input=fmt_input)
    # Run the agent
    started = time.perf_counter()
    agent.action_handler(msg)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    print(f"⏱️ test_agent action_handler took {elapsed_ms:.1f} ms")



if __name__ == "__main__":
    main()