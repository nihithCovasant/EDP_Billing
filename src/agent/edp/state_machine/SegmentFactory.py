"""
SegmentFactory.get_segment_state_machine(segment_code) -> the concrete
AbstractSegmentStateMachine instance that drives that segment/process.

Both base machines (RealSegmentStateMachine, PostTradeStateMachine) hold all
the behaviour; a code differs only in a small set of declarative fields
(segment_code, and for post-trade the two CbosClient method names + a
depends-on-previous flag). Those fields live in the SEGMENT_SPECS table
below — one row per code — instead of in 14 near-empty per-code subclasses.

Instances are stateless aside from those spec-derived attributes, so a
fresh one per code is cheap; cached here anyway since there's no reason to
re-instantiate identical objects every wake cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from .AbstractStateMachine import AbstractSegmentStateMachine
from .PostTradeStateMachine import PostTradeStateMachine
from .RealSegmentStateMachine import RealSegmentStateMachine


@dataclass(frozen=True)
class SegmentSpec:
    """One row of the segment / post-trade manifest — everything the factory
    needs to build the right state machine for a code."""

    code: str
    is_post_trade: bool = False
    # Post-trade only: CbosClient methods this process dispatches to.
    trigger_method_name: str = ""
    check_triggered_method_name: str = ""
    depends_on_previous_process: bool = False


_SEGMENT_SPECS: tuple[SegmentSpec, ...] = (
    # 9 real segments — the identical 7-step pipeline in
    # RealSegmentStateMachine; they differ only by code.
    SegmentSpec("EQ"),        # Cash
    SegmentSpec("DR"),        # F&O
    SegmentSpec("CUR"),       # Currency Derivatives
    SegmentSpec("SLB"),
    SegmentSpec("NCDEX"),
    SegmentSpec("NCDEXPHY"),
    SegmentSpec("MCX"),
    SegmentSpec("MCXPHY"),
    SegmentSpec("NSECOM"),
    # 5 post-trade (T+1) processes — shared PostTradeStateMachine logic; each
    # names the CbosClient trigger + "already triggered" check methods it
    # dispatches to.
    SegmentSpec(
        "COLVAL", is_post_trade=True,
        trigger_method_name="trigger_collateral_valuation",
        check_triggered_method_name="check_collateral_valuation_triggered",
    ),
    SegmentSpec(
        "COLALLOC", is_post_trade=True,
        trigger_method_name="trigger_collateral_allocation",
        check_triggered_method_name="check_collateral_allocation_triggered",
    ),
    SegmentSpec(
        "MTFFT", is_post_trade=True,
        trigger_method_name="trigger_mtf_fund_transfer",
        check_triggered_method_name="check_mtf_fund_transfer_triggered",
    ),
    # DMRPT/DMSTMT have no CBOS GTG/holiday-check endpoint — their readiness
    # gate is "the previous process in POST_TRADE_ORDER reached a terminal DB
    # status" (PostTradeStateMachine._check_previous_process_terminal), so
    # depends_on_previous_process=True.
    SegmentSpec(
        "DMRPT", is_post_trade=True,
        trigger_method_name="trigger_daily_margin_reporting",
        check_triggered_method_name="check_daily_margin_reporting_triggered",
        depends_on_previous_process=True,
    ),
    SegmentSpec(
        "DMSTMT", is_post_trade=True,
        trigger_method_name="trigger_daily_margin_statements",
        check_triggered_method_name="check_daily_margin_statements_triggered",
        depends_on_previous_process=True,
    ),
)

_SPEC_BY_CODE: dict[str, SegmentSpec] = {s.code: s for s in _SEGMENT_SPECS}

_INSTANCES: dict[str, AbstractSegmentStateMachine] = {}


def _build(spec: SegmentSpec) -> AbstractSegmentStateMachine:
    if spec.is_post_trade:
        return PostTradeStateMachine(
            segment_code=spec.code,
            trigger_method_name=spec.trigger_method_name,
            check_triggered_method_name=spec.check_triggered_method_name,
            depends_on_previous_process=spec.depends_on_previous_process,
        )
    return RealSegmentStateMachine(spec.code)


class SegmentFactory:
    @staticmethod
    def get_segment_state_machine(segment_code: str) -> AbstractSegmentStateMachine:
        instance = _INSTANCES.get(segment_code)
        if instance is not None:
            return instance

        spec = _SPEC_BY_CODE.get(segment_code)
        if spec is None:
            raise ValueError(
                f"Unknown segment type: {segment_code!r}. "
                f"Allowed codes are: {sorted(_SPEC_BY_CODE)}"
            )
        instance = _build(spec)
        _INSTANCES[segment_code] = instance
        return instance
