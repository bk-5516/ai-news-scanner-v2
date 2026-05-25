"""FastAPI application with APScheduler for periodic scans."""
import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from scanner.db import init_db
from scanner.settings import settings
from web.routes.api import router as api_router
from web.routes.dashboard import router as dashboard_router

log = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _scheduled_scan() -> None:
    log.info("Scheduled scan starting...")
    try:
        from scanner.pipeline import run_scan
        stats = await run_scan(verbose=True)
        log.info("Scheduled scan done: %s", stats)
    except Exception as exc:
        log.error("Scheduled scan failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(
        _scheduled_scan,
        "interval",
        hours=settings.scan_interval_hours,
        id="periodic_scan",
        replace_existing=True,
    )
    scheduler.start()
    log.info("Scheduler started — scan every %dh", settings.scan_interval_hours)
    yield
    scheduler.shutdown()


app = FastAPI(title="AI News Scanner v2", lifespan=lifespan)
app.include_router(dashboard_router)
app.include_router(api_router)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    uvicorn.run(
        "web.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
