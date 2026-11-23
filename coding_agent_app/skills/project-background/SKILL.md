---
name: project-background
description: Explains the Instacart reorder-recommendation project and its Power BI dashboard, including the champion model (an ensemble chosen by Recall@5), the metrics (Recall@5, NDCG@5, Precision@5, F1@5), the heuristic baseline, what BigQuery holds after modeling, and what the dashboard is for. Read it when a plan uses a project term you do not know, or to see where a dashboard page's data comes from.
---

# Project background

Two short files in this folder. Read them whole.

| File | Read it when |
|---|---|
| `executive_summary.txt` | What the project is for, what the champion model is, the metrics, and the baseline it is compared with |
| `system_architecture.txt` | How data flows from BigQuery through modeling to Power BI, which stage picks the champion, and what BigQuery holds after modeling (evaluation metrics, SHAP data, dimension tables) |

The model's own descriptions say what each dashboard object means (`INFO.VIEW.MEASURES()`, or the `///` lines in the TMDL files). Use these files for the project terms behind them.
