# models/news_tool.py

import json
import uuid
import feedparser
from datetime import datetime
from email.utils import parsedate_to_datetime
from app.assistant.lib.core_tools.base_tool.base_tool import BaseTool
from app.assistant.utils.pydantic_classes import ToolMessage, ToolResult, Message
from app.models.base import get_session
from app.assistant.news.news_schema import NewsCategory, NewsScore, NewsLabel
import random
from math import exp
# Configure logging
from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)
from app.assistant.event_repository.event_repository import EventRepositoryManager
from app.assistant.ServiceLocator.service_locator import DI

# Default RSS Feeds
DEFAULT_FEEDS = [
    # **General News**
    "http://feeds.bbci.co.uk/news/rss.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
    "http://rss.cnn.com/rss/cnn_topstories.rss",
    "https://feeds.feedburner.com/time/topstories",
    "http://rss.cnn.com/rss/edition.rss",
    "https://feeds.npr.org/1001/rss.xml",
    "https://news.google.com/rss/search?q=site%3Areuters.com&hl=en-US&gl=US&ceid=US%3Aen",
    "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
    "https://feeds.washingtonpost.com/rss/world",
    # "https://www.pbs.org/newshour/feeds/rss/headlines",  # DISABLED: Malformed feed
    # "https://www.yahoo.com/news/rss/topstories",  # DISABLED: Malformed feed
    "https://www.vox.com/rss/index.xml",
    "http://rss.cnn.com/rss/cnn_world.rss",
    "https://abcnews.go.com/abcnews/topstories",
    "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "https://www.salon.com/feed/",
    # "https://www.newyorker.com/feed/everything",  # DISABLED: Malformed feed
    "https://nypost.com/feed/",

    # **Technology**
    "http://feeds.feedburner.com/TechCrunch/",
    "https://www.wired.com/feed/rss",
    "https://www.theverge.com/rss/index.xml",
    "http://feeds.arstechnica.com/arstechnica/index",
    "https://www.cnet.com/rss/news/",
    "https://www.zdnet.com/news/rss.xml",
    "https://www.technologyreview.com/feed/",
    "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    "https://lifehacker.com/feed/rss",

    # **Science & Environment**
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://www.sciencedaily.com/rss/top.xml",

    # **Finance & Business**
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://www.forbes.com/most-popular/feed/",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",  # The Wall Street Journal

    # **World Affairs**
    "https://www.aljazeera.com/xml/rss/all.xml",
    "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
    "https://rss.dw.com/rdf/rss-en-all",

    # **Progressive News Sources**
    "https://www.vox.com/rss/index.xml",
    "https://feeds.propublica.org/propublica/main",
    "https://www.thenation.com/feed/",
    "https://www.huffpost.com/section/front-page/feed",
    "https://www.thenewcivilrightsmovement.com/feed",

    # **Additional Niche/Other**
    "https://lifehacker.com/rss",
    "https://www.rollingstone.com/music/music-news/feed/",
    "http://feeds.eonline.com/eonline/topstories"
]



class GetNews(BaseTool):
    """
    Tool to fetch, label, score, and rank news articles from RSS/Atom feeds.
    """

    # Class-level list to store links of selected articles
    selected_articles = []

    def __init__(self):
        super().__init__('get_news_tool')
        self.labeled_articles = []
        self.articles = []
        self.group_id = 0
        self.top_n = 5  # Number of articles to return to the user
        self.max_fetch = 10  # Desired number of new articles
        self.request_id = None
        self.repo_manager = EventRepositoryManager()


    def execute(self, tool_message: ToolMessage) -> None:


        """
        Fetch news, send them to the LabelAgent, and notify the user.
        """

        self.request_id = tool_message.request_id
        tool_data = tool_message.tool_data.get("arguments", {})

        # Extract feed URLs or use defaults
        feed_urls = tool_data.get("feed_urls", None) or DEFAULT_FEEDS


        # Step 1: Fetch News Articles
        try:
            self.articles = self._fetch_news(feed_urls)
        except Exception as e:
            logger.error("Failed to fetch news articles: %s", e)
            logger.debug("news fetch exception", exc_info=True)


        if not self.articles:
            raise ValueError("No new news articles fetched.")

        #print("sending articles to labeling")
        # Step 2: Send Articles to LabelAgent for Labeling
        try:
            return self.process_articles(self.articles)
        except Exception as e:
            logger.error("Failed to process news articles: %s", e)
            logger.debug("news process exception", exc_info=True)

    def process_articles(self, articles):
        """
        Batch send articles to the LabelAgent for labeling.
        """
        self.labeled_articles = []  # Reset previous data

        # Prepare the batch content: list of articles with title and body
        batch_content = [
            {"title": article["title"], "body": article.get("summary", "")}
            for article in articles
        ]

        # Construct and publish batch message
        batch_message = Message(
            data_type="label_request",
            sender="NewsTool",
            receiver="LabelAgent",
            content=json.dumps(batch_content),
            group_id=self.group_id,
            request_id=str(uuid.uuid4())
        )

        self.label_handler(batch_message)

    def label_handler(self, feedback_msg):
        import time
        label_start = time.time()
        logger.info(f"📰 NEWS: Starting label_handler")
        
        label_agent = DI.agent_factory.create_agent('label')

        try:
            # Parse the feedback content
            content = json.loads(feedback_msg.content)
        except Exception as e:
            logger.error(f"Error parsing feedback content: {e}")
            return []

        results = []
        for item in content:
            try:
                # Extract fields from the item
                category = 'news'
                title = item.get('title', '')
                body = item.get('body', '')
                preference = item.get('preference', '')  # Preserving preference

                # Combine title and body
                combined_message_content = f"{title}\n\n{body}"
                agent_input = {"news_item": combined_message_content}
                # Construct a Message object for the LabelAgent
                message = Message(
                    data_type="agent_msg",
                    sender="FeedbackProcessor",
                    receiver="LabelAgent",
                    agent_input = agent_input,
                    data={"category": category, "preference": preference}
                )
                # Pass the Message to the LabelAgent
                item_start = time.time()
                result = label_agent.action_handler(message)
                logger.info(f"📰 NEWS: Label call {content.index(item)+1}/{len(content)} took {time.time()-item_start:.1f}s")
                data_list = result.data_list
                
                # Small yield to let other operations proceed
                time.sleep(0.1)
                # print(result)
                if data_list:
                    for label_item in data_list:
                        category = label_item.get('category')
                        labels = label_item.get('labels')
                        results.append({
                            "category": category,
                            "labels": labels,
                            "original_message": json.dumps(label_item)  # Keep the original data
                        })
                else:
                    results.append({"error": "No result returned", "item": item})
            except Exception as e:
                logger.error(f"Error processing item: {item}. Exception: {e}")
                results.append({"error": str(e), "item": item})

        # print("\nAll results: ", results)
        # Construct ToolResult
        tool_result = ToolResult(
            result_type="label_results",
            content="Labeling operation completed.",
            data_list=results
        )

        # Construct ToolMessage
        tool_message = ToolMessage(
            tool_name="LabelAgent",
            data_type="tool_result",
            sender="LabelAgent",
            receiver=None,
            content=json.dumps(content),
            tool_data={},
            tool_result=tool_result,
            request_id=str(uuid.uuid4())
        )


        try:
            # Extract and validate tool_result from ToolMessage
            tool_result = tool_message.tool_result
            if not tool_result or not tool_result.data_list:
                logger.error("Tool result is empty or invalid.")
                return

            labeled_data = tool_result.data_list  # List of labeled data dictionaries

            # Validate length to ensure labeled_data matches articles
            if len(labeled_data) != len(self.articles):
                logger.error("Mismatch between labeled data count and fetched articles.")
                return

            # Map each article with its corresponding labels from labeled_data
            self.labeled_articles = []
            for article, labeled_item in zip(self.articles, labeled_data):
                category = labeled_item.get("category", "Uncategorized")
                labels = labeled_item.get("labels", [])
                self.labeled_articles.append((article, (category, labels)))

            logger.info(f"Received labeled data for {len(self.labeled_articles)} articles.")
            logger.info(f"📰 NEWS: Total label_handler took {time.time()-label_start:.1f}s")

            # Proceed to rank and select top articles
            self.rank_and_select_articles()

        except Exception as e:
            logger.error(f"Error in label_done_handler: {e}")

    def rank_and_select_articles(self):
        """
        Score labeled articles based on database values and select top N articles.
        """
        scored_articles = []

        session = get_session()
        try:
            for article, labels in self.labeled_articles:
                category, label_list = labels
                total_score = 50  # Default neutral score

                # Fetch category score
                category_score = self._fetch_score(session, NewsCategory, NewsScore, "category_id", category)

                # Fetch label scores
                label_scores = [
                    self._fetch_score(session, NewsLabel, NewsScore, "label_id", label)
                    for label in label_list
                ]

                # Combine scores
                if label_scores:
                    average_label_score = sum(label_scores) / len(label_scores)
                    total_score = (category_score + average_label_score) / 2
                else:
                    total_score = category_score

                scored_articles.append({"article": article, "score": total_score})
        finally:
            session.close()

        if not scored_articles:
            logger.error("No scored articles available for selection.")
            return

        # Normalize scores and select top N articles
        scores = [exp(article["score"] / 10) for article in scored_articles]
        total = sum(scores)
        probabilities = [score / total for score in scores]
        selected_articles = random.sample(scored_articles, k=min(self.top_n, len(scored_articles)))

        # Extract links of selected articles to prevent future duplication
        selected_links = [article["article"]["link"] for article in selected_articles]

        logger.info(f"Selected top {len(selected_articles)} articles based on scores.")
        self.publish_selected_articles(selected_articles)

        # Persist selected articles to the local list
        self._persist_selected_articles(selected_links)

    def publish_selected_articles(self, selected_articles):

        articles_data = [
            {
                "data_type": "news",
                "title": article["article"]["title"],
                "link": article["article"]["link"],
                "published": article["article"].get("published", ""),
                "summary": article["article"].get("summary", ""),
                "source": article["article"].get("source", ""),
                "score": article["score"],
            }
            for article in selected_articles
        ]

        # Batch write to repository - collect IDs first, then write all at once
        news_ids = [article.get('link') for article in articles_data]
        
        logger.debug(f"Batch writing {len(articles_data)} news articles to repo")
        for article in articles_data:
            news_id = article.get('link')
            self.repo_manager.store_event(
                id=news_id,
                event_data=article,
                data_type="news",
            )

        self.repo_manager.sync_events_with_server(news_ids, "news")

        data_list = articles_data

        tool_result = ToolResult(result_type="news", content="", data_list=data_list)
        message = ToolMessage(
            tool_name="get_news",
            data_type="tool_result",
            sender="NewsTool",
            receiver=None,
            content="Top news articles selected.",
            tool_data={"articles": articles_data},
            group_id=self.group_id,
            tool_result=tool_result
        )


        return message

    def _persist_selected_articles(self, selected_links):
        """
        Persist the links of selected articles to the class-level selected_articles list to prevent future duplication.
        """
        for link in selected_links:
            if link not in self.selected_articles:
                self.selected_articles.append(link)
        logger.info(f"Persisted {len(selected_links)} selected articles to the local list.")

    def _fetch_news(self, feed_urls):
        """
        Fetch news articles from RSS/Atom feeds, filter for today's articles,
        exclude previously selected articles, and randomly select up to max_fetch articles.
        Store fetched articles in the repository.
        """

        # print(f"\n\nUsing feeds: {feed_urls}\n\n")

        all_news = []
        today = datetime.now().date()

        selected_links = set(self.selected_articles)

        for url in feed_urls:
            try:
                feed = feedparser.parse(url)
                if feed.bozo:
                    raise ValueError(f"Malformed feed at {url}. Skipping...")

                for entry in feed.entries:
                    published_date = self._parse_date(entry.get("published", None))

                    if published_date and published_date.date() == today:
                        link = entry.get("link", "No link")
                        if link in selected_links:
                            continue  # Skip previously selected articles

                        news_item = {
                            "title": entry.get("title", "No title"),
                            "link": link,
                            "published": published_date.isoformat() if published_date else "No publication date",
                            "summary": entry.get("summary", "No summary"),
                            "source": feed.feed.get("title", "Unknown Source"),
                        }

                        all_news.append(news_item)

            except Exception as e:
                logger.error(f"Error processing feed {url}: {e}")

        random.shuffle(all_news)
        selected_news = all_news[:self.max_fetch]

        # print(f"selected_news {selected_news}")

        logger.info(f"Fetched {len(selected_news)} new articles from today's feeds.")
        return selected_news


    def _parse_date(self, date_str):
        """
        Parse a date string into a naive datetime object, ignoring any timezone information.
        Returns datetime.min if parsing fails.
        """
        try:
            parsed_date = parsedate_to_datetime(date_str)
            # Strip timezone info to make it naive
            return parsed_date.replace(tzinfo=None)
        except Exception:
            return datetime.min

    def _fetch_score(self, session, model, score_model, key_field, key_value):
        """
        Fetch a score from the database for a given model and key.
        """
        try:
            obj = session.query(model).filter_by(name=key_value).first()
            if not obj:
                return 50  # Default score if no match found

            key_id = getattr(obj, "id", None)
            score_obj = session.query(score_model).filter_by(**{key_field: key_id}).first()
            return getattr(score_obj, "score", 50) if score_obj else 50
        except Exception as e:
            logger.warning(f"Error fetching score for {key_value}: {e}")
            return 50


def get_tool_class():
    """
    Returns the class for the tool.
    This function is required by the tool registry.
    """
    return GetNews