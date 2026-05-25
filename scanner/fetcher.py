from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import feedparser
import httpx
from bs4 import BeautifulSoup

from .models import RawArticle, SourceConfig
from .settings import settings

log = logging.getLogger(__name__)

_USER_AGENT_BROWSER = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_USER_AGENT_RSS = "AINewsScanner/2.0 (feed reader; +https://github.com/user/ai-news-scanner)"
_USER_AGENT = _USER_AGENT_BROWSER
_domain_locks: dict[str, asyncio.Lock] = {}
_domain_last_req: dict[str, float] = {}


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _domain(url: str) -> str:
    return urlparse(url).netloc


async def _rate_limit(domain: str, rps: float) -> None:
    if domain not in _domain_locks:
        _domain_locks[domain] = asyncio.Lock()
    async with _domain_locks[domain]:
        min_interval = 1.0 / rps
        elapsed = time.monotonic() - _domain_last_req.get(domain, 0)
        if elapsed < min_interval:
            await asyncio.sleep(min_interval - elapsed)
        _domain_last_req[domain] = time.monotonic()


async def _get(client: httpx.AsyncClient, url: str, timeout: int,
               rps: float, retries: int = 1) -> Optional[httpx.Response]:
    domain = _domain(url)
    for attempt in range(retries + 1):
        try:
            await _rate_limit(domain, rps)
            resp = await client.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            if attempt < retries and _is_transient(exc):
                await asyncio.sleep(2 ** attempt)
                continue
            log.debug("fetch error %s: %s", url, exc)
            return None
    return None


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError))


def _truncate(text: str, max_len: int = 400) -> str:
    text = text.strip()
    return text[:max_len] + "…" if len(text) > max_len else text


def _parse_rss(content: bytes, source: SourceConfig, fetched_at: str) -> list[RawArticle]:
    feed = feedparser.parse(content)
    articles: list[RawArticle] = []
    for entry in feed.entries[: source.max_articles_per_run]:
        url = entry.get("link", "").strip()
        if not url:
            continue
        title = entry.get("title", "").strip()
        if not title:
            continue
        snippet = _truncate(
            entry.get("summary", "") or entry.get("description", "")
        )
        published = None
        if entry.get("published_parsed"):
            import calendar
            ts = calendar.timegm(entry.published_parsed)
            published = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

        articles.append(RawArticle(
            url=url,
            url_hash=_url_hash(url),
            title=title,
            source_name=source.name,
            category=source.category,
            fetched_at=fetched_at,
            snippet=snippet,
            published_at=published,
            raw_feed_entry=json.dumps({
                "title": title, "link": url, "summary": snippet
            }, ensure_ascii=False),
        ))
    return articles


def _parse_html(html: str, base_url: str, source: SourceConfig,
                fetched_at: str) -> list[RawArticle]:
    scrape = source.scrape_config or {}
    selector = scrape.get("link_selector", "a[href]")
    max_links = scrape.get("max_links", 25)

    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    articles: list[RawArticle] = []

    for a in soup.select(selector)[:max_links * 3]:  # over-select then trim
        href = a.get("href", "").strip()
        if not href:
            continue
        # resolve relative URLs
        if href.startswith("/"):
            parsed = urlparse(base_url)
            href = f"{parsed.scheme}://{parsed.netloc}{href}"
        elif not href.startswith("http"):
            continue
        if href in seen:
            continue
        seen.add(href)

        title = a.get_text(strip=True) or a.get("title", "").strip()
        if len(title) < 8:
            continue
        # skip obvious nav links
        if any(kw in title.lower() for kw in ["更多", "more", "home", "login", "sign in"]):
            continue

        articles.append(RawArticle(
            url=href,
            url_hash=_url_hash(href),
            title=title,
            source_name=source.name,
            category=source.category,
            fetched_at=fetched_at,
            snippet="",
            published_at=None,
            raw_feed_entry=json.dumps({"title": title, "link": href}, ensure_ascii=False),
        ))
        if len(articles) >= max_links:
            break
    return articles


async def fetch_source(client: httpx.AsyncClient, source: SourceConfig) -> tuple[SourceConfig, list[RawArticle], Optional[str]]:
    """Fetch a single source. Returns (source, articles, error_msg)."""
    fetched_at = _now_iso()
    rps = source.rate_limit_rps
    timeout = source.fetch_timeout_s

    if source.feed_url:
        # Try RSS-friendly UA first; some sites (OpenAI) block browser UAs on RSS endpoints
        resp = await _get(client, source.feed_url, timeout, rps)
        if resp is None:
            # Retry with RSS user agent
            rss_client = httpx.AsyncClient(
                limits=httpx.Limits(max_connections=5),
                headers={"User-Agent": _USER_AGENT_RSS, "Accept": "application/rss+xml,application/xml,*/*"},
                follow_redirects=True,
            )
            async with rss_client:
                resp = await _get(rss_client, source.feed_url, timeout, rps)
            if resp is None:
                return source, [], f"Failed to fetch RSS: {source.feed_url}"
        articles = _parse_rss(resp.content, source, fetched_at)
        if articles:
            return source, articles, None
        # RSS returned but empty — fall through to HTML if configured
        if not source.scrape_config:
            return source, [], "RSS feed returned 0 articles"

    # HTML scrape path
    url = source.home_url
    resp = await _get(client, url, timeout, rps)
    if resp is None:
        return source, [], f"Failed to fetch HTML: {url}"
    articles = _parse_html(resp.text, url, source, fetched_at)
    if not articles:
        return source, [], "HTML scraper found 0 articles"
    return source, articles, None


async def fetch_all(sources: list[SourceConfig]) -> list[tuple[SourceConfig, list[RawArticle], Optional[str]]]:
    """Fetch all sources concurrently. Returns list of (source, articles, error)."""
    limits = httpx.Limits(max_connections=settings.fetch_max_connections, max_keepalive_connections=10)
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    async with httpx.AsyncClient(limits=limits, headers=headers,
                                  follow_redirects=True, verify=True) as client:
        tasks = [fetch_source(client, src) for src in sources if src.enabled]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    out: list[tuple[SourceConfig, list[RawArticle], Optional[str]]] = []
    for src, result in zip([s for s in sources if s.enabled], results):
        if isinstance(result, Exception):
            out.append((src, [], str(result)))
        else:
            out.append(result)
    return out
