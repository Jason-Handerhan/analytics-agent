import json
from typing import Literal

import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, model_validator

from app.mcp_server.server import mcp


class CombineResultsArgs(BaseModel):
    data: list[list[dict]] = Field(
        ..., min_length=2,
        description="Two or more already-fetched results to combine. For "
                     "'join', the first input is the left table.")
    method: Literal["stack", "join"] = Field(
        ..., description="'stack' appends rows -- use when every input has the "
                           "same columns but covers a different slice. 'join' "
                           "merges rows on join_key into wider rows -- use when "
                           "inputs have different columns about the same entities.")
    join_key: list[str] | None = Field(
        default=None,
        description="One or more column names to join on -- required when "
                     "method is 'join', must exist in every input. Omit for 'stack'.")
    join_how: Literal["inner", "left"] = Field(
        default="inner",
        description="'inner' (default) keeps only rows that match in every "
                     "input. 'left' keeps every row from the first input, "
                     "filling unmatched numeric columns with 0 and unmatched "
                     "categorical columns with null.")

    @model_validator(mode="after")
    def _join_key_required_for_join(self) -> "CombineResultsArgs":
        if self.method == "join" and not self.join_key:
            raise ValueError("join_key is required when method is 'join'.")
        return self


def _stack(dataframes: list[pd.DataFrame]) -> list[dict]:
    """Appends rows from every input; column sets must match."""
    column_sets = [frozenset(df.columns) for df in dataframes]
    if len(set(column_sets)) > 1:
        raise ToolError(
            f"stack requires every input to have the same columns. Got: "
            f"{[sorted(cs) for cs in column_sets]}. Use 'join' instead if these "
            "inputs describe the same entities with different columns.")
    return pd.concat(dataframes, ignore_index=True).to_dict(orient="records")


def _join(dataframes: list[pd.DataFrame], join_key: list[str], join_how: Literal["inner", "left"]) -> list[dict]:
    """Merges every input onto the first (left) input on join_key. 'left'
    fills unmatched numeric columns with 0, categorical columns with null."""
    key_desc = ", ".join(join_key)
    column_sets = [frozenset(df.columns) for df in dataframes]
    if not all(set(join_key) <= cs for cs in column_sets):
        raise ToolError(
            f"join_key [{key_desc}] must all be columns in every input. Got: "
            f"{[sorted(cs) for cs in column_sets]}.")

    merged = dataframes[0]
    for df in dataframes[1:]:
        merged = merged.merge(df, on=join_key, how=join_how)

    # inner: empty means nothing matched. left: never empty, so test an inner merge instead.
    if join_how == "inner":
        nothing_matched = merged.empty
    else:
        test = dataframes[0]
        for df in dataframes[1:]:
            test = test.merge(df, on=join_key, how="inner")
        nothing_matched = test.empty

    if nothing_matched:
        # Same columns --> likely should have been stacked
        if len(set(column_sets)) == 1:
            raise ToolError(
                f"Joining on [{key_desc}] produced no matching rows, even though "
                "every input has these columns -- Try 'stack' instead.")
        #Different columns --> likely a key mismatch
        raise ToolError(
            f"Joining on [{key_desc}] produced no matching rows, even though every "
            f"input has these columns -- Check whether [{key_desc}] is really the right key, "
            f"or whether the id format/namespace differs between inputs.")

    if join_how == "left":
        # Fill unmatched numeric columns with 0; leave categorical columns null.
        numeric_cols = [c for c in merged.columns if pd.api.types.is_numeric_dtype(merged[c])]
        merged[numeric_cols] = merged[numeric_cols].fillna(0)

    return json.loads(merged.to_json(orient="records"))


@mcp.tool()
def combine_results(args: CombineResultsArgs) -> list[dict]:
    """Stacks or joins several already-fetched results into one combined
    table. Never computes a new value -- only recombines rows and columns
    that already exist in the inputs.
    """
    dataframes = [pd.DataFrame(rows) for rows in args.data]
    if args.method == "stack":
        return _stack(dataframes)
    return _join(dataframes, args.join_key, args.join_how)
