"""Layer 1 tests for app/mcp_server/chart_tool.py -- pure rendering logic,
no MCP server, no live data (docs/testing.md).

Constructed the same way as notebooks/phase3_chart_specs.ipynb -- a raw
data/spec dict validated through GenerateChartArgs, the exact path
generate_chart itself uses -- but one test per chart type packs render,
dispatch, every guardrail, and every nuance into a single function rather
than spreading them across many. Not exhaustive by design.
"""
import pandas as pd
import pytest
from fastmcp.exceptions import ToolError
from matplotlib.figure import Figure
from pydantic import ValidationError

from app.mcp_server.chart_tool import (
    ChartSpecBase,
    GenerateChartArgs,
    BarChartSpec, HorizontalBarChartSpec, GroupedBarChartSpec,
    StackedHorizontalBarChartSpec, LineChartSpec, HistogramChartSpec,
    BoxChartSpec, HeatmapChartSpec, ConcentrationChartSpec, ParetoChartSpec,
    MAX_BAR_CATEGORIES, MAX_GROUPED_BAR_CATEGORIES, MAX_STACKED_BAR_CATEGORIES,
    MAX_STACKED_BAR_SEGMENTS, MAX_LINE_POINTS, MAX_LINE_HUE_GROUPS,
    MAX_HISTOGRAM_BUCKETS, MAX_BOX_CATEGORIES, MAX_HEATMAP_AXIS,
    MAX_HEATMAP_ANNOT_FONTSIZE, MAX_PARETO_LABELS, MAX_CATEGORY_LABEL_LENGTH,
    TITLE_FONTSIZE, AXIS_LABEL_FONTSIZE, TICK_LABEL_FONTSIZE, LEGEND_FONTSIZE,
)

LABELS = {"title": "T", "x_label": "X", "y_label": "Y"}


def validate_and_render(data: list[dict], spec: dict, expected_cls: type) -> Figure:
    """Validates through the same GenerateChartArgs the real tool uses (confirms
    the discriminator dispatched to the expected class), then renders."""
    args = GenerateChartArgs.model_validate({"data": data, "spec": spec})
    assert isinstance(args.spec, expected_cls)
    return args.spec.render(pd.DataFrame(args.data))


def test_clean_label_and_format_value():
    """Cross-cutting ChartSpecBase helpers, shared by every chart type."""
    class _Dummy(ChartSpecBase):
        def required_fields(self): return []
        def render(self, df): pass
    spec = _Dummy(**LABELS)

    assert spec._clean_label("[Model] Total Lift") == "Model Total Lift"
    assert spec._clean_label("[Driver]") == "Driver"
    # table[Column] / 'table name'[Column] are real DAX shapes, not bare
    # aliases -- stripping them would mangle the reference, not clean it.
    assert spec._clean_label("evaluation_metrics[Model]") == "evaluation_metrics[Model]"
    assert spec._clean_label("'evaluation_metrics'[Recall]") == "'evaluation_metrics'[Recall]"

    assert spec._format_value(5231, "auto") == "5,231"
    assert spec._format_value(50_000_000, "auto") == "50M"
    assert spec._format_value(1_234_567_890, "auto") == "1.2B"
    assert spec._format_value(0.5, "percent") == "50.0%"
    assert spec._format_value(500, "currency") == "$500.00"
    assert spec._format_value(50_000_000, "currency") == "$50M"


@pytest.mark.parametrize("cls,chart_type,x_field,y_field", [
    (BarChartSpec, "bar", "department", "orders"),
    (HorizontalBarChartSpec, "horizontal_bar", "orders", "department"),
])
def test_bar_and_horizontal_bar(cls, chart_type, x_field, y_field):
    data = [{"department": "produce", "orders": 5231}, {"department": "dairy", "orders": 4102}]
    spec = {"chart_type": chart_type, "x_field": x_field, "y_field": y_field, **LABELS}
    assert isinstance(validate_and_render(data, spec, cls), Figure)

    over_cap = [{"department": f"d{i}", "orders": i} for i in range(MAX_BAR_CATEGORIES + 1)]
    with pytest.raises(ToolError, match="Too many categories"):
        validate_and_render(over_cap, spec, cls)

    dupes = [{"department": "produce", "orders": 1}, {"department": "produce", "orders": 2}]
    with pytest.raises(ToolError, match="one row per"):
        validate_and_render(dupes, spec, cls)

    if chart_type == "horizontal_bar":
        # Only horizontal_bar/stacked_horizontal_bar check label length -- long
        # labels squeeze the plot area and break value-label placement.
        long_label = "x" * (MAX_CATEGORY_LABEL_LENGTH + 1)
        bad = [{"orders": 5, "department": long_label}, {"orders": 3, "department": "short"}]
        with pytest.raises(ToolError, match="exceed 40 characters"):
            validate_and_render(bad, spec, cls)


def test_grouped_bar():
    data = [{"model": "LightGBM", "recall": 0.367, "[Split]": "test"},
            {"model": "LightGBM", "recall": 0.412, "[Split]": "train"}]
    spec = {"chart_type": "grouped_bar", "x_field": "model", "y_field": "recall",
            "hue_field": "[Split]", **LABELS}
    fig = validate_and_render(data, spec, GroupedBarChartSpec)
    ax = fig.axes[0]
    legend = ax.get_legend()

    # Font hierarchy is shared by every chart type via _finish() -- checked
    # once here rather than repeated in every other test.
    assert legend.get_title().get_text() == "Split"  # bracket-cleaned
    assert ax.title.get_fontsize() == TITLE_FONTSIZE
    assert ax.xaxis.label.get_fontsize() == AXIS_LABEL_FONTSIZE
    assert ax.xaxis.get_ticklabels()[0].get_fontsize() == TICK_LABEL_FONTSIZE
    assert legend.get_texts()[0].get_fontsize() == LEGEND_FONTSIZE

    over_cap = [{"model": f"m{i}", "recall": 0.1, "split": "test"}
                for i in range(MAX_GROUPED_BAR_CATEGORIES + 1)]
    with pytest.raises(ToolError, match="Too many x-axis categories"):
        validate_and_render(over_cap, {**spec, "hue_field": "split"}, GroupedBarChartSpec)


def test_stacked_horizontal_bar():
    data = [{"department": "produce", "[Driver]": "reordered", "orders": 3910},
            {"department": "produce", "[Driver]": "first-time", "orders": 1321}]
    spec = {"chart_type": "stacked_horizontal_bar", "category_field": "department",
            "value_field": "orders", "segment_field": "[Driver]", **LABELS}
    fig = validate_and_render(data, spec, StackedHorizontalBarChartSpec)
    ax = fig.axes[0]

    # Each segment's left edge is the running total of prior segments.
    offsets = [(p.get_x(), p.get_width()) for c in ax.containers for p in c.patches]
    assert offsets == [(0.0, 1321.0), (1321.0, 3910.0)]
    assert ax.get_legend().get_title().get_text() == "Driver"  # bracket-cleaned

    # A segment narrower than 10% of the axis range gets no center label --
    # it would overlap adjacent segments in a bar this narrow.
    narrow = [{"department": "produce", "[Driver]": "big", "orders": 950},
              {"department": "produce", "[Driver]": "small", "orders": 50}]
    fig2 = validate_and_render(narrow, spec, StackedHorizontalBarChartSpec)
    assert [t.get_text() for t in fig2.axes[0].texts] == ["950", ""]

    too_many_categories = [{"department": f"d{i}", "[Driver]": "a", "orders": 1}
                            for i in range(MAX_STACKED_BAR_CATEGORIES + 1)]
    with pytest.raises(ToolError, match="Too many categories"):
        validate_and_render(too_many_categories, spec, StackedHorizontalBarChartSpec)

    too_many_segments = [{"department": "produce", "[Driver]": f"s{i}", "orders": 1}
                         for i in range(MAX_STACKED_BAR_SEGMENTS + 1)]
    with pytest.raises(ToolError, match="Too many segments"):
        validate_and_render(too_many_segments, spec, StackedHorizontalBarChartSpec)

    long_label = "x" * (MAX_CATEGORY_LABEL_LENGTH + 1)
    bad_label = [{"department": long_label, "[Driver]": "a", "orders": 1},
                 {"department": "short", "[Driver]": "a", "orders": 1}]
    with pytest.raises(ToolError, match="exceed 40 characters"):
        validate_and_render(bad_label, spec, StackedHorizontalBarChartSpec)


def test_line():
    # Unsorted input must not draw a zigzag -- render() sorts by x_field.
    data = [{"order_dow": 1, "orders": 5788, "[Split]": "test"},
            {"order_dow": 0, "orders": 6209, "[Split]": "test"}]
    spec = {"chart_type": "line", "x_field": "order_dow", "y_field": "orders",
            "hue_field": "[Split]", **LABELS}
    fig = validate_and_render(data, spec, LineChartSpec)
    assert list(fig.axes[0].lines[0].get_xdata()) == [0, 1]
    assert fig.axes[0].get_legend().get_title().get_text() == "Split"  # bracket-cleaned

    over_points = [{"order_dow": i, "orders": i, "[Split]": "test"} for i in range(MAX_LINE_POINTS + 1)]
    with pytest.raises(ToolError, match="Too many x-axis points"):
        validate_and_render(over_points, spec, LineChartSpec)

    over_hue = [{"order_dow": 0, "orders": i, "[Split]": f"s{i}"} for i in range(MAX_LINE_HUE_GROUPS + 1)]
    with pytest.raises(ToolError, match="Too many hue groups"):
        validate_and_render(over_hue, spec, LineChartSpec)


def test_histogram():
    # Bars are sized from the real gap between bucket edges, not a fixed width.
    data = [{"bucket": 7, "n": 41203}, {"bucket": 14, "n": 38102}]
    spec = {"chart_type": "histogram", "bucket_field": "bucket", "count_field": "n", **LABELS}
    fig = validate_and_render(data, spec, HistogramChartSpec)
    assert [p.get_width() for p in fig.axes[0].patches] == pytest.approx([6.3, 6.3])

    over_cap = [{"bucket": i, "n": 1} for i in range(MAX_HISTOGRAM_BUCKETS + 1)]
    with pytest.raises(ToolError, match="Too many buckets"):
        validate_and_render(over_cap, spec, HistogramChartSpec)

    dupes = [{"bucket": 7, "n": 1}, {"bucket": 7, "n": 2}]
    with pytest.raises(ToolError, match="one row per bucket"):
        validate_and_render(dupes, spec, HistogramChartSpec)


def test_box():
    data = [{"department": "produce", "q1": 0.40, "med": 0.61, "q3": 0.72,
             "whislo": 0.11, "whishi": 0.95}]
    spec = {"chart_type": "box", "category_field": "department", "q1_field": "q1",
            "med_field": "med", "q3_field": "q3", "whislo_field": "whislo",
            "whishi_field": "whishi", **LABELS}
    assert isinstance(validate_and_render(data, spec, BoxChartSpec), Figure)

    over_cap = [{"department": f"d{i}", "q1": 0.1, "med": 0.2, "q3": 0.3,
                 "whislo": 0.0, "whishi": 0.4} for i in range(MAX_BOX_CATEGORIES + 1)]
    with pytest.raises(ToolError, match="Too many categories"):
        validate_and_render(over_cap, spec, BoxChartSpec)


def test_heatmap():
    # A duplicate (x, y) pair means the query returned the wrong grain --
    # that must fail, not silently pick one value.
    dupes = [{"order_dow": 0, "order_hour": 10, "orders": 812},
             {"order_dow": 0, "order_hour": 10, "orders": 999}]
    spec = {"chart_type": "heatmap", "x_field": "order_hour", "y_field": "order_dow",
            "value_field": "orders", **LABELS}
    with pytest.raises(ToolError):
        validate_and_render(dupes, spec, HeatmapChartSpec)

    over_cap = [{"order_dow": i, "order_hour": 0, "orders": 1} for i in range(MAX_HEATMAP_AXIS + 1)]
    with pytest.raises(ToolError, match="Too many y-axis categories"):
        validate_and_render(over_cap, spec, HeatmapChartSpec)

    small_grid = [{"order_hour": x, "order_dow": y, "orders": x + y} for x in range(2) for y in range(2)]
    fig = validate_and_render(small_grid, spec, HeatmapChartSpec)
    assert len(fig.axes[0].texts) == 4
    assert fig.axes[0].texts[0].get_fontsize() <= MAX_HEATMAP_ANNOT_FONTSIZE

    # Below MIN_HEATMAP_ANNOT_FONTSIZE, annotations are dropped entirely --
    # color + colorbar carry the value instead of illegible overlapping text.
    dense_grid = [{"order_hour": x, "order_dow": y, "orders": x + y}
                  for x in range(MAX_HEATMAP_AXIS) for y in range(MAX_HEATMAP_AXIS)]
    fig2 = validate_and_render(dense_grid, spec, HeatmapChartSpec)
    assert len(fig2.axes[0].texts) == 0


def test_concentration():
    """20 rows (the required grain), front-loaded cumulative curve."""
    pct = list(range(5, 101, 5))
    cum = [30, 50, 62, 70, 76, 80, 83, 86, 88, 90,
           91, 92, 93, 94, 95, 96, 97, 98, 99, 100]
    data = [{"pct": p, "cum_pct": c} for p, c in zip(pct, cum)]

    spec = {"chart_type": "concentration", "percentile_field": "pct",
            "cumulative_pct_field": "cum_pct", **LABELS}
    with pytest.raises(ToolError, match="exactly 20 rows"):
        validate_and_render(data[:10], spec, ConcentrationChartSpec)

    # 66% falls exactly halfway between (15, 62) and (20, 70) -> rank_pct 17.5,
    # via true linear interpolation, not the nearest row.
    cum_mode = {**spec, "highlight_at_cum_pct": 66}
    fig = validate_and_render(data, cum_mode, ConcentrationChartSpec)
    assert fig.axes[1].texts[-1].xy == pytest.approx((17.5, 66.0))

    # Fixing rank_pct=17.5 instead should recover cum_pct=66 on the same curve.
    rank_mode = {**spec, "highlight_at_rank_pct": 17.5}
    fig2 = validate_and_render(data, rank_mode, ConcentrationChartSpec)
    assert fig2.axes[1].texts[-1].xy == pytest.approx((17.5, 66.0))

    both_set = {**spec, "highlight_at_cum_pct": 80, "highlight_at_rank_pct": 20}
    with pytest.raises(ValidationError, match="only one of"):
        GenerateChartArgs.model_validate({"data": data, "spec": both_set})


def test_pareto():
    # Regression case: the labeled cumulative % at each position must match
    # the CORRECTLY sorted order, not the original input order.
    data = [{"department": "A", "orders": 10}, {"department": "B", "orders": 70},
            {"department": "C", "orders": 20}]
    spec = {"chart_type": "pareto", "category_field": "department", "value_field": "orders", **LABELS}
    fig = validate_and_render(data, spec, ParetoChartSpec)
    ax1, ax2 = fig.axes[0], fig.axes[1]
    # Sorted descending: B (70, 70%), C (20, 90%), A (10, 100%).
    assert [t.get_text() for t in ax1.get_xticklabels()] == ["B", "C", "A"]
    assert [t.get_text() for t in ax2.texts] == ["70%", "90%", "100%"]

    many_rows = [{"department": f"a{i}", "orders": 1000 - i} for i in range(134)]
    fig2 = validate_and_render(many_rows, spec, ParetoChartSpec)
    assert len(fig2.axes[1].texts) == MAX_PARETO_LABELS

    over_cap = [{"department": f"a{i}", "orders": i} for i in range(MAX_BAR_CATEGORIES + 1)]
    with pytest.raises(ToolError, match="Too many categories"):
        validate_and_render(over_cap, spec, ParetoChartSpec)
