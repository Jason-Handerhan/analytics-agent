"""Layer 1 tests for app/orchestrator/tools.py -- pure functions, no
LLM/BigQuery/MCP calls (docs/testing.md).
"""
import pytest

import app.orchestrator.tools as tools_module
from app.orchestrator.tools import get_measure_dax


@pytest.mark.asyncio
async def test_get_measure_dax(monkeypatch):
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
