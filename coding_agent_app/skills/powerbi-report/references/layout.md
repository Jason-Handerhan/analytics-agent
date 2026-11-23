# Layout Composition

## Contents

- [Core Principles](#core-principles)
- [Reading Patterns](#reading-patterns)
- [Grid System](#grid-system)
  - [8-Point Base Grid](#8-point-base-grid)
  - [12-Column Grid](#12-column-grid)
  - [Concrete Arithmetic (FHD 1920 x 1080 canvas)](#concrete-arithmetic-fhd-1920-x-1080-canvas)
- [Slicer Placement Rules](#slicer-placement-rules)
  - [1–3 slicers — inline with title](#13-slicers--inline-with-title)
  - [4+ slicers — vertical filter rail](#4-slicers--vertical-filter-rail)
  - [Bounds and overlap](#bounds-and-overlap)
- [Visual Hierarchy Levers](#visual-hierarchy-levers)
- [Whitespace Tiers](#whitespace-tiers)
- [Power BI Grounding](#power-bi-grounding)


## Core Principles

1. **Top-left carries the heaviest message** — Readers start there; place the descriptive title, primary KPI callout, or hero chart there.
2. **Align to a grid** — Every edge snaps to an 8px grid; misalignment signals carelessness.
3. **Whitespace is structure** — Gaps separate groups more effectively than borders or boxes.
4. **Size + contrast + position = hierarchy** — Largest, highest-contrast element in the best position gets attention first.
5. **Group by proximity, not by boxes** — Elements 8–16px apart read as related; ≥32px apart read as distinct (Gestalt).
6. **7 ± 2 visual groups per page** — More overwhelms working memory; fewer may under-inform.
7. **Repetition creates rhythm** — Consistent sizing, spacing, and placement across pages builds trust and scannability.

---

## Reading Patterns

| Pattern | Shape | When to Use | Archetype Fit |
|---|---|---|---|
| **F-pattern** | Left-anchored, top-heavy scanning | Dense analytical pages, tables, filter rails | Analytical, Operational |
| **Z-pattern** | Top-left → top-right → bottom-left → bottom-right | Sparse executive summaries, narrative pages | Executive, Narrative |

**F-pattern** works when the left column holds navigation (slicers, filter rail) and the eye scans rightward into content.
**Z-pattern** works when the page has a clear headline, a hero visual, and a small number of supporting elements.

---

## Grid System

### 8-Point Base Grid

All spacing, sizing, and positioning use multiples of 8px.

| Value | Pixels | Use |
|---|---|---|
| 1 unit | 8 | Minimum internal padding |
| 2 units | 16 | Tight spacing between visuals in a group |
| 3 units | 24 | Standard spacing between groups, page margins |
| 4 units | 32 | Section breaks, major separations |
| 6 units | 48 | Banner height, header strip |

### 12-Column Grid

Greenfield reports default to FHD (`1920 x 1080`) unless the user asks for a
different size. Brownfield redesigns preserve the existing canvas size unless a
resize is approved. A new page in an existing report is designed as greenfield but takes the report's canvas size.

Use a 12-track grid inside the page margins. On FHD, subtract 32px margins on
each side (`1856px` usable), then use 24px gutters between tracks. This leaves
`1592px` for 12 equal tracks, or ~132.7px per track. A visual spanning N tracks
is `(N * 132.7) + ((N - 1) * 24)`, then rounded to the 8px grid during
placement. On existing 1280px canvases, use 24px side margins and compact
gutters (usually 16px), or express placements as ratios instead of copying FHD
pixel widths. For multi-span rows, add one gutter between adjacent visuals; for
example, an 8 + 4 split is ~1229px + 24px gutter + ~603px.

| Layout | Columns | Span Width (before final 8px rounding) | Use |
|---|---|---|---|
| Full width | 12 | ~1856 FHD / ~1232 720p | Hero chart, full-width table |
| Two-thirds + one-third | 8 + 4 | ~1229 + ~603 FHD | Primary chart + slicer panel |
| Half + half | 6 + 6 | ~916 + ~916 FHD | Side-by-side comparison |
| Asymmetric 7-5 | 7 + 5 | ~1072 + ~760 FHD | Narrative: hero chart + annotation |
| Asymmetric 8-4 | 8 + 4 | ~1229 + ~603 FHD | Analytical: chart + detail panel |
| Thirds | 4 + 4 + 4 | ~603 each FHD | KPI cards row, small multiples |
| Quarters | 3 + 3 + 3 + 3 | ~446 each FHD | 4-up status tiles |

### Concrete Arithmetic (FHD 1920 x 1080 canvas)

```text
Canvas:       1920 x 1080
Margins:      32px each side -> usable 1856 x 1016
Header:       72px tall -> content area 1856 x 944
Footer:       optional; do not reserve a footer band unless it has content
```

---

## Slicer Placement Rules

Choose slicer placement by count — fewer slicers go inline with the
title to preserve full chart width.

| Slicer count | Placement | Width impact |
|---|---|---|
| **1–3** | Inline with title row | Zero — cards and charts span full content width |
| **4+** | Vertical filter rail, usually 2 grid columns | F-pattern; slicers fill the column |

**Key rule**: a vertical rail is only justified when slicers fill ≥50%
of the column height. A short list of three dropdown slicers usually leaves too
much dead space in a full-height rail, so keep them inline with the title.

### 1–3 slicers — inline with title

Place slicers at the right end of the title row. Each slicer gets about 12% of
page width (about 230px on FHD, 160px on 1280px pages; use about 10% for short
labels like "Year"), h=64 on FHD or h=56 on 1280px pages, spaced 16-24px apart.
The title textbox fills the remaining width on the left.
Reserve the entire title/slicer band before placing any chart/card/table below
it; the reserved band still belongs to the title first, with slicers aligned to
the right. If a slicer must be taller than the standard header-band height,
increase the reserved band; do not let the next row start under the slicer.
Z-order cannot fix a bad layout — it only decides which overlapping visual hides
the other.

```text
┌──────────────────────────────┬──────────┬──────────┬──────────┐
│  TITLE                       │ Slicer 1 │ Slicer 2 │ Slicer 3 │
├──────────┬──────────┬────────┴──┬───────┴──────────┴──────────┤
│ Card 1   │ Card 2   │ Card 3   │ Card 4                      │  full width
└──────────┴──────────┴──────────┴──────────────────────────────┘
```

### 4+ slicers — vertical filter rail

The canonical F-pattern layout. Rail is usually 2 grid columns wide: about
310px on FHD and about 206px on 1280px brownfield pages. Content area occupies
the remaining width.

### Bounds and overlap

- No visual bounding boxes may overlap unless the overlap is deliberate and
  non-data-bearing (for example, a background shape behind a group).
- Slicers are interactive controls, so they must be fully visible in screenshots;
  their lower portions must not sit behind charts/tables.
- Compute each visual's `right = x + width` and `bottom = y + height`; sibling
  visuals in the same layer must have either a horizontal or vertical gap.
- Keep at least 16px between grouped visuals and 24px between major sections.

---

## Visual Hierarchy Levers

| Lever | How to Apply | Rule |
|---|---|---|
| **Size** | Hero visual is largest only when the page has a true hero question; otherwise balance halves/thirds | Size should reflect analytical priority, not default habit |
| **Contrast** | Dark/saturated on light canvas | High contrast = primary; low contrast = context |
| **Position** | Top-left = #1 priority; center = hero; bottom-right = least priority | Follows natural reading order |
| **Rule of thirds** | Place focal point at intersection of thirds grid lines | Creates dynamic, non-centered compositions |
| **Gestalt proximity** | 8–16px gap = "these belong together" | 32px+ gap = "these are separate groups" |
| **Enclosure** | Light background fill to group (use sparingly) | Only when proximity alone is ambiguous |

---

## Whitespace Tiers

| Tier | Pixels | Use |
|---|---|---|
| **Tight** | 16 | Between visuals within the same logical group |
| **Normal** | 24 | Between groups, page edge margins |
| **Section** | 32 | Between major page sections (header/body, body/footer) |

**Rule**: pick one tier for intra-group and one for inter-group. Never mix three spacing values within a single row of visuals.

---

## Power BI Grounding

| Property | Key / Location | Notes |
|---|---|---|
| Page size | `page.width`, `page.height` | Greenfield default 1920×1080; preserve existing size for brownfield unless resize is approved |
| Visual position | `position.x`, `position.y` | Snap to multiples of 8 |
| Visual size | `position.width`, `position.height` | Snap to multiples of 8 |
| Z-order | `position.z` | Increment by 1000 for layering headroom |
| Tab order | `tabOrder` | Must match visual reading order for accessibility |
| Bounds constraint | — | No visual may extend beyond page width/height |
| Page background | `wallpaper` / `background` | Keep white or near-white |

---

> Anti-patterns: see references/anti-patterns.md
