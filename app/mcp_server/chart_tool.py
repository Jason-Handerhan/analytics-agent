import datetime
import io
import re
import uuid
from abc import ABC, abstractmethod
from typing import Annotated, Literal, Union

import matplotlib
matplotlib.use("Agg")          # non-interactive backend
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
import seaborn as sns
import pandas as pd
import numpy as np
from fastmcp.exceptions import ToolError
from google.cloud import storage
from google.oauth2.credentials import Credentials
from pydantic import BaseModel, Field, model_validator

from app.mcp_server.server import mcp

STYLE      = "whitegrid"     # seaborn style
CONTEXT    = "talk"          # seaborn font-size context
PALETTE    = "deep"          # seaborn color palette
FIGSIZE    = (9.0, 5.5)      # inches
DPI        = 150
GRID_ALPHA = 0.3             # gridline opacity

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
HEATMAP_FONT_FACTOR        = 18   # scales cell size (inches) to annotation fontsize (points)
MAX_HEATMAP_ANNOT_FONTSIZE = 14   # cap so a sparse grid doesn't get oversized numbers
MIN_HEATMAP_ANNOT_FONTSIZE = 5    # below this, drop annotations -- color + colorbar carry the value

MAX_CATEGORY_LABEL_LENGTH = 40   # longer labels crowd the plot area and break
                                  # segment/value-label placement -- horizontal_bar,
                                  # stacked_horizontal_bar only

# Title > axis labels > ticks/legend -- applied uniformly to every chart type in _finish().
TITLE_FONTSIZE       = 16
AXIS_LABEL_FONTSIZE  = 14
TICK_LABEL_FONTSIZE  = 10
LEGEND_FONTSIZE      = 10

# Bar/segment value labels (_annotate_values, _annotate_segments) -- drawn
# mid-render, not part of _finish()'s chrome hierarchy above. Heatmap's cell
# annotations are separate and already dynamically sized, not this constant.
VALUE_ANNOTATION_FONTSIZE = 8

# Matches a standalone "[Alias]" token, not a qualified "table[Column]" reference.
# Used to strip brackets from chart labeling
BRACKET_ALIAS_RE = re.compile(r"(?<![\w'])\[([^\[\]]+)\]")


class ChartSpecBase(BaseModel, ABC):
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

    def _clean_label(self, text: str) -> str:
        """Strips any standalone "[Alias]" bracket wrapper out of display
        text (a title, axis label, or legend title)"""
        return BRACKET_ALIAS_RE.sub(r"\1", text)

    def _style_legend(self, ax: plt.Axes) -> None:
        legend = ax.get_legend()
        legend.set_title(self._clean_label(legend.get_title().get_text()),
                          prop={"size": LEGEND_FONTSIZE})
        for text in legend.get_texts():
            text.set_fontsize(LEGEND_FONTSIZE)

    def _finish(self, fig: plt.Figure, ax: plt.Axes) -> plt.Figure:
        ax.set_title(self._clean_label(self.title), pad=12, fontsize=TITLE_FONTSIZE)
        ax.set_xlabel(self._clean_label(self.x_label), fontsize=AXIS_LABEL_FONTSIZE)
        ax.set_ylabel(self._clean_label(self.y_label), fontsize=AXIS_LABEL_FONTSIZE)
        ax.tick_params(axis="both", labelsize=TICK_LABEL_FONTSIZE)
        if ax.get_legend():
            self._style_legend(ax)
        ax.grid(True, alpha=GRID_ALPHA)
        sns.despine(ax=ax)

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
                        padding=2, fontsize=VALUE_ANNOTATION_FONTSIZE)

    def _format_value(self, value: float, value_format: str) -> str:
        """Formats one value for an axis tick or bar label. 'auto' picks based
        on magnitude"""
        if value_format == "percent":
            return f"{value:.1%}"
        prefix = "$" if value_format == "currency" else ""
        abs_value = abs(value)
        for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M")):
            if abs_value >= threshold:
                text = f"{value / threshold:.1f}"
                return f"{prefix}{text[:-2] if text.endswith('.0') else text}{suffix}"
        if abs_value >= 1000:
            return f"{prefix}{value:,.0f}"
        if value_format == "currency":
            return f"{prefix}{value:,.2f}"
        return f"{value:,.4g}"

    def _apply_value_format(self, ax: plt.Axes, axis: Literal["x", "y"]) -> None:
        """Formats an axis's tick labels using self.value_format, defaulting
        to 'auto' for a class that has no such field."""
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

    def _check_max_label_length(self, categories: pd.Series, cat_field_name: str) -> None:
        too_long = [v for v in categories.unique() if len(str(v)) > MAX_CATEGORY_LABEL_LENGTH]
        if too_long:
            raise ToolError(
                f"Category values in '{cat_field_name}' exceed {MAX_CATEGORY_LABEL_LENGTH} "
                f"characters (e.g. {too_long[0]!r}). Alias to a shorter label in the query.")


class FormattedValueSpec(BaseModel):
    """Mixin for chart types with a value axis that isn't self-evidently a
    plain number -- adds the field _format_value/_apply_value_format need."""
    value_format: Literal["auto", "percent", "currency"] = Field(
        default="auto",
        description="How to format axis labels and value annotations. 'auto' "
                    "picks based on magnitude; set 'percent' or 'currency' when "
                    "the value isn't a plain number.")


class BarChartSpec(ChartSpecBase, FormattedValueSpec):
    """One bar per category. Fields: x_field (cat), y_field (num).

    Expects one row per category, already aggregated:
        [{"department": "produce", "orders": 5231},
         {"department": "dairy",   "orders": 4102}]

    Keep categories readable: departments (~21) or aisles (~134), not products
    (~50k). An un-aggregated query hits the 1,000-row cap and fails.
    """
    chart_type: Literal["bar"] = Field(description='Always "bar".')
    x_field: str = Field(description="Column with category names.")
    y_field: str = Field(description="Column with the numeric value per category.")

    def required_fields(self) -> list[str]:
        return [self.x_field, self.y_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        self._check_no_duplicate_grain(df, [self.x_field])
        self._check_max_categories(df[self.x_field].nunique(), MAX_BAR_CATEGORIES, "categories")
        fig, ax = self._setup()
        sns.barplot(data=df, x=self.x_field, y=self.y_field, ax=ax)
        self._annotate_values(ax)
        self._apply_value_format(ax, "y")
        return self._finish(fig, ax)


class HorizontalBarChartSpec(ChartSpecBase, FormattedValueSpec):
    """Bar with axes swapped — for long names or many categories.
    Fields: x_field (num), y_field (cat).

        [{"orders": 5231, "aisle": "fresh vegetables"},
         {"orders": 4102, "aisle": "packaged cheese"}]
    """
    chart_type: Literal["horizontal_bar"] = Field(description='Always "horizontal_bar".')
    x_field: str = Field(description="Column with the numeric value.")
    y_field: str = Field(description="Column with category names.")

    def required_fields(self) -> list[str]:
        return [self.x_field, self.y_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        self._check_no_duplicate_grain(df, [self.y_field])
        self._check_max_categories(df[self.y_field].nunique(), MAX_BAR_CATEGORIES, "categories")
        self._check_max_label_length(df[self.y_field], self.y_field)
        fig, ax = self._setup()
        sns.barplot(data=df, x=self.x_field, y=self.y_field, ax=ax)
        self._annotate_values(ax)
        self._apply_value_format(ax, "x")
        return self._finish(fig, ax)


class GroupedBarChartSpec(ChartSpecBase, FormattedValueSpec):
    """A metric across two dimensions — Recall@5 by model AND split.
    Fields: x_field (cat), y_field (num), hue_field (cat).

        [{"model": "LightGBM", "recall": 0.367, "split": "test"},
         {"model": "LightGBM", "recall": 0.412, "split": "train"}]
    """
    chart_type: Literal["grouped_bar"] = Field(description='Always "grouped_bar".')
    x_field: str = Field(description="Column with category names.")
    y_field: str = Field(description="Column with the numeric value.")
    hue_field: str = Field(description="Column with the second dimension to group bars by.")

    def required_fields(self) -> list[str]:
        return [self.x_field, self.y_field, self.hue_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        self._check_no_duplicate_grain(df, [self.x_field, self.hue_field])
        self._check_max_categories(df[self.x_field].nunique(), MAX_GROUPED_BAR_CATEGORIES,
                                    "x-axis categories")
        fig, ax = self._setup()
        sns.barplot(data=df, x=self.x_field, y=self.y_field,
                    hue=self.hue_field, ax=ax)
        self._annotate_values(ax)
        self._apply_value_format(ax, "y")
        return self._finish(fig, ax)


class StackedHorizontalBarChartSpec(ChartSpecBase, FormattedValueSpec):
    """Composition within each category — segments summing to a whole bar.
    Fields: category_field (cat), value_field (num), segment_field (cat).

    Long form, one row per (category, segment) pair — NOT pre-pivoted:
        [{"department": "produce", "segment": "reordered", "orders": 3910},
         {"department": "produce", "segment": "first-time", "orders": 1321}]

    Different question from `grouped_bar`: that compares a metric ACROSS a
    second dimension (bars side by side); this shows what each category is
    MADE OF (segments summing to one bar).

    Keep segments few — 3-5 reads well, 10 is a muddle. High-cardinality
    segments belong in a different chart entirely.
    """
    chart_type: Literal["stacked_horizontal_bar"] = Field(description='Always "stacked_horizontal_bar".')
    category_field: str = Field(description="Column with each bar's category.")
    value_field: str = Field(description="Column with each segment's numeric value.")
    segment_field: str = Field(description="Column with the segment name within each category.")

    def required_fields(self) -> list[str]:
        return [self.category_field, self.value_field, self.segment_field]

    def _annotate_segments(self, ax: plt.Axes, containers: list) -> None:
        """Labels each segment at its center, in white -- skipped for any
        segment narrower than 10% of the AXIS range"""
        axis_min, axis_max = ax.get_xlim()
        axis_range = axis_max - axis_min
        value_format = getattr(self, "value_format", "auto")
        for container in containers:
            ax.bar_label(
                container, label_type="center", color="white", fontsize=VALUE_ANNOTATION_FONTSIZE,
                fmt=lambda v: self._format_value(v, value_format) if v / axis_range >= 0.10 else "",
            )

    def render(self, df: pd.DataFrame) -> plt.Figure:
        self._check_no_duplicate_grain(df, [self.category_field, self.segment_field])
        self._check_max_categories(df[self.category_field].nunique(),
                                    MAX_STACKED_BAR_CATEGORIES, "categories")
        self._check_max_categories(df[self.segment_field].nunique(),
                                    MAX_STACKED_BAR_SEGMENTS, "segments")
        self._check_max_label_length(df[self.category_field], self.category_field)
        # Pivot to wide, then stack bars using a running left offset.
        grid = df.pivot(index=self.category_field, columns=self.segment_field,
                        values=self.value_field).fillna(0)
        fig, ax = self._setup()
        left = pd.Series(0.0, index=grid.index)
        containers = []
        for segment in grid.columns:
            containers.append(ax.barh(grid.index, grid[segment], left=left, label=str(segment)))
            left += grid[segment]
        ax.legend(title=self.segment_field, bbox_to_anchor=(1.02, 1),
                  loc="upper left")     # legend outside the plot area -- _finish() cleans/sizes it
        self._apply_value_format(ax, "x")
        self._annotate_segments(ax, containers)
        return self._finish(fig, ax)


class LineChartSpec(ChartSpecBase, FormattedValueSpec):
    """Trends over an ordinal axis.
    Fields: x_field (ordinal), y_field (num), hue_field (optional).

    One row per x value, sorted — the renderer sorts, but gaps stay gaps:
        [{"order_dow": 0, "orders": 6209},
         {"order_dow": 1, "orders": 5788}]
    """
    chart_type: Literal["line"] = Field(description='Always "line".')
    x_field: str = Field(description="Column with the ordinal x-axis value.")
    y_field: str = Field(description="Column with the numeric value.")
    hue_field: str | None = Field(default=None,
        description="Optional column to draw a separate line per value.")

    def required_fields(self) -> list[str]:
        return [f for f in (self.x_field, self.y_field, self.hue_field) if f]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        grain_cols = [self.x_field] if self.hue_field is None else [self.x_field, self.hue_field]
        self._check_no_duplicate_grain(df, grain_cols)
        self._check_max_categories(df[self.x_field].nunique(), MAX_LINE_POINTS, "x-axis points")
        if self.hue_field is not None:
            self._check_max_categories(df[self.hue_field].nunique(), MAX_LINE_HUE_GROUPS, "hue groups")
        s = df.sort_values(self.x_field)
        fig, ax = self._setup()
        sns.lineplot(data=s, x=self.x_field, y=self.y_field,
                     hue=self.hue_field, marker="o", ax=ax)
        self._apply_value_format(ax, "y")
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
    chart_type: Literal["histogram"] = Field(description='Always "histogram".')
    bucket_field: str = Field(description="Column with each bucket's lower edge.")
    count_field: str = Field(description="Column with the row count in that bucket.")

    def required_fields(self) -> list[str]:
        return [self.bucket_field, self.count_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        self._check_no_duplicate_grain(df, [self.bucket_field])
        self._check_max_categories(df[self.bucket_field].nunique(), MAX_HISTOGRAM_BUCKETS, "buckets")
        # Bars drawn from pre-binned data, not sns.histplot.
        s = df.sort_values(self.bucket_field)
        fig, ax = self._setup()
        width = (s[self.bucket_field].diff().dropna().min()
                 if len(s) > 1 else 1)
        ax.bar(s[self.bucket_field], s[self.count_field],
               width=width * 0.9, align="edge")
        self._apply_value_format(ax, "y")
        return self._finish(fig, ax)


class BoxChartSpec(ChartSpecBase, FormattedValueSpec):
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
    chart_type: Literal["box"] = Field(description='Always "box".')
    category_field: str = Field(description="Column with each box's category.")
    q1_field: str = Field(description="Column with the first quartile.")
    med_field: str = Field(description="Column with the median.")
    q3_field: str = Field(description="Column with the third quartile.")
    whislo_field: str = Field(description="Column with the lower whisker value.")
    whishi_field: str = Field(description="Column with the upper whisker value.")

    def required_fields(self) -> list[str]:
        return [self.category_field, self.q1_field, self.med_field,
                self.q3_field, self.whislo_field, self.whishi_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        self._check_no_duplicate_grain(df, [self.category_field])
        self._check_max_categories(df[self.category_field].nunique(), MAX_BOX_CATEGORIES, "categories")
        # ax.bxp's required stat keys, one dict per box.
        stats = [{"label": r[self.category_field],
                  "q1":     r[self.q1_field],
                  "med":    r[self.med_field],
                  "q3":     r[self.q3_field],
                  "whislo": r[self.whislo_field],
                  "whishi": r[self.whishi_field],
                  "fliers": []}
                 for _, r in df.iterrows()]
        fig, ax = self._setup()
        ax.bxp(stats, showfliers=False)
        self._apply_value_format(ax, "y")
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
    chart_type: Literal["heatmap"] = Field(description='Always "heatmap".')
    x_field: str = Field(description="Column with the x-axis category.")
    y_field: str = Field(description="Column with the y-axis category.")
    value_field: str = Field(description="Column with the numeric value for each cell.")

    def required_fields(self) -> list[str]:
        return [self.x_field, self.y_field, self.value_field]

    def render(self, df: pd.DataFrame) -> plt.Figure:
        self._check_no_duplicate_grain(df, [self.x_field, self.y_field])
        self._check_max_categories(df[self.x_field].nunique(), MAX_HEATMAP_AXIS, "x-axis categories")
        self._check_max_categories(df[self.y_field].nunique(), MAX_HEATMAP_AXIS, "y-axis categories")
        grid = df.pivot(index=self.y_field, columns=self.x_field,
                        values=self.value_field)
        fig, ax = self._setup()
        cell_width = FIGSIZE[0] / grid.shape[1]
        cell_height = FIGSIZE[1] / grid.shape[0]
        fontsize = min(min(cell_width, cell_height) * HEATMAP_FONT_FACTOR, MAX_HEATMAP_ANNOT_FONTSIZE)
        annot = fontsize >= MIN_HEATMAP_ANNOT_FONTSIZE
        sns.heatmap(grid, annot=annot, fmt=".4g", cmap="Blues", ax=ax,
                    annot_kws={"fontsize": fontsize} if annot else None)
        return self._finish(fig, ax)


class ConcentrationChartSpec(ChartSpecBase):
    """Cumulative share vs. rank percentile, with each bucket's own share as
    bars — "do 20% of X drive 80% of Y?"
    Fields: percentile_field, cumulative_pct_field, one of
    highlight_at_cum_pct/highlight_at_rank_pct.

    y_label describes the BARS (each bucket's own share) -- the cumulative
    line's own axis is conventionally unlabeled, matching pareto.

        [{"pct": 5, "cum_pct": 12.4},
         {"pct": 10, "cum_pct": 19.8}]

    Exactly 20 rows, one per 5% bucket, with the cumulative math done IN SQL —
    computing it here would make percentages relative to whatever survived the
    row cap, putting the crossing in the wrong place while the chart still
    looks fine:
        SELECT NTILE(20) OVER (ORDER BY orders DESC) AS pct,
               SUM(SUM(orders)) OVER (ORDER BY NTILE(...)) * 100.0
                 / SUM(SUM(orders)) OVER () AS cum_pct
        FROM ... GROUP BY product_id
    """

    chart_type: Literal["concentration"] = Field(description='Always "concentration".')
    percentile_field: str = Field(description="Column with the rank percentile bucket, 1-20 (one per 5%).")
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
                f"(ORDER BY <value> DESC) AS {self.percentile_field} to produce the right grain.")
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
        ax2 = ax1.twinx()
        ax2.plot(s[self.category_field], cum_pct,
                 color=self.cumulative_color, marker="o")
        ax2.set_ylabel(CUMULATIVE_AXIS_LABEL)
        ax2.grid(False)

        n = len(s)
        label_indices = (range(n) if n <= MAX_PARETO_LABELS
                         else np.linspace(0, n - 1, MAX_PARETO_LABELS, dtype=int))
        for i in label_indices:
            ax2.annotate(f"{cum_pct.iloc[i]:.0f}%", xy=(i, cum_pct.iloc[i]),
                        xytext=(0, 8), textcoords="offset points",
                        ha="center", fontsize=8)
        return self._finish(fig, ax1)


class GenerateChartArgs(BaseModel):
    data: list[dict]
    spec: Annotated[
        Union[BarChartSpec, HorizontalBarChartSpec, GroupedBarChartSpec,
              StackedHorizontalBarChartSpec, LineChartSpec, HistogramChartSpec,
              BoxChartSpec, HeatmapChartSpec, ConcentrationChartSpec, ParetoChartSpec],
        Field(discriminator="chart_type",
              description="Which chart type to render, with that type's own fields."),
    ]


def render_and_upload(fig: plt.Figure, bucket_name: str, storage_backend: str,
                       expiration_hours: float, access_token: str,
                       service_account_email: str) -> str:
    if storage_backend != "gcs":
        raise ToolError(
            f"Unsupported storage_backend '{storage_backend}'. Only 'gcs' is implemented.")
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=DPI, bbox_inches="tight")
    plt.close(fig)                 
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
    fig = args.spec.render(df)
    return ChartResult(chart_url=render_and_upload(
        fig, bucket_name, storage_backend, expiration_hours,
        access_token, service_account_email))
