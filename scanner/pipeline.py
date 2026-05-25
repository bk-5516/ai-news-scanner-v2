"""Main pipeline: fetch → dedupe → LLM score → summarize → store."""
from __future__ import annotations
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .db import (
    get_conn, insert_raw_article, is_url_seen, insert_article,
    start_pipeline_run, finish_pipeline_run,
)
from .fetcher import fetch_all
from .health import SourceRunResult
from .llm import score_batch, summarize_batch, SCORE_BATCH_SIZE, SUMMARIZE_BATCH_SIZE
from .loader import load_sources
from .models import ScoredArticle
from .settings import settings

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunks(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


async def run_scan(verbose: bool = False) -> dict:
    """Run a full scan cycle. Returns stats dict."""
    started_at = _now_iso()
    start_time = time.monotonic()
    stats = dict(raw_fetched=0, new_raw=0, scored=0, passed=0, summarized=0,
                 dupes=0, llm_calls=0, errors=0)

    sources = load_sources()
    if verbose:
        log.info("Loaded %d sources", len(sources))

    with get_conn() as conn:
        run_id = start_pipeline_run(conn, started_at)

    # ── Phase 1: Fetch ────────────────────────────────────────────────────────
    fetch_results = await fetch_all(sources)
    fetch_time = _now_iso()

    # Collect new raw articles and record source health
    new_articles_by_id: dict[str, dict] = {}  # temp_id → article info
    source_run_results: dict[str, SourceRunResult] = {}

    with get_conn() as conn:
        for src, articles, error in fetch_results:
            srr = SourceRunResult(source_name=src.name)
            stats["raw_fetched"] += len(articles)

            if error and not articles:
                srr.fetch_ok = False
                srr.error_msg = error
                stats["errors"] += 1
                srr.finish(fetch_time, conn)
                continue

            srr.articles_fetched = len(articles)
            new_count = 0
            for article in articles:
                if is_url_seen(conn, article.url_hash):
                    continue
                raw_id = insert_raw_article(conn, article)
                if raw_id is None:
                    continue
                new_count += 1
                stats["new_raw"] += 1
                temp_id = f"{src.name}::{article.url_hash[:8]}"
                new_articles_by_id[temp_id] = {
                    "id": temp_id,
                    "raw_id": raw_id,
                    "url": article.url,
                    "title": article.title,
                    "snippet": article.snippet,
                    "source_name": src.name,
                    "category": article.category,
                    "published_at": article.published_at,
                    "threshold": src.relevance_threshold,
                }

            srr.articles_fetched = len(articles)
            source_run_results[src.name] = srr

    if verbose:
        log.info("Fetched %d raw, %d new", stats["raw_fetched"], stats["new_raw"])

    if not new_articles_by_id:
        with get_conn() as conn:
            finish_pipeline_run(conn, run_id, _now_iso(),
                                raw_fetched=stats["raw_fetched"],
                                new_articles=0, dupes_found=0,
                                llm_calls=0, llm_cost_est=0.0)
        return stats

    # ── Phase 2: LLM Relevance Scoring ───────────────────────────────────────
    # Group by category
    by_category: dict[str, list[dict]] = {}
    for art in new_articles_by_id.values():
        by_category.setdefault(art["category"], []).append(art)

    score_map: dict[str, int] = {}  # temp_id → score

    for category, cat_articles in by_category.items():
        for batch in _chunks(cat_articles, SCORE_BATCH_SIZE):
            try:
                results, cache_hit = score_batch(batch, category)
                stats["llm_calls"] += 1
                for r in results:
                    score_map[r.article_id] = r.score
                if verbose:
                    log.info("Scored batch of %d (%s) — cache=%s", len(batch), category, cache_hit)
            except Exception as e:
                log.error("score_batch error: %s", e)
                for art in batch:
                    score_map[art["id"]] = 0

    stats["scored"] = len(score_map)

    # Filter by threshold
    passing: list[dict] = []
    for art in new_articles_by_id.values():
        score = score_map.get(art["id"], 0)
        art["relevance_score"] = score
        if score >= art["threshold"]:
            passing.append(art)

    stats["passed"] = len(passing)
    if verbose:
        log.info("%d/%d articles passed relevance threshold", stats["passed"], stats["scored"])

    if not passing:
        with get_conn() as conn:
            for src_name, srr in source_run_results.items():
                srr.finish(_now_iso(), conn)
            finish_pipeline_run(conn, run_id, _now_iso(),
                                raw_fetched=stats["raw_fetched"],
                                new_articles=0, dupes_found=0,
                                llm_calls=stats["llm_calls"], llm_cost_est=0.0)
        return stats

    # ── Phase 3: Summarization + Dedup ───────────────────────────────────────
    summarize_map: dict[str, dict] = {}

    for batch in _chunks(passing, SUMMARIZE_BATCH_SIZE):
        try:
            results = summarize_batch(batch)
            stats["llm_calls"] += 1
            for r in results:
                summarize_map[r.article_id] = {
                    "summary": r.summary,
                    "themes": r.themes,
                    "duplicate_of": r.duplicate_of,
                }
            if verbose:
                log.info("Summarized batch of %d", len(batch))
        except Exception as e:
            log.error("summarize_batch error: %s", e)

    # ── Phase 4: Store to DB ──────────────────────────────────────────────────
    stored_at = _now_iso()
    article_id_map: dict[str, int] = {}  # temp_id → db id (for dedup resolution)

    with get_conn() as conn:
        # First pass: insert all non-duplicates
        for art in passing:
            tid = art["id"]
            summ = summarize_map.get(tid, {})
            dup_of_tid = summ.get("duplicate_of")

            scored = ScoredArticle(
                raw_id=art["raw_id"],
                url=art["url"],
                title=art["title"],
                source_name=art["source_name"],
                category=art["category"],
                published_at=art["published_at"],
                relevance_score=art["relevance_score"],
                summary=summ.get("summary", ""),
                themes=summ.get("themes", []),
                is_duplicate=dup_of_tid is not None,
                duplicate_of_id=None,  # resolve in second pass
                stored_at=stored_at,
                llm_model=settings.llm_model,
            )
            db_id = insert_article(conn, scored)
            article_id_map[tid] = db_id
            if dup_of_tid:
                stats["dupes"] += 1
            else:
                stats["summarized"] += 1

        # Second pass: resolve duplicate_of foreign keys
        for art in passing:
            tid = art["id"]
            summ = summarize_map.get(tid, {})
            dup_of_tid = summ.get("duplicate_of")
            if dup_of_tid and dup_of_tid in article_id_map:
                db_id = article_id_map[tid]
                dup_db_id = article_id_map[dup_of_tid]
                conn.execute(
                    "UPDATE articles SET duplicate_of_id = ? WHERE id = ?",
                    (dup_db_id, db_id),
                )

        # Update per-source pass counts
        source_pass_counts: dict[str, int] = {}
        for art in passing:
            source_pass_counts[art["source_name"]] = source_pass_counts.get(art["source_name"], 0) + 1

        for src_name, srr in source_run_results.items():
            srr.articles_passed = source_pass_counts.get(src_name, 0)
            srr.finish(stored_at, conn)

        finish_pipeline_run(
            conn, run_id, _now_iso(),
            raw_fetched=stats["raw_fetched"],
            new_articles=stats["summarized"],
            dupes_found=stats["dupes"],
            llm_calls=stats["llm_calls"],
            llm_cost_est=0.0,
        )

    duration = time.monotonic() - start_time
    log.info(
        "Scan complete in %.1fs: %d fetched, %d new, %d passed, %d stored, %d dupes, %d LLM calls",
        duration, stats["raw_fetched"], stats["new_raw"], stats["passed"],
        stats["summarized"], stats["dupes"], stats["llm_calls"],
    )
    return stats
