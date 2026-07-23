"""
Chat-tool-level behavior for src/tools/edp_glossary.py (get_edp_glossary).
Static reference data -- no HTTP mocking needed.
"""

from __future__ import annotations

import src.tools.edp_glossary as edp_glossary


async def _invoke(tool, **kwargs) -> str:
    return await tool.ainvoke(kwargs)


async def test_segment_code_lookup():
    result = await _invoke(edp_glossary.get_edp_glossary, term="EQ")
    assert "Cash" in result


async def test_post_trade_code_lookup():
    result = await _invoke(edp_glossary.get_edp_glossary, term="COLVAL")
    assert "Collateral Valuation" in result


async def test_domain_term_lookup_case_insensitive():
    result = await _invoke(edp_glossary.get_edp_glossary, term="Carried Forward")
    assert "carries forward" in result.lower()


async def test_domain_term_lookup_with_extra_whitespace():
    result = await _invoke(edp_glossary.get_edp_glossary, term="  stale   heartbeat  ")
    assert "heartbeat" in result.lower()


async def test_unknown_term_returns_helpful_fallback():
    result = await _invoke(edp_glossary.get_edp_glossary, term="not a real term")
    assert "don't have a glossary entry" in result


async def test_no_term_returns_full_glossary():
    result = await _invoke(edp_glossary.get_edp_glossary)
    assert "EQ" in result and "COLVAL" in result and "carried forward" in result
