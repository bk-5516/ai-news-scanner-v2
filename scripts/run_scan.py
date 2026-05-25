#!/usr/bin/env python3
"""Run a full scan cycle: fetch → LLM score → summarize → store."""
import asyncio
import logging
import sys
from pathlib import Path

# Load .env BEFORE importing settings so env-var overrides don't shadow the file.
# dotenv only sets variables that aren't already set (unless override=True).
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.db import init_db
from scanner.pipeline import run_scan
from scanner.settings import settings

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)


async def main() -> None:
    init_db()
    stats = await run_scan(verbose=True)
    print("\n── Scan Stats ──────────────────")
    for k, v in stats.items():
        print(f"  {k:<15} {v}")


if __name__ == "__main__":
    asyncio.run(main())
