"""Async client for the engine-owned saga's two sibling services
(BATCH_HANDOFF_CONTRACT.md in the EDPBilling_FIle_Upload repo):

  - the RPA download bot  — POST /edpb/mcx/download · /edpb/bse_member/download
    (a full-segment run finalizes a checksummed manifest.json and returns its
    path + batch_id in the response)
  - the uploader          — POST /batches {manifest_path} · GET /batches/{id}

Mock mode mirrors CbosClient's: deterministic canned responses so the whole
pipeline runs without either service (tests, local dev, demos). Tests swap the
client wholesale via set_edpb_client().

The engine's X-Request-ID is sent on every call — the bot embeds it in the
manifest as correlation_id and the uploader carries it into its audit rows, so
one id traces a segment-day across all three services (wayfinder ticket 11).
"""

from __future__ import annotations

import itertools
import uuid
from dataclasses import dataclass, field
from datetime import date

import httpx
from cams_otel_lib import Logger as logger
from cams_otel_lib import otel_trace

from edpb_core import CORRELATION_HEADER
from edpb_core.batch_api import BATCHES_PATH, DownloadOutcome

from .config import load_edp_config

# Which bot endpoint + body serves each download segment (keys must stay a
# subset of edpb_core.DOWNLOAD_SEGMENTS - the vocabulary lives there; this
# table only adds the transport detail). EQ's full-segment run is the BSE
# "all" action; MCX's single endpoint IS its full-segment run.
_SEGMENT_ROUTES: dict[str, tuple[str, dict]] = {
    "MCX": ("/edpb/mcx/download", {}),
    "EQ": ("/edpb/bse_member/download", {"action": "all"}),
}


# Vocabulary lives in edpb-core (shared enum); alias kept for this module's
# call sites and the state machine's imports.
DownloadStatus = DownloadOutcome
_FINALIZED = (DownloadOutcome.SUCCESS.value, DownloadOutcome.PARTIAL.value)


def _is_transient_status(status_code: int) -> bool:
    """5xx and 429 are retryable; anything else 4xx is terminal."""
    return status_code >= 500 or status_code == 429


@dataclass
class DownloadResult:
    """Outcome of one bot download call, normalized across portals."""

    status: str  # success | partial | no_data | failed | error
    manifest_path: str | None = None
    batch_id: str | None = None
    message: str = ""
    is_transient: bool = False  # True for network errors / 5xx / 429 (retryable)


@dataclass
class BatchSubmitResult:
    """Outcome of handing a manifest to the uploader's POST /batches."""

    accepted: bool  # 202 queued or 200 already-known
    batch_id: str | None = None
    batch_status: str = ""  # the uploader's reported status
    message: str = ""
    is_transient: bool = False


@dataclass
class BatchStatusResult:
    """GET /batches/{batch_id} — the uploader's per-batch verdict."""

    found: bool
    status: str = ""  # queued|uploading|confirmed|unconfirmed|incomplete|failed|rejected
    missing_slots: list[dict] = field(default_factory=list)
    error: str | None = None


class EdpbClient:
    """HTTP client for the bot + uploader, or a deterministic in-process mock."""

    def __init__(
        self,
        download_url: str,
        uploader_url: str,
        *,
        use_mock: bool = False,
        download_timeout: float = 240.0,
        request_id: str | None = None,
    ) -> None:
        self.download_url = download_url.rstrip("/")
        self.uploader_url = uploader_url.rstrip("/")
        self.use_mock = use_mock
        self.download_timeout = download_timeout
        self.request_id = request_id or f"edp-{uuid.uuid4().hex[:12]}"
        # Mock bookkeeping (mirrors CbosClient's style).
        self._mock_batch_counter = itertools.count(1)
        self._mock_batches: dict[str, str] = {}  # batch_id -> status

    def _headers(self, correlation_id: str | None = None) -> dict[str, str]:
        """Per-run correlation id when the caller has one (the engine mints
        one per segment-day, see RealSegmentStateMachine._run_correlation_id);
        the client's own id is only the fallback."""
        return {CORRELATION_HEADER: correlation_id or self.request_id}

    @staticmethod
    def supports_segment(segment: str) -> bool:
        return segment.upper() in _SEGMENT_ROUTES

    # ------------------------------------------------------------------
    # Bot: full-segment download (finalizes + returns the manifest)
    # ------------------------------------------------------------------

    @otel_trace
    async def request_download(
        self,
        segment: str,
        trade_date: date,
        correlation_id: str | None = None,
    ) -> DownloadResult:
        seg = segment.upper()
        if seg not in _SEGMENT_ROUTES:
            return DownloadResult(
                status=DownloadOutcome.ERROR.value,
                message=f"no download route for segment {seg}",
            )

        if self.use_mock:
            return self._mock_download(seg, trade_date)

        path, extra = _SEGMENT_ROUTES[seg]
        payload = {"trade_date": trade_date.isoformat(), **extra}
        url = f"{self.download_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.download_timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers(correlation_id))
        except httpx.HTTPError as exc:
            return DownloadResult(
                status=DownloadOutcome.ERROR.value,
                message=f"bot unreachable: {exc}",
                is_transient=True,
            )

        if resp.status_code != 200:
            return DownloadResult(
                status=DownloadOutcome.ERROR.value,
                message=f"bot HTTP {resp.status_code}: {resp.text[:200]}",
                is_transient=_is_transient_status(resp.status_code),
            )

        body = resp.json()
        return DownloadResult(
            status=str(body.get("status", DownloadOutcome.FAILED.value)),
            manifest_path=body.get("manifest_path"),
            batch_id=body.get("batch_id"),
            message=str(body.get("message", "")),
        )

    def _mock_download(self, segment: str, trade_date: date) -> DownloadResult:
        batch_id = f"{segment}-{trade_date.isoformat()}-mock{next(self._mock_batch_counter):04d}"
        logger.info(f"[EDPB][MOCK] segment={segment} api=request_download -> success ({batch_id})")
        return DownloadResult(
            status=DownloadOutcome.SUCCESS.value,
            manifest_path=f"/mock/{trade_date.strftime('%d-%m-%Y')}/{segment}/manifest.json",
            batch_id=batch_id,
            message="mock download",
        )

    # ------------------------------------------------------------------
    # Uploader: batch submission + status
    # ------------------------------------------------------------------

    @otel_trace
    async def submit_batch(
        self,
        manifest_path: str,
        correlation_id: str | None = None,
    ) -> BatchSubmitResult:
        if self.use_mock:
            return self._mock_submit(manifest_path)

        url = f"{self.uploader_url}{BATCHES_PATH}"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url,
                    json={"manifest_path": manifest_path},
                    headers=self._headers(correlation_id),
                )
        except httpx.HTTPError as exc:
            return BatchSubmitResult(accepted=False, message=f"uploader unreachable: {exc}", is_transient=True)

        if resp.status_code in (200, 202):
            body = resp.json()
            return BatchSubmitResult(
                accepted=True,
                batch_id=body.get("batch_id"),
                batch_status=str(body.get("status", "")),
            )
        if _is_transient_status(resp.status_code):
            return BatchSubmitResult(accepted=False, message=f"uploader HTTP {resp.status_code}", is_transient=True)
        # 4xx — the manifest itself is the problem (schema/checksum); retrying
        # the same one cannot fix it.
        return BatchSubmitResult(accepted=False, message=f"uploader HTTP {resp.status_code}: {resp.text[:300]}")

    def _mock_submit(self, manifest_path: str) -> BatchSubmitResult:
        batch_id = manifest_path.strip("/").replace("/", "-")
        self._mock_batches[batch_id] = "confirmed"
        logger.info(f"[EDPB][MOCK] api=submit_batch manifest={manifest_path} -> queued")
        return BatchSubmitResult(accepted=True, batch_id=batch_id, batch_status="queued")

    @otel_trace
    async def get_batch_status(
        self,
        batch_id: str,
        correlation_id: str | None = None,
    ) -> BatchStatusResult:
        if self.use_mock:
            return BatchStatusResult(found=True, status=self._mock_batches.get(batch_id, "confirmed"))

        url = f"{self.uploader_url}/batches/{batch_id}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=self._headers(correlation_id))
        except httpx.HTTPError as exc:
            return BatchStatusResult(found=False, error=f"uploader unreachable: {exc}")

        if resp.status_code == 404:
            return BatchStatusResult(found=False, error="unknown batch_id")
        if resp.status_code != 200:
            return BatchStatusResult(found=False, error=f"uploader HTTP {resp.status_code}")

        body = resp.json()
        detail = body.get("status_detail") or {}
        return BatchStatusResult(
            found=True,
            status=str(body.get("status", "")),
            missing_slots=list(detail.get("missing_slots", [])),
        )


# ---------------------------------------------------------------------------
# Process-wide accessor (mirrors the uploader's cbos_client get/set/reset
# pattern): built lazily from EdpBootstrapConfig, swappable in tests.
# ---------------------------------------------------------------------------

_client: EdpbClient | None = None


def get_edpb_client() -> EdpbClient:
    global _client
    if _client is None:
        cfg = load_edp_config()
        _client = EdpbClient(
            download_url=cfg.edpb_download_url,
            uploader_url=cfg.edpb_uploader_url,
            use_mock=cfg.edpb_use_mock,
            download_timeout=cfg.edpb_download_timeout_seconds,
        )
    return _client


def set_edpb_client(client: EdpbClient) -> None:
    global _client
    _client = client


def reset_edpb_client() -> None:
    global _client
    _client = None
