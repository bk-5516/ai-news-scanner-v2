#!/usr/bin/env python3
"""Fetch all sources and store articles directly — no LLM, no API key needed.

Articles are stored with relevance_score=5 and a placeholder summary so the
dashboard can render them. Useful for testing fetching without an Anthropic key.
"""
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.db import init_db, get_conn, insert_raw_article, is_url_seen, insert_article
from scanner.fetcher import fetch_all
from scanner.loader import load_sources
from scanner.models import ScoredArticle
from scanner.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger(__name__)


async def main() -> None:
    init_db()
    sources = load_sources()
    log.info("Loaded %d enabled sources", len(sources))

    fetch_results = await fetch_all(sources)
    stored_at = datetime.now(timezone.utc).isoformat()

    total_fetched = 0
    total_new = 0
    total_stored = 0

    with get_conn() as conn:
        for src, articles, error in fetch_results:
            if error and not articles:
                log.warning("%-35s ERROR: %s", src.name, error)
                continue

            new_count = 0
            stored_count = 0
            for article in articles:
                total_fetched += 1
                if is_url_seen(conn, article.url_hash):
                    continue
                raw_id = insert_raw_article(conn, article)
                if raw_id is None:
                    continue
                new_count += 1
                total_new += 1

                scored = ScoredArticle(
                    raw_id=raw_id,
                    url=article.url,
                    title=article.title,
                    source_name=src.name,
                    category=article.category,
                    published_at=article.published_at,
                    relevance_score=5,
                    summary=article.snippet or "(no summary — run with LLM for summaries)",
                    themes=[],
                    is_duplicate=False,
                    duplicate_of_id=None,
                    stored_at=stored_at,
                    llm_model="none",
                )
                insert_article(conn, scored)
                stored_count += 1
                total_stored += 1

            status = "OK" if not error else f"PARTIAL ({error})"
            log.info("%-35s %2d fetched  %2d new  %2d stored  %s",
                     src.name, len(articles), new_count, stored_count, status)

    print(f"\n── Fetch-Only Stats ─────────────────")
    print(f"  {'fetched':<15} {total_fetched}")
    print(f"  {'new (unseen)':<15} {total_new}")
    print(f"  {'stored':<15} {total_stored}")
    print(f"\nDashboard: http://127.0.0.1:8080")
    print("Note: articles show score=5 with no LLM summary. Run run_scan.py with an API key for real scores.")


if __name__ == "__main__":
    asyncio.run(main())
