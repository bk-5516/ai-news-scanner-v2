from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SourceConfig:
    name: str
    home_url: str
    category: str
    tier: int
    language: str
    enabled: bool
    tags: list[str]
    feed_url: Optional[str] = None
    relevance_threshold: int = 5
    max_articles_per_run: int = 30
    rate_limit_rps: float = 0.5
    fetch_timeout_s: int = 15
    scrape_config: Optional[dict] = None
    notes: str = ""


@dataclass
class RawArticle:
    url: str
    url_hash: str
    title: str
    source_name: str
    category: str
    fetched_at: str           # ISO8601 UTC
    snippet: str = ""
    published_at: Optional[str] = None
    raw_feed_entry: str = ""  # JSON blob


@dataclass
class ScoredArticle:
    raw_id: int
    url: str
    title: str
    source_name: str
    category: str
    published_at: Optional[str]
    relevance_score: int
    summary: str = ""
    themes: list[str] = field(default_factory=list)
    is_duplicate: bool = False
    duplicate_of_id: Optional[int] = None
    stored_at: str = ""
    llm_model: str = ""
    llm_cached: bool = False
