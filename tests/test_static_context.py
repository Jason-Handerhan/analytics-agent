"""Layer 1 test for app/orchestrator/context.py — no real credentials needed
(docs/testing.md). get_bigquery_schema is monkeypatched so this never
touches a real BigQuery client.
"""
import app.orchestrator.context as context


def test_static_context_contains_all_six_components(monkeypatch):
    """All six static-context components present, in a fixed order, and
    MEASURE_REGISTRY contains no measure's DAX body. BigQuery few-shot
    examples live in run_bigquery_sql's own docstring now, not here --
    tests/test_tools.py covers those."""
    monkeypatch.setattr(context, "get_bigquery_schema", lambda: "BIGQUERY SCHEMA -- fake")
    context.get_static_context.cache_clear()

    result = context.get_static_context()

    # Claude returns a list of content blocks; OpenAI/Gemini return a string.
    text = result[0]["text"] if isinstance(result, list) else result

    markers = [
        "SYSTEM INSTRUCTIONS",
        "This section is background",  # orientation bundle
        "TABLE REGISTRY",
        "MEASURE REGISTRY",
        "RELATIONSHIPS",
        "PARAMETERS",
        "DAX EXAMPLES",
        "BIGQUERY SCHEMA",
    ]
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions), "static context components are out of order"

    # No measure's DAX body in the registry
    from app.model_schema import MEASURE_DAX, MEASURE_REGISTRY

    for dax in MEASURE_DAX.values():
        assert dax.strip() not in MEASURE_REGISTRY

    context.get_static_context.cache_clear()
