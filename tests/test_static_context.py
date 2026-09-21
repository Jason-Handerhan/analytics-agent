"""Layer 1 test for app/gateway/context.py — no real credentials needed
(docs/testing.md). get_bigquery_schema is monkeypatched so this never
touches a real BigQuery client.
"""
import app.gateway.context as context


def test_static_context_contains_all_seven_components(monkeypatch):
    """Orientation bundle, TABLE_REGISTRY, MEASURE_REGISTRY,
    RELATIONSHIPS/PARAMETERS, bigquery_schema, system instructions, few-shots —
    all seven, every turn, in a FIXED order: a cache hit needs a
    byte-identical prefix.

    Two silent-failure modes this covers. Table registry and relationships
    describe DIFFERENT tables (disconnected vs. connected) — dropping either
    blinds run_dax_query to half the model. And MEASURE_REGISTRY must carry
    names and descriptions but NOT dax bodies; leaking those defeats the
    reason the split exists (.claude/rules/gateway.md).
    """
    monkeypatch.setattr(context, "get_bigquery_schema", lambda: "BIGQUERY SCHEMA -- fake")
    context.get_static_context.cache_clear()

    result = context.get_static_context()
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
        "BIGQUERY EXAMPLES",
    ]
    positions = [text.index(marker) for marker in markers]
    assert positions == sorted(positions), "static context components are out of order"

    # No measure's DAX body leaked into the registry meant to hold only
    # names and descriptions.
    from app.gateway.model_schema import MEASURE_DAX, MEASURE_REGISTRY

    for dax in MEASURE_DAX.values():
        assert dax.strip() not in MEASURE_REGISTRY

    context.get_static_context.cache_clear()
