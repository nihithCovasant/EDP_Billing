"""Filesystem-only concerns: walking the segment/date tree, and moving files
between source/upload/fail locations. No database, queue, or network calls
happen here - see upload_service.py for orchestration."""

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from app.core.config import get_settings

settings = get_settings()

UPLOAD_SUBFOLDER = "upload"
FAILED_SUBFOLDER = "fail"
# Never treated as a segment/date folder / never scanned for source files.
_PROCESSED_SUBFOLDER_NAMES = {UPLOAD_SUBFOLDER, FAILED_SUBFOLDER}


def get_root() -> Path:
    return Path(settings.file_root_path)


def get_today_folder_name() -> str:
    return datetime.now().strftime(settings.date_folder_format)


def get_processing_dates() -> list[str]:
    """Dates the scheduler should scan, most recent first: [T, T-1]."""
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    return [today.strftime(settings.date_folder_format), yesterday.strftime(settings.date_folder_format)]


def list_subdirs(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return [
        p
        for p in folder.iterdir()
        if p.is_dir() and p.name not in _PROCESSED_SUBFOLDER_NAMES
    ]


def list_files(folder: Path) -> list[Path]:
    """Files directly under `folder`. Never recurses, so upload/ and fail/
    subfolders are naturally excluded."""
    if not folder.is_dir():
        return []
    return [p for p in folder.iterdir() if p.is_file()]


def discover_files_for_date(root: Path, folder_date: str):
    """Yields (file_path, segment) for every source file found directly
    under root/{segment}/{folder_date}/."""
    for segment_folder in list_subdirs(root):
        date_folder = segment_folder / folder_date
        for file_path in list_files(date_folder):
            yield file_path, segment_folder.name


def build_destination_dir(segment: str, folder_date: str | None = None) -> Path:
    """Build {FILE_ROOT_PATH}/{segment}/{date}/ for a given segment."""
    folder_date = folder_date or get_today_folder_name()
    return get_root() / segment / folder_date


def save_uploaded_file(content: bytes, file_name: str, segment: str) -> Path:
    """Write uploaded bytes to the standard segment/date folder, creating dirs as needed."""
    dest_dir = build_destination_dir(segment)
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / file_name
    with open(dest_path, "wb") as fh:
        fh.write(content)

    return dest_path


def move_to_upload(file_path: Path) -> Path:
    return _move_file(file_path, UPLOAD_SUBFOLDER)


def move_to_failed(file_path: Path) -> Path:
    return _move_file(file_path, FAILED_SUBFOLDER)


def _move_file(file_path: Path, subfolder_name: str) -> Path:
    """Move file_path into a sibling subfolder (upload/ or fail/) of its parent
    (the date folder), creating the subfolder if needed. Removes the file
    from its source location."""
    dest_dir = file_path.parent / subfolder_name
    dest_dir.mkdir(parents=True, exist_ok=True)

    dest_path = dest_dir / file_path.name
    shutil.move(str(file_path), str(dest_path))
    return dest_path
