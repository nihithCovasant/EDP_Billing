"""
EDP glossary chat tool — explains segment/process codes and common domain
terms for new staff, in one consistent place, instead of terms only ever
appearing inline in other tools' output.

Static, no HTTP calls — this is reference data, not live system state.

Auto-discovered by the tool registry (src/tools/registry.py) — no manual
registration needed.
"""

from __future__ import annotations

from langchain_core.tools import tool

_SEGMENT_GLOSSARY = {
    "EQ": "Cash (Equities) — also called CASH. The main equity trading segment.",
    "DR": "Derivative — also called F&O (Futures & Options).",
    "CUR": "Currency — also called CD (Currency Derivatives).",
    "SLB": "Securities Lending & Borrowing.",
    "NCDEX": "Commodity trading on the NCDEX exchange.",
    "NCDEXPHY": "Physical commodity settlement on NCDEX.",
    "MCX": "Commodity trading on the MCX exchange.",
    "MCXPHY": "Physical commodity settlement on MCX.",
    "NSECOM": "Commodity trading on NSE.",
}

_POST_TRADE_GLOSSARY = {
    "COLVAL": "Collateral Valuation — values client collateral for margin purposes. Runs T+1.",
    "COLALLOC": "Collateral Allocation — allocates valued collateral against margin "
    "requirements. Runs T+1, after COLVAL.",
    "MTFFT": "MTF Fund Transfer — funds transfer step for Margin Trading Facility. Runs T+1.",
    "DMRPT": "Daily Margin Reporting — regulatory margin reporting. Runs T+1, depends "
    "on the prior post-trade process completing.",
    "DMSTMT": "Daily Margin Statements — client-facing margin statements. Runs T+1, last in the post-trade chain.",
}

_TERM_GLOSSARY = {
    "carried forward": (
        "When no workflow config was explicitly uploaded for a trading date, the system "
        "'carries forward' the most recently uploaded config from an earlier date and uses "
        "it as-is, until a new upload supersedes it. get_edp_active_version flags this."
    ),
    "deferred": (
        "If a workflow config is uploaded/applied for today AFTER today's processing has "
        "already started, the change is deferred — applied to tomorrow's trading date "
        "instead of disrupting the in-flight run today."
    ),
    "stale heartbeat": (
        "An IN_PROGRESS segment is expected to update its 'last heartbeat' timestamp "
        "regularly while being actively processed. If that heartbeat goes silent for longer "
        "than the stale threshold, the segment is flagged STALE — a signal the agent may have "
        "stopped actively working it, even though it hasn't failed outright."
    ),
    "gtg": (
        "'Good To Go' — a CBOS status check confirming a prerequisite condition is met "
        "(e.g. the holiday check, or that bill posting/reconciliation has completed) before "
        "the pipeline proceeds to the next step."
    ),
    "trade date": (
        "The trading day a segment's processing belongs to — set once when its record is "
        "created and never recomputed from 'today', so it stays stable even if processing "
        "runs past midnight."
    ),
    "wake cycle": (
        "One pass of the agent's 24/7 loop — it wakes on a fixed interval, checks every "
        "configured segment/process against its own window and CBOS status, and advances "
        "whichever ones are ready."
    ),
    "window": (
        "The start/end time (HH:MM, IST) during which a segment or post-trade process is "
        "eligible to run. Outside its window, the agent won't attempt that segment yet (or "
        "considers it overdue if the window has already closed)."
    ),
    "version name": (
        "The required label every uploaded workflow config is saved under, so it can be "
        "found again later and reapplied by name (e.g. 'diwali_2026', 'revised_cash_window')."
    ),
    "process id": (
        "PROCESSID — the CBOS-assigned identifier for one segment's billing run on a given "
        "trade date. Reserved once (PROCESSID=0 signals 'create new'), then reused for every "
        "subsequent CBOS call for that segment/date."
    ),
}


@tool
async def get_edp_glossary(term: str | None = None) -> str:
    """
    Explain a segment/process code or a common EDP domain term in plain
    language. Use this when a new staff member asks "what does EQ mean",
    "what is COLVAL", "what does carried forward/deferred/stale heartbeat
    mean", or generally "explain this term". If `term` is omitted, returns
    the full glossary (segment codes, post-trade process codes, and domain
    terms) as an overview.

    `term` can be a segment code (EQ, DR, ...), a post-trade code (COLVAL,
    DMRPT, ...), or a domain phrase (carried forward, deferred, GTG, stale
    heartbeat, wake cycle, window, version name, process id) — matching is
    case-insensitive and tolerant of extra whitespace.
    """
    if term:
        key = term.strip().upper()
        if key in _SEGMENT_GLOSSARY:
            return f"**{key}** — {_SEGMENT_GLOSSARY[key]}"
        if key in _POST_TRADE_GLOSSARY:
            return f"**{key}** — {_POST_TRADE_GLOSSARY[key]}"
        term_key = " ".join(term.strip().lower().split())
        if term_key in _TERM_GLOSSARY:
            return f"**{term.strip()}** — {_TERM_GLOSSARY[term_key]}"
        return (
            f"I don't have a glossary entry for **{term}**. Ask for the full glossary "
            f"(omit `term`) to see everything I do know."
        )

    lines = ["### 📖 EDP Billing glossary", "", "**Trade segments**"]
    for code, desc in _SEGMENT_GLOSSARY.items():
        lines.append(f"- **{code}** — {desc}")
    lines += ["", "**Post-trade processes**"]
    for code, desc in _POST_TRADE_GLOSSARY.items():
        lines.append(f"- **{code}** — {desc}")
    lines += ["", "**Domain terms**"]
    for phrase, desc in _TERM_GLOSSARY.items():
        lines.append(f"- **{phrase}** — {desc}")
    return "\n".join(lines)
