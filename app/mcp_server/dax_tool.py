import asyncio

import requests
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from app.mcp_server.server import mcp


def _coerce_value(v):
    """Coerces a string that round-trips exactly through int/float back to a
    real number; leaves everything else (including date strings) untouched."""
    if not isinstance(v, str):
        return v
    for cast in (int, float):
        try:
            if str(cast(v)) == v:
                return cast(v)
        except ValueError:
            pass
    return v


class RunDaxQueryArgs(BaseModel):
    dax: str = Field(description="A single DAX EVALUATE query to run against the semantic model. "
                                  "Reference an existing measure by name where one covers the need, "
                                  "rather than recomposing its logic. Prefer TOPN over returning a "
                                  "large table.")


@mcp.tool(exclude_args=["access_token", "workspace_id", "dataset_id", "row_cap", "timeout_seconds"])
async def run_dax_query(args: RunDaxQueryArgs, access_token: str | None = None, workspace_id: str | None = None,
                         dataset_id: str | None = None, row_cap: int | None = None,
                         timeout_seconds: float | None = None) -> list[dict]:
    """Runs a DAX query against a Power BI semantic model (executeQueries).

    One EVALUATE per request -- for a compound question, call this multiple
    times or combine with UNION inside one EVALUATE. Prefer TOPN over
    returning a large table -- exceeding the row cap returns an actionable
    error telling you to narrow the query.
    """
    def _run() -> list[dict]:
        resp = requests.post(
            f"https://api.powerbi.com/v1.0/myorg/groups/{workspace_id}"
            f"/datasets/{dataset_id}/executeQueries",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"queries": [{"query": args.dax}], "serializerSettings": {"includeNulls": True}},
            timeout=timeout_seconds,
        )
        if not resp.ok:
            raise ToolError(f"DAX query failed: {resp.text}")
        return resp.json()["results"][0]["tables"][0]["rows"]

    rows = await asyncio.to_thread(_run)
    if len(rows) > row_cap:
        raise ToolError(f"Exceeded {row_cap} rows. Add a filter or tighten TOPN to narrow it.")
    return [{k: _coerce_value(v) for k, v in row.items()} for row in rows]
