import sqlite3
import time
from dataclasses import dataclass, field
from typing import Optional

from .db import record_source_run


@dataclass
class SourceRunResult:
    source_name: str
    start_time: float = field(default_factory=time.monotonic)
    fetch_ok: bool = True
    articles_fetched: int = 0
    articles_passed: int = 0
    error_msg: str = ""

    def finish(self, run_at: str, conn: sqlite3.Connection) -> None:
        duration_ms = int((time.monotonic() - self.start_time) * 1000)
        record_source_run(
            conn,
            source_name=self.source_name,
            run_at=run_at,
            fetch_ok=self.fetch_ok,
            articles_fetched=self.articles_fetched,
            articles_passed=self.articles_passed,
            error_msg=self.error_msg,
            duration_ms=duration_ms,
        )
