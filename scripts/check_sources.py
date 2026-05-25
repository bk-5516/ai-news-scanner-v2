#!/usr/bin/env python3
"""Test all configured sources and print a health report."""
import asyncio
import sys
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env", override=True)

sys.path.insert(0, str(Path(__file__).parent.parent))

from scanner.fetcher import fetch_all
from scanner.loader import load_sources


async def main() -> None:
    sources = load_sources()
    print(f"Checking {len(sources)} sources...\n")

    results = await fetch_all(sources)

    ok_count = 0
    fail_count = 0
    total_articles = 0

    rows = []
    for src, articles, error in results:
        status = "OK " if not error else "ERR"
        n = len(articles)
        total_articles += n
        if error:
            fail_count += 1
        else:
            ok_count += 1
        rows.append((status, src.category, src.name, n, error or ""))

    # Sort: errors first, then by category, then by name
    rows.sort(key=lambda r: (r[0] != "ERR", r[1], r[2]))

    col_w = [5, 12, 45, 8, 50]
    header = f"{'ST':<{col_w[0]}} {'CATEGORY':<{col_w[1]}} {'SOURCE':<{col_w[2]}} {'ARTICLES':>{col_w[3]}} {'ERROR':<{col_w[4]}}"
    print(header)
    print("─" * sum(col_w))
    for status, cat, name, n, err in rows:
        err_short = err[:48] + ".." if len(err) > 48 else err
        print(f"{status:<{col_w[0]}} {cat:<{col_w[1]}} {name:<{col_w[2]}} {n:>{col_w[3]}} {err_short}")

    print("─" * sum(col_w))
    print(f"\nSummary: {ok_count} OK, {fail_count} failed, {total_articles} total articles")


if __name__ == "__main__":
    asyncio.run(main())
