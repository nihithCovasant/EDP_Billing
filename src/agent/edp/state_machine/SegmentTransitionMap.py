"""
segment_code -> {from_state: {allowed_to_states}} — a validation safety net
that execute_handler() checks every transition against before it's applied,
mirroring the sketch's SegmentTransitionMap.

The actual per-family maps (REAL_SEGMENT_TRANSITION_MAP / POST_TRADE_TRANSITION_MAP)
are built in TradeSegmentTransitionFactory.py, which is the readable, auditable
place to see every declared transition; this module only holds the generic
map data structure.
"""

from __future__ import annotations

from collections import defaultdict


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
