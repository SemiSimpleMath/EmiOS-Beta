from sqlalchemy import Column, Integer, String, Float, ForeignKey, Text
from sqlalchemy.orm import relationship
from app.models.base import Base  # Import Base from base.py

class NewsCategory(Base):
    __tablename__ = "news_categories"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)

    labels = relationship("NewsLabel", back_populates="category")

class NewsLabel(Base):
    __tablename__ = "news_labels"
    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    category_id = Column(Integer, ForeignKey("news_categories.id"), nullable=False)

    category = relationship("NewsCategory", back_populates="labels")
    scores = relationship("NewsScore", back_populates="label")

class NewsScore(Base):
    __tablename__ = "news_scores"
    id = Column(Integer, primary_key=True)
    category_id = Column(Integer, ForeignKey("news_categories.id"), nullable=True)
    label_id = Column(Integer, ForeignKey("news_labels.id"), nullable=True)
    score = Column(Float, default=50.0, nullable=False)
    feedback_count = Column(Integer, default=0, nullable=False)
    likes = Column(Integer, default=0, nullable=False)
    dislikes = Column(Integer, default=0, nullable=False)
    category = relationship("NewsCategory")
    label = relationship("NewsLabel", back_populates="scores")


class NewsArticle(Base):
    __tablename__ = "news_articles"
    id = Column(Integer, primary_key=True)
    title = Column(String(255), nullable=False)
    url = Column(String(255), unique=True, nullable=False)
    summary = Column(Text, nullable=True)  # Retain if needed
    category_id = Column(Integer, ForeignKey("news_categories.id"), nullable=False)
    category = relationship("NewsCategory")

class NewsFeedback(Base):
    __tablename__ = "news_feedbacks"  # Changed to plural for consistency
    id = Column(Integer, primary_key=True)
    article_id = Column(Integer, ForeignKey("news_articles.id"), nullable=False)
    feedback_value = Column(Integer, nullable=False)  # +1, -1, +5, -5, etc.
    timestamp = Column(String, nullable=False)  # Retained as String

    article = relationship("NewsArticle")


