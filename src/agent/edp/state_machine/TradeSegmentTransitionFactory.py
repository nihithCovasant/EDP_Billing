"""
Explicit, human-auditable transition table — the entry point to read if you
want to see every legal phase transition without tracing through a loop.

Mirrors the manager's TradeSegmentTransitionFactory.load_segment_transition_map()
sketch 1:1 in naming and shape: one static method that declares every allowed
from_state -> to_state edge, per segment/process, using add_allowed_transition().

Same real chain for all 10 real segments, applied once per family instead of
retyped 10x, so the sketch's own bug class (hand-typed CASH transitions that
quietly missed a FAILED edge on WAITING_FOR_CONTRACTNOTE_GTG / the
TRIGGERED_* states) can't happen here — every segment in a family is
guaranteed to get the exact same, fully-declared edge set.
"""

from __future__ import annotations

from ..models import SegmentPhase, SegmentStatus
from ..utils.constants import POST_TRADE_ORDER, SEGMENT_ORDER
from .SegmentTransitionMap import SegmentTransitionMap

# The two fixed phase chains this codebase's pipelines follow — same source
# of truth SegmentTransitionMap.py uses, restated here for the explicit,
# line-by-line declaration below.
REAL_SEGMENT_PHASE_CHAIN: tuple[SegmentPhase, ...] = (
    SegmentPhase.HOLIDAY_CHECK,
    SegmentPhase.RESERVE_PID,
    SegmentPhase.AWAIT_FILE_UPLOAD,
    SegmentPhase.TRIGGER,
    SegmentPhase.AWAIT_BILLPOSTING,
    SegmentPhase.AWAIT_RECON,
    SegmentPhase.AWAIT_CONTRACT_NOTE,
)

POST_TRADE_PHASE_CHAIN: tuple[SegmentPhase, ...] = (
    SegmentPhase.AWAIT_GTG,
    SegmentPhase.TRIGGER_JOB,
    SegmentPhase.AWAIT_CONFIRM,
)


class TradeSegmentTransitionFactory:
    """
        transition_map = TradeSegmentTransitionFactory.load_segment_transition_map(
            allowed_segments=SEGMENT_ORDER,
        )
    """

    @staticmethod
    def load_segment_transition_map(allowed_segments: tuple[str, ...]) -> SegmentTransitionMap:
        """
        Every real segment (CASH/EQ, DR, CUR, SLB, NCDEX, NCDEXPHY, MCX,
        MCXPHY, NSECOM, MF) follows the identical 7-step chain below —
        written out explicitly once, then applied to every segment code so
        the same edges are guaranteed for all of them:

          HOLIDAY_CHECK       -> RESERVE_PID
          HOLIDAY_CHECK        -> FAILED, SKIPPED
          RESERVE_PID          -> AWAIT_FILE_UPLOAD
          RESERVE_PID          -> FAILED, SKIPPED
          AWAIT_FILE_UPLOAD    -> TRIGGER
          AWAIT_FILE_UPLOAD    -> FAILED, SKIPPED
          TRIGGER              -> AWAIT_BILLPOSTING
          TRIGGER              -> FAILED, SKIPPED
          AWAIT_BILLPOSTING    -> AWAIT_RECON
          AWAIT_BILLPOSTING    -> FAILED, SKIPPED
          AWAIT_RECON          -> AWAIT_CONTRACT_NOTE
          AWAIT_RECON          -> FAILED, SKIPPED
          AWAIT_CONTRACT_NOTE  -> COMPLETED
          AWAIT_CONTRACT_NOTE  -> FAILED, SKIPPED

        Note every phase (not just the *_GTG / *_COMPLETION-equivalent ones)
        can reach FAILED/SKIPPED — a permanent CBOS error or a CBOS SKIP
        signal can legitimately occur at any polling stage, and a window
        timeout can strike whichever phase is active when the deadline hits.
        """
        return TradeSegmentTransitionFactory._load(
            allowed_segments, REAL_SEGMENT_PHASE_CHAIN,
        )

    @staticmethod
    def load_post_trade_transition_map(allowed_segments: tuple[str, ...]) -> SegmentTransitionMap:
        """
        Every post-trade process (COL_VAL, COL_ALLOC, MTF_FT, DM_STMT,
        DM_RPT) follows the identical 3-step chain below:

          AWAIT_GTG      -> TRIGGER_JOB
          AWAIT_GTG      -> FAILED, SKIPPED
          TRIGGER_JOB    -> AWAIT_CONFIRM
          TRIGGER_JOB    -> FAILED, SKIPPED
          AWAIT_CONFIRM  -> COMPLETED
          AWAIT_CONFIRM  -> FAILED, SKIPPED
        """
        return TradeSegmentTransitionFactory._load(
            allowed_segments, POST_TRADE_PHASE_CHAIN,
        )

    @staticmethod
    def _load(
        allowed_segments: tuple[str, ...], phase_chain: tuple[SegmentPhase, ...],
    ) -> SegmentTransitionMap:
        transition_map = SegmentTransitionMap(allowed_segments)
        for segment in allowed_segments:
            for i, phase in enumerate(phase_chain):
                if i + 1 < len(phase_chain):
                    transition_map.add_allowed_transition(segment, phase, phase_chain[i + 1])
                transition_map.add_allowed_transition(segment, phase, SegmentStatus.FAILED)
                transition_map.add_allowed_transition(segment, phase, SegmentStatus.SKIPPED)
            # Only the final in-progress phase may complete.
            transition_map.add_allowed_transition(
                segment, phase_chain[-1], SegmentStatus.COMPLETED,
            )
        return transition_map


# Built once at import time — the maps every concrete state machine class
# actually uses at runtime (see RealSegmentStateMachine.py / PostTradeStateMachine.py).
REAL_SEGMENT_TRANSITION_MAP = TradeSegmentTransitionFactory.load_segment_transition_map(SEGMENT_ORDER)
POST_TRADE_TRANSITION_MAP = TradeSegmentTransitionFactory.load_post_trade_transition_map(POST_TRADE_ORDER)
