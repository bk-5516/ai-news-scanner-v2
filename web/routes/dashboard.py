"""HTML dashboard routes."""
from __future__ import annotations
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Form, HTTPException, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from scanner.db import (
    get_conn, get_articles, search_articles, get_source_health,
    get_last_pipeline_run, get_votes_for_articles, upsert_vote,
    get_all_user_sources, get_user_keywords,
)

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter()

THEMES = [
    "Foundation Models", "Policy & Regulation", "Hardware & Chips",
    "Corporate Moves", "Research", "Applications", "Geopolitics",
    "Funding", "Robotics", "Safety & Alignment", "Cybersecurity",
]

CATEGORIES = [
    {"id": "global_ai", "label": "Global AI"},
    {"id": "china_ai", "label": "China AI"},
]


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    category: str = Query("global_ai"),
    min_score: int = Query(5, ge=0, le=10),
    theme: Optional[str] = Query(None),
    days: int = Query(7, ge=1, le=90),
    q: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
):
    limit = 25
    with get_conn() as conn:
        if q:
            rows = search_articles(conn, query=q, category=category, limit=limit)
        else:
            rows = get_articles(conn, category=category, min_score=min_score,
                                theme=theme, days=days, limit=limit, offset=offset)
        last_run = get_last_pipeline_run(conn)
        total_for_page = conn.execute(
            """SELECT COUNT(*) FROM articles
               WHERE category = ? AND relevance_score >= ? AND is_duplicate = 0
               AND (published_at >= datetime('now', '-' || ? || ' days') OR published_at IS NULL)""",
            (category, min_score, days),
        ).fetchone()[0]

    articles = [_enrich(dict(r)) for r in rows]

    # Load vote state for displayed articles
    article_ids = [a["id"] for a in articles]
    with get_conn() as conn:
        votes = get_votes_for_articles(conn, article_ids)
    for a in articles:
        a["vote"] = votes.get(a["id"])

    return templates.TemplateResponse(request=request, name="index.html", context={
        "articles": articles,
        "categories": CATEGORIES,
        "active_category": category,
        "themes": THEMES,
        "active_theme": theme,
        "min_score": min_score,
        "days": days,
        "query": q or "",
        "offset": offset,
        "limit": limit,
        "total": total_for_page,
        "has_more": (offset + limit) < total_for_page,
        "next_offset": offset + limit,
        "last_run": dict(last_run) if last_run else None,
    })


@router.get("/health", response_class=HTMLResponse)
async def health_page(request: Request):
    with get_conn() as conn:
        rows = get_source_health(conn)
        last_run = get_last_pipeline_run(conn)
        # Get category per source from source_runs
        cat_map: dict[str, str] = {}
        for row in conn.execute(
            "SELECT source_name, category FROM articles GROUP BY source_name"
        ).fetchall():
            cat_map[row[0]] = row[1]

    sources = [dict(r) for r in rows]
    for s in sources:
        s["category"] = cat_map.get(s["source_name"], "")

    return templates.TemplateResponse(request=request, name="health.html", context={
        "sources": sources,
        "last_run": dict(last_run) if last_run else None,
        "categories": CATEGORIES,
    })


@router.get("/manage", response_class=HTMLResponse)
async def manage_page(request: Request):
    with get_conn() as conn:
        sources = [dict(r) for r in get_all_user_sources(conn)]
        keywords = [dict(r) for r in get_user_keywords(conn)]
    return templates.TemplateResponse(request=request, name="manage.html", context={
        "sources": sources,
        "keywords": keywords,
        "categories": CATEGORIES,
    })


@router.post("/manage/keywords", response_class=HTMLResponse)
async def add_keyword_htmx(
    request: Request,
    keyword: str = Form(...),
    category: str = Form(""),
    notes: str = Form(""),
):
    from scanner.db import add_user_keyword
    with get_conn() as conn:
        new_id = add_user_keyword(conn, keyword=keyword,
                                   category=category or None, notes=notes)
    kw = {"id": new_id, "keyword": keyword, "category": category or None, "notes": notes}
    return templates.TemplateResponse(request=request, name="partials/keyword_row.html",
                                      context={"kw": kw})


@router.post("/manage/sources", response_class=HTMLResponse)
async def add_source_htmx(
    request: Request,
    name: str = Form(...),
    home_url: str = Form(...),
    feed_url: str = Form(""),
    category: str = Form(...),
    language: str = Form("zh"),
    relevance_threshold: int = Form(5),
    notes: str = Form(""),
):
    from scanner.db import add_user_source
    with get_conn() as conn:
        new_id = add_user_source(conn, name=name, home_url=home_url,
                                  feed_url=feed_url or None, category=category,
                                  language=language, relevance_threshold=relevance_threshold,
                                  notes=notes)
    src = {"id": new_id, "name": name, "home_url": home_url, "category": category,
           "language": language, "relevance_threshold": relevance_threshold}
    return templates.TemplateResponse(request=request, name="partials/source_row.html",
                                      context={"src": src})


@router.post("/feedback", response_class=HTMLResponse)
async def record_feedback(
    request: Request,
    article_id: int = Form(...),
    action: str = Form(...),
):
    """Toggle upvote/downvote; returns updated button partial for HTMX."""
    if action not in ("upvote", "downvote"):
        raise HTTPException(status_code=400, detail="Invalid action")
    with get_conn() as conn:
        new_vote = upsert_vote(conn, article_id, action)
    return templates.TemplateResponse(
        request=request,
        name="partials/feedback_buttons.html",
        context={"article_id": article_id, "vote": new_vote},
    )


def _enrich(article: dict) -> dict:
    if isinstance(article.get("themes"), str):
        try:
            article["themes"] = json.loads(article["themes"])
        except (json.JSONDecodeError, TypeError):
            article["themes"] = []
    return article
