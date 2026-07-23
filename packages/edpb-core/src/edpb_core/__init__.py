"""edpb-core — the shared EDPB pipeline contract.

One package, three consumers (EDP_Billing engine, EDPBilling_FIle_Upload,
mofsl_file_download_rpa_bot). It owns the CONTRACT — segment codes, date
formats, correlation-id conventions, the batch manifest schema, batch API
statuses, CBOS v5 endpoint/payload constants, and THE mock CBOS server —
while each service keeps its own thin transport client. That split is
deliberate: wire shapes drift when copied, transport code doesn't need to
be shared to stay correct.

See EDPBilling_FIle_Upload/docs/BATCH_HANDOFF_CONTRACT.md and
docs/CBOS_HANDOFF_CONTRACT.md for the prose contracts these types encode.
"""

from edpb_core.batch_api import BatchStatus
from edpb_core.correlation import CORRELATION_HEADER, mint_run_id
from edpb_core.dates import folder_date_to_iso, iso_to_folder_date
from edpb_core.segments import DOWNLOAD_SEGMENTS, POST_TRADE_ORDER, SEGMENT_ORDER, Segment

__all__ = [
    "BatchStatus",
    "CORRELATION_HEADER",
    "DOWNLOAD_SEGMENTS",
    "POST_TRADE_ORDER",
    "SEGMENT_ORDER",
    "Segment",
    "folder_date_to_iso",
    "iso_to_folder_date",
    "mint_run_id",
]
