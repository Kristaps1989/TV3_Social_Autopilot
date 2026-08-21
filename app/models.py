from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    guid: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    url: Mapped[str] = mapped_column(String(1024))
    canonical_url: Mapped[str] = mapped_column(String(1024), index=True)
    title: Mapped[str] = mapped_column(String(1024))
    lead: Mapped[str] = mapped_column(Text, default="")
    section: Mapped[str] = mapped_column(String(64), default="news", index=True)
    categories: Mapped[list] = mapped_column(JSON, default=list)
    term_ids: Mapped[list] = mapped_column(JSON, default=list)
    images: Mapped[list] = mapped_column(JSON, default=list)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    editor_status: Mapped[str] = mapped_column(String(16), default="can", index=True)
    editor_timeframe: Mapped[str] = mapped_column(String(16), default="")
    feed_name: Mapped[str] = mapped_column(String(64), default="")
    raw_json: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # AI decision results
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ai_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_reason: Mapped[str] = mapped_column(Text, default="")
    labels: Mapped[list] = mapped_column(JSON, default=list)
    sensitivity: Mapped[list] = mapped_column(JSON, default=list)

    posts: Mapped[list["Post"]] = relationship(back_populates="article")


class Post(Base):
    __tablename__ = "posts"

    STATES = ("proposed", "scheduled", "publishing", "published", "failed", "cancelled", "skipped")

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    channel: Mapped[str] = mapped_column(String(64), index=True)
    format: Mapped[str] = mapped_column(String(32), default="link")
    copy: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[list] = mapped_column(JSON, default=list)
    media: Mapped[list] = mapped_column(JSON, default=list)
    link_url: Mapped[str] = mapped_column(String(1024), default="")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    state: Mapped[str] = mapped_column(String(16), default="proposed", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    platform_post_id: Mapped[str] = mapped_column(String(256), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    dry_run: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    article: Mapped[Article] = relationship(back_populates="posts")


class Evaluation(Base):
    """Rule-engine + AI outcome per (article, channel) — powers 'why wasn't this posted?'."""

    __tablename__ = "evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    channel: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))  # eligible|blocked|forced_now|ai_skip|posted
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PostMetrics(Base):
    __tablename__ = "post_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    reactions: Mapped[int] = mapped_column(Integer, default=0)
    ga_sessions: Mapped[int] = mapped_column(Integer, default=0)
    ga_pageviews: Mapped[int] = mapped_column(Integer, default=0)


class DecisionLog(Base):
    __tablename__ = "decisions_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), index=True)
    model: Mapped[str] = mapped_column(String(128), default="")
    prompt_hash: Mapped[str] = mapped_column(String(64), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    raw_response: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    """Runtime switches: kill switch, per-channel pause."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256), default="")


def get_setting(session, key: str, default: str = "") -> str:
    row = session.get(Setting, key)
    return row.value if row else default


def set_setting(session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))
    session.commit()
