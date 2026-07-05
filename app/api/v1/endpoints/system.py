from fastapi import APIRouter

from app.core.queue import file_queue
from app.services.upload_service import discover_and_enqueue

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/run-now")
def run_now():
    discover_and_enqueue()
    return {"status": "triggered"}


@router.get("/queue-status")
def queue_status():
    """Lets external tooling (tests, monitoring) observe queue depth without
    reaching into process internals.

    queue_size (qsize) drops to 0 as soon as a worker dequeues the last item -
    while it may still be mid-flight (network calls, file move, DB commit).
    unfinished_tasks only drops once the worker calls task_done(), so it's the
    correct "is everything truly done" signal for callers like the test
    harness that need to know processing has actually finished.
    """
    return {
        "queue_size": file_queue.qsize(),
        "unfinished_tasks": file_queue.unfinished_tasks,
    }
