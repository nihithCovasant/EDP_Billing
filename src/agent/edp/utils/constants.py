"""
Fixed constants for the post-segment MTF operations chain.

Per EDP_Trade_Process_API_v2.docx (steps 12-24), after all trade segments
(EQ, DR, CUR, SLB, NCDEX, MCX, NSECOM, MF) complete, MOFSL runs a further
6-step chain: Collateral Valuation -> Collateral Allocation -> Fund Transfer
-> MTF Buy -> MTF Sell -> Weekly Auto Closure.

These steps are NOT per-segment — they run once per trading day, gated on
fixed segments (DR / EQ) regardless of which segment code triggered them.
We model this as one virtual "segment" so it reuses the existing sequencing,
locking, and window-deadline machinery in segment_execution without any
schema change.

Step 26 (Corporate Action Position Change) is intentionally NOT implemented —
it depends on manual Ops file drops between 10PM-11:59PM and was explicitly
scoped out.
"""

from __future__ import annotations

# Virtual segment representing the post-segment MTF operations chain.
# Given the highest sequence_order so it only starts once every real
# trade segment has reached COMPLETED or SKIPPED.
MTF_OPS_SEGMENT_CODE = "MTFOPS"
MTF_OPS_SEGMENT_NAME = "MTF Operations (Collateral / Fund Transfer / Buy-Sell)"
MTF_OPS_SEQUENCE_ORDER = 900

# Trigger calls (Steps 13, 15, 17, 19, 20, 22, 24) always use this fixed
# LOGINID per the API doc — distinct from the per-segment CV0001 login used
# for GTG checks and the main 7-stage pipeline.
MTF_TRIGGER_LOGIN_ID = "G_LID"

# GTG (file_process_status) checks in the MTF chain are hardcoded to a
# specific segment in the API doc, not the (virtual) segment being processed.
COLLATERAL_GTG_SEGMENT = "DR"   # Steps 12, 14, 16
MTF_GTG_SEGMENT = "EQ"          # Steps 18, 21, 23
