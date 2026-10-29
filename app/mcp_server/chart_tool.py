import pandas as pd
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from app.mcp_server.server import mcp
from app.shared_chart import GenerateChartArgs, render_and_upload


class ChartResult(BaseModel):
    chart_url: str


@mcp.tool(exclude_args=["bucket_name", "storage_backend", "expiration_hours",
                        "access_token", "service_account_email"])
def generate_chart(
    args: GenerateChartArgs,
    bucket_name: str | None = None,
    storage_backend: str | None = None,
    expiration_hours: float | None = None,
    access_token: str | None = None,
    service_account_email: str | None = None,
) -> ChartResult:
    """Renders a chart from tabular data and returns its image URL. `data`
    must include every field the chosen chart type requires -- see that
    type's own description for its exact shape and grain."""
    df = pd.DataFrame(args.data)

    #Validate llm input fields for chart are in the data
    missing = [f for f in args.spec.required_fields() if f not in df.columns]
    if missing:
        raise ToolError(
            f"Field(s) {missing} not in source data. Available: {list(df.columns)}")

    # Ensure no nulls in required columns and return actionable error if one or more do
    nulls = [f for f in args.spec.required_fields() if df[f].isna().any()]
    if nulls:
        raise ToolError(
            f"Field(s) {nulls} contain null values -- charts can't render missing "
            "data meaningfully. Fill them in the query (e.g. COALESCE) or exclude "
            "those rows, then retry.")

    fig = args.spec.render(df)
    chart_url, _ = render_and_upload(
        fig, bucket_name, storage_backend, expiration_hours,
        access_token, service_account_email)
    return ChartResult(chart_url=chart_url)
