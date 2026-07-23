"""Engine-owned saga states (wayfinder ticket 10 / BATCH_HANDOFF_CONTRACT.md):
INIT -> DOWNLOADING -> UPLOADING -> WAITING_FOR_FILE_UPLOAD for
config.download_segments (MCX + EQ), with the bot/uploader driven through
EdpbClient. Scripted fake clients pin the failure semantics: no_data waits,
download failures exhaust a bounded budget, uploader 4xx is terminal,
transient uploader errors retry, and an INCOMPLETE batch fails the segment
loudly instead of waiting out the window.
"""

from __future__ import annotations

from datetime import date

from src.agent.edp import edpb_client as edpb_client_module
from src.agent.edp.edpb_client import (
    BatchStatusResult,
    BatchSubmitResult,
    DownloadResult,
    EdpbClient,
)
from src.agent.edp.models import SegmentState, SegmentStatus
from src.agent.edp.orchestrator import EdpOrchestrator
from src.tools.cbos_client import CbosClient

from . import helpers


class ScriptedEdpbClient(EdpbClient):
    """Mock client whose download/submit/status answers come from scripts —
    per-segment lists consumed one call at a time (last entry repeats)."""

    def __init__(self, downloads=None, submits=None, statuses=None):
        super().__init__("http://bot.mock", "http://uploader.mock", use_mock=True)
        self._downloads: dict[str, list[DownloadResult]] = downloads or {}
        self._submits: list[BatchSubmitResult] = submits or []
        self._statuses: dict[str, BatchStatusResult] = statuses or {}
        self.download_calls: list[tuple[str, date]] = []
        self.submit_calls: list[str] = []
        self.correlation_ids: dict[str, list[str | None]] = {"download": [], "submit": [], "status": []}

    async def request_download(
        self,
        segment: str,
        trade_date: date,
        correlation_id: str | None = None,
    ) -> DownloadResult:
        self.download_calls.append((segment.upper(), trade_date))
        self.correlation_ids["download"].append(correlation_id)
        script = self._downloads.get(segment.upper())
        if not script:
            return self._mock_download(segment.upper(), trade_date)
        return script.pop(0) if len(script) > 1 else script[0]

    async def submit_batch(
        self,
        manifest_path: str,
        correlation_id: str | None = None,
    ) -> BatchSubmitResult:
        self.submit_calls.append(manifest_path)
        self.correlation_ids["submit"].append(correlation_id)
        if not self._submits:
            return self._mock_submit(manifest_path)
        return self._submits.pop(0) if len(self._submits) > 1 else self._submits[0]

    async def get_batch_status(
        self,
        batch_id: str,
        correlation_id: str | None = None,
    ) -> BatchStatusResult:
        self.correlation_ids["status"].append(correlation_id)
        return self._statuses.get(batch_id, BatchStatusResult(found=True, status="confirmed"))


def _orchestrator(cfg):
    cbos = CbosClient(cfg.cbos_status_url, cfg.cbos_process_url, use_mock=True)
    cbos.mock_set_ready_after(1)
    return EdpOrchestrator(cfg, cbos)


async def test_download_segments_traverse_saga_states(cfg, session_factory, test_date):
    """MCX + EQ complete via INIT -> DOWNLOADING -> UPLOADING -> ... and their
    processes_json records the manifest handoff; non-download segments (CUR)
    never touch the new states."""
    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    for code in ("MCX", "EQ"):
        row = by_code[code]
        assert row.segment_status == SegmentStatus.COMPLETED
        download = row.processes_json[SegmentState.DOWNLOADING.value]
        assert download["status"] == "COMPLETED"
        assert download["batch_id"].startswith(f"{code}-")
        assert download["manifest_path"].endswith("/manifest.json")
        assert SegmentState.UPLOADING.value in row.processes_json

    cur = by_code["CUR"]
    assert cur.segment_status == SegmentStatus.COMPLETED
    assert SegmentState.DOWNLOADING.value not in cur.processes_json
    assert SegmentState.UPLOADING.value not in cur.processes_json


async def test_no_data_waits_then_succeeds(cfg, session_factory, test_date):
    """no_data = the exchange hasn't published yet: the segment stays in
    DOWNLOADING (no attempt burned) and succeeds when files appear."""
    client = ScriptedEdpbClient(
        downloads={
            "MCX": [
                DownloadResult(status="no_data", message="nothing published"),
                DownloadResult(status="no_data", message="nothing published"),
                DownloadResult(
                    status="success", manifest_path="/m/MCX/manifest.json", batch_id=f"MCX-{'2068-01-01'}-x1"
                ),
            ]
        }
    )
    edpb_client_module.set_edpb_client(client)

    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    assert by_code["MCX"].segment_status == SegmentStatus.COMPLETED
    mcx_downloads = [c for c in client.download_calls if c[0] == "MCX"]
    assert len(mcx_downloads) == 3, "two no_data waits, then the successful third call"


async def test_download_failure_exhausts_budget_then_fails(cfg, session_factory, test_date):
    client = ScriptedEdpbClient(
        downloads={
            "MCX": [
                DownloadResult(status="failed", message="auth_failed at portal"),
            ]
        }
    )
    edpb_client_module.set_edpb_client(client)

    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    row = by_code["MCX"]
    assert row.segment_status == SegmentStatus.FAILED
    assert row.skip_category == "DOWNLOAD_ERROR"
    assert "auth_failed" in (row.skip_reason or "")
    assert len([c for c in client.download_calls if c[0] == "MCX"]) == cfg.edpb_download_max_attempts


async def test_uploader_rejection_is_terminal(cfg, session_factory, test_date):
    """A 4xx from POST /batches (bad manifest) cannot be fixed by resending —
    the segment fails immediately with the uploader's message."""
    client = ScriptedEdpbClient(
        submits=[
            BatchSubmitResult(accepted=False, message="uploader HTTP 422: checksum mismatch"),
        ]
    )
    edpb_client_module.set_edpb_client(client)

    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    for code in ("MCX", "EQ"):
        assert by_code[code].segment_status == SegmentStatus.FAILED
        assert by_code[code].skip_category == "UPLOAD_ERROR"
        assert "checksum mismatch" in (by_code[code].skip_reason or "")


async def test_transient_uploader_error_retries_then_succeeds(cfg, session_factory, test_date):
    client = ScriptedEdpbClient(
        submits=[
            BatchSubmitResult(accepted=False, message="uploader unreachable", is_transient=True),
            BatchSubmitResult(accepted=False, message="uploader unreachable", is_transient=True),
            BatchSubmitResult(accepted=True, batch_id="MCX-x", batch_status="queued"),
        ]
    )
    edpb_client_module.set_edpb_client(client)

    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}
    # Both download segments share the scripted submit sequence; at least one
    # rode through the transient failures to COMPLETED.
    assert by_code["MCX"].segment_status == SegmentStatus.COMPLETED


async def test_incomplete_batch_fails_segment_loudly(cfg, session_factory, test_date):
    """The uploader's completeness gate parked the batch: FILEUPLOAD would
    stay FALSE forever. The engine must FAIL the segment now (terminal email
    fires) with the missing slots in the reason — not wait out the window."""
    downloads = {
        "MCX": [
            DownloadResult(
                status="success",
                manifest_path="/m/MCX/manifest.json",
                batch_id="MCX-INCOMPLETE-1",
            )
        ]
    }
    statuses = {
        "MCX-INCOMPLETE-1": BatchStatusResult(
            found=True,
            status="incomplete",
            missing_slots=[{"upload_id": "127", "step_no": 1, "name": "MCX Product Master Upload"}],
        )
    }
    client = ScriptedEdpbClient(downloads=downloads, statuses=statuses)
    edpb_client_module.set_edpb_client(client)

    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    row = by_code["MCX"]
    assert row.segment_status == SegmentStatus.FAILED
    assert row.skip_category == "BATCH_INCOMPLETE"
    assert "127" in (row.skip_reason or "")
    assert "MCX Product Master Upload" in (row.skip_reason or "")


async def test_one_correlation_id_per_segment_run(cfg, session_factory, test_date):
    """Ticket 11: the id minted at the first DOWNLOADING entry is persisted in
    processes_json and sent on EVERY call of that segment's run — download,
    submit, and batch-status alike — so one grep traces the whole journey."""
    client = ScriptedEdpbClient()
    edpb_client_module.set_edpb_client(client)

    orchestrator = _orchestrator(cfg)
    await helpers.seed_day(session_factory, test_date, cfg)
    rows = await helpers.drive_until_terminal(orchestrator, session_factory, test_date)
    by_code = {r.segment_code: r for r in rows}

    per_segment_ids = set()
    for code in ("MCX", "EQ"):
        cid = by_code[code].processes_json[SegmentState.DOWNLOADING.value]["correlation_id"]
        assert cid.startswith(f"edp-{code.lower()}-"), cid
        per_segment_ids.add(cid)
    assert len(per_segment_ids) == 2, "each segment-day run gets its OWN id"

    sent = set(client.correlation_ids["download"] + client.correlation_ids["submit"] + client.correlation_ids["status"])
    assert sent == per_segment_ids, (
        f"every client call must carry a per-run id (sent={sent}, expected={per_segment_ids})"
    )
