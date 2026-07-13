"""Export articles from SQLite to docs/data/articles.json for GitHub Pages."""
from __future__ import annotations
import json
from pathlib import Path

from scanner.db import get_conn
from scanner.settings import settings

OUTPUT = Path(__file__).parent.parent / "docs" / "data" / "articles.json"


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, title, url, source_name, category,
                   published_at, stored_at, relevance_score, summary, themes
            FROM articles
            WHERE is_duplicate = 0
            ORDER BY stored_at DESC
            LIMIT 1000
        """).fetchall()

        last_run = conn.execute("""
            SELECT started_at, raw_fetched, new_articles
            FROM pipeline_runs
            ORDER BY started_at DESC LIMIT 1
        """).fetchone()

    articles = []
    for row in rows:
        a = dict(row)
        if isinstance(a.get("themes"), str):
            try:
                a["themes"] = json.loads(a["themes"])
            except Exception:
                a["themes"] = []
        articles.append(a)

    data = {
        "last_scan": dict(last_run) if last_run else None,
        "articles": articles,
    }

    OUTPUT.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"Exported {len(articles)} articles → {OUTPUT}")


if __name__ == "__main__":
    main()
