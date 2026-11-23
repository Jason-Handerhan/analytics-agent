# PBIR File Layout

A PBIP project on disk looks like this:

```text
<Report>.pbip                              # Project manifest
├── <Report>.Report/
│   ├── .platform                          # Fabric metadata (type, logicalId)
│   ├── definition.pbir                    # Report → SemanticModel binding
│   ├── definition/
│   │   ├── version.json                   # Format version (e.g. "2.0.0")
│   │   ├── report.json                    # Report-level: themes, settings, resources
│   │   └── pages/
│   │       ├── pages.json                 # Page order + active page name
│   │       └── <pageId>/
│   │           ├── page.json              # Page: displayName, size, type, filters
│   │           └── visuals/
│   │               └── <visualId>/
│   │                   └── visual.json    # Visual: type, position, query, formatting
│   ├── CustomVisuals/                     # Private .pbiviz packages (also need a report.json resourcePackages entry — see references/custom-visuals.md)
│   └── StaticResources/
│       ├── SharedResources/BaseThemes/    # Built-in base themes
│       └── RegisteredResources/           # User images, custom theme JSON
└── <Report>.SemanticModel/                # the semantic-model skill
```

### Key Files

| File | Purpose | Agent rule |
|------|---------|------------|
| `.platform` | Fabric/PBIP report item metadata | Keep it with the `.Report` folder |
| `definition.pbir` | Report → semantic model binding via `byPath` or `byConnection` | Leave it alone; the publish tool sets the model binding |
| `version.json` | PBIR format metadata | Preserve the full scaffolded file, including `$schema` |
| `report.json` | Report-level settings, themes, resources | Edit through references and validate after changes |
| `pages.json` | Page order and active page | Add every new page to `pageOrder`; preserve `activePageName` |
| `page.json` | Page metadata, size, filters | Preserve dimensions unless resizing is approved |
| `visual.json` | Visual type, position, query, formatting | Validate roles and formatting with CLI metadata |
| `localSettings.json` | User-local settings | Do not commit or rely on it |

Schema URLs live under `developer.microsoft.com/json-schemas/fabric/…`, but
**different PBIR files use different schema families** — the `item/report/definition/`
prefix only applies to the files inside the `definition/` folder, not to the
`.pbip` or `definition.pbir`:

| File | Schema family (URL after `…/fabric/`) | Example (versions at time of writing) |
|------|----------------------------------------|----------------------------------------|
| `<Report>.pbip` | `pbip/pbipProperties` | `pbip/pbipProperties/1.0.0/schema.json` |
| `definition.pbir` | `item/report/definitionProperties` | `item/report/definitionProperties/2.0.0/schema.json` |
| `version.json` | `item/report/definition/versionMetadata` | `…/definition/versionMetadata/1.0.0/schema.json` |
| `pages.json` | `item/report/definition/pagesMetadata` | `…/definition/pagesMetadata/1.1.0/schema.json` |
| `page.json` | `item/report/definition/page` | `…/definition/page/2.1.0/schema.json` |
| `report.json` | `item/report/definition/report` | `…/definition/report/3.3.0/schema.json` |
| `visual.json` | `item/report/definition/visualContainer` | `…/definition/visualContainer/2.9.0/schema.json` |

The suffixes are versioned PBIR contracts that Power BI Desktop bumps with most
releases (e.g. `visualContainer/2.9.0`, `page/2.1.0`, `report/3.3.0` at the
time of writing — newer values may appear in any user's PBIP). When editing,
**always preserve the existing `$schema` value**; when adding a new file, copy
the `$schema` URL from an existing file of the same type in the same report.
Do not bump versions on your own. Validate with `validate_dashboard`.

### Greenfield: creating a brand-new report

For a **brand-new report there is no existing file to copy a `$schema` from**, so
do **not** hand-author the project-shell files
(`.pbip`, `.platform`, `definition.pbir`, `version.json`, `report.json`,
`pages.json`, first `page.json`) from memory. The two failure modes that causes
— both from guessing a `$schema` — are:

| Desktop error | Cause |
|---------------|-------|
| `UnrecognizedSchemaVersion` | Wrong **family/path** — e.g. a `.pbip` whose `$schema` points at an `item/report/…` URL instead of `pbip/pbipProperties` |
| *"This version of Power BI does not support the version you have provided in `<file>`"* | Correct family, but a **version number** the installed build does not recognize (usually guessed too new) |

**Primary path — scaffold the project with the CLI (no Desktop required):**

```bash
powerbi-report-author scaffold <output-dir> --name <ReportName> \
  [--model-path ../<Model>.SemanticModel] [--page-name "Page 1"] [--force] [--offline]
```

This writes a complete, valid PBIP shell — `.pbip`, `.platform`,
`definition.pbir`, `version.json`, `report.json` (with a real Desktop base
theme), `pages.json`, and one empty `page.json` — then self-validates the result
offline. Use it for **every** new report, then author pages/visuals into it with
the normal edit, `validate_dashboard`, `publish_dashboard`, `render_dashboard` loop.

- **`--model-path`** — relative path (from the `.Report` dir) to a co-located
  semantic model, e.g. `Sales.SemanticModel`. Omit it to get a placeholder
  binding (`<ReportName>.SemanticModel`); the CLI flags it as a placeholder
  and the binding must be repointed before the report opens in Desktop (the publish tool sets the model binding).
- **Schema versions** — by default the CLI fetches the latest published
  `$schema` version per family from the public `microsoft/json-schemas` mirror
  at scaffold time (the response `schema.source` is `github`). If any family
  can't be fetched (offline, rate-limited), it falls back per-family to the
  versions pinned in the CLI (`schema.source` becomes `pinned`/`mixed`,
  `schema.fallbacks` lists what fell back). Pass **`--offline`** to skip the
  network entirely and use the pinned versions. The pinned versions are
  older-but-valid; Desktop and the Fabric service accept them and silently
  upgrade on first save/publish.
- If a scaffolded file triggers *"does not support the version you have provided
  in `<file>`"* on an **older** Desktop build (the live-fetched version is newer
  than that build supports), re-scaffold with **`--offline`** — that uses the
  older-but-valid versions pinned in the CLI, which Desktop accepts and upgrades
  in place.
