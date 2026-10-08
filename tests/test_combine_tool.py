"""Layer 1 tests for combine_results -- no credentials needed (docs/testing.md).
Each test runs a batch of cases in one place.
"""
import pytest
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from app.mcp_server.combine_tool import CombineResultsArgs, combine_results


def test_combine_results_stack_join_and_guardrails():
    """Stack and join success paths (including out-of-order keys, composite keys,
    and left-join null fills), plus every guardrail's error message."""
    stacked = combine_results(CombineResultsArgs(
        data=[[{"f": "a", "c": 0.1}], [{"f": "b", "c": 0.2}]], method="stack"))
    assert stacked == [{"f": "a", "c": 0.1}, {"f": "b", "c": 0.2}]

    inner = combine_results(CombineResultsArgs(
        data=[[{"pid": 1, "rev": 100}, {"pid": 2, "rev": 200}],
              [{"pid": 2, "units": 20}, {"pid": 1, "units": 10}]],
        method="join", join_key=["pid"], join_how="inner"))
    assert inner == [{"pid": 1, "rev": 100, "units": 10}, {"pid": 2, "rev": 200, "units": 20}]

    composite = combine_results(CombineResultsArgs(
        data=[[{"region": "w", "pid": 1, "rev": 100}, {"region": "e", "pid": 1, "rev": 150}],
              [{"region": "w", "pid": 1, "units": 10}, {"region": "e", "pid": 1, "units": 5}]],
        method="join", join_key=["region", "pid"]))
    assert composite == [
        {"region": "w", "pid": 1, "rev": 100, "units": 10},
        {"region": "e", "pid": 1, "rev": 150, "units": 5},
    ]

    left = combine_results(CombineResultsArgs(
        data=[[{"pid": 1, "rev": 100}, {"pid": 2, "rev": 200}],
              [{"pid": 1, "refund": 5, "reason": "damaged"}]],
        method="join", join_key=["pid"], join_how="left"))
    assert left == [
        {"pid": 1, "rev": 100, "refund": 5.0, "reason": "damaged"},
        {"pid": 2, "rev": 200, "refund": 0.0, "reason": None},
    ]

    bad_cases = [
        (dict(data=[[{"a": 1}], [{"b": 2}]], method="stack"),
         "same columns"),
        (dict(data=[[{"pid": 1, "rev": 1}], [{"sku": 1, "units": 2}]], method="join", join_key=["pid"]),
         "must all be columns"),
        (dict(data=[[{"f": "a", "c": 0.1}], [{"f": "b", "c": 0.2}]], method="join", join_key=["f"]),
         "Try 'stack'"),
        (dict(data=[[{"f": "a", "c": 0.1}], [{"f": "b", "c": 0.2}]], method="join", join_key=["f"], join_how="left"),
         "Try 'stack'"),
        (dict(data=[[{"pid": 1, "rev": 100}], [{"pid": 999, "refund": 5}]], method="join", join_key=["pid"]),
         "right key"),
    ]
    for kwargs, expected in bad_cases:
        with pytest.raises(ToolError, match=expected):
            combine_results(CombineResultsArgs(**kwargs))

    with pytest.raises(ValidationError, match="join_key is required"):
        CombineResultsArgs(data=[[{"pid": 1}], [{"pid": 1}]], method="join")
