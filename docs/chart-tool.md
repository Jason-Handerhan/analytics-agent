# Chart Tool Design

**It renders; it never computes.** The query already did the grouping,
filtering, and joins. No aggregation, no `group_by`, no branching on which tool
produced the data. If a chart needs data shaped differently, fix the **query**,
not the chart tool.

## Base class — shared styling contract

Every `render()`: start with `self._setup()`, end with `self._finish(fig, ax)`.
That keeps styling consistent without each class reimplementing it.

```python
# app/mcp_server/chart_tool.py
from abc import ABC, abstractmethod
from typing import Literal, Union, Annotated
import matplotlib
matplotlib.use("Agg")          # required — Cloud Run has no display
import matplotlib.pyplot as plt
import seaborn as sns
import pandas as pd
import numpy as np
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field, model_validator

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
CONCENTRATION_BUCKETS = 20   # fixed NTILE grain -- 5% per bucket
MAX_PARETO_LABELS     = 25   # cumulative-% labels shown before evenly thinning
MAX_LABELED_BARS      = 25   # bar value labels skipped entirely above this count
CUMULATIVE_AXIS_LABEL = "Cumulative % of Total"   # fixed right-axis label -- concentration/pareto

MAX_BAR_CATEGORIES         = 150  # bar, horizontal_bar, pareto -- matches the
                                   # documented "aisles ~134" ceiling
MAX_GROUPED_BAR_CATEGORIES = 15   # denser than plain bar -- multiple bars per cluster
MAX_STACKED_BAR_CATEGORIES = 50
MAX_STACKED_BAR_SEGMENTS   = 10   # matches the documented "3-5 reads well, 10 is a muddle"
MAX_LINE_POINTS            = 100
MAX_LINE_HUE_GROUPS        = 8
MAX_HISTOGRAM_BUCKETS      = 50
MAX_BOX_CATEGORIES         = 30
MAX_HEATMAP_AXIS           = 30

class ChartSpecBase(BaseModel, ABC):
    # source_tool_call_id is NOT here. It's on the model-facing schema only
    # (below, in GenerateChartToolCallArgs) — this base is shared by the
    # REAL MCP tool's schema too, which takes resolved `data` instead and
    # must never see a tool_call_id at all (.claude/rules/tools.md).
    title: str = Field(..., min_length=1, description="Chart title.")
    x_label: str = Field(..., min_length=1, description="X-axis label.")
    y_label: str = Field(..., min_length=1, description="Y-axis label.")

    @abstractmethod
    def required_fields(self) -> list[str]: ...

    @abstractmethod
    def render(self, df: pd.DataFrame) -> plt.Figure: ...

    def _setup(self) -> tuple[plt.Figure, plt.Axes]:
        sns.set_theme(style=STYLE, context=CONTEXT, palette=PALETTE)
        return plt.subplots(figsize=FIGSIZE, dpi=DPI)

    def _finish(self, fig: plt.Figure, ax: plt.Axes) -> plt.Figure:
        ax.set_title(self.title, pad=12)
        ax.set_xlabel(self.x_label)
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
        """Bar value labels -- skipped entirely once any container holds more
        than MAX_LABELED_BARS bars, so every caller (present and future) gets
        the same overcrowding protection with no per-call-site threshold."""
        if any(len(container) > MAX_LABELED_BARS for container in ax.containers):
            return
        value_format = getattr(self, "value_format", "auto")
        for container in ax.containers:
            ax.bar_label(container, fmt=lambda v: self._format_value(v, value_format),
                        padding=2, fontsize=9)

    def _format_value(self, value: float, value_format: str) -> str:
        """Formats one value for an axis tick or bar label. 'auto' picks based
        on magnitude -- comma-grouped whole numbers at 1000+, up to 4
        significant figures below that -- since a chart can't know a column
        is a percent or dollar amount from the number alone."""
        if value_format == "percent":
            return f"{value:.1%}"
        if value_format == "currency":
            return f"${value:,.2f}"
        if abs(value) >= 1000:
            return f"{value:,.0f}"
        return f"{value:,.4g}"

    def _apply_value_format(self, ax: plt.Axes, axis: Literal["x", "y"]) -> None:
        """Formats an axis's tick labels using self.value_format, defaulting
        to 'auto' for a class that has no such field (e.g. histogram, which
        never needs the percent/currency choice)."""
        value_format = getattr(self, "value_format", "auto")
        getattr(ax, f"{axis}axis").set_major_formatter(
            FuncFormatter(lambda v, pos: self._format_value(v, value_format)))

    def _check_no_duplicate_grain(self, df: pd.DataFrame, columns: list[str]) -> None:
        """Raises if any combination of `columns` repeats -- the data isn't
        aggregated to the grain this chart type expects."""
        dupes = df[df.duplicated(subset=columns, keep=False)]
        if not dupes.empty:
            example = dupes[columns].iloc[0].to_dict()
            raise ToolError(
                f"Data isn't aggregated to one row per {', '.join(columns)} -- found "
                f"a repeated value: {example}. Aggregate in the query before charting.")

    def _check_max_categories(self, count: int, limit: int, label: str) -> None:
        if count > limit:
            raise ToolError(
                f"Too many {label} ({count}) for a readable chart -- max {limit}. "
                "Filter, aggregate, or narrow the query to reduce it.")
```

**Two shared grain guardrails on the base class, used by most (not all) concrete
types (2026-09-27).** `_check_no_duplicate_grain` catches data that isn't
aggregated to the grain a chart type expects -- several render paths fail
*silently* on this rather than erroring, confirmed live before building the
check: `sns.barplot`/`sns.lineplot` silently average duplicate-category rows
(tested: two rows `[100, 300]` for one category plotted as a bar of height
200, the mean, not 400, the sum); a plain `ax.bar()` on a string axis (pareto)
desyncs bars from tick labels once a duplicate breaks the 1:1 assumption,
misattributing a real value to the wrong category name; a plain `ax.bar()` on
a numeric axis (histogram) draws overlapping bars at the same position,
silently hiding the shorter one entirely. `.pivot()` (heatmap,
stacked_horizontal_bar) already raised on duplicates before this existed;
routing those two through the same shared check as everything else just gives
a consistent, actionable `ToolError` instead of a raw pandas one.
`_check_max_categories` is a separate, independent guardrail -- it fails for
a different reason (a chart that's correctly aggregated but still too dense
to read) and should give a different message, not be folded into the same
check.

**The threshold lives inside `_annotate_values` itself, not at each call
site (2026-09-27).** `ax.containers` already carries the count that matters
regardless of caller shape -- confirmed live for `grouped_bar` specifically,
since it's the trickiest case: seaborn creates one container per hue level,
each holding one bar per x-category, so `len(container)` is the x-category
count either way. A future chart type that starts calling
`_annotate_values` gets this protection automatically, with nothing to
remember to add at the call site.

**Both evaluated candidates resolved (2026-09-28), in different directions.**
`grouped_bar` just calls `self._annotate_values(ax)` -- the mechanism above
already covers it exactly as anticipated, no changes needed.
`stacked_horizontal_bar` needed a genuinely different mechanism instead of
this one -- see its own section below.

**Styling is fixed rather than model-controlled** — but the same trust
split still explains why that's a free choice: no styling field can change
what the chart *shows*, only how it looks. Since nothing about it needed
guarding, the only question was whether exposing it *helped*, and it didn't.
The original framing holds: "LLM chooses which data, deterministic code
computes the numbers." Fixing the styling extends that — the model now
chooses *only* data and labels.

**`value_format` is the one exception, and deliberately so (2026-09-28) —
it isn't styling.** Colors/fonts/sizing never change what the chart *shows*;
whether `0.65` displays as `"0.65"` or `"65%"` genuinely does, and only the
model knows which is right — it wrote the query that produced the column.
A magnitude-only heuristic can comma-format `247139 -> "247,139"` and round
decimals sensibly, but it cannot tell a rate from a plain number by the
value alone. So the split is: **`_format_value`/`_apply_value_format`
handle magnitude automatically** (every chart gets this for free); **the
model opts in to `value_format: "percent" | "currency"`** only when the
column actually needs it.

```python
class FormattedValueSpec(BaseModel):
    """Mixin for chart types with a value axis that isn't self-evidently a
    plain number -- adds the field _format_value/_apply_value_format need."""
    value_format: Literal["auto", "percent", "currency"] = Field(
        default="auto",
        description="How to format axis labels and value annotations. 'auto' "
                    "picks based on magnitude; set 'percent' or 'currency' when "
                    "the value isn't a plain number.")
```

**A mixin, not a `ChartSpecBase` field.** Only six types have a genuinely
ambiguous value axis — `bar`, `horizontal_bar`, `grouped_bar`,
`stacked_horizontal_bar`, `line`, `box`. `pareto`/`concentration` already
hardcode `%` for their cumulative axis; **`heatmap` is deliberately
excluded** — `sns.heatmap(annot=True, fmt=...)` only accepts a plain
format-spec string per cell, not a callable, so plugging in custom
formatting there means pre-building a matching-shape array of
already-formatted strings instead. Judged not worth the complexity for one
chart type; left as-is. Given cell values are unlabeled with a formatter
either way, `heatmap` instead gets a **`cmap="Blues"`** — a single-hue
sequential ramp, light→dark, the correct encoding for a magnitude-only,
non-diverging grid (day-of-week × hour order counts have no meaningful
zero-crossing, so a diverging map like the previous default would have been
semantically wrong, not just a style preference).

**`histogram`'s count doesn't need the *choice* — it's never a percent or
currency — but it still gets `_apply_value_format`, just without the mixin
(2026-09-28).** Originally excluded entirely on the reasoning that its count
was "obviously plain," conflating "doesn't need the percent/currency option"
with "doesn't need magnitude formatting at all." `_apply_value_format`/
`_annotate_values` read `getattr(self, "value_format", "auto")` rather than
`self.value_format` directly specifically so a class can get the safe
`"auto"` formatting without opting into the mixin's model-facing choice —
`histogram.render()` calls `self._apply_value_format(ax, "y")` on that basis
alone. `pareto`'s and `concentration`'s own primary bar axis has the same
gap today (never wired to a formatter) — not fixed here, same root cause,
flagged for later. **A landmine if that axis ever is wired up for
`concentration` specifically:** `bucket_share` there is already on a 0–100
scale (a difference of `cum_pct` values), not 0–1 — `_format_value("percent")`
assumes a 0–1 fraction (`f"{value:.1%}"`) and would incorrectly re-multiply
by 100.

**The naive `f"{v:,.4g}"` looked right until checked against real values —
confirmed live it silently breaks on exactly the case that matters most:**

```pycon
>>> f"{247139.0:,.4g}"
'2.471e+05'          # scientific notation -- wrong for an order count
```

`g` format switches to scientific notation once the requested significant
figures (4) is fewer than the integer's digit count. Fixed with a magnitude
split instead — large values get fixed-point with comma grouping (never
scientific), small values get general format:

```python
def _format_value(self, value: float, value_format: str) -> str:
    if value_format == "percent":
        return f"{value:.1%}"
    if value_format == "currency":
        return f"${value:,.2f}"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.4g}"
```

**No handling for magnitudes below ~0.0001, where `g` also flips to
scientific notation — deliberately, not an oversight.** Considered and
dropped: this project's real chart data (order counts, reorder rates,
recall/precision metrics, profit lift) doesn't produce values that small,
and guarding against it would be defensive complexity for a case that
can't realistically occur here. Revisit only if a real chart type's values
turn out to need it.

**Two different wiring mechanisms, not one** — `bar_label` and axis ticks
don't share a formatting hook:
- `ax.bar_label(fmt=...)` accepts a callable directly:
  `fmt=lambda v: self._format_value(v, self.value_format)`.
- Axis ticks need `matplotlib.ticker.FuncFormatter`:
  `ax.yaxis.set_major_formatter(FuncFormatter(lambda v, pos: self._format_value(v, self.value_format)))`.

Applied per chart type on whichever axis is the value axis: `bar`/
`grouped_bar`/`line`/`box` → `"y"`; `horizontal_bar`/
`stacked_horizontal_bar` → `"x"`.

**Both new methods live on `ChartSpecBase`, not as module-level functions —
matching this file's actual dominant pattern, confirmed by re-reading it,
not assumed.** `_annotate_values`, `_check_no_duplicate_grain`, and
`_check_max_categories` don't touch `self` in their bodies either, and all
three are still methods; only `_find_crossing`/`_annotate_crossing` were
ever free functions in this file, and that was the minority case, not the
rule. `_apply_value_format` and the updated `_annotate_values` read
`self.value_format` directly, same as `_finish` already reads `self.title` —
only `_format_value` itself stays pure, taking `value_format` as an
explicit parameter rather than touching `self`, so it works the same
whether called with a real spec's setting or a literal test value.

**`title`/`x_label`/`y_label` are required, not optional-with-fallback
(2026-09-27).** A raw DAX column renders as `Products[department]`; only the
model knows the question well enough to write "Department" — so the earlier
"falls back to the column name when omitted" design left the *worse* outcome
reachable on purpose. Under `strict=True`, every field already has to appear
in the schema's `required` list regardless of whether it carries a Python
default (`.claude/rules/orchestrator.md`) — the only thing an optional
`None` default actually bought was letting the model emit `null` and get the
raw-column-name fallback. Requiring a real, non-empty string (`min_length=1`)
closes that path instead of just discouraging it.

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

## `stacked_horizontal_bar` segment labels — centered, white, size-aware (2026-09-28)

A different mechanism from `_annotate_values` entirely — that one labels at
the bar's *edge* and skips a whole container past a bar-count threshold;
this labels at each segment's *center*, in white (readable against a filled
segment, unlike the default dark label color), and skips individual
segments too narrow to hold text, not whole containers.

```python
def _annotate_segments(self, ax: plt.Axes, containers: list) -> None:
    axis_min, axis_max = ax.get_xlim()
    axis_range = axis_max - axis_min
    value_format = getattr(self, "value_format", "auto")
    for container in containers:
        ax.bar_label(
            container, label_type="center", color="white", fontsize=9,
            fmt=lambda v: self._format_value(v, value_format) if v / axis_range >= 0.10 else "",
        )
```

**The "too narrow" threshold is 10% of the *axis* range, not 10% of that
row's own bar total — a real correction caught during design, not the
original plan.** Every row shares the same x-scale, so a segment's rendered
*pixel* width — the thing that actually determines whether a label
overflows — depends only on its value relative to the whole axis span, not
on how long its own row's total bar happens to be. A row-relative version
could pass a segment that's 50% of a short bar's total while it's still
visually tiny against the chart's overall scale — precisely the overflow
case this exists to prevent. `bar_label`'s `fmt` callable returning `""` is
what makes the skip trivial: an empty label renders as nothing, no separate
conditional-removal step needed.

**Must run only after every segment is drawn, in a second pass — checking
mid-loop would compare against a not-yet-final axis range.** Each `ax.barh`
call stacks a new segment onto the running `left` offset, and matplotlib
expands the x-axis limits as each one is added. `render()` collects every
call's returned container into a list, then calls `_annotate_segments` once,
after the loop, so `ax.get_xlim()` reflects the true, final stacked width —
not order-dependent on which segment happens to be biggest.

**A method on `StackedHorizontalBarChartSpec` itself, not `ChartSpecBase`**
— single-caller, single-chart-type logic, same reasoning as
`ConcentrationChartSpec`'s own `_find_crossing`/`_annotate_crossing`.

**This code listing predates `FormattedValueSpec`/`value_format`
(2026-09-28) and doesn't show it** — six of the classes below
(`bar`/`horizontal_bar`/`grouped_bar`/`stacked_horizontal_bar`/`line`/`box`)
now also inherit `FormattedValueSpec` and call `self._apply_value_format(ax, ...)`
in `render()`; see the real source for the current field list. Same
known-stale status as the missing `Field(description=...)` wrappers below —
flagged, not fixed, since resyncing this whole listing is separate work from
whatever change last touched it.

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
        self._check_no_duplicate_grain(df, [self.x_field])
        self._check_max_categories(df[self.x_field].nunique(), MAX_BAR_CATEGORIES, "categories")
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
        self._check_no_duplicate_grain(df, [self.y_field])
        self._check_max_categories(df[self.y_field].nunique(), MAX_BAR_CATEGORIES, "categories")
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
        self._check_no_duplicate_grain(df, [self.x_field, self.hue_field])
        self._check_max_categories(df[self.x_field].nunique(), MAX_GROUPED_BAR_CATEGORIES,
                                    "x-axis categories")
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
        self._check_no_duplicate_grain(df, [self.category_field, self.segment_field])
        self._check_max_categories(df[self.category_field].nunique(),
                                    MAX_STACKED_BAR_CATEGORIES, "categories")
        self._check_max_categories(df[self.segment_field].nunique(),
                                    MAX_STACKED_BAR_SEGMENTS, "segments")
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
        grain_cols = [self.x_field] if self.hue_field is None else [self.x_field, self.hue_field]
        self._check_no_duplicate_grain(df, grain_cols)
        self._check_max_categories(df[self.x_field].nunique(), MAX_LINE_POINTS, "x-axis points")
        if self.hue_field is not None:
            self._check_max_categories(df[self.hue_field].nunique(), MAX_LINE_HUE_GROUPS, "hue groups")
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
        self._check_no_duplicate_grain(df, [self.bucket_field])
        self._check_max_categories(df[self.bucket_field].nunique(), MAX_HISTOGRAM_BUCKETS, "buckets")
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
        self._check_no_duplicate_grain(df, [self.category_field])
        self._check_max_categories(df[self.category_field].nunique(), MAX_BOX_CATEGORIES, "categories")
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
        self._check_no_duplicate_grain(df, [self.x_field, self.y_field])
        self._check_max_categories(df[self.x_field].nunique(), MAX_HEATMAP_AXIS, "x-axis categories")
        self._check_max_categories(df[self.y_field].nunique(), MAX_HEATMAP_AXIS, "y-axis categories")
        grid = df.pivot(index=self.y_field, columns=self.x_field,
                        values=self.value_field)
        fig, ax = self._setup()
        sns.heatmap(grid, annot=True, fmt=".4g", ax=ax)
        return self._finish(fig, ax)

```

**Only the found value is labeled, not both — the guide line's own position
already shows the given one (2026-09-27).** A horizontal line at y=80 already
tells the reader "80%"; restating it in the label next to the newly-computed
x-value was redundant. **The white background box, not a computed offset
direction, is what keeps the label legible over the curve and guide line.**
Both lines pass directly through the intersection point by construction, so
no fixed offset avoids crossing one or the other for every possible curve
shape — a semi-opaque box makes the exact position not matter.

**No out-of-range check in `_find_crossing` — deliberately, not an oversight
(2026-09-27).** An earlier version raised `ToolError` if the target fell
outside `cum_pct.min()`/`cum_pct.max()`, reasoning that a valid `ge=1, le=99`
target could still land below the *actual data's* minimum (concentration
charts are for skewed distributions — it's entirely plausible for the first
5% bucket alone to already account for 35%+ of the total). That reasoning
was correct, but pointed at the wrong fix: the real bug was that the curve
never included its own origin. A cumulative curve always starts at (0, 0) by
definition — 0% of the ranked population trivially accounts for 0% of the
total — and the original code jumped straight to the first real bucket
instead of plotting that point. Once `render()` anchors the curve at (0, 0)
(below), its domain provably spans the full 0–100 range on both axes, so any
`highlight_at_cum_pct`/`highlight_at_rank_pct` value Pydantic accepts
(`ge=0.1, le=99.9`) is now structurally guaranteed to fall inside it — the
check became dead code once the actual bug was fixed, not just unlikely to
fire.

```python
class ConcentrationChartSpec(ChartSpecBase):
    """Cumulative share vs. rank percentile, with each bucket's own share as
    bars — "do 20% of X drive 80% of Y?"
    Fields: percentile_field, cumulative_pct_field, one of
    highlight_at_cum_pct/highlight_at_rank_pct.

    y_label describes the BARS (each bucket's own share) -- the cumulative
    line's own axis is conventionally unlabeled, matching pareto.

        [{"pct_of_products": 5, "cum_pct": 12.4},
         {"pct_of_products": 10, "cum_pct": 19.8}]

    Exactly 20 rows, one per 5% bucket, with the cumulative math done IN SQL —
    computing it here would make percentages relative to whatever survived the
    row cap, putting the crossing in the wrong place while the chart still
    looks fine. percentile_field must be the actual percent of the ranked
    population (5, 10, ..., 100), not the raw bucket index -- it's plotted
    directly as the x-axis:
        WITH ranked AS (
          SELECT product_id, orders,
                 NTILE(20) OVER (ORDER BY orders DESC) AS bucket
          FROM ...
        ),
        bucketed AS (
          SELECT bucket, SUM(orders) AS bucket_orders FROM ranked GROUP BY bucket
        )
        SELECT bucket * 5 AS pct_of_products,
               SUM(bucket_orders) OVER (ORDER BY bucket) * 100.0
                 / SUM(bucket_orders) OVER () AS cum_pct
        FROM bucketed
        ORDER BY bucket
    """

    chart_type: Literal["concentration"] = Field(description='Always "concentration".')
    percentile_field: str = Field(description="Column with the percent of the ranked population "
                                   "covered so far -- 5, 10, 15, ..., 100. Not the raw bucket index.")
    cumulative_pct_field: str = Field(description="Column with the cumulative percent of total.")
    highlight_at_cum_pct: float | None = Field(default=None, ge=0.1, le=99.9,
        description="Find and label the rank percent where the curve reaches this percent "
                    "of the total. Mutually exclusive with highlight_at_rank_pct; defaults "
                    "to 80 if neither is set.")
    highlight_at_rank_pct: float | None = Field(default=None, ge=0.1, le=99.9,
        description="Find and label what percent of the total the top N percent (by rank) "
                    "accounts for. Mutually exclusive with highlight_at_cum_pct.")
    cumulative_color: str = Field(default="red", description="Line color for the cumulative curve.")

    @model_validator(mode="after")
    def _exactly_one_highlight(self) -> "ConcentrationChartSpec":
        if self.highlight_at_cum_pct is not None and self.highlight_at_rank_pct is not None:
            raise ValueError("Set only one of highlight_at_cum_pct or highlight_at_rank_pct, not both.")
        if self.highlight_at_cum_pct is None and self.highlight_at_rank_pct is None:
            self.highlight_at_cum_pct = 80.0
        return self

    def required_fields(self) -> list[str]:
        return [self.percentile_field, self.cumulative_pct_field]

    def _find_crossing(self, rank_pct: np.ndarray, cum_pct: np.ndarray) -> tuple[float, float]:
        """Exact linear interpolation on a sorted (rank_pct, cum_pct) curve.
        Returns the (rank_pct, cum_pct) point at whichever highlight was set."""
        if self.highlight_at_cum_pct is not None:
            return float(np.interp(self.highlight_at_cum_pct, cum_pct, rank_pct)), self.highlight_at_cum_pct
        return self.highlight_at_rank_pct, float(np.interp(self.highlight_at_rank_pct, rank_pct, cum_pct))

    def _annotate_crossing(self, ax: plt.Axes, rank_pct: np.ndarray, cum_pct: np.ndarray) -> None:
        """Draws exactly one guide line -- horizontal if highlight_at_cum_pct was
        set, vertical if highlight_at_rank_pct was -- labeling only the value
        that line's own position doesn't already show."""
        x, y = self._find_crossing(rank_pct, cum_pct)
        if self.highlight_at_cum_pct is not None:
            ax.axhline(y, linestyle="--", color="gray")
            label = f"Intersection: {x:.0f}%"
        else:
            ax.axvline(x, linestyle="--", color="gray")
            label = f"Intersection: {y:.0f}%"
        ax.annotate(label, xy=(x, y), xytext=(8, 8), textcoords="offset points",
                    fontsize=9, bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                                           edgecolor="gray", alpha=0.9))

    def render(self, df: pd.DataFrame) -> plt.Figure:
        if len(df) != CONCENTRATION_BUCKETS:
            raise ToolError(
                f"Concentration chart requires exactly {CONCENTRATION_BUCKETS} rows, one per "
                f"5% bucket. Got {len(df)}. Query with NTILE({CONCENTRATION_BUCKETS}) OVER "
                f"(ORDER BY <value> DESC), then multiply the bucket by 5 to get {self.percentile_field} "
                "-- the real percent of the ranked population, not the raw bucket index.")
        s = df.sort_values(self.percentile_field).reset_index(drop=True)
        rank_pct = s[self.percentile_field].to_numpy()
        cum_pct = s[self.cumulative_pct_field].to_numpy()
        bucket_share = np.diff(cum_pct, prepend=0.0)
        curve_rank = np.concatenate([[0.0], rank_pct])
        curve_cum = np.concatenate([[0.0], cum_pct])

        fig, ax1 = self._setup()
        width = np.diff(rank_pct).min() * 0.9 if len(rank_pct) > 1 else 1
        ax1.bar(rank_pct, bucket_share, width=width)
        ax2 = ax1.twinx()
        ax2.plot(curve_rank, curve_cum, color=self.cumulative_color)
        self._annotate_crossing(ax2, curve_rank, curve_cum)
        ax2.set_ylabel(CUMULATIVE_AXIS_LABEL)
        ax2.grid(False)
        return self._finish(fig, ax1)

class ParetoChartSpec(ChartSpecBase):
    """Sorted bars + cumulative line, labeled at each point (or evenly thinned
    when there are many categories) — "which categories dominate?"
    Fields: category_field, value_field.

        [{"department": "produce", "orders": 5231},
         {"department": "dairy",   "orders": 4102}]

    LOW-CARDINALITY ONLY — departments (~21) or aisles (~134), never products
    (~50k): every bar needs a readable label. For products use `concentration`.
    Sorting and the cumulative line are computed here; aggregation is not.
    """
    chart_type: Literal["pareto"] = Field(description='Always "pareto".')
    category_field: str = Field(description="Column with each bar's category.")
    value_field: str = Field(description="Column with the numeric value per category.")
    cumulative_color: str = Field(default="red", description="Line color for the cumulative curve.")

    def required_fields(self) -> list[str]:
        return [self.category_field, self.value_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        self._check_no_duplicate_grain(df, [self.category_field])
        self._check_max_categories(df[self.category_field].nunique(), MAX_BAR_CATEGORIES, "categories")
        s = df.sort_values(self.value_field, ascending=False).reset_index(drop=True)
        cum_pct = s[self.value_field].cumsum() / s[self.value_field].sum() * 100

        fig, ax1 = self._setup()
        ax1.bar(s[self.category_field], s[self.value_field])
        self._apply_value_format(ax1, "y")
        ax2 = ax1.twinx()
        ax2.plot(s[self.category_field], cum_pct,
                 color=self.cumulative_color, marker="o")
        ax2.set_ylabel(CUMULATIVE_AXIS_LABEL)
        ax2.grid(False)

        # Every point labeled below MAX_PARETO_LABELS; evenly thinned above it
        # so the curve's shape stays legible instead of overlapping labels.
        n = len(s)
        label_indices = (range(n) if n <= MAX_PARETO_LABELS
                         else np.linspace(0, n - 1, MAX_PARETO_LABELS, dtype=int))
        for i in label_indices:
            ax2.annotate(f"{cum_pct.iloc[i]:.0f}%", xy=(i, cum_pct.iloc[i]),
                        xytext=(0, 8), textcoords="offset points",
                        ha="center", fontsize=8)
        return self._finish(fig, ax1)
```

## Discriminated union — makes the LLM's choice type-safe

**No shared `ChartSpec` name — the union is written inline at each field that
uses it (2026-09-27).** A single top-level alias would need to live in one
file, and the two places that need it — `GenerateChartArgs` (MCP-hosted) and
`GenerateChartToolCallArgs` (orchestrator-only, see below) — are deliberately
in different modules. Each writes the same `Annotated[Union[...],
Field(discriminator=...)]` at its own `spec` field instead of importing a
shared alias.

```python
spec: Annotated[
    Union[BarChartSpec, HorizontalBarChartSpec, GroupedBarChartSpec,
          StackedHorizontalBarChartSpec, LineChartSpec,
          HistogramChartSpec, BoxChartSpec, HeatmapChartSpec,
          ConcentrationChartSpec, ParetoChartSpec],
    Field(discriminator="chart_type",
          description="Which chart type to render, with that type's own fields."),
]
```

A request using the wrong fields for its `chart_type` fails Pydantic validation
before any code runs. **A new class missing from this union works in isolation
and is unreachable by the LLM** — there's a completeness test for exactly this.

## The tool — the union MUST be a field on a model, not a bare parameter

**Never `def generate_chart(spec: <the union>)`.** Passing the bare
`Annotated[Union[...], Field(discriminator=...)]` as the parameter type loses
the discriminator. Pydantic emits it correctly from `model_json_schema()` when
the union is a **field on a model** — but tool frameworks build their schema by
introspecting the *function signature* and synthesizing a per-parameter schema,
and that synthesis is where `Field(discriminator=...)` on a bare parameter gets
dropped or flattened to a plain `anyOf`. Confirmed this holds with the
description added too — both `discriminator` and `description` survive on the
same `Field()` call.

**Not cosmetic, and it interacts with `strict=True`:** a real discriminator
lets the provider dispatch on `chart_type`; a flattened `anyOf` makes it
validate against all ten variants in turn — slower, and a strictly weaker
structural guarantee.

**Confirmed live (2026-09-28): Claude's `strict=True` rejects `oneOf`/
`discriminator` outright** (`Schema type 'oneOf' is not supported`), and
separately rejects `ge=`/`le=` numeric bounds (Concentration's two
`highlight_at_*` fields) — both fire only once `generate_chart` is actually
bound through a live agent turn, not before. **`strict=True` is currently
dropped from `agent_node`'s `bind_tools()` call entirely** (`.claude/rules/orchestrator.md`)
to keep the discriminator rather than lose it — this is a global change, not
scoped to `generate_chart`. A per-tool split is possible (pre-convert just
this tool via `convert_to_anthropic_tool(tool, strict=None)` into a raw
Anthropic-format dict, which bypasses the `strict` kwarg entirely, and hand
that dict to the same `bind_tools()` call alongside the other tools, left as
normal objects so they still get `strict=True`) — confirmed working live, but
ties the fix to Anthropic's own tool-dict shape, undermining the
model-swappable design the rest of this project protects (`.claude/rules/orchestrator.md`'s
`build_static_context(model, ...)` pattern). **Circle back before treating
`generate_chart` as done**: work out the equivalent for OpenAI and Gemini, or
confirm accepting the portability cost is worth keeping strict mode
elsewhere.

**Two wrapper models, in two different modules — same substitution pattern as
`run_bigquery_sql`'s `conversation_id`, just inverted (`.claude/rules/tools.md`).**
`generate_chart` is MCP-hosted with a fully generic, shareable contract: it
takes `data` directly, and has no idea a "tool call" or a conversation even
exists — `GenerateChartArgs` lives in `app/mcp_server/chart_tool.py`, next to
the ten spec classes it validates against. The model never sees `data` — it
sees `source_tool_call_id` instead, so it never re-transcribes rows through
the context window (token cost, and a paraphrased number would break the
verification contract). **`GenerateChartToolCallArgs` is never touched by the
MCP server at all** — it exists purely for this project's own bind-time
substitution trick (below), so it lives in `app/orchestrator/tools.py`
alongside `SubmitAnswerArgs`, not in `chart_tool.py` (2026-09-27 reusability
discussion, same "MCP hosting means built for reuse beyond this project" rule
as `.claude/rules/tools.md`'s hosting table). `dispatch_tool` is the only
thing that ever sees both shapes, and it's what translates one into the other.

**`GenerateChartToolCallArgs.spec` is built with `pydantic.create_model`,
reusing `GenerateChartArgs.model_fields["spec"]` directly, not a second
hand-written union (2026-09-27).** The alternative — writing out the same
ten-type `Annotated[Union[...], Field(discriminator=...)]` a second time in
`tools.py` — means an eleventh chart type needs updating in two files, in
two different modules, with nothing to catch a missed one. Reusing the
`FieldInfo` object itself carries the annotation, the discriminator, *and*
the description in one call — confirmed live, not assumed:

```python
spec_field = GenerateChartArgs.model_fields["spec"]
GenerateChartToolCallArgs = create_model(
    "GenerateChartToolCallArgs",
    source_ref=(str, Field(
        ..., description="The reference id printed alongside the run_bigquery_sql or "
                          "run_dax_query result you want to chart -- e.g. 'ref_1'. Copy it "
                          "exactly as shown in that tool's result; never invent one.")),
    spec=(spec_field.annotation, spec_field),
)
```

**Renamed from `source_tool_call_id` to `source_ref` once built for real
(2026-09-29).** The id-based design below predates the actual implementation:
`resolve_chart_data` doesn't match against a raw tool-call id at all — it
matches against a separate `ref_id` label (`"ref_1"`, `"ref_2"`, ...)
assigned only to a successful, chartable result and printed back to the
model alongside it, via `_lookup_chart_source`
(`.claude/rules/orchestrator.md`, "Batch ordering"). The code below still
shows the original id-based sketch for the reasoning that motivated it; see
that section for the real mechanism.

**No class-level docstring — same pattern as `SubmitAnswerArgs`
(`.claude/rules/tools.md`).** An args model carries field descriptions only;
"what does this tool do" is the *tool's* description, not the args model's.
For every other tool that's a `@tool`-decorated function's own docstring; for
`generate_chart` specifically, it's the model-facing stand-in tool object
built in the bind-time substitution step (below) — built now
(`chart_tool_call_standin` in `app/orchestrator/tools.py`), with its own
description: "Renders a chart from an earlier tool call's result and returns
its image URL. `source_ref` must be the reference id printed alongside the
`run_bigquery_sql` or `run_dax_query` result you want to chart (e.g.
`'ref_1'`) -- copy it exactly as shown; never invent one."

**Every string handed to `create_model`/`Field` here is model-facing, not a
code comment — write it as an instruction to the LLM, not a note to a future
reader (2026-09-27).** A first pass left `source_tool_call_id` with no
description at all — backwards, since this whole model exists to be read by
the LLM deciding how to call the tool. That kind of explanation belongs in
this doc's surrounding prose, never inside the schema text itself.

**One accepted wrinkle from the split: `app/orchestrator/tools.py` imports
`GenerateChartArgs` directly from `app.mcp_server.chart_tool`** (not the ten
spec classes individually — `create_model` only needs the one field). Fine
today since both are co-located in one process; if the MCP server ever moves
to its own Cloud Run service (`.claude/rules/tools.md` flags this as a
possible future move), this import breaks and `GenerateChartToolCallArgs`
needs `spec`'s type re-exposed some other way. Not solving that now.

```python
# app/mcp_server/chart_tool.py -- the REAL MCP tool's args. Never a
# tool_call_id, never conversation state. Usable by any caller with its own
# data, this project's orchestrator included.
class GenerateChartArgs(BaseModel):
    data: list[dict]
    spec: Annotated[Union[...], Field(discriminator="chart_type", description="...")]

# app/orchestrator/tools.py -- the MODEL-FACING schema, bound via
# bind_tools(), never sent to the MCP server as-is. spec's type is reused
# from GenerateChartArgs, not redeclared.
_chart_spec_field = GenerateChartArgs.model_fields["spec"]
GenerateChartToolCallArgs = create_model(
    "GenerateChartToolCallArgs",
    source_ref=(str, Field(
        ..., description="The reference id printed alongside the run_bigquery_sql or "
                          "run_dax_query result you want to chart -- e.g. 'ref_1'. Copy it "
                          "exactly as shown in that tool's result; never invent one.")),
    spec=(_chart_spec_field.annotation, _chart_spec_field),
)

# Same reasoning as run_dax_query's access_token/workspace_id/dataset_id
# (.claude/rules/tools.md): real parameters, hidden from the model, so a
# different deployer can point this at their own bucket/identity with no
# code change (2026-09-27 reusability discussion).
#
# FastMCP requirement, confirmed live: every name in exclude_args must have a
# default value on the function signature, or registration raises ValueError
# ("Parameter '...' in exclude_args must have a default value") -- the same
# reason run_dax_query's own excluded params are all `| None = None`.
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
    return ChartResult(chart_url=render_and_upload(
        fig, bucket_name, storage_backend, expiration_hours,
        access_token, service_account_email))
```

**Resolved (2026-09-27), not "confirm at build time" anymore.** Confirmed
live against the real registered tool: adding the five injected parameters as
siblings of `args: GenerateChartArgs` does make FastMCP nest `args` as its own
object property (`{"properties": {"args": {...}}}`) rather than flattening
`data`/`spec` to the top level the way it did when `args` was the sole
parameter. The discriminated union inside it is unaffected — `spec`'s
`oneOf`/discriminator/per-type descriptions all render correctly nested
under `args`. Functionally fine, just one more level of nesting than the
single-parameter case: the model supplies `{"args": {"data": [...], "spec":
{...}}}`, not `{"data": [...], "spec": {...}}` directly.

**Five hidden values, not four — `service_account_email` joins the set
(2026-09-27, found while implementing `render_and_upload`).** Signing with
just an `access_token` isn't a real code path: `generate_signed_url_v4`'s
actual logic (confirmed by reading the installed `google-cloud-storage`
source directly, not assumed) only takes the token-based signing branch when
**both** `access_token` and `service_account_email` are passed as explicit
keyword arguments to `generate_signed_url()` itself —

```python
if not access_token or not service_account_email:
    ensure_signed_credentials(credentials)   # requires real Signing credentials
    ...
if access_token and service_account_email:
    signature = _sign_message(...)           # the actual token-based path
```

— so a bare `Credentials(token=access_token)` handed to the client, with
nothing passed to `generate_signed_url()` itself, raises `AttributeError` at
the first real chart. `service_account_email` is `exclude_args`-hidden and
injected alongside the other four; this project's default is `AGENT_SA_EMAIL`
(`app/config.py`), matching `agent-sa`'s real email
(`agent-sa@instacart-ml-model.iam.gserviceaccount.com`, confirmed live via
`gcloud iam service-accounts list`). A different caller's own token needs
their own matching email alongside it — the two travel as a pair.

**Where the five hidden values come from — mirrors `inject_dax_args`
(`.claude/rules/orchestrator.md`), now built as `inject_chart_args` in
`orchestrator.py`.** `dispatch_other_call` is where they get added before
dispatch, same as `resolve_chart_data` above. This project's own default:
`bucket_name` from `GCS_CHART_BUCKET`, `storage_backend="gcs"` (the only
branch `render_and_upload` implements), `expiration_hours` from a config
constant, `service_account_email` from `AGENT_SA_EMAIL`, and `access_token`
from `get_gcp_access_token()` — that refreshes this process's own ADC
credentials and reads `.token` off them. **Not the same shape as
`get_power_bi_token()`**, which does a real MSAL client-credentials flow
against a third party with no other option; this one just re-exports the
identity the process already runs as (`agent-sa` on Cloud Run), because GCS
is a native GCP service ADC already authenticates to. A different caller
supplying their own token-and-email pair here runs the upload under their
own GCS identity instead — same trust model as Power BI's RBAC already
gating `run_dax_query` (2026-09-27 reusability discussion).

### Two tool objects, one name — the bind-time schema substitution

**The MCP server only ever advertises `GenerateChartArgs`'s real schema —
`GenerateChartToolCallArgs` never reaches it.** `bind_tools()` is a
LangChain-level call, unrelated to MCP: it takes a plain Python list of tool
objects and serializes whatever `args_schema` each one carries into the LLM
request. For every other MCP tool, the object passed to `bind_tools()` is the
same one `client.get_tools()` produced from the server's own `tools/list`
answer, so there's nothing to reconcile. For `generate_chart` specifically,
the orchestrator swaps in a second, hand-built LangChain tool — same
`name="generate_chart"`, `args_schema=GenerateChartToolCallArgs` — used
*only* in the list passed to `bind_tools()`. The real, MCP-loaded tool object
(`args_schema=GenerateChartArgs`) is what `dispatch_tool` still calls
`.ainvoke()` on. The model is bound to exactly one schema at a time and never
sees the other; the two shapes are reconciled by `resolve_chart_data`, below,
before anything crosses the MCP wire.

**Resolution — `source_ref -> data` — happens in `orchestrator.py`'s
`dispatch_other_call`, not in the tool.** (The generic `dispatch_tool` shown
in `docs/approval-workflow.md` is the pre-approval-workflow sketch; the real
resolution lives inline in `call_tool_node`'s dispatch path today — that doc
flags the reconciliation as still pending.) No separate cache needed:
`state["tool_calls"]` already holds every prior call's native-Python
`result` this turn (`.claude/rules/orchestrator.md`).

```python
CHARTABLE_TOOLS = {"run_bigquery_sql", "run_dax_query"}

def _lookup_chart_source(source_ref: str, prior_tool_calls: list[ToolCallRecord]) -> ToolCallRecord:
    """Finds the successful, chartable tool call source_ref points at, or
    raises an actionable error if it can't be found."""
    source_tc = next(
        (r for r in prior_tool_calls
         if r.get("ref_id") == source_ref and r["success"] and r["name"] in CHARTABLE_TOOLS),
        None)
    if source_tc is None:
        raise ToolError(
            f"'{source_ref}' is not a successfully completed run_bigquery_sql or "
            "run_dax_query call from earlier this turn -- it may not exist, may have failed, "
            "or may be from later in this same batch and hasn't run yet. Fetch the data "
            "first, then call generate_chart in a follow-up step.")
    return source_tc

def resolve_chart_data(chart_tc: dict, prior_tool_calls: list[ToolCallRecord]) -> dict:
    """Rewrites a model-facing chart call into the real MCP tool's args shape.

    Scoped to THIS turn's tool_calls deliberately — charting a previous
    turn's result would render stale numbers under a fresh question. Matches
    against ref_id, never the raw tool-call id -- see "Batch ordering"
    (.claude/rules/orchestrator.md) for why.
    """
    call_args = GenerateChartToolCallArgs.model_validate(chart_tc["args"])
    source_tc = _lookup_chart_source(call_args.source_ref, prior_tool_calls)
    return {
        **chart_tc,
        "args": {"args": {"data": source_tc["result"], "spec": call_args.spec.model_dump()}},
    }
```

**The double-nested `{"args": {"args": {...}}}` shape is deliberate, not a
typo — it's the FastMCP nesting this doc already documents above** ("Resolved
(2026-09-27), not 'confirm at build time' anymore"): `generate_chart`'s real
signature takes `args: GenerateChartArgs` alongside its five injected
parameters as siblings, so FastMCP nests the data/spec payload under its own
`args` key rather than flattening it to the top level. The outer `args` is
the tool-call's own dispatch envelope (`{"args": {...}}`, matching every
other tool's call shape); the inner one is `generate_chart`'s own parameter
name.

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

def render_and_upload(fig: plt.Figure, bucket_name: str, storage_backend: str,
                       expiration_hours: float, access_token: str,
                       service_account_email: str) -> str:
    if storage_backend != "gcs":
        raise ToolError(
            f"Unsupported storage_backend '{storage_backend}'. Only 'gcs' is implemented.")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)                 # required — see below
    buf.seek(0)
    storage_client = storage.Client(credentials=Credentials(token=access_token))
    blob = storage_client.bucket(bucket_name).blob(f"charts/{uuid.uuid4()}.png")
    blob.upload_from_file(buf, content_type="image/png")
    return blob.generate_signed_url(
        version="v4",
        expiration=datetime.timedelta(hours=expiration_hours),
        method="GET",
        service_account_email=service_account_email,
        access_token=access_token,
    )
```

**Resolved (2026-09-27), not "confirm at build time" anymore.** Read
`generate_signed_url_v4`'s actual installed source rather than assuming:
signing with only a bearer token requires passing `access_token` **and**
`service_account_email` as explicit keyword arguments to
`generate_signed_url()` itself — the `Credentials(token=...)` object handed
to the client authenticates the *upload*, not the signing. Without both
kwargs, the function falls back to `credentials.sign_bytes(...)`, which a
bare token-backed `Credentials` object doesn't implement, and raises
`AttributeError` at the first real chart. This is why `service_account_email`
joined the injected-args set above.

**`storage_backend` exists to keep the door open, not because anything but
`"gcs"` is built.** Adding a real second backend later is a new branch plus
its own injected config, not a rearchitecture — deliberately not building
one now (2026-09-27 reusability discussion).

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
`local-dev-environment-setup.md` Step 13 item 6. **Specific to this
project's own default `access_token`/`service_account_email` pair**
(`agent-sa`'s own, via `get_gcp_access_token()` and `AGENT_SA_EMAIL`) — a
different caller supplying their own pair needs this same self-referential
grant on *their* identity, not `agent-sa`'s.

**Trade-off now baked in:** a chart link expires. That's fine for a chat
answer read in the same sitting; it means a saved/shared chart link goes dead
after the expiry window, which the anonymous-bucket approach wouldn't have
had. Acceptable here since nothing currently persists chart URLs beyond the
conversation. `expiration_hours` being a real parameter now (rather than a
hardcoded `1`) doesn't change this trade-off, only who gets to set it.

**`plt.close(fig)` after saving** — figures aren't garbage-collected on return
and leak memory across requests on a long-running instance. Won't show up in a
quick local test.

## Bounded, not unlimited

Chart types are a small, growable set of classes — not arbitrary code
execution. Adding one = one class, two methods, one union entry. If a request
needs a type not on the list, that's a real bounded limitation: say so rather
than improvising.
