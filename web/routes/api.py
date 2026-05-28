"""JSON API routes."""
from __future__ import annotations
import json
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Query
from fastapi.responses import FileResponse

from scanner.db import (
    get_conn, get_articles, search_articles, get_source_health,
    get_last_pipeline_run, log_click,
    get_all_user_sources, add_user_source, delete_user_source,
    get_user_keywords, add_user_keyword, delete_user_keyword,
)
from scanner.settings import settings

router = APIRouter(prefix="/api")


@router.get("/articles")
def list_articles(
    category: str = Query(..., description="global_ai or china_ai"),
    min_score: int = Query(5, ge=0, le=10),
    theme: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    with get_conn() as conn:
        rows = get_articles(conn, category=category, min_score=min_score,
                            theme=theme, days=days, limit=limit, offset=offset)
    return {"articles": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.get("/articles/{article_id}")
def get_article(article_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    return _row_to_dict(row)


@router.get("/search")
def search(
    q: str = Query(..., min_length=1),
    category: Optional[str] = Query(None),
    limit: int = Query(25, ge=1, le=100),
):
    with get_conn() as conn:
        rows = search_articles(conn, query=q, category=category, limit=limit)
    return {"articles": [_row_to_dict(r) for r in rows], "count": len(rows)}


@router.get("/health")
def source_health():
    with get_conn() as conn:
        rows = get_source_health(conn)
    return {"sources": [dict(r) for r in rows]}


@router.get("/stats")
def stats():
    with get_conn() as conn:
        last_run = get_last_pipeline_run(conn)
        total = conn.execute("SELECT COUNT(*) FROM articles WHERE is_duplicate = 0").fetchone()[0]
        by_cat = conn.execute(
            "SELECT category, COUNT(*) FROM articles WHERE is_duplicate = 0 GROUP BY category"
        ).fetchall()
    return {
        "last_run": dict(last_run) if last_run else None,
        "total_articles": total,
        "by_category": {row[0]: row[1] for row in by_cat},
    }


@router.get("/backup")
def download_backup(token: str = Query(...)):
    """Download the SQLite DB. Protected by BACKUP_TOKEN env var."""
    if not settings.backup_token or token != settings.backup_token:
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(
        settings.db_path,
        media_type="application/octet-stream",
        filename="news.db",
    )


@router.post("/click", status_code=204)
async def record_click(article_id: int = Form(...)):
    """Record an article click. Fire-and-forget; returns 204 No Content."""
    with get_conn() as conn:
        log_click(conn, article_id)


@router.post("/scan", status_code=202)
async def trigger_scan(background_tasks: BackgroundTasks):
    """Trigger a background scan. Returns 202 immediately."""
    from scanner.pipeline import run_scan
    background_tasks.add_task(_run_scan_task)
    return {"status": "scan started"}


@router.get("/sources")
def list_user_sources():
    with get_conn() as conn:
        rows = get_all_user_sources(conn)
    return {"sources": [dict(r) for r in rows]}


@router.post("/sources", status_code=201)
def create_user_source(
    name: str = Form(...),
    home_url: str = Form(...),
    feed_url: str = Form(""),
    category: str = Form(...),
    language: str = Form("zh"),
    relevance_threshold: int = Form(5),
    notes: str = Form(""),
):
    with get_conn() as conn:
        new_id = add_user_source(conn, name=name, home_url=home_url,
                                  feed_url=feed_url or None, category=category,
                                  language=language, relevance_threshold=relevance_threshold,
                                  notes=notes)
    return {"id": new_id, "name": name}


@router.delete("/sources/{source_id}", status_code=204)
def remove_user_source(source_id: int):
    with get_conn() as conn:
        delete_user_source(conn, source_id)


@router.get("/keywords")
def list_keywords():
    with get_conn() as conn:
        rows = get_user_keywords(conn)
    return {"keywords": [dict(r) for r in rows]}


@router.post("/keywords", status_code=201)
def create_keyword(
    keyword: str = Form(...),
    category: str = Form(""),
    notes: str = Form(""),
):
    with get_conn() as conn:
        new_id = add_user_keyword(conn, keyword=keyword,
                                   category=category or None, notes=notes)
    return {"id": new_id, "keyword": keyword}


@router.delete("/keywords/{keyword_id}", status_code=204)
def remove_keyword(keyword_id: int):
    with get_conn() as conn:
        delete_user_keyword(conn, keyword_id)


async def _run_scan_task() -> None:
    import logging
    log = logging.getLogger("web.scan")
    try:
        from scanner.pipeline import run_scan
        stats = await run_scan(verbose=True)
        log.info("Background scan complete: %s", stats)
    except Exception as e:
        log.error("Background scan failed: %s", e)


def _row_to_dict(row) -> dict:
    d = dict(row)
    if "themes" in d and isinstance(d["themes"], str):
        try:
            d["themes"] = json.loads(d["themes"])
        except (json.JSONDecodeError, TypeError):
            d["themes"] = []
    return d
