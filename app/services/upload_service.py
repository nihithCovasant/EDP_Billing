"""Coordinates file discovery, queueing, CBOS upload, database updates, and
file movement. The scheduler only calls discover_and_enqueue(); the worker
only calls process_task(). All the actual orchestration logic lives here,
not in either of those thin callers."""

import logging
from datetime import datetime
from pathlib import Path

from app.clients import cbos_client
from app.core.database import SessionLocal
from app.core.queue import FileTask, enqueue
from app.repositories.uploaded_file_repository import UploadedFileRepository
from app.services import file_service

logger = logging.getLogger("upload_service")


# --------------------------------------------------------------------------
# Discovery: scan the filesystem and enqueue new files. No CBOS calls.
# --------------------------------------------------------------------------

def discover_and_enqueue() -> None:
    """Walk {FILE_ROOT_PATH}/{segment}/{date}/{file} for both T and T-1 and
    push newly-discovered, not-yet-uploaded files onto the upload queue."""
    root = file_service.get_root()

    session = SessionLocal()
    try:
        repo = UploadedFileRepository(session)
        for folder_date in file_service.get_processing_dates():
            _discover_date(repo, root, folder_date)
    finally:
        session.close()


def _discover_date(repo: UploadedFileRepository, root: Path, folder_date: str) -> None:
    logger.info("Processing date: %s", folder_date)

    files_found = 0
    for file_path, segment in file_service.discover_files_for_date(root, folder_date):
        logger.info("Found file: %s", file_path.name)
        files_found += 1
        _maybe_enqueue(repo, file_path, folder_date, segment)

    logger.info("Found %d files for %s", files_found, folder_date)


def _maybe_enqueue(repo: UploadedFileRepository, file_path: Path, folder_date: str, segment: str) -> None:
    if repo.is_uploaded(file_path):
        logger.debug("Skipping already-uploaded file %s", file_path)
        return

    enqueue(FileTask(file_path=str(file_path), folder_date=folder_date, segment=segment))


# --------------------------------------------------------------------------
# Manual upload endpoint support
# --------------------------------------------------------------------------

def save_manual_upload(content: bytes, file_name: str, segment: str, repo: UploadedFileRepository):
    """Used by POST /upload: save the file to the standard location and mark
    it pending. Never talks to CBOS directly - the scheduler's next pass
    discovers and enqueues it like any other file."""
    dest_path = file_service.save_uploaded_file(content, file_name, segment)
    return repo.upsert_pending(dest_path, folder_date=file_service.get_today_folder_name(), segment=segment)


# --------------------------------------------------------------------------
# Worker: process a single queued file.
# --------------------------------------------------------------------------

def process_task(task: FileTask) -> None:
    """The full per-file flow: upload to CBOS -> move to upload/ or fail/ ->
    update the database. Called by the worker loop for one queue item at a time."""
    file_path = Path(task.file_path)
    if not file_path.exists():
        logger.warning("Queued file no longer exists, skipping: %s", file_path)
        return

    session = SessionLocal()
    try:
        repo = UploadedFileRepository(session)
        record = repo.get_or_create(file_path, task.folder_date, task.segment)

        logger.info("Processing file: %s", file_path.name)
        try:
            response = cbos_client.upload_file(str(file_path), file_path.name)
            logger.info("Upload successful: %s", file_path.name)

            dest_path = file_service.move_to_upload(file_path)
            repo.update(
                record,
                status="uploaded",
                cbos_response=str(response),
                cbos_upload_id=response.get("upload_id"),
                uploaded_at=datetime.utcnow(),
                file_path=str(dest_path),
            )
            logger.info("Moved to upload folder")

        except cbos_client.CBOSUploadError as exc:
            logger.error("Upload failed: %s", file_path.name)

            dest_path = file_service.move_to_failed(file_path)
            repo.update(
                record,
                status="failed",
                cbos_response=str(exc),
                retry_count=(record.retry_count or 0) + 1,
                file_path=str(dest_path),
            )
            logger.info("Moved to failed folder")

        repo.commit()
    finally:
        session.close()
