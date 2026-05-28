import sqlite3
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from .models import RawArticle, ScoredArticle
from .settings import settings


def init_db(db_path: Optional[Path] = None) -> None:
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


@contextmanager
def get_conn(db_path: Optional[Path] = None) -> Generator[sqlite3.Connection, None, None]:
    path = db_path or settings.db_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_raw_article(conn: sqlite3.Connection, article: RawArticle) -> Optional[int]:
    """Insert raw article; returns rowid or None if URL already exists."""
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO raw_articles
               (url, url_hash, title, snippet, source_name, category, fetched_at, published_at, raw_feed_entry)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (article.url, article.url_hash, article.title, article.snippet,
             article.source_name, article.category, article.fetched_at,
             article.published_at, article.raw_feed_entry),
        )
        if cur.rowcount == 0:
            return None
        conn.execute("INSERT OR IGNORE INTO seen_urls (url_hash, first_seen) VALUES (?, ?)",
                     (article.url_hash, article.fetched_at))
        return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def is_url_seen(conn: sqlite3.Connection, url_hash: str) -> bool:
    row = conn.execute("SELECT 1 FROM seen_urls WHERE url_hash = ?", (url_hash,)).fetchone()
    return row is not None


def insert_article(conn: sqlite3.Connection, article: ScoredArticle) -> int:
    cur = conn.execute(
        """INSERT OR REPLACE INTO articles
           (raw_id, url, title, summary, source_name, category, published_at,
            relevance_score, themes, is_duplicate, duplicate_of_id, stored_at, llm_model, llm_cached)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (article.raw_id, article.url, article.title, article.summary,
         article.source_name, article.category, article.published_at,
         article.relevance_score, json.dumps(article.themes, ensure_ascii=False),
         int(article.is_duplicate), article.duplicate_of_id, article.stored_at,
         article.llm_model, int(article.llm_cached)),
    )
    return cur.lastrowid


def record_source_run(conn: sqlite3.Connection, source_name: str, run_at: str,
                      fetch_ok: bool, articles_fetched: int = 0,
                      articles_passed: int = 0, error_msg: str = "",
                      duration_ms: int = 0) -> None:
    conn.execute(
        """INSERT INTO source_runs
           (source_name, run_at, fetch_ok, articles_fetched, articles_passed_llm, error_msg, duration_ms)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (source_name, run_at, int(fetch_ok), articles_fetched, articles_passed,
         error_msg or None, duration_ms),
    )


def start_pipeline_run(conn: sqlite3.Connection, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO pipeline_runs (started_at) VALUES (?)", (started_at,)
    )
    return cur.lastrowid


def finish_pipeline_run(conn: sqlite3.Connection, run_id: int, finished_at: str,
                        raw_fetched: int, new_articles: int, dupes_found: int,
                        llm_calls: int, llm_cost_est: float,
                        error: str = "") -> None:
    conn.execute(
        """UPDATE pipeline_runs SET finished_at=?, raw_fetched=?, new_articles=?,
           dupes_found=?, llm_calls=?, llm_cost_est_usd=?, error=?
           WHERE id=?""",
        (finished_at, raw_fetched, new_articles, dupes_found, llm_calls,
         llm_cost_est, error or None, run_id),
    )


def get_articles(conn: sqlite3.Connection, category: str, min_score: int = 0,
                 theme: Optional[str] = None, days: int = 7,
                 limit: int = 25, offset: int = 0) -> list[sqlite3.Row]:
    params: list = [category, min_score]
    theme_clause = ""
    if theme:
        theme_clause = "AND themes LIKE ?"
        params.append(f'%{theme}%')
    params += [days, limit, offset]
    return conn.execute(
        f"""SELECT * FROM articles
            WHERE category = ? AND relevance_score >= ? AND is_duplicate = 0
            {theme_clause}
            AND (published_at >= datetime('now', '-' || ? || ' days') OR published_at IS NULL)
            ORDER BY COALESCE(published_at, stored_at) DESC
            LIMIT ? OFFSET ?""",
        params,
    ).fetchall()


def search_articles(conn: sqlite3.Connection, query: str, category: Optional[str] = None,
                    limit: int = 25) -> list[sqlite3.Row]:
    if category:
        return conn.execute(
            """SELECT a.* FROM articles a
               JOIN articles_fts fts ON a.id = fts.rowid
               WHERE articles_fts MATCH ? AND a.category = ? AND a.is_duplicate = 0
               ORDER BY rank LIMIT ?""",
            (query, category, limit),
        ).fetchall()
    return conn.execute(
        """SELECT a.* FROM articles a
           JOIN articles_fts fts ON a.id = fts.rowid
           WHERE articles_fts MATCH ? AND a.is_duplicate = 0
           ORDER BY rank LIMIT ?""",
        (query, limit),
    ).fetchall()


def get_source_health(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT
               sr.source_name,
               MAX(sr.run_at) AS last_run,
               SUM(CASE WHEN sr.run_at >= datetime('now', '-7 days') THEN 1 ELSE 0 END) AS runs_7d,
               SUM(CASE WHEN sr.run_at >= datetime('now', '-7 days') AND sr.fetch_ok = 1 THEN 1 ELSE 0 END) AS ok_7d,
               SUM(CASE WHEN sr.run_at >= datetime('now', '-7 days') THEN sr.articles_fetched ELSE 0 END) AS fetched_7d,
               SUM(CASE WHEN sr.run_at >= datetime('now', '-7 days') THEN sr.articles_passed_llm ELSE 0 END) AS passed_7d,
               (SELECT error_msg FROM source_runs sr2
                WHERE sr2.source_name = sr.source_name ORDER BY run_at DESC LIMIT 1) AS last_error
           FROM source_runs sr
           GROUP BY sr.source_name
           ORDER BY last_run DESC""",
    ).fetchall()


def get_last_pipeline_run(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()


# ── Feedback helpers ──────────────────────────────────────────────────────────

def get_votes_for_articles(conn: sqlite3.Connection, article_ids: list[int]) -> dict[int, str]:
    """Return {article_id: vote} for the given article IDs that have a vote."""
    if not article_ids:
        return {}
    placeholders = ",".join("?" * len(article_ids))
    rows = conn.execute(
        f"SELECT article_id, vote FROM article_votes WHERE article_id IN ({placeholders})",
        article_ids,
    ).fetchall()
    return {row["article_id"]: row["vote"] for row in rows}


def upsert_vote(conn: sqlite3.Connection, article_id: int, action: str) -> Optional[str]:
    """Toggle vote for an article. Returns new vote ('upvote'/'downvote') or None if cleared."""
    now = datetime.now(timezone.utc).isoformat()
    current = conn.execute(
        "SELECT vote FROM article_votes WHERE article_id = ?", (article_id,)
    ).fetchone()
    current_vote = current["vote"] if current else None

    if current_vote == action:
        # Same vote clicked again — remove it
        conn.execute("DELETE FROM article_votes WHERE article_id = ?", (article_id,))
        conn.execute(
            "INSERT INTO article_feedback (article_id, action, created_at) VALUES (?, 'unvote', ?)",
            (article_id, now),
        )
        return None
    else:
        # New or changed vote
        conn.execute(
            "INSERT OR REPLACE INTO article_votes (article_id, vote, updated_at) VALUES (?, ?, ?)",
            (article_id, action, now),
        )
        conn.execute(
            "INSERT INTO article_feedback (article_id, action, created_at) VALUES (?, ?, ?)",
            (article_id, action, now),
        )
        return action


def log_click(conn: sqlite3.Connection, article_id: int) -> None:
    """Record a click event."""
    conn.execute(
        "INSERT INTO article_feedback (article_id, action, created_at) VALUES (?, 'click', ?)",
        (article_id, datetime.now(timezone.utc).isoformat()),
    )


# ── User sources helpers ──────────────────────────────────────────────────────

def get_user_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM user_sources WHERE enabled = 1 ORDER BY created_at"
    ).fetchall()


def get_all_user_sources(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM user_sources ORDER BY created_at").fetchall()


def add_user_source(conn: sqlite3.Connection, name: str, home_url: str,
                    feed_url: Optional[str], category: str, language: str,
                    relevance_threshold: int, notes: str) -> int:
    cur = conn.execute(
        """INSERT INTO user_sources (name, home_url, feed_url, category, language,
           relevance_threshold, notes, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, home_url, feed_url or None, category, language,
         relevance_threshold, notes, datetime.now(timezone.utc).isoformat()),
    )
    return cur.lastrowid


def delete_user_source(conn: sqlite3.Connection, source_id: int) -> None:
    conn.execute("DELETE FROM user_sources WHERE id = ?", (source_id,))


# ── User keywords helpers ─────────────────────────────────────────────────────

def get_user_keywords(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM user_keywords ORDER BY created_at").fetchall()


def add_user_keyword(conn: sqlite3.Connection, keyword: str,
                     category: Optional[str], notes: str) -> int:
    cur = conn.execute(
        "INSERT INTO user_keywords (keyword, category, notes, created_at) VALUES (?, ?, ?, ?)",
        (keyword, category or None, notes, datetime.now(timezone.utc).isoformat()),
    )
    return cur.lastrowid


def delete_user_keyword(conn: sqlite3.Connection, keyword_id: int) -> None:
    conn.execute("DELETE FROM user_keywords WHERE id = ?", (keyword_id,))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    url_hash        TEXT NOT NULL,
    title           TEXT NOT NULL,
    snippet         TEXT DEFAULT '',
    source_name     TEXT NOT NULL,
    category        TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    published_at    TEXT,
    raw_feed_entry  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_raw_url_hash   ON raw_articles(url_hash);
CREATE INDEX IF NOT EXISTS idx_raw_fetched_at ON raw_articles(fetched_at);
CREATE INDEX IF NOT EXISTS idx_raw_source     ON raw_articles(source_name);

CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id          INTEGER NOT NULL REFERENCES raw_articles(id),
    url             TEXT NOT NULL UNIQUE,
    title           TEXT NOT NULL,
    summary         TEXT DEFAULT '',
    source_name     TEXT NOT NULL,
    category        TEXT NOT NULL,
    published_at    TEXT,
    relevance_score INTEGER NOT NULL DEFAULT 0,
    themes          TEXT NOT NULL DEFAULT '[]',
    is_duplicate    INTEGER NOT NULL DEFAULT 0,
    duplicate_of_id INTEGER REFERENCES articles(id),
    stored_at       TEXT NOT NULL,
    llm_model       TEXT DEFAULT '',
    llm_cached      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_articles_category   ON articles(category);
CREATE INDEX IF NOT EXISTS idx_articles_published  ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_relevance  ON articles(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source     ON articles(source_name);
CREATE INDEX IF NOT EXISTS idx_articles_stored_at  ON articles(stored_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_not_dup    ON articles(is_duplicate, relevance_score DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
    title,
    summary,
    themes,
    content='articles',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
    INSERT INTO articles_fts(rowid, title, summary, themes)
    VALUES (new.id, new.title, new.summary, new.themes);
END;
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, themes)
    VALUES ('delete', old.id, old.title, old.summary, old.themes);
END;
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
    INSERT INTO articles_fts(articles_fts, rowid, title, summary, themes)
    VALUES ('delete', old.id, old.title, old.summary, old.themes);
    INSERT INTO articles_fts(rowid, title, summary, themes)
    VALUES (new.id, new.title, new.summary, new.themes);
END;

CREATE TABLE IF NOT EXISTS source_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name         TEXT NOT NULL,
    run_at              TEXT NOT NULL,
    fetch_ok            INTEGER NOT NULL,
    articles_fetched    INTEGER NOT NULL DEFAULT 0,
    articles_passed_llm INTEGER NOT NULL DEFAULT 0,
    error_msg           TEXT,
    duration_ms         INTEGER
);
CREATE INDEX IF NOT EXISTS idx_source_runs_name ON source_runs(source_name, run_at DESC);

CREATE TABLE IF NOT EXISTS seen_urls (
    url_hash  TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    raw_fetched      INTEGER DEFAULT 0,
    new_articles     INTEGER DEFAULT 0,
    dupes_found      INTEGER DEFAULT 0,
    llm_calls        INTEGER DEFAULT 0,
    llm_cost_est_usd REAL DEFAULT 0.0,
    error            TEXT
);

CREATE TABLE IF NOT EXISTS article_votes (
    article_id  INTEGER PRIMARY KEY REFERENCES articles(id),
    vote        TEXT NOT NULL CHECK(vote IN ('upvote', 'downvote')),
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id  INTEGER NOT NULL REFERENCES articles(id),
    action      TEXT NOT NULL CHECK(action IN ('upvote', 'downvote', 'unvote', 'click')),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_feedback_article ON article_feedback(article_id, created_at DESC);

CREATE TABLE IF NOT EXISTS user_sources (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    name                TEXT NOT NULL,
    home_url            TEXT NOT NULL,
    feed_url            TEXT,
    category            TEXT NOT NULL,
    language            TEXT NOT NULL DEFAULT 'zh',
    enabled             INTEGER NOT NULL DEFAULT 1,
    relevance_threshold INTEGER NOT NULL DEFAULT 5,
    notes               TEXT DEFAULT '',
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT NOT NULL,
    category    TEXT,
    notes       TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
"""
