import jinja2
from datetime import datetime

def generate_daily_summary_page(analysis_data):
    """
    Generates the daily summary HTML page from a data dictionary.

    Args:
        analysis_data (dict): A dictionary containing all the data
                              for the daily summary.

    Returns:
        str: The rendered HTML content.
    """
    # The HTML template is stored in a multi-line string.
    # In a real Flask app, this would be in a separate file (e.g., templates/dashboard.html).
    html_template = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Daily Summary for {{ data.header.date_str }}</title>
        <script src="https://cdn.tailwindcss.com"></script>
        <link rel="preconnect" href="https://fonts.googleapis.com">
        <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
        <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
        <style>
            body {
                font-family: 'Inter', sans-serif;
            }
            .card {
                background-color: white;
                border-radius: 0.75rem;
                box-shadow: 0 4px 6px -1px rgb(0 0 0 / 0.1), 0 2px 4px -2px rgb(0 0 0 / 0.1);
                padding: 1.5rem;
                transition: all 0.2s ease-in-out;
            }
            .card:hover {
                transform: translateY(-2px);
                box-shadow: 0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1);
            }
        </style>
    </head>
    <body class="bg-slate-50 text-slate-800">

        <div class="container mx-auto p-4 md:p-8 max-w-7xl">

            <!-- Header -->
            <header class="mb-8">
                <h1 class="text-4xl font-bold text-slate-900">Daily Summary</h1>
                <p class="text-lg text-slate-500">{{ data.header.day_of_week }}, {{ data.header.date_str }}</p>
            </header>

            <!-- Main Grid -->
            <div class="grid grid-cols-1 lg:grid-cols-3 gap-6">

                <!-- Left Column -->
                <div class="lg:col-span-2 space-y-6">
                    
                    <!-- Narrative Summary -->
                    <div class="card">
                        <div class="flex items-center mb-4">
                            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-sky-500 mr-3"><path d="M12 6.52c.44-.63.98-1.2 1.6-1.72C15.4 3.53 17.58 3 20 3c.42 0 .83.03 1.24.09.4.05.78.13 1.15.25A2.5 2.5 0 0 1 24 5.74v.28c0 4.54-2.8 8.7-7.22 11.22A47.8 47.8 0 0 1 12 21a47.8 47.8 0 0 1-4.78-3.72C2.8 14.7 0 10.52 0 6.02v-.28a2.5 2.5 0 0 1 2.26-2.42c.37-.12.75-.2 1.15-.25A5.5 5.5 0 0 1 4 3c2.42 0 4.6.54 6.4 1.8z"></path></svg>
                            <h2 class="text-2xl font-semibold">Today's Outlook</h2>
                        </div>
                        <p class="text-slate-600 leading-relaxed">
                            {{ data.narrative }}
                        </p>
                    </div>

                    <!-- Schedule -->
                    <div class="card">
                        <div class="flex items-center mb-4">
                            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-indigo-500 mr-3"><rect width="18" height="18" x="3" y="4" rx="2" ry="2"></rect><line x1="16" x2="16" y1="2" y2="6"></line><line x1="8" x2="8" y1="2" y2="6"></line><line x1="3" x2="21" y1="10" y2="10"></line></svg>
                            <h2 class="text-2xl font-semibold">Schedule</h2>
                        </div>
                        <ul class="space-y-4">
                            {% for item in data.schedule %}
                            <li class="flex items-start">
                                <div class="w-28 text-right mr-4 shrink-0">
                                    <p class="font-medium text-indigo-600">{{ item.start_time }} - {{ item.end_time }}</p>
                                </div>
                                <div class="border-l-2 border-indigo-200 pl-4 w-full">
                                    <p class="font-semibold">{{ item.title }}</p>
                                    <p class="text-sm text-slate-500">{{ item.notes }}</p>
                                </div>
                            </li>
                            {% endfor %}
                        </ul>
                    </div>

                    <!-- Free Time -->
                    <div class="card">
                        <div class="flex items-center mb-4">
                             <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-emerald-500 mr-3"><path d="M12 12v-2a4 4 0 0 0-4-4H4"></path><path d="M12 12v-2a4 4 0 0 1 4-4h4"></path><path d="M12 12v6"></path><path d="M12 12h-1a2 2 0 0 0-2 2v2a2 2 0 0 0 2 2h2a2 2 0 0 0 2-2v-2a2 2 0 0 0-2-2h-1z"></path></svg>
                            <h2 class="text-2xl font-semibold">Free Time & Suggestions</h2>
                        </div>
                         <div class="space-y-4">
                            {% for window in data.free_time_windows %}
                            <div class="p-4 bg-emerald-50 border border-emerald-200 rounded-lg">
                                <p class="font-semibold text-emerald-800">{{ window.start_time }} - {{ window.end_time }} <span class="font-normal text-emerald-600">({{ window.length_minutes }} mins)</span></p>
                                <ul class="list-disc list-inside mt-2 text-slate-600">
                                    {% for suggestion in window.suggestions %}
                                    <li><span class="font-semibold">{{ suggestion.split(':')[0] }}:</span>{{ suggestion.split(':')[1] }}</li>
                                    {% endfor %}
                                </ul>
                            </div>
                            {% endfor %}
                        </div>
                    </div>
                    
                    <!-- Email Triage -->
                    <div class="card">
                        <div class="flex items-center mb-4">
                             <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-amber-500 mr-3"><path d="M22 13V6a2 2 0 0 0-2-2H4a2 2 0 0 0-2 2v12c0 1.1.9 2 2 2h8"></path><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"></path><path d="M19 16v-1.5a2.5 2.5 0 0 0-5 0V16"></path><path d="M20 21v-5"></path><path d="M22 19h-4"></path></svg>
                            <h2 class="text-2xl font-semibold">Email Triage</h2>
                        </div>
                        {% if not data.email_triage.urgent and not data.email_triage.time_sensitive %}
                        <div class="text-center p-8 bg-slate-50 rounded-lg">
                            <p class="text-slate-500">No emails require your attention today.</p>
                        </div>
                        {% else %}
                        <!-- Logic to display emails would go here -->
                        {% endif %}
                    </div>

                    <!-- News Headlines -->
                    {% if data.news and data.news.sources %}
                    <div class="card">
                        <div class="flex items-center mb-4">
                            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-rose-500 mr-3"><path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"></path><path d="M18 14h-8"></path><path d="M15 18h-5"></path><path d="M10 6h8v4h-8V6Z"></path></svg>
                            <h2 class="text-2xl font-semibold">News Headlines</h2>
                        </div>
                        <div class="space-y-6">
                            {% for source in data.news.sources %}
                            <div>
                                <h3 class="font-semibold text-lg text-slate-700 mb-2">{{ source.source_name }}</h3>
                                {% if source.screenshot_path %}
                                <img src="{{ source.screenshot_path }}" alt="{{ source.source_name }} top stories" class="w-full rounded-lg mb-3 border border-slate-200" loading="lazy">
                                {% endif %}
                                {% if source.headlines %}
                                <ul class="space-y-1">
                                    {% for headline in source.headlines %}
                                    <li class="text-sm text-slate-600 pl-3 border-l-2 border-rose-200">{{ headline }}</li>
                                    {% endfor %}
                                </ul>
                                {% endif %}
                            </div>
                            {% endfor %}
                        </div>
                    </div>
                    {% endif %}

                </div>

                <!-- Right Column -->
                <div class="lg:col-span-1 space-y-6">
                    
                    <!-- Metrics -->
                    <div class="card">
                        <div class="flex items-center mb-4">
                             <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-blue-500 mr-3"><path d="M3 3v18h18"></path><path d="M18.7 8a6 6 0 0 0-9.4 0l-2.3 2.3a4 4 0 0 0 5.6 5.6l2.3-2.3a2 2 0 0 0-2.8-2.8l-2.3 2.3"></path></svg>
                            <h2 class="text-xl font-semibold">At a Glance</h2>
                        </div>
                        <div class="grid grid-cols-2 gap-4 text-center">
                            <div>
                                <p class="text-3xl font-bold text-blue-600">{{ data.metrics.workload_score }}<span class="text-lg">/10</span></p>
                                <p class="text-sm text-slate-500">Workload Score</p>
                            </div>
                            <div>
                                <p class="text-3xl font-bold text-emerald-600">{{ data.metrics.total_free_minutes }}</p>
                                <p class="text-sm text-slate-500">Free Minutes</p>
                            </div>
                            <div>
                                <p class="text-3xl font-bold text-slate-600">{{ data.metrics.total_task_minutes }}</p>
                                <p class="text-sm text-slate-500">Task Minutes</p>
                            </div>
                            <div>
                                <p class="text-3xl font-bold text-sky-600">{{ (data.task_plan.load_recommendation | default('N/A')).capitalize() if data.task_plan.load_recommendation else 'N/A' }}</p>
                                <p class="text-sm text-slate-500">Recommendation</p>
                            </div>
                        </div>
                    </div>
                    
                    <!-- Task Plan -->
                    <div class="card">
                        <div class="flex items-center mb-4">
                            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-cyan-500 mr-3"><path d="M12 20h.01"></path><path d="M12 14h.01"></path><path d="M12 8h.01"></path><path d="M12 2h.01"></path><path d="M20 12h-.01"></path><path d="M14 12h-.01"></path><path d="M8 12h-.01"></path><path d="M2 12h-.01"></path><path d="m4.929 4.929-.01.01"></path><path d="m19.071 4.929-.01.01"></path><path d="m4.929 19.071-.01.01"></path><path d="m19.071 19.071-.01.01"></path></g><circle cx="12" cy="12" r="10"></circle></svg>
                            <h2 class="text-xl font-semibold">Task Plan</h2>
                        </div>
                        {% if not data.task_plan.tasks %}
                        <div class="text-center p-4 bg-slate-50 rounded-lg">
                            <p class="text-slate-500">All tasks are complete. No new tasks planned.</p>
                        </div>
                        {% endif %}
                         <p class="text-sm text-slate-500 mt-4"><span class="font-semibold">Rationale:</span> {{ data.task_plan.load_rationale }}</p>
                    </div>

                    <!-- Upcoming Events -->
                    <div class="card">
                        <div class="flex items-center mb-4">
                            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-purple-500 mr-3"><path d="M8 2v4"></path><path d="M16 2v4"></path><path d="M21 13V6a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h8"></path><path d="M3 10h18"></path><path d="M19 22v-6"></path><path d="m22 19-3-3-3 3"></path></svg>
                            <h2 class="text-xl font-semibold">On the Horizon</h2>
                        </div>
                        <ul class="space-y-3">
                            {% for event in data.upcoming_events %}
                            <li class="p-3 {% if event.watch_flag %}bg-purple-50 border border-purple-200{% else %}bg-slate-50{% endif %} rounded-lg">
                                <div class="flex justify-between items-center">
                                    <p class="font-semibold {% if event.watch_flag %}text-purple-800{% endif %}">{{ event.title }}</p>
                                    <span class="text-sm font-medium {% if event.watch_flag %}text-purple-600 bg-purple-100{% else %}text-slate-600{% endif %} px-2 py-1 rounded-full">{{ event.days_until }} days</span>
                                </div>
                                {% if event.lead_actions %}
                                <p class="text-sm text-slate-500 mt-1">{{ ", ".join(event.lead_actions) }}</p>
                                {% endif %}
                            </li>
                            {% endfor %}
                        </ul>
                    </div>

                    <!-- Conflicts & Assumptions -->
                    <div class="card bg-slate-100 border border-slate-200">
                        <div class="flex items-center mb-4">
                            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-slate-500 mr-3"><circle cx="12" cy="12" r="10"></circle><line x1="12" x2="12" y1="8" y2="12"></line><line x1="12" x2="12.01" y1="16" y2="16"></line></svg>
                            <h2 class="text-xl font-semibold">Notes</h2>
                        </div>
                        <div class="text-sm text-slate-600 space-y-3">
                            <div>
                                <h3 class="font-semibold mb-1">Conflicts</h3>
                                {% if not data.conflicts %}
                                <p>No conflicts detected.</p>
                                {% else %}
                                <!-- Logic to display conflicts would go here -->
                                {% endif %}
                            </div>
                            <div>
                                <h3 class="font-semibold mb-1">Assumptions</h3>
                                <ul class="list-disc list-inside">
                                    {% for assumption in data.assumptions %}
                                    <li>{{ assumption }}</li>
                                    {% endfor %}
                                </ul>
                            </div>
                        </div>
                    </div>

                </div>
            </div>
        </div>

    </body>
    </html>
    """
    
    # Set up the Jinja2 environment and load the template from the string
    env = jinja2.Environment(loader=jinja2.BaseLoader())
    template = env.from_string(html_template)
    
    # Render the template with the provided data
    return template.render(data=analysis_data)

def convert_daily_summary_result_to_html_data(daily_summary_result):
    """
    Converts the raw daily summary result from the manager into the format
    expected by the HTML template.
    
    Args:
        daily_summary_result (dict): The raw result from daily_summary_manager
        
    Returns:
        dict: Formatted data for the HTML template
    """
    def format_time(iso_time_str):
        """Convert ISO time string to readable time format"""
        if not iso_time_str:
            return "-"
        try:
            # Parse ISO format like "2025-08-24T08:00:00-07:00"
            dt = datetime.fromisoformat(iso_time_str.replace('Z', '+00:00'))
            return dt.strftime('%I:%M %p')  # e.g., "8:00 AM"
        except (ValueError, AttributeError):
            return "-"
    
    def process_schedule_items(items):
        """Process schedule items to add formatted time fields"""
        processed_items = []
        for item in items:
            processed_item = item.copy()
            processed_item['start_time'] = format_time(item.get('start'))
            processed_item['end_time'] = format_time(item.get('end'))
            processed_items.append(processed_item)
        return processed_items
    
    def process_free_time_windows(windows):
        """Process free time windows to add formatted time fields"""
        processed_windows = []
        for window in windows:
            processed_window = window.copy()
            processed_window['start_time'] = format_time(window.get('start'))
            processed_window['end_time'] = format_time(window.get('end'))
            processed_windows.append(processed_window)
        return processed_windows
    
    # Extract the actual summary data from the result structure
    # Handle case where daily_summary_result might be a string
    if isinstance(daily_summary_result, str):
        try:
            import json
            daily_summary_result = json.loads(daily_summary_result)
        except json.JSONDecodeError:
            # If it's not valid JSON, treat it as an error message
            return {
                'header': {
                    'date_str': datetime.now().strftime('%B %d, %Y'),
                    'day_of_week': datetime.now().strftime('%A')
                },
                'narrative': f"Daily summary generation failed: {daily_summary_result}",
                'schedule': [],
                'free_time_windows': [],
                'upcoming_events': [],
                'task_plan': {},
                'email_triage': {},
                'metrics': {},
                'assumptions': [],
                'conflicts': []
            }
    
    summary_data = daily_summary_result.get('summary', daily_summary_result)
    
    # If summary_data is a string (error message or invalid data), handle gracefully
    if isinstance(summary_data, str):
        try:
            import json
            summary_data = json.loads(summary_data)
        except json.JSONDecodeError:
            # It's an error message, not JSON - return error page
            return {
                'header': {
                    'date_str': datetime.now().strftime('%B %d, %Y'),
                    'day_of_week': datetime.now().strftime('%A')
                },
                'narrative': f"Daily summary generation failed: {summary_data}",
                'schedule': [],
                'free_time_windows': [],
                'upcoming_events': [],
                'task_plan': {},
                'email_triage': {},
                'metrics': {},
                'assumptions': [],
                'conflicts': []
            }
    
    # Ensure task_plan has required fields
    task_plan = summary_data.get('task_plan', {})
    if not isinstance(task_plan, dict):
        task_plan = {}
    task_plan.setdefault('load_recommendation', 'rest')
    task_plan.setdefault('load_rationale', '')
    task_plan.setdefault('tasks', [])
    
    # Process news section — fix screenshot paths to be servable.
    news = summary_data.get('news', {})
    if isinstance(news, dict):
        sources = news.get('sources', [])
        if not isinstance(sources, list):
            sources = []
        for source in sources:
            path = source.get('screenshot_path', '')
            if path and not path.startswith(('http', '/static', '/dev/file')):
                source['screenshot_path'] = f'/dev/file/{path}'.replace('\\', '/')
    else:
        sources = []

    return {
        'header': {
            'date_str': datetime.now().strftime('%B %d, %Y'),
            'day_of_week': datetime.now().strftime('%A')
        },
        'narrative': summary_data.get('narrative', 'No narrative available.'),
        'schedule': process_schedule_items(summary_data.get('schedule', [])),
        'free_time_windows': process_free_time_windows(summary_data.get('free_time_windows', [])),
        'upcoming_events': summary_data.get('upcoming_events', []),
        'task_plan': task_plan,
        'email_triage': summary_data.get('email_triage', {}),
        'news': {'sources': sources},
        'metrics': summary_data.get('metrics', {}),
        'assumptions': summary_data.get('assumptions', []),
        'conflicts': summary_data.get('conflicts', [])
    }
