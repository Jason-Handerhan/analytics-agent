# Writing a dashboard plan

A dashboard plan changes the Power BI dashboard (its pages and visuals) and its semantic model. The plan's `description` is markdown about the pages: what each is for and how it looks. The coding agent works out the model (it finds or builds the measures and columns the pages need), the files to copy and the code. Write the information below in whatever structure fits the request; it is a list of what the plan should hold, not a template.

## Where data comes from

- **The dashboard's semantic model** is what the pages can use now. Its tables, columns, measures and relationships, with descriptions, are in your context, and `get_measure_dax` gives a measure's DAX. For a page with page info, `get_page_info` returns what it shows, how to navigate it, its key calculations and a reference screenshot.
- **`agent_safe` tables** hold the data from before the ML model and the prediction explanations. Their columns are in your context. These tables are row-level, so most pages need them aggregated first.
- **BigQuery tables built by code** are what can be brought into the model. You cannot query these datasets, but you can read the code that builds them with `get_repo_contents`:
  - **Before the ML model** (orders, products, features): the SQLX in `definitions/` (`sources_*`, `staging/`, `silver/`, `gold/`).
  - **After the ML model** (evaluation metrics, SHAP values, ensemble weights, the dimension tables the dashboard filters by): `Notebooks/evaluation/model_evaluation.ipynb` writes them to the `ml_model_results` dataset. Read it to see what it builds today.

## How much to aggregate

Aggregate in BigQuery as far as the pages allow: bring in the coarsest grain that still meets what the pages show, including any slicer or breakdown the user asked for, and no finer.

## Step 1: does the dashboard need new data?

Do this first, for any dashboard plan, before asking about design.

1. **Check that the available data supports what the user described**, chart by chart, card by card and slicer by slicer, against the model in your context and the aggregation rule above: the number, the fields it is broken down and filtered by, and the grain. Ask the user if a description leaves the data unclear. You do not have to be exhaustive.
2. **Decide, using this table:**

   | The situation | What to do |
   |---|---|
   | Everything the pages need is in the model, or a measure can calculate it from what is there (a ratio, sum, difference or rank) | Dashboard plan only. |
   | A table the code already builds has it, but it is not in the model | Dashboard plan only: the coding agent brings the table into the model. First ask the user to confirm they have run that code. |
   | Nothing builds it yet, or it is built only at a finer grain than the pages need, or it needs a join | Code plan first, in SQLX or the notebook as above and aggregated to the grain the pages need (read the `code` guide). Then step 3. |

   Aggregate `agent_safe` in a notebook in `Notebooks/dashboard_data/`, using SQL, and write the result to the `agent_dashboard` dataset.
3. **If a code plan comes first, save a task list** with `manage_tasks`, then stop to show it to the user for confirmation. Do not start the first task:
   1. Code plan: write, approve, run, review.
   2. Open and merge the pull request.
   3. The user runs the new code to build the table. Confirm its name, columns and grain with them.
   4. Dashboard plan: write, approve, run, review.
   5. Open its pull request.

## For any dashboard plan, when it applies

- **Constraints.** What must not change ('do not touch page X').
- **Decisions.** Choices the user settled, each as an instruction with their reason if they gave one. If the user says the published sandbox dashboard is out of step with the files and asks to publish over it, record that here.
- **Suggested code.** Only code the user supplied, or when they asked to work through it (for example a DAX measure tested with the DAX tool). Do not draft code yourself unprompted.
- **Style, an existing dashboard.** Match its colors, fonts and card style unless the user asked for something different.
- **Style, a new dashboard.** The user describes it. Save the plan with what you know, then ask for what is missing in one question, and write down what they say:
  - the company or brand it should follow (its colors, fonts, a logo if they have one);
  - the main colors, as names or hex codes (for example 'purple and orange' for a FedEx dashboard);
  - light or dark;
  - the look, offering these in plain words (the looks the coding agent can build; they can mix looks):
    - editorial newsroom: broadsheet, type-led, one sharp accent;
    - dark instrument panel: dark, high contrast, color only for status;
    - calm and clinical: near-white, teal and sage;
    - Financial Times style: pink-tinted broadsheet, tabular numerals;
    - Bauhaus: geometric, primary colors sparingly;
    - monospace terminal: developer feel, ticker-style KPI bars;
    - dense analyst workbench: off-white, dense, two accents;
    - playful and energetic: warm, bright, coral, soft shapes;
    - minimal: white, black type, one accent;
    - corporate cool: cool grey, slate type, one tech accent;
    - scholarly calm: stone surface, forest green, serif headings;
    - or the company's own brand;
  - anything to avoid.

  Colors that carry meaning (red for bad, green for good) are chosen separately from the brand colors. The coding agent turns this into a theme.
- **Specific styling.** If the user wants a particular card, chart or slicer styled differently, say which and how.

## A new page or dashboard: for each page

- **Name** and **what it answers.** What the page lets its audience see or decide, in one or two sentences and the user's words (for example 'which model to ship, and where the champion is weakest').
- **Cards.** Headline numbers. One item each: the number, based on what the user asked for but made clearer (for example 'Champion F1'), and a label if it should differ. Use an existing measure's name if you know one that fits; the coding agent finds or builds the measure. Five is the default maximum (about 250 to 300 pixels each, so they fit in one row).
- **Charts.** Charts and tables, each with what it shows (a measure by a field, for example 'Champion F1 by model') and the chart type (for example clustered bar, line, table). List them in priority order, most important first, and a detail table last.
- **Slicers.** One item each: the field and the slicer type (for example dropdown, list, date range), and 'synced with <page>' if it should share state. List them broadest first (for example the data split, then the model, then the metric). Write 'None' if the user wants no slicers: never leave this out. Four is the default maximum.
- **More cards or slicers than the default maximums (5 cards, 4 slicers).** If the user wants more, follow up: it is outside good design practice, but there may be a good reason, so ask what they want and write down what they decide.
- **Field parameters, bookmarks and buttons.** What they are, so you can recognize them in a user's wish:
  - a *field parameter* lets the viewer switch which measure or which field a visual shows, through a slicer ('let me switch the chart between recall and F1');
  - a *bookmark* is a saved state of a page (which filters are set, which visuals show) that a button or the viewer can apply ('a view with only the top models', or switching one chart for another in the same spot);
  - a *button* goes to a page, applies a bookmark, clears the filters or opens a link.

  Include them where they belong in the plan: they are often tied to a chart or a slicer. For each, say what it does, what it is tied to, where it sits, and what is shown when the page first opens.
- **Layout.** Say where things go in words, not only in an order. If you list items in an order, say what it means in position ('priority order: left to right, then down'). Example wording: 'title across the top, slicers in a row beneath it, four cards beneath the slicers, then the champion chart on the left and the detail table on the right'.
  - **If the user described a layout,** use it and write it in.
  - **Otherwise use the default,** and label it as the default in the plan:
    - *A new page in the existing dashboard:* follow how its existing pages are arranged. If they are arranged inconsistently, ask which page to base it on, or whether the user wants the new-dashboard layout.
    - *A new dashboard:* the title at the top, up to 3 slicers at the right end of the title row (4 or more in a filter rail down the side), the cards in one row beneath, then the charts in priority order reading left to right and down, the detail table last.
  - After you save, show the layout and ask whether the user wants anything different.

## Editing an existing page

Include (per page):
- Page name: exact text
- What changes: which visual or object, which property, what it becomes, and anything it affects (a bookmark, button, slicer sync or navigation link)
- What stays: state that anything not required for the requested changes should remain unchanged

## Deleting a page or visual

Include:
- What goes: the page name, and the visual if only one visual
- What points at it, to remove or retarget: buttons, bookmarks, navigation, slicer sync, page order

## A request that changes only the model

Say what the user wants changed, in their words: what a new measure or column computes or holds, or which BigQuery table to bring in, with any definition or name they gave. The coding agent works out the rest.

## Large requests

A request that adds more than one page is large:
- Settle what is shared first (goal, audience, style, slicers on every page).
- Save a `manage_tasks` list, one task per page.
- Plan one page per turn: resend the whole description with the new page added, the others unchanged, and show the user only the new page.
- The user approves once, when the last page is in.
- At most 3 new pages per plan. For more, split into several plans and plan the first now.

## What the reviewer checks (for the user, not the plan)

This is not part of the plan's description: do not write it there, or adapt it to the plan. The plan only sets `review` on or off. When review is on, a reviewer checks the finished work against the criteria below and sends the coding agent back to fix what fails. Tell the user what is checked when you show the plan.

Judge only what the diff adds or changes, and what those lines refer to. Problems that already exist in the dashboard the diff does not touch are not failures. A criterion that does not apply to this task passes.
1. Everything the plan describes for each page is present in the diff, with every card, chart, slicer, bookmark and button it names and the chart and slicer types it gives.
2. Every field a visual uses exists in the model or is added by the diff.
3. Every table, column and measure the diff adds has a `///` description, and every new measure has a `formatString`.
4. The final report says what each new measure calculates.
5. No bookmark, button, interaction or slicer sync points at something the diff removed.
6. The diff changes nothing under dashboard/ the plan did not ask for, and does not rearrange an existing page the plan did not ask to change.
7. Every claim the report makes about what the diff changes is true, and it claims no change the diff lacks. Claims about what was run are not judged.
8. The final image render of each changed page shows what the plan describes for it, readable, with nothing clipped or overlapping.
