"""Layer 1 test for app/orchestrator/context.py — no real credentials needed
(docs/testing.md). get_bigquery_schema is monkeypatched so this never
touches a real BigQuery client.
"""
import pytest

import app.orchestrator.context as context


def test_static_context_contains_all_six_components(monkeypatch):
    """All six static-context components present, in a fixed order, and
    MEASURE_REGISTRY contains no measure's DAX body. BigQuery few-shot
    examples live in run_bigquery_sql's own docstring now, not here --
    tests/test_tools.py covers those."""
    monkeypatch.setattr(context, "get_bigquery_schema", lambda: "BIGQUERY SCHEMA -- fake")
    context.get_static_context.cache_clear()

    text = context.get_static_context()

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


@pytest.mark.parametrize("model_provider,expects_cache_block", [
    ("anthropic", True),
    ("openai", False),
    ("gemini", False),
])
def test_build_static_context_dispatches_by_provider(model_provider, expects_cache_block):
    """Anthropic gets an explicit cache_control block; OpenAI/Gemini get a
    plain string. This ensures proper shape for context cache."""
    static_text = "x" * 5000
    out = context.build_static_context(model_provider, static_text)

    assert isinstance(out, list) == expects_cache_block
    if expects_cache_block:
        assert out == [{"type": "text", "text": static_text,
                         "cache_control": {"type": "ephemeral"}}]
    elif model_provider == "gemini":
        assert out == static_text + "\n\n" + context.GEMINI_THINKING_INSTRUCTIONS
    else:
        assert out == static_text


def test_build_static_context_raises_on_unknown_provider():
    """Test value error raised for config.py authoring mistake"""
    with pytest.raises(ValueError, match="No caching strategy"):
        context.build_static_context("mystery-provider", "x" * 5000)
