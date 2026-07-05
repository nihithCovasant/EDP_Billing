"""End-to-end local test harness.

Usage (from the project root):
    python -m scripts.run_local_test [success|fail|random]

If a mode is given, it overrides CBOS_SIMULATION_MODE from .env.test for
this run (handy for exercising Scenario 1 / 2 / 3 without editing the file).

Flow: generate test data -> start the dummy CBOS server (scripts/dummy_cbos.py)
-> start the File Upload Handler (app.main:app) -> trigger the scheduler ->
wait for the queue to drain -> verify filesystem + database results -> tear
everything down.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

import requests
from dotenv import dotenv_values

# This file lives in scripts/, so the project root (where .env.test and the
# app/ package live) is one level up.
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env.test"

MAIN_APP_URL = "http://127.0.0.1:8000"
DUMMY_CBOS_URL = "http://127.0.0.1:9000"

HEALTH_TIMEOUT_SECONDS = 30
QUEUE_DRAIN_TIMEOUT_SECONDS = 60
POLL_INTERVAL_SECONDS = 2


def load_test_env() -> dict:
    if not ENV_FILE.exists():
        raise RuntimeError(f"{ENV_FILE} not found")
    values = dotenv_values(ENV_FILE)
    return {k: v for k, v in values.items() if v is not None}


def wait_for_health(url: str, name: str, timeout: int = HEALTH_TIMEOUT_SECONDS) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = requests.get(f"{url}/health", timeout=2)
            if resp.ok:
                print(f"[OK] {name} is up at {url}")
                return
        except requests.RequestException:
            pass
        time.sleep(1)
    raise RuntimeError(f"{name} did not become healthy within {timeout}s")


def start_process(module: str, port: int, env: dict) -> subprocess.Popen:
    cmd = [
        sys.executable, "-m", "uvicorn", f"{module}:app",
        "--host", "127.0.0.1", "--port", str(port),
    ]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(cmd, cwd=str(BASE_DIR), env=env, creationflags=creationflags)


def stop_process(proc: subprocess.Popen, name: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    print(f"Stopping {name}...")
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def wait_for_queue_empty() -> None:
    from app.core.database import SessionLocal
    from app.models import UploadedFile

    deadline = time.time() + QUEUE_DRAIN_TIMEOUT_SECONDS
    stable_checks = 0

    while time.time() < deadline:
        try:
            status = requests.get(f"{MAIN_APP_URL}/queue-status", timeout=5).json()
            unfinished_tasks = status["unfinished_tasks"]
        except requests.RequestException:
            unfinished_tasks = -1

        session = SessionLocal()
        try:
            pending = session.query(UploadedFile).filter_by(status="pending").count()
        finally:
            session.close()

        print(f"  unfinished_tasks={unfinished_tasks} pending_in_db={pending}")

        # unfinished_tasks only drops once a worker calls task_done() - i.e.
        # truly finished, not just dequeued - unlike qsize() this has no
        # dequeued-but-still-processing race window.
        if unfinished_tasks == 0 and pending == 0:
            stable_checks += 1
            if stable_checks >= 2:
                print("Queue drained.")
                return
        else:
            stable_checks = 0

        time.sleep(POLL_INTERVAL_SECONDS)

    raise RuntimeError("Timed out waiting for queue to drain")


def verify_results() -> bool:
    from app.core.database import SessionLocal
    from app.models import UploadedFile

    root = Path(os.environ["FILE_ROOT_PATH"])

    remaining_source_files = [
        p for p in root.rglob("*")
        if p.is_file() and p.parent.name not in ("upload", "fail")
    ]
    uploaded_files = list(root.rglob("upload/*"))
    failed_files = list(root.rglob("fail/*"))

    session = SessionLocal()
    try:
        total = session.query(UploadedFile).count()
        uploaded_count = session.query(UploadedFile).filter_by(status="uploaded").count()
        failed_count = session.query(UploadedFile).filter_by(status="failed").count()
        pending_count = session.query(UploadedFile).filter_by(status="pending").count()
        retried_count = session.query(UploadedFile).filter(UploadedFile.retry_count > 0).count()
        registered_count = session.query(UploadedFile).filter(
            UploadedFile.cbos_upload_id.isnot(None)
        ).count()
    finally:
        session.close()

    try:
        cbos_stats = requests.get(f"{DUMMY_CBOS_URL}/stats", timeout=5).json()
    except requests.RequestException:
        cbos_stats = {}

    print("\n--- Filesystem ---")
    print(f"Remaining at source (expected 0): {len(remaining_source_files)}")
    print(f"Moved to upload/: {len(uploaded_files)}")
    print(f"Moved to fail/: {len(failed_files)}")

    print("\n--- Database (uploaded_files) ---")
    print(f"Total rows: {total} | uploaded={uploaded_count} failed={failed_count} pending={pending_count}")
    print(f"Rows with retry_count > 0: {retried_count}")
    print(f"Rows with cbos_upload_id set: {registered_count} (should equal uploaded={uploaded_count})")

    print("\n--- Dummy CBOS /stats ---")
    print(cbos_stats)

    passed = len(remaining_source_files) == 0 and pending_count == 0 and total > 0
    print(f"\nRESULT: {'PASS' if passed else 'FAIL'}")
    return passed


def main() -> None:
    mode_override = sys.argv[1] if len(sys.argv) > 1 else None
    if mode_override and mode_override not in ("success", "fail", "random"):
        raise SystemExit("mode must be one of: success, fail, random")

    test_env = load_test_env()
    if mode_override:
        test_env["CBOS_SIMULATION_MODE"] = mode_override

    full_env = os.environ.copy()
    full_env.update(test_env)

    # Applied to this process too, so the verification step reads the same
    # FILE_ROOT_PATH / DATABASE_URL the subprocesses were given.
    os.environ.update(test_env)

    print(f"=== Mode: {test_env.get('CBOS_SIMULATION_MODE')} ===")

    print("\n=== Step 1-2: Generating test data ===")
    subprocess.run(
        [sys.executable, "-m", "scripts.generate_test_data"], cwd=str(BASE_DIR), env=full_env, check=True
    )

    dummy_cbos_proc = None
    main_app_proc = None
    try:
        print("\n=== Step 3: Starting dummy CBOS server ===")
        dummy_cbos_proc = start_process("scripts.dummy_cbos", 9000, full_env)
        wait_for_health(DUMMY_CBOS_URL, "Dummy CBOS")

        print("\n=== Step 4: Starting File Upload Handler ===")
        main_app_proc = start_process("app.main", 8000, full_env)
        wait_for_health(MAIN_APP_URL, "File Upload Handler")

        print("\n=== Step 5: Triggering scheduler ===")
        resp = requests.post(f"{MAIN_APP_URL}/run-now", timeout=10)
        resp.raise_for_status()
        print("Scheduler triggered:", resp.json())

        print("\n=== Step 6: Waiting for queue to drain ===")
        wait_for_queue_empty()

        print("\n=== Step 7: Verifying results ===")
        passed = verify_results()

    finally:
        stop_process(main_app_proc, "File Upload Handler")
        stop_process(dummy_cbos_proc, "Dummy CBOS")

    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
