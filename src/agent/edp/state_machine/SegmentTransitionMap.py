"""
segment_code -> {from_phase: {allowed_to_phases}} — a validation safety net
that execute_handler() checks every transition against before it's applied,
mirroring the sketch's SegmentTransitionMap.

Built once at import time from the two fixed phase sequences (real segment
7-step chain, post-trade 3-step chain) rather than hand-declared per
segment, since every segment within a family shares the exact same chain.
Every phase implicitly allows:
  - a self-loop (BLOCKED — still waiting on CBOS, no phase change this cycle)
  - FAILED (permanent CBOS/system errors and window timeouts can occur at
    any stage)
  - SKIPPED (CBOS SKIP signals can occur at any polling stage)
COMPLETED is only reachable from each family's final phase.
"""

from __future__ import annotations

from collections import defaultdict

from ..models import SegmentPhase, SegmentStatus
from ..utils.constants import POST_TRADE_ORDER, SEGMENT_ORDER

# The two fixed phase chains this codebase's pipelines follow.
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

# Terminal "phases" reachable from any in-progress phase — represented as
# SegmentStatus values, not SegmentPhase, since that's what a terminal
# transition actually sets on the row.
_ALWAYS_REACHABLE = (SegmentStatus.FAILED, SegmentStatus.SKIPPED)


class SegmentTransitionMap:
    def __init__(self, allowed_segments: tuple[str, ...]):
        self.allowed_segments = allowed_segments
        self.transitions_map: dict[str, dict[object, set[object]]] = defaultdict(
            lambda: defaultdict(set)
        )

    def check_valid_segment(self, segment: str) -> None:
        if segment not in self.allowed_segments:
            raise ValueError(
                f"Invalid segment: {segment}. Allowed segments are: {self.allowed_segments}"
            )

    def add_allowed_transition(self, segment: str, from_state: object, to_state: object) -> None:
        self.check_valid_segment(segment)
        self.transitions_map[segment][from_state].add(to_state)

    def get_segment_transitions(self, segment: str) -> dict[object, set[object]]:
        self.check_valid_segment(segment)
        return self.transitions_map[segment]

    def is_allowed(self, segment: str, from_state: object, to_state: object) -> bool:
        """True if from_state -> to_state is a registered transition for
        this segment, or a same-state no-op (BLOCKED — nothing changed)."""
        if from_state == to_state:
            return True
        self.check_valid_segment(segment)
        return to_state in self.transitions_map[segment].get(from_state, set())


def _build_map_for_family(
    codes: tuple[str, ...], phase_chain: tuple[SegmentPhase, ...]
) -> SegmentTransitionMap:
    tmap = SegmentTransitionMap(codes)
    for code in codes:
        for i, phase in enumerate(phase_chain):
            if i + 1 < len(phase_chain):
                tmap.add_allowed_transition(code, phase, phase_chain[i + 1])
            for terminal in _ALWAYS_REACHABLE:
                tmap.add_allowed_transition(code, phase, terminal)
        # Only the final in-progress phase may complete.
        tmap.add_allowed_transition(code, phase_chain[-1], SegmentStatus.COMPLETED)
    return tmap


# Built once at import time — one map per pipeline family, both shared by
# every concrete state machine class in that family (see SegmentFactory.py).
REAL_SEGMENT_TRANSITION_MAP = _build_map_for_family(SEGMENT_ORDER, REAL_SEGMENT_PHASE_CHAIN)
POST_TRADE_TRANSITION_MAP = _build_map_for_family(POST_TRADE_ORDER, POST_TRADE_PHASE_CHAIN)
