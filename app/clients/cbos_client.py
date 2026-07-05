import logging

import requests

from app.core.config import get_settings

logger = logging.getLogger("cbos_client")
settings = get_settings()


class CBOSUploadError(Exception):
    pass


def upload_file(file_path: str, file_name: str) -> dict:
    """Upload a single file to CBOS's document upload endpoint.

    Returns the parsed JSON response (expected shape:
    {"status": "success", "upload_id": "<uuid>", "file_name": ..., "message": ...}).
    Raises CBOSUploadError on non-2xx responses or transport failures.
    """
    try:
        with open(file_path, "rb") as fh:
            files = {"file": (file_name, fh)}
            response = requests.post(
                settings.cbos_upload_url, files=files, timeout=settings.cbos_timeout_seconds
            )
    except requests.RequestException as exc:
        raise CBOSUploadError(f"Request to CBOS failed for {file_name}: {exc}") from exc

    if not response.ok:
        raise CBOSUploadError(
            f"CBOS upload failed for {file_name}: {response.status_code} {response.text}"
        )

    return response.json()
