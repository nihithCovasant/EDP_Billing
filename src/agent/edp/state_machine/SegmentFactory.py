"""
SegmentFactory.get_segment_state_machine(segment_code) -> the concrete
AbstractSegmentStateMachine instance that drives that segment/process.

Replaces pipeline.executor.get_segment_state_handler()'s
is_post_trade_process() dict-family lookup with an explicit code -> class
mapping, one entry per concrete leaf class. Instances are stateless (aside
from the SEGMENT_CODE/TRIGGER_METHOD_NAME class attributes), so a fresh one
per call is cheap; cached here anyway since there's no reason to
re-instantiate identical objects every wake cycle.
"""

from __future__ import annotations

from .AbstractStateMachine import AbstractSegmentStateMachine
from .post_trade.ColAllocStateMachine import ColAllocStateMachine
from .post_trade.ColValStateMachine import ColValStateMachine
from .post_trade.DmRptStateMachine import DmRptStateMachine
from .post_trade.DmStmtStateMachine import DmStmtStateMachine
from .post_trade.MtfFtStateMachine import MtfFtStateMachine
from .segments.CashSegmentStateMachine import CashSegmentStateMachine
from .segments.CurSegmentStateMachine import CurSegmentStateMachine
from .segments.DrSegmentStateMachine import DrSegmentStateMachine
from .segments.McxPhySegmentStateMachine import McxPhySegmentStateMachine
from .segments.McxSegmentStateMachine import McxSegmentStateMachine
from .segments.NcdexPhySegmentStateMachine import NcdexPhySegmentStateMachine
from .segments.NcdexSegmentStateMachine import NcdexSegmentStateMachine
from .segments.NseComSegmentStateMachine import NseComSegmentStateMachine
from .segments.SlbSegmentStateMachine import SlbSegmentStateMachine

_CLASS_BY_CODE: dict[str, type[AbstractSegmentStateMachine]] = {
    # 9 real segments
    "EQ": CashSegmentStateMachine,
    "DR": DrSegmentStateMachine,
    "CUR": CurSegmentStateMachine,
    "SLB": SlbSegmentStateMachine,
    "NCDEX": NcdexSegmentStateMachine,
    "NCDEXPHY": NcdexPhySegmentStateMachine,
    "MCX": McxSegmentStateMachine,
    "MCXPHY": McxPhySegmentStateMachine,
    "NSECOM": NseComSegmentStateMachine,
    # 5 post-trade processes
    "COLVAL": ColValStateMachine,
    "COLALLOC": ColAllocStateMachine,
    "MTFFT": MtfFtStateMachine,
    "DMRPT": DmRptStateMachine,
    "DMSTMT": DmStmtStateMachine,
}

_INSTANCES: dict[str, AbstractSegmentStateMachine] = {}


class SegmentFactory:
    @staticmethod
    def get_segment_state_machine(segment_code: str) -> AbstractSegmentStateMachine:
        instance = _INSTANCES.get(segment_code)
        if instance is not None:
            return instance

        klass = _CLASS_BY_CODE.get(segment_code)
        if klass is None:
            raise ValueError(
                f"Unknown segment type: {segment_code!r}. "
                f"Allowed codes are: {sorted(_CLASS_BY_CODE)}"
            )
        instance = klass()
        _INSTANCES[segment_code] = instance
        return instance
