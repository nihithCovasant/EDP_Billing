"""The segment vocabulary — the one place the 9 real segments, 5 post-trade
processes, and the bot-downloadable subset are declared.

Every service previously restated these (EDP_Billing utils/constants,
the uploader's manifest schema enum, the bot's route table); a new segment
now lands here first.
"""

from __future__ import annotations

import enum


class Segment(enum.StrEnum):
    """The 9 real market segments, in fixed execution order."""

    EQ = "EQ"  # Cash
    DR = "DR"  # F&O / Derivatives
    CUR = "CUR"  # Currency
    SLB = "SLB"
    NCDEX = "NCDEX"
    NCDEXPHY = "NCDEXPHY"
    MCX = "MCX"
    MCXPHY = "MCXPHY"
    NSECOM = "NSECOM"


SEGMENT_ORDER: tuple[str, ...] = tuple(s.value for s in Segment)

# T+1 post-trade processes, in dependency order (DMRPT waits on MTFFT,
# DMSTMT on DMRPT — see EDP_Billing's PostTradeStateMachine).
POST_TRADE_ORDER: tuple[str, ...] = ("COLVAL", "COLALLOC", "MTFFT", "DMRPT", "DMSTMT")

# Segments the RPA bot can download as a FULL-SEGMENT run today (the engine's
# DOWNLOADING/UPLOADING states apply only to these; the rest wait for files).
DOWNLOAD_SEGMENTS: tuple[str, ...] = (Segment.MCX.value, Segment.EQ.value)
