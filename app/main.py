import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.database import init_db
from app.core.logging import configure_logging
from app.scheduler.scheduler import start_scheduler, stop_scheduler
from app.workers.upload_worker import run as run_worker

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Server Start -> Initialize DB -> Start Queue Worker -> Start Scheduler
    # -> Begin File Processing. No manual startup step is required for
    # either the scheduler or the worker.
    init_db()

    worker_thread = threading.Thread(target=run_worker, name="cbos-upload-worker", daemon=True)
    worker_thread.start()

    start_scheduler()

    yield
    stop_scheduler()


app = FastAPI(title="File Uploader", lifespan=lifespan)
app.include_router(api_router)
