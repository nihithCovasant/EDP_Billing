"""The uploader's batches API contract (BATCH_HANDOFF_CONTRACT.md) as types —
statuses and endpoint paths, so callers (bot callback, EDP_Billing engine)
and the uploader itself agree by construction.
"""

from __future__ import annotations

import enum


class BatchStatus(str, enum.Enum):
    QUEUED = "queued"
    UPLOADING = "uploading"
    CONFIRMED = "confirmed"          # FILEUPLOAD went TRUE
    UNCONFIRMED = "unconfirmed"      # in CBOS; FILEUPLOAD not yet TRUE
    INCOMPLETE = "incomplete"        # completeness gate parked the batch
    FAILED = "failed"
    REJECTED = "rejected"            # intake rejected (schema/checksum)

    @property
    def is_terminal_bad(self) -> bool:
        """States the engine fails a segment on immediately (FILEUPLOAD can
        never go TRUE from these without human/ops action)."""
        return self in (BatchStatus.INCOMPLETE, BatchStatus.FAILED, BatchStatus.REJECTED)


class DownloadOutcome(str, enum.Enum):
    """The bot's classified download outcomes (PortalStatus/McxStatus on the
    bot side) plus the engine client's transport-level ERROR."""

    SUCCESS = "success"
    PARTIAL = "partial"
    NO_DATA = "no_data"
    FAILED = "failed"
    ERROR = "error"

    @property
    def finalized(self) -> bool:
        """True when this outcome carries a finalized manifest."""
        return self in (DownloadOutcome.SUCCESS, DownloadOutcome.PARTIAL)


BATCHES_PATH = "/batches"
BATCH_STATUS_PATH = "/batches/{batch_id}"
BATCHES_RESCAN_PATH = "/batches/rescan"
BATCH_PROCEED_PATH = "/batches/{batch_id}/proceed"
