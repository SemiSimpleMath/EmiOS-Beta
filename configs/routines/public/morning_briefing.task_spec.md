## Title
Morning briefing

## Description
Reference task spec for the `morning_briefing` routine. Gathers headlines (CNN + BBC),
weather, important emails from the overnight window, current todos, and the next 7
days of calendar events, then asks `daily_summary::daily_summary` to compile a
structured JSON briefing and saves it.

To use: copy this file to `tasks/morning_briefing/task_spec.md` and the companion
`morning_briefing.compiled.json` to `tasks/morning_briefing/morning_briefing.json`.
Then toggle the routine on via the `/routines` admin UI.

## Steps
1. **Go to cnn.com, take a screenshot, and extract the top headlines. + Go to bbc.com, take a screenshot, and extract the top headlines.** → `playwright_manager`
   - instruction: Visit https://www.cnn.com and https://www.bbc.com. For each site: navigate to the home page, dismiss any cookie/consent banner per the site skill, take a full-page screenshot, and extract the top N headlines. Pass an explicit filename to browser_take_screenshot — `cnn-${today}.png` for CNN and `bbc-${today}.png` for BBC — so the file lands at a stable, dated path under data/playwright_screenshots/. Produce a single combined artifact that contains: site name, the screenshot filename used, and a list of extracted top headlines for that site. Return a structured result with both sites included.
   - pinned_tools: playwright_browse, mcp::npm/playwright-mcp::browser_take_screenshot, mcp::npm/playwright-mcp::browser_navigate
   - produces: artifact_1 (Combined web-scrape artifact with screenshots and top headlines for CNN and BBC (structured: {cnn: {screenshot, headlines}, bbc: {screenshot, headlines}}).)
2. **Get predicted weather for today.** `deterministic`
   - tools:
     - `get_weather(forecast_type="forecast")`
   - produces: artifact_2 (Predicted weather for today (forecast data: temperatures, precipitation chance, summary).)
3. **Retrieve important emails from the last 10 hours.** `deterministic`
   - tools:
     - `get_important_emails(start_date="${hours_ago_10}", end_date="${now}", unseen=False)`
   - produces: artifact_3 (List of important emails from the last 10 hours (subject, sender, snippet, timestamp, importance score).)
4. **Retrieve current todo tasks.** `deterministic`
   - tools:
     - `get_todo_tasks(include_completed=False)`
   - produces: artifact_4 (Current active todo tasks (titles, due dates, lists, priorities).)
5. **Retrieve calendar events for the next 7 days.** `deterministic`
   - tools:
     - `get_calendar_events(start_date="${now}", end_date="${days_from_now_7}", single_events=True)`
   - produces: artifact_calendar (Calendar events for the next 7 days — drives the `upcoming_events` field in the daily summary.)
6. **Use the daily_summary::daily_summary agent to compile all gathered items into a daily briefing summary.** `deterministic`
   - tools:
     - `invoke_agent(agent_name="daily_summary::daily_summary", agent_input={'task': "Compile a concise daily briefing in structured JSON. Include top headlines (by site), today's weather summary, important emails, current todos, and upcoming_events drawn from the week-ahead calendar.", 'site_scrapes': '${artifact_1}', 'weather': '${artifact_2}', 'emails': '${artifact_3}', 'todos': '${artifact_4}', 'week_calendar': '${artifact_calendar}'})`
   - produces: artifact_5 (Daily briefing compiled by the agent (structured JSON containing headlines, weather, emails, todos, week-ahead events, and metadata).)
   - consumes: artifact_1, artifact_2, artifact_3, artifact_4, artifact_calendar
7. **Save the daily briefing as JSON to app/daily_summaries/daily_summary_${today}.json.** `deterministic`
   - tools:
     - `write_text_file(file_path="app/daily_summaries/daily_summary_${today}.json", content="${artifact_5}", ensure_newline=True)`
   - produces: artifact_6 (Metadata about the saved file (path and confirmation) or the saved JSON content reference.)
   - consumes: artifact_5

## Completion
The task completes when the briefing file is saved.
