"""Layer 1 tests for app/orchestrator/tools.py -- pure functions, no
LLM/BigQuery/MCP calls (docs/testing.md).
"""
import pytest

import app.orchestrator.tools as tools_module
from app.orchestrator.tools import PAGE_INFO_DIR, PAGES, get_measure_dax, get_page_info


@pytest.mark.asyncio
async def test_get_measure_dax(monkeypatch):
    """A standalone measure returns just itself; one referencing others
    pulls them in via one-hop expansion."""
    monkeypatch.setattr(tools_module, "MEASURE_DAX", {
        "Solo Measure": "SUM(table[column])",
        "Combo Measure": "[Solo Measure] + [Other Measure]",
        "Other Measure": "COUNT(table[id])",
        "Unrelated Measure": "AVERAGE(table[value])",
    })

    # A measure with no reference to another measure -- returns just itself
    result = await get_measure_dax.coroutine(measure_names=["Solo Measure"])
    assert result == {"Solo Measure": "SUM(table[column])"}

    # A measure referencing two other measures -- both get pulled in, one hop
    result = await get_measure_dax.coroutine(measure_names=["Combo Measure"])
    assert result == {
        "Combo Measure": "[Solo Measure] + [Other Measure]",
        "Solo Measure": "SUM(table[column])",
        "Other Measure": "COUNT(table[id])",
    }


@pytest.mark.asyncio
async def test_get_page_info():
    """PAGES matches the real committed files, and every page returns its
    own file's exact content."""
    assert PAGES == tuple(sorted(p.stem for p in PAGE_INFO_DIR.glob("*.txt")))

    for page_name in PAGES:
        expected = (PAGE_INFO_DIR / f"{page_name}.txt").read_text()
        result = await get_page_info.coroutine(page_name=page_name)
        assert result == {"page_name": page_name, "content": expected}
