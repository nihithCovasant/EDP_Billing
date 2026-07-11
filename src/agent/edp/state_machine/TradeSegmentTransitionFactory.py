"""
Explicit, human-auditable transition table — the entry point to read if you
want to see every legal state transition without tracing through a loop.

No "phases" here — only states. Every edge below is declared individually
(not generated from a generic from->next loop) so the asymmetry the manager
called out is impossible to miss: FAILED is reachable from every
non-terminal state, SUCCEEDED is reachable ONLY from the last state in each
chain, and SKIPPED is reachable ONLY from the holiday-check operation —
INIT for real segments, WAITING_FOR_GTG (post-trade's first poll, which
doubles as its holiday check since post-trade has no separate INIT state)
for post-trade processes.
"""

from __future__ import annotations

from ..models import SegmentState, SegmentStatus
from ..utils.constants import POST_TRADE_ORDER, SEGMENT_ORDER
from .SegmentTransitionMap import SegmentTransitionMap


class TradeSegmentTransitionFactory:
    """
    Usage:
        transition_map = TradeSegmentTransitionFactory.load_segment_transition_map(
            allowed_segments=SEGMENT_ORDER,
        )
    """

    @staticmethod
    def load_segment_transition_map(allowed_segments: tuple[str, ...]) -> SegmentTransitionMap:
        """
        Every real segment (CASH/EQ, DR, CUR, SLB, NCDEX, NCDEXPHY, MCX,
        MCXPHY, NSECOM) follows the identical happy-flow chain below —
        written out explicitly once, then applied to every segment code so
        the same edges are guaranteed for all of them:

          INIT                                  -> WAITING_FOR_FILE_UPLOAD
          INIT                                  -> SKIPPED, FAILED
          WAITING_FOR_FILE_UPLOAD               -> TRIGGERED
          WAITING_FOR_FILE_UPLOAD               -> FAILED
          TRIGGERED                             -> WAITING_FOR_BILLPOSTING
          TRIGGERED                             -> FAILED
          WAITING_FOR_BILLPOSTING               -> WAITING_FOR_RECON
          WAITING_FOR_BILLPOSTING               -> FAILED
          WAITING_FOR_RECON                     -> WAITING_FOR_CONTRACT_NOTE_GENERATION
          WAITING_FOR_RECON                     -> FAILED
          WAITING_FOR_CONTRACT_NOTE_GENERATION  -> SUCCEEDED
          WAITING_FOR_CONTRACT_NOTE_GENERATION  -> FAILED

        SKIPPED is reachable ONLY from INIT (the holiday-check operation) —
        a market holiday is the only documented reason a segment is ever
        skipped rather than failed or completed. FAILED is reachable from
        every state — a permanent CBOS error can occur at any step, and a
        window timeout can strike whichever state is active when the
        deadline hits.
        """
        m = SegmentTransitionMap(allowed_segments)
        for seg in allowed_segments:
            m.add_allowed_transition(seg, SegmentState.INIT, SegmentState.WAITING_FOR_FILE_UPLOAD)
            m.add_allowed_transition(seg, SegmentState.INIT, SegmentStatus.SKIPPED)
            m.add_allowed_transition(seg, SegmentState.INIT, SegmentStatus.FAILED)

            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_FILE_UPLOAD, SegmentState.TRIGGERED)
            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_FILE_UPLOAD, SegmentStatus.FAILED)

            m.add_allowed_transition(seg, SegmentState.TRIGGERED, SegmentState.WAITING_FOR_BILLPOSTING)
            m.add_allowed_transition(seg, SegmentState.TRIGGERED, SegmentStatus.FAILED)

            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_BILLPOSTING, SegmentState.WAITING_FOR_RECON)
            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_BILLPOSTING, SegmentStatus.FAILED)

            m.add_allowed_transition(
                seg, SegmentState.WAITING_FOR_RECON, SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION,
            )
            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_RECON, SegmentStatus.FAILED)

            m.add_allowed_transition(
                seg, SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION, SegmentStatus.COMPLETED,
            )
            m.add_allowed_transition(
                seg, SegmentState.WAITING_FOR_CONTRACT_NOTE_GENERATION, SegmentStatus.FAILED,
            )
        return m

    @staticmethod
    def load_post_trade_transition_map(allowed_segments: tuple[str, ...]) -> SegmentTransitionMap:
        """
        Every post-trade process (COLVAL, COLALLOC, MTFFT, DMRPT, DMSTMT)
        follows the identical happy-flow chain below:

          WAITING_FOR_GTG        -> TRIGGERED
          WAITING_FOR_GTG        -> WAITING_FOR_COMPLETION   (direct, already triggered — no new trigger fired)
          WAITING_FOR_GTG        -> SKIPPED                  (holiday check — same operation INIT does for real segments)
          WAITING_FOR_GTG        -> FAILED
          TRIGGERED               -> WAITING_FOR_COMPLETION
          TRIGGERED               -> FAILED
          WAITING_FOR_COMPLETION -> SUCCEEDED
          WAITING_FOR_COMPLETION -> FAILED

        SKIPPED is reachable ONLY from WAITING_FOR_GTG — post-trade has no
        separate INIT state, so its first poll doubles as the holiday check,
        exactly like INIT does for real segments.
        """
        m = SegmentTransitionMap(allowed_segments)
        for seg in allowed_segments:
            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_GTG, SegmentState.TRIGGERED)
            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_GTG, SegmentState.WAITING_FOR_COMPLETION)
            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_GTG, SegmentStatus.SKIPPED)
            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_GTG, SegmentStatus.FAILED)

            m.add_allowed_transition(seg, SegmentState.TRIGGERED, SegmentState.WAITING_FOR_COMPLETION)
            m.add_allowed_transition(seg, SegmentState.TRIGGERED, SegmentStatus.FAILED)

            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_COMPLETION, SegmentStatus.COMPLETED)
            m.add_allowed_transition(seg, SegmentState.WAITING_FOR_COMPLETION, SegmentStatus.FAILED)
        return m


# Built once at import time — the maps every concrete state machine class
# actually uses at runtime (see RealSegmentStateMachine.py / PostTradeStateMachine.py).
REAL_SEGMENT_TRANSITION_MAP = TradeSegmentTransitionFactory.load_segment_transition_map(SEGMENT_ORDER)
POST_TRADE_TRANSITION_MAP = TradeSegmentTransitionFactory.load_post_trade_transition_map(POST_TRADE_ORDER)
