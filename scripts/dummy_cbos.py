"""Local simulator for CBOS's document upload endpoint.

CBOS exposes a single upload API - this simulator implements exactly that,
plus /health, plus a couple of inspection endpoints (/uploads, /stats) that
run_local_test.py uses to verify results without touching the filesystem.
"""

import logging
import os
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile

load_dotenv(".env.test")
load_dotenv()  # fallback to .env if .env.test isn't present

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("dummy_cbos")

STORAGE_DIR = Path(os.getenv("DUMMY_CBOS_STORAGE", "dummy_cbos_storage"))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# success | fail | random (random = CBOS_RANDOM_SUCCESS_RATE chance of success)
SIMULATION_MODE = os.getenv("CBOS_SIMULATION_MODE", "success").lower()
RANDOM_SUCCESS_RATE = float(os.getenv("CBOS_RANDOM_SUCCESS_RATE", "0.7"))

app = FastAPI(title="Dummy CBOS")

_uploads: list[dict] = []
_stats = {"total_received": 0, "successful": 0, "failed": 0}


def _should_succeed() -> bool:
    if SIMULATION_MODE == "success":
        return True
    if SIMULATION_MODE == "fail":
        return False
    return random.random() < RANDOM_SUCCESS_RATE


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if not file.filename:
        logger.error("Upload rejected: no file name provided")
        raise HTTPException(status_code=400, detail="file name is required")

    logger.info("File received: %s", file.filename)
    content = await file.read()
    if not content:
        logger.error("Upload rejected: %s is empty", file.filename)
        raise HTTPException(status_code=400, detail="uploaded file is empty")

    upload_id = str(uuid.uuid4())
    logger.info("Upload ID generated: %s", upload_id)

    _stats["total_received"] += 1

    if not _should_succeed():
        _stats["failed"] += 1
        logger.error("Simulated failure for %s (mode=%s)", file.filename, SIMULATION_MODE)
        raise HTTPException(
            status_code=500,
            detail={
                "status": "failed",
                "upload_id": upload_id,
                "file_name": file.filename,
                "message": "Simulated CBOS failure",
            },
        )

    dest_path = STORAGE_DIR / f"{upload_id}_{file.filename}"
    with open(dest_path, "wb") as fh:
        fh.write(content)
    logger.info("File saved: %s", dest_path)

    _uploads.append({
        "upload_id": upload_id,
        "file_name": file.filename,
        "size_bytes": len(content),
        "received_at": datetime.now(timezone.utc).isoformat(),
    })
    _stats["successful"] += 1

    return {
        "status": "success",
        "upload_id": upload_id,
        "file_name": file.filename,
        "message": "File uploaded successfully",
    }


@app.get("/uploads")
def get_uploads():
    return {"count": len(_uploads), "uploads": _uploads}


@app.get("/stats")
def get_stats():
    return _stats


@app.get("/health")
def health():
    return {"status": "ok", "mode": SIMULATION_MODE}
