# Chart Tool Design

**It renders; it never computes.** The query already did the grouping,
filtering, and joins. No aggregation, no `group_by`, no branching on which tool
produced the data. If a chart needs data shaped differently, fix the **query**,
not the chart tool.

## Base class — shared styling contract

Every `render()`: start with `self._setup()`, end with `self._finish(fig, ax)`.
That keeps styling consistent without each class reimplementing it.

```python
# app/mcp_server/chart_tools.py
from abc import ABC, abstractmethod
from typing import Literal, Union, Annotated
import matplotlib
matplotlib.use("Agg")          # required — Cloud Run has no display
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
from pydantic import BaseModel, Field

# Styling is FIXED — module constants, never model-supplied. Twelve cosmetic
# fields against ~four substantive ones meant most of the schema the model saw
# was decoration, spending attention on `palette` instead of `x_field`. And an
# agent picking darkgrid on one chart and whitegrid on the next looks
# unconsidered; fixed styling looks deliberate. Trade-off: no "make it wider"
# or "drop the legend" follow-ups.
STYLE      = "whitegrid"     # clean gridlines, no heavy background
CONTEXT    = "talk"          # larger fonts than "notebook" — these are read
                             # embedded in a dashboard, not in a notebook
PALETTE    = "deep"          # seaborn's default; colorblind-safe enough
FIGSIZE    = (9.0, 5.5)      # ~16:10, fits the Power Apps card without
                             # letterboxing
DPI        = 150             # crisp on high-DPI displays, still a small PNG
GRID_ALPHA = 0.3             # visible but recessive behind the data

class ChartSpecBase(BaseModel, ABC):
    # source_tool_call_id is NOT here. It's on the model-facing schema only
    # (below, in GenerateChartToolCallArgs) — this base is shared by the
    # REAL MCP tool's schema too, which takes resolved `data` instead and
    # must never see a tool_call_id at all (.claude/rules/tools.md).
    title: str | None = None
    # Axis labels ARE exposed — substantive, not cosmetic. Raw column names
    # are often unreadable (DAX returns `Products[department]`), and only the
    # model knows the question well enough to write "Department". None falls
    # back to the column name.
    x_label: str | None = None
    y_label: str | None = None

    @abstractmethod
    def required_fields(self) -> list[str]: ...

    @abstractmethod
    def render(self, df: pd.DataFrame) -> plt.Figure: ...

    def _setup(self) -> tuple[plt.Figure, plt.Axes]:
        sns.set_theme(style=STYLE, context=CONTEXT, palette=PALETTE)
        return plt.subplots(figsize=FIGSIZE, dpi=DPI)

    def _finish(self, fig: plt.Figure, ax: plt.Axes) -> plt.Figure:
        if self.title:
            ax.set_title(self.title, pad=12)
        if self.x_label:
            ax.set_xlabel(self.x_label)
        if self.y_label:
            ax.set_ylabel(self.y_label)
        ax.grid(True, alpha=GRID_ALPHA)
        sns.despine(ax=ax)          # drop top/right spines — less chart junk

        # Rotate x tick labels only when they'd actually collide.
        # Unconditional rotation looks wrong on 3 short categories; leaving it
        # off looks wrong on 15 long ones.
        #
        # x-axis ONLY, and deliberately: on horizontal charts (horizontal_bar,
        # stacked_horizontal_bar, box) the categories sit on y, where they read
        # left-to-right already and must never be rotated. x there holds
        # numbers, which are short — so this rarely fires and correctly does
        # nothing when it doesn't.
        labels = [tick.get_text() for tick in ax.get_xticklabels()]
        if labels and (len(labels) > 6 or max(len(l) for l in labels) > 10):
            plt.setp(ax.get_xticklabels(), rotation=45, ha="right")

        fig.tight_layout()
        return fig

    def _annotate_values(self, ax: plt.Axes) -> None:
        """Bar labels, always on where a spec calls this. Reading a value off
        a bar beats squinting at the axis, and it's the kind of polish that
        should be consistent rather than a per-chart choice."""
        for container in ax.containers:
            ax.bar_label(container, fmt="%.4g", padding=2, fontsize=9)
```

**Styling is fixed rather than model-controlled** — but the same trust
split still explains why that's a free choice: no styling field can change
what the chart *shows*, only how it looks. Since nothing about it needed
guarding, the only question was whether exposing it *helped*, and it didn't.
The original framing holds: "LLM chooses which data, deterministic code
computes the numbers." Fixing the styling extends that — the model now
chooses *only* data and labels.

**Axis labels are the exception, and they're substantive.** A raw DAX column
renders as `Products[department]`; only the model knows the question well
enough to write "Department". Same for a `y_label` like "Orders" over
`[order_count]`. Leaving them `None` falls back to the column name, which is
the right default when nothing better is known.

## Every spec's docstring states its required grain

**The model never sees this code — it sees the tool schema, and docstrings
are the only per-type guidance in it.** Field names like `x_field` say nothing
about whether data should arrive aggregated, and the SQL is written *before*
the chart call, so by then a wrong-grain decision is already made.

Docstrings are the right place because they're **free**: the schema is sent
every turn regardless, so this costs nothing extra and doesn't grow the static
context. Few-shots are the expensive option — reserve them for patterns the
model won't invent (`concentration`'s windowed cumulative), not for
`GROUP BY`, which it produces unprompted.

Each docstring should say: **what one row represents**, **what must already be
computed in SQL**, and **any cardinality limit** with the reason.

## The full supported set

Build `bar`, `line`, and `concentration` first — they cover the most likely
questions. Add the rest as real questions call for them. No new dependencies:
all are stock seaborn/matplotlib.

**All ten are written out in full.** Four of them aren't inferable from the
others: `histogram` uses `ax.bar` on already-binned data (`sns.histplot` would
re-bin the bucket edges), `box` uses `ax.bxp` whose input dict keys are
matplotlib's contract rather than ours, `heatmap` pivots to wide form first,
and `stacked_horizontal_bar` tracks a running `left=` offset because
matplotlib has no native stacked-barh.

**`scatter` and `beeswarm` are dropped — both need raw points.** Aggregating
destroys the chart's meaning, and raw points blow the 1,000-row cap. Neither
is needed: correlation at category grain reads fine as `bar`, and mean
absolute SHAP is one number per feature — `horizontal_bar` over a DAX result.
A real beeswarm would need its own tool that queries BigQuery and returns only
a URL, since the cap protects the prompt, not matplotlib.

**`histogram` and `box` take pre-computed statistics, not raw rows** — the cap
would otherwise truncate a distribution to an arbitrary slice and draw a
confidently wrong chart. Same reasoning as `concentration`'s SQL-computed
cumulative:

```sql
-- histogram: bin in SQL, exact counts over the whole dataset
SELECT FLOOR(days_since_prior_order / 7) * 7 AS bucket, COUNT(*) AS n
FROM ... GROUP BY bucket

-- box: APPROX_QUANTILES per category; ax.bxp() accepts precomputed stats
SELECT department,
       APPROX_QUANTILES(reorder_rate, 4)[OFFSET(1)] AS q1,
       APPROX_QUANTILES(reorder_rate, 4)[OFFSET(2)] AS med,
       APPROX_QUANTILES(reorder_rate, 4)[OFFSET(3)] AS q3
FROM ... GROUP BY department
```

**Every spec's docstring carries a two-row shape example** — the exact
`list[dict]` the tool expects, not SQL. The model reads these while composing
the *query*, so showing the target shape lets it work backward to the right
`GROUP BY`. SQL examples would be worse on both counts: the query has already
run by the time this docstring is read, and one example can't be correct for
both BigQuery and DAX. Two rows is enough — the shape is the lesson.

**Docstrings, not the system prompt.** Tool schemas are cached alongside the
static block, so it's token-neutral — and an example that travels with its
spec class can't drift out of sync.

**Row-cap warnings stay per-spec** because each type fails differently: `bar`
errors out, `histogram` truncates silently, `concentration` still renders with
the 80% crossing in the wrong place.

```python
class BarChartSpec(ChartSpecBase):
    """One bar per category. Fields: x_field (cat), y_field (num).

    Expects one row per category, already aggregated:
        [{"department": "produce", "orders": 5231},
         {"department": "dairy",   "orders": 4102}]

    Keep categories readable: departments (~21) or aisles (~134), not products
    (~50k). An un-aggregated query hits the 1,000-row cap and fails.
    """
    chart_type: Literal["bar"]
    x_field: str                  # categorical
    y_field: str                  # numeric

    def required_fields(self) -> list[str]:
        return [self.x_field, self.y_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        fig, ax = self._setup()
        sns.barplot(data=df, x=self.x_field, y=self.y_field, ax=ax)
        self._annotate_values(ax)
        return self._finish(fig, ax)

class HorizontalBarChartSpec(ChartSpecBase):
    """Bar with axes swapped — for long names or many categories.
    Fields: x_field (num), y_field (cat).

        [{"orders": 5231, "aisle": "fresh vegetables"},
         {"orders": 4102, "aisle": "packaged cheese"}]
    """
    chart_type: Literal["horizontal_bar"]
    x_field: str                  # numeric
    y_field: str                  # categorical

    def required_fields(self) -> list[str]:
        return [self.x_field, self.y_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        fig, ax = self._setup()
        sns.barplot(data=df, x=self.x_field, y=self.y_field, ax=ax)
        self._annotate_values(ax)
        return self._finish(fig, ax)

class GroupedBarChartSpec(ChartSpecBase):
    """A metric across two dimensions — Recall@5 by model AND split.
    Fields: x_field (cat), y_field (num), hue_field (cat).

        [{"model": "LightGBM", "recall": 0.367, "split": "test"},
         {"model": "LightGBM", "recall": 0.412, "split": "train"}]
    """
    chart_type: Literal["grouped_bar"]
    x_field: str
    y_field: str
    hue_field: str

    def required_fields(self) -> list[str]:
        return [self.x_field, self.y_field, self.hue_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        fig, ax = self._setup()
        sns.barplot(data=df, x=self.x_field, y=self.y_field,
                    hue=self.hue_field, ax=ax)
        return self._finish(fig, ax)

class StackedHorizontalBarChartSpec(ChartSpecBase):
    """Composition within each category — segments summing to a whole bar.
    Fields: category_field (cat), value_field (num), segment_field (cat).

    Long form, one row per (category, segment) pair — NOT pre-pivoted:
        [{"department": "produce", "segment": "reordered", "orders": 3910},
         {"department": "produce", "segment": "first-time", "orders": 1321}]

    Different question from `grouped_bar`: that compares a metric ACROSS a
    second dimension (bars side by side); this shows what each category is
    MADE OF (segments summing to one bar). Horizontal because category names
    are usually long and segment labels need room.

    Keep segments few — 3-5 reads well, 10 is a muddle. High-cardinality
    segments belong in a different chart entirely.
    """
    chart_type: Literal["stacked_horizontal_bar"]
    category_field: str
    value_field: str
    segment_field: str

    def required_fields(self) -> list[str]:
        return [self.category_field, self.value_field, self.segment_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        # Pivot to wide, then plot cumulative left edges — matplotlib has no
        # native stacked-barh, so `left=` carries the running offset.
        grid = df.pivot(index=self.category_field, columns=self.segment_field,
                        values=self.value_field).fillna(0)
        fig, ax = self._setup()
        left = pd.Series(0.0, index=grid.index)
        for segment in grid.columns:
            ax.barh(grid.index, grid[segment], left=left, label=str(segment))
            left += grid[segment]
        ax.legend(title=self.segment_field, bbox_to_anchor=(1.02, 1),
                  loc="upper left")     # outside — segments crowd the plot
        return self._finish(fig, ax)

class LineChartSpec(ChartSpecBase):
    """Trends over an ordinal axis.
    Fields: x_field (ordinal), y_field (num), hue_field (optional).

    One row per x value, sorted — the renderer sorts, but gaps stay gaps:
        [{"order_dow": 0, "orders": 6209},
         {"order_dow": 1, "orders": 5788}]
    """
    chart_type: Literal["line"]
    x_field: str
    y_field: str
    hue_field: str | None = None

    def required_fields(self) -> list[str]:
        return [f for f in (self.x_field, self.y_field, self.hue_field) if f]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        # Sort here: a line chart of unsorted rows draws a zigzag that looks
        # like data rather than a bug.
        s = df.sort_values(self.x_field)
        fig, ax = self._setup()
        sns.lineplot(data=s, x=self.x_field, y=self.y_field,
                     hue=self.hue_field, marker="o", ax=ax)
        return self._finish(fig, ax)

class HistogramChartSpec(ChartSpecBase):
    """Distribution of one continuous variable.
    Fields: bucket_field, count_field. Renders with ax.bar.

    Expects data BINNED IN SQL — one row per bucket, ~20-40 rows:
        [{"bucket": 7,  "n": 41203},
         {"bucket": 14, "n": 38102}]

    Raw observations are millions of rows; the 1,000-row cap would truncate to
    an arbitrary slice and produce a confidently wrong chart. Bin first:
        SELECT FLOOR(days_since_prior_order / 7) * 7 AS bucket, COUNT(*) AS n
        FROM ... GROUP BY bucket ORDER BY bucket
    """
    chart_type: Literal["histogram"]
    bucket_field: str             # bucket's lower edge, from SQL
    count_field: str              # rows in that bucket

    def required_fields(self) -> list[str]:
        return [self.bucket_field, self.count_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        # ax.bar, NOT sns.histplot — the data is already binned, so there is
        # nothing left to bin. histplot would re-bin the bucket edges.
        s = df.sort_values(self.bucket_field)
        fig, ax = self._setup()
        width = (s[self.bucket_field].diff().dropna().min()
                 if len(s) > 1 else 1)
        ax.bar(s[self.bucket_field], s[self.count_field],
               width=width * 0.9, align="edge")
        return self._finish(fig, ax)

class BoxChartSpec(ChartSpecBase):
    """Distribution across categories, from PRE-COMPUTED quantiles.
    Fields: category_field, q1_field, med_field, q3_field, whislo_field,
    whishi_field. Renders with ax.bxp.

        [{"department": "produce", "q1": 0.40, "med": 0.61, "q3": 0.72,
          "whislo": 0.11, "whishi": 0.95}]

    Raw rows per category would blow the 1,000-row cap. Compute in SQL:
        SELECT department,
               APPROX_QUANTILES(reorder_rate, 4)[OFFSET(1)] AS q1,
               APPROX_QUANTILES(reorder_rate, 4)[OFFSET(2)] AS med,
               APPROX_QUANTILES(reorder_rate, 4)[OFFSET(3)] AS q3,
               MIN(reorder_rate) AS whislo, MAX(reorder_rate) AS whishi
        FROM ... GROUP BY department
    """
    chart_type: Literal["box"]
    category_field: str
    q1_field: str
    med_field: str
    q3_field: str
    whislo_field: str
    whishi_field: str

    def required_fields(self) -> list[str]:
        return [self.category_field, self.q1_field, self.med_field,
                self.q3_field, self.whislo_field, self.whishi_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        # ax.bxp takes PRE-COMPUTED stats — these exact dict keys are its
        # documented contract, not names of our choosing.
        stats = [{"label": r[self.category_field],
                  "q1":     r[self.q1_field],
                  "med":    r[self.med_field],
                  "q3":     r[self.q3_field],
                  "whislo": r[self.whislo_field],
                  "whishi": r[self.whishi_field],
                  "fliers": []}                    # required key, even if empty
                 for _, r in df.iterrows()]
        fig, ax = self._setup()
        ax.bxp(stats, showfliers=False)
        return self._finish(fig, ax)

class HeatmapChartSpec(ChartSpecBase):
    """Two-category grid. Fields: x_field (cat), y_field (cat), value_field.
    Renders via df.pivot(...) then sns.heatmap(annot=True).

    LONG form — one row per CELL, not per row of source data:
        [{"order_dow": 0, "order_hour": 10, "orders": 812},
         {"order_dow": 0, "order_hour": 11, "orders": 934}]

    7 days x 24 hours = 168 rows. Both axes must be low-cardinality, or the
    grid is unreadable before the row cap is even reached.
    """
    chart_type: Literal["heatmap"]
    x_field: str
    y_field: str
    value_field: str

    def required_fields(self) -> list[str]:
        return [self.x_field, self.y_field, self.value_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        # A reshape, not an aggregation. Duplicate index/column pairs raise —
        # and that error is correct: the query returned the wrong grain.
        grid = df.pivot(index=self.y_field, columns=self.x_field,
                        values=self.value_field)
        fig, ax = self._setup()
        sns.heatmap(grid, annot=True, fmt=".4g", ax=ax)
        return self._finish(fig, ax)

class ConcentrationChartSpec(ChartSpecBase):
    """Cumulative share vs. rank percentile — "do 20% of X drive 80% of Y?"
    Fields: percentile_field, cumulative_pct_field, highlight_threshold_pct.

        [{"pct": 1, "cum_pct": 12.4},
         {"pct": 2, "cum_pct": 19.8}]

    ~100 rows, with the cumulative math done IN SQL — computing it here would
    make percentages relative to whatever survived the row cap, putting the
    80% crossing in the wrong place while the chart still looks fine:
        SELECT NTILE(100) OVER (ORDER BY orders DESC) AS pct,
               SUM(SUM(orders)) OVER (ORDER BY NTILE(...)) * 100.0
                 / SUM(SUM(orders)) OVER () AS cum_pct
        FROM ... GROUP BY product_id
    """

    chart_type: Literal["concentration"]
    percentile_field: str          # 1-100, from NTILE(100) in SQL
    cumulative_pct_field: str      # cumulative % of total, computed in SQL
    highlight_threshold_pct: float = Field(default=80.0, ge=1, le=99)
    cumulative_color: str = "red"

    def required_fields(self) -> list[str]:
        return [self.percentile_field, self.cumulative_pct_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        s = df.sort_values(self.percentile_field).reset_index(drop=True)
        fig, ax = self._setup()
        ax.plot(s[self.percentile_field], s[self.cumulative_pct_field],
                color=self.cumulative_color)
        ax.axhline(self.highlight_threshold_pct, linestyle="--", color="gray")

        # Where the curve crosses the threshold — a real x-value on a numeric
        # axis, not a categorical position, so no index/position mismatch.
        above = s[s[self.cumulative_pct_field] >= self.highlight_threshold_pct]
        if not above.empty:
            ax.axvline(above.iloc[0][self.percentile_field],
                       linestyle="--", color="gray")
        ax.set_xlabel("Percentile of products (ranked by volume)")
        ax.set_ylabel("Cumulative % of total")
        return self._finish(fig, ax)

class ParetoChartSpec(ChartSpecBase):
    """Sorted bars + cumulative line — "which categories dominate?"
    Fields: category_field, value_field, highlight_threshold_pct.

        [{"department": "produce", "orders": 5231},
         {"department": "dairy",   "orders": 4102}]

    LOW-CARDINALITY ONLY — departments (~21) or aisles (~134), never products
    (~50k): every bar needs a readable label. For products use `concentration`.
    Sorting and the cumulative line are computed here; aggregation is not.
    """
    chart_type: Literal["pareto"]
    category_field: str
    value_field: str
    highlight_threshold_pct: float = Field(default=80.0, ge=1, le=99)
    cumulative_color: str = "red"

    def required_fields(self) -> list[str]:
        return [self.category_field, self.value_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        s = df.sort_values(self.value_field, ascending=False).reset_index(drop=True)
        cum_pct = s[self.value_field].cumsum() / s[self.value_field].sum() * 100
        # reset_index is LOAD-BEARING: without it idxmax() returns the ORIGINAL
        # index label, but axvline on a categorical axis needs the POSITION.
        # With unsorted input those differ and the line lands on the wrong bar
        # — silently, since the chart still renders.
        crossing = int((cum_pct >= self.highlight_threshold_pct).idxmax())

        fig, ax1 = self._setup()
        ax1.bar(s[self.category_field], s[self.value_field])
        ax2 = ax1.twinx()
        ax2.plot(s[self.category_field], cum_pct,
                 color=self.cumulative_color, marker="o")
        ax2.axhline(self.highlight_threshold_pct, linestyle="--", color="gray")
        ax2.axvline(crossing, linestyle="--", color="gray")
        ax2.grid(False)                  # avoid double grid from twinx
        return self._finish(fig, ax1)
```

## Discriminated union — makes the LLM's choice type-safe

```python
ChartSpec = Annotated[
    Union[BarChartSpec, HorizontalBarChartSpec, GroupedBarChartSpec,
          StackedHorizontalBarChartSpec, LineChartSpec,
          HistogramChartSpec, BoxChartSpec, HeatmapChartSpec,
          ConcentrationChartSpec, ParetoChartSpec],
    Field(discriminator="chart_type"),
]
```

A request using the wrong fields for its `chart_type` fails Pydantic validation
before any code runs. **A new class missing from this union works in isolation
and is unreachable by the LLM** — there's a completeness test for exactly this.

## The tool — the union MUST be wrapped in a model, not passed bare

**Never `def generate_chart(spec: ChartSpec)`.** Passing the bare
`Annotated[Union[...], Field(discriminator=...)]` as the parameter type loses
the discriminator. Pydantic emits it correctly from `model_json_schema()` when
the union is a **field on a model** — but tool frameworks build their schema by
introspecting the *function signature* and synthesizing a per-parameter schema,
and that synthesis is where `Field(discriminator=...)` on a bare parameter gets
dropped or flattened to a plain `anyOf`.

**Not cosmetic, and it interacts with `strict=True`:** a real discriminator
lets the provider dispatch on `chart_type`; a flattened `anyOf` makes it
validate against all ten variants in turn — slower, and a strictly weaker
structural guarantee.

**The fix — wrap it, so schema generation goes through Pydantic's own
well-tested path:**

**Two wrapper models, not one — same substitution pattern as
`run_bigquery_sql`'s `conversation_id`, just inverted (`.claude/rules/tools.md`).**
`generate_chart` is MCP-hosted with a fully generic, shareable contract: it
takes `data` directly, and has no idea a "tool call" or a conversation even
exists. The model never sees `data` — it sees `source_tool_call_id` instead,
so it never re-transcribes rows through the context window (token cost, and
a paraphrased number would break the verification contract). `dispatch_tool`
is the only thing that ever sees both shapes, and it's what translates one
into the other.

```python
# The REAL MCP tool's args — never a tool_call_id, never conversation state.
# Usable by any caller with its own data, this project's orchestrator included.
class GenerateChartArgs(BaseModel):
    data: list[dict]
    spec: ChartSpec        # the Annotated discriminated union, as a FIELD

# The MODEL-FACING schema — bound via bind_tools(), never sent to the MCP
# server as-is. Same spec fields (chart_type, x_label, ...); data replaced by
# a reference the model can actually supply without hallucinating numbers.
class GenerateChartToolCallArgs(BaseModel):
    source_tool_call_id: str
    spec: ChartSpec

def generate_chart(args: GenerateChartArgs) -> ChartResult:
    """The real MCP tool. No injected state — data has already been
    resolved by dispatch_tool before this is ever called."""
    # Rows are already list[dict] with coerced types — the query tools
    # normalize at the source, so this is a plain construction.
    df = pd.DataFrame(args.data)
    missing = [f for f in args.spec.required_fields() if f not in df.columns]
    if missing:
        # Carry the REMEDY, not just the diagnosis. This error fires exactly
        # when the model is re-planning, which is better timing than any
        # up-front guidance — so hand it the query it needs, not a puzzle.
        # The spec's docstring — with its required shape and any SQL — is in
        # the model's context for the whole turn, so naming the gap is enough.
        raise ToolError(
            f"Field(s) {missing} not in source data. Available: {list(df.columns)}")
    fig = args.spec.render(df)
    return ChartResult(chart_url=render_and_upload(fig))
```

**Resolution — `source_tool_call_id → data` — happens in `dispatch_tool`, not
in the tool.** No separate cache needed: `state["tool_calls"]` already holds
every call's native-Python `result` this turn
(`.claude/rules/orchestrator.md`).

```python
def resolve_chart_data(tool_call: dict, tool_calls: list[ToolCallRecord]) -> dict:
    """Rewrite a model-facing chart call into the real MCP tool's args.

    Scoped to THIS turn's tool_calls deliberately — charting a previous
    turn's result would render stale numbers under a fresh question.
    """
    call_args = GenerateChartToolCallArgs.model_validate(tool_call["args"])
    rec = next((tc for tc in tool_calls if tc["id"] == call_args.source_tool_call_id),
               None)
    if rec is None or not rec["success"]:
        raise ToolError(
            f"No tool call '{call_args.source_tool_call_id}' found this turn.")
    return {"data": rec["result"], "spec": call_args.spec.model_dump()}
```

## Delivery — a URL, never image bytes

**Decided (2026-09-13): signed URLs, not a public bucket.** The original plan
was `blob.make_public()` on an anonymously-readable bucket — acceptable for
public Kaggle data, since nothing here is sensitive. It turned out not to be
available: this project's GCP project enforces **uniform bucket-level
access** via org policy, and — because it's a personal, org-less account —
there's no Organization or Folder resource where `roles/orgpolicy.policyAdmin`
can be granted to override it. Not a permissions gap to fix; there is no
resource in the hierarchy that holds the override. Signed URLs work fine
under enforced uniform bucket-level access because they don't depend on
per-object ACLs at all.

```python
import datetime

def render_and_upload(fig: plt.Figure) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)                 # required — see below
    buf.seek(0)
    blob = storage_client.bucket(GCS_CHART_BUCKET).blob(f"charts/{uuid.uuid4()}.png")
    blob.upload_from_file(buf, content_type="image/png")
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(hours=1),   # proposed — revisit if a
                                                    # chart needs to stay live
                                                    # longer than one sitting
        method="GET",
    )
```

The `https://` URL goes into `chart_url` as a plain JSON string; a Power Apps
**Image control** binds to it and fetches the image itself, like a browser
loading `<img src>`. Same as before — return the `https://` signed URL, never
a `gs://` URI.

**Cloud Run gotcha: V4 signing needs an extra grant.** `agent-sa` runs on
attached service-account credentials with no private key file, and
`generate_signed_url()` needs one to sign locally — without it, it falls back
to the IAM Credentials API's `signBlob`, which requires `agent-sa` to hold
`roles/iam.serviceAccountTokenCreator` **on itself** (a self-referential
binding, easy to miss since every other grant in this project names a
*different* identity as the member). Without this, signing fails at first
chart, not at deploy. Setup command lives in
`local-dev-environment-setup.md` Step 13 item 6.

**Trade-off now baked in:** a chart link expires. That's fine for a chat
answer read in the same sitting; it means a saved/shared chart link goes dead
after the expiry window, which the anonymous-bucket approach wouldn't have
had. Acceptable here since nothing currently persists chart URLs beyond the
conversation.

**`plt.close(fig)` after saving** — figures aren't garbage-collected on return
and leak memory across requests on a long-running instance. Won't show up in a
quick local test.

## Bounded, not unlimited

Chart types are a small, growable set of classes — not arbitrary code
execution. Adding one = one class, two methods, one union entry. If a request
needs a type not on the list, that's a real bounded limitation: say so rather
than improvising.
