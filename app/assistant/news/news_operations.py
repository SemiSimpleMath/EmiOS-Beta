# models/news_operations.py
from datetime import datetime, timezone
from math import log


from sqlalchemy.exc import IntegrityError
from sqlalchemy import exists

from app.assistant.news.news_schema import NewsCategory, NewsLabel, NewsScore, NewsArticle, NewsFeedback

from app.assistant.utils.logging_config import get_logger
logger = get_logger(__name__)

def add_news_category(session, category_name):
    """
    Add a category to the database or fetch its ID if it exists.
    """
    logger.debug(f"Attempting to add or fetch category: {category_name}")

    category = session.query(NewsCategory).filter_by(name=category_name).first()
    if category:
        logger.info(f"Found existing category '{category_name}' with ID {category.id}.")
        return category.id
    else:
        category = NewsCategory(name=category_name)
        session.add(category)
        try:
            session.flush()  # Assigns an ID without committing
            logger.info(f"Created new category '{category_name}' with ID {category.id}.")
            return category.id
        except IntegrityError as e:
            session.rollback()
            logger.error(f"IntegrityError while adding category '{category_name}': {e}")
            raise
        except Exception as e:
            session.rollback()
            logger.error(f"Unexpected error while adding category '{category_name}': {e}")
            raise


def add_news_label(session, label_name, category_name):
    """
    Add a label tied to a category or fetch its ID if it exists.
    """
    logger.debug(f"Attempting to add or fetch label: {label_name} under category: {category_name}")

    category_id = add_news_category(session, category_name)

    label = session.query(NewsLabel).filter_by(name=label_name).first()
    if label:
        logger.info(f"Found existing label '{label_name}' with ID {label.id}.")
        return label.id
    else:
        label = NewsLabel(name=label_name, category_id=category_id)
        session.add(label)
        try:
            session.flush()  # Assigns an ID without committing
            logger.info(f"Created new label '{label_name}' with ID {label.id}.")
            return label.id
        except IntegrityError as e:
            session.rollback()
            logger.error(f"IntegrityError while adding label '{label_name}': {e}")
            raise
        except Exception as e:
            session.rollback()
            logger.error(f"Unexpected error while adding label '{label_name}': {e}")
            raise


def record_news_feedback(session, article_title, article_url, category_name, labels, feedback_value, commit=True):
    """
    Record feedback for a news article and update scores for related categories and labels.
    Ensure that the same article does not contribute multiple likes/dislikes.
    """
    try:
        # Add or fetch category
        category_id = add_news_category(session, category_name)
        logger.debug(f"Resolved category_id: {category_id} for category_name: {category_name}")

        if category_id is None:
            logger.error("add_news_category returned None for category_id.")
            raise ValueError("Failed to resolve category_id.")

        # Add or fetch article
        article = session.query(NewsArticle).filter_by(url=article_url).first()
        if not article:
            article = NewsArticle(title=article_title, url=article_url, category_id=category_id)
            session.add(article)
            session.flush()  # Assigns an ID without committing
            logger.info(f"Created new article '{article_title}' with ID {article.id}.")
        else:
            logger.info(f"Found existing article '{article_title}' with ID {article.id}.")

        # Check if feedback for this article already exists
        feedback_exists = session.query(
            exists().where(NewsFeedback.article_id == article.id)
        ).scalar()
        if feedback_exists:
            logger.info(f"Feedback already exists for article ID {article.id}. Skipping.")
            return

        # Record feedback
        feedback = NewsFeedback(
            article_id=article.id,
            feedback_value=feedback_value,
            timestamp=str(datetime.now(timezone.utc))
        )
        session.add(feedback)
        logger.info(f"Recorded feedback for article ID {article.id}.")

        # Update category score
        category_score = session.query(NewsScore).filter_by(category_id=category_id, label_id=None).first()
        if not category_score:
            # Initialize with default score and feedback counts
            category_score = NewsScore(
                category_id=category_id,
                score=50.0,
                feedback_count=1,
                likes=1 if feedback_value > 0 else 0,
                dislikes=1 if feedback_value < 0 else 0
            )
            session.add(category_score)
            logger.info(f"Initialized category score for category ID {category_id}.")
        else:
            # Apply weighted adjustment formula
            total_feedbacks = category_score.feedback_count
            adjustment = feedback_value * (1 / (1 + log(total_feedbacks + 1)))
            category_score.score = max(min(category_score.score + adjustment, 100.0), 0.0)
            category_score.feedback_count += 1
            if feedback_value > 0:
                category_score.likes += 1
            else:
                category_score.dislikes += 1
            logger.info(f"Updated category score for category ID {category_id}.")

        # Update label scores
        for label_name in labels:
            label_id = add_news_label(session, label_name, category_name)
            label_score = session.query(NewsScore).filter_by(label_id=label_id).first()
            if not label_score:
                label_score = NewsScore(
                    label_id=label_id,
                    score=50.0,
                    feedback_count=1,
                    likes=1 if feedback_value > 0 else 0,
                    dislikes=1 if feedback_value < 0 else 0
                )
                session.add(label_score)
                logger.info(f"Initialized label score for label ID {label_id}.")
            else:
                # Apply weighted adjustment formula
                total_feedbacks = label_score.feedback_count
                adjustment = feedback_value * (1 / (1 + log(total_feedbacks + 1)))
                label_score.score = max(min(label_score.score + adjustment, 100.0), 0.0)
                label_score.feedback_count += 1
                if feedback_value > 0:
                    label_score.likes += 1
                else:
                    label_score.dislikes += 1
                logger.info(f"Updated label score for label ID {label_id}.")

        if commit:
            session.commit()
            logger.info("Session committed successfully.")
    except IntegrityError as e:
        session.rollback()
        logger.error("IntegrityError while recording feedback: %s", e)
        raise
    except Exception as e:
        session.rollback()
        logger.error("Unexpected error while recording feedback: %s", e)
        raise

