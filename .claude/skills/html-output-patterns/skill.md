# Skill: HTML Output Patterns

## Purpose

Standards for producing interactive HTML reports as a deliverable format alongside Marp/PDF decks. Pairs with the `html-report-maker` agent: this skill defines the rules; the agent applies them.

The skill exists because HTML reports earn their format only when they're interactive. A static HTML "deck" is a worse PDF — bigger file, no print fidelity, no upside. The interactivity (drill-downs, view toggles, hover tooltips, glossary) is the value proposition.

## When to Use

- Generating an HTML report (any `.html` deliverable from the analyst pipeline)
- Reviewing an HTML report for quality before delivery
- Choosing between Marp/PDF and HTML for an analysis (this skill's principles inform the choice)

Pairs with: `html-report-maker` (agent), `presentation-themes` (sibling — Marp-side standards), `visualization-patterns` (charts), `stakeholder-communication` (audience adaptation).

## Foundational Principle: Progressive Disclosure

> HTML reports are interactive exploration tools, not static documents. If a stakeholder gets the same information from a static screenshot of the report, the format failed.

Every slide is a question. The interactions (drill-downs, toggles, hover tooltips) are the answers. Keep first-impression density LOW. Push detail behind interaction.

This is the rule that distinguishes "HTML output done well" from "Marp deck rendered as HTML." Apply it relentlessly.

**Severity:** BLOCKER for any slide whose first impression replicates a Marp slide's information density (one chart + supporting text + so-what is fine; a dense table without drill-down is not; six KPIs without grouping is not).

## Mandatory Components

Every HTML report includes all six. Severity = BLOCKER if any are missing without explicit justification.

### 1. Sidebar nav with section labels

Fixed-position left sidebar listing every slide grouped by section. Active link tracking via scroll position (vertical) or scroll-snap position (horizontal).

```
nav .logo            → short report title
nav .section-label   → uppercase section header (e.g., "01 / OVERVIEW")
nav a                → one per slide, with hover + active states
```

Severity:
- BLOCKER if missing
- BLOCKER if any nav anchor (`<a href="#x">`) has no matching slide `id="x"`
- WARNING if more than 18 slides total (consider splitting)

### 2. Drill-down on every aggregation chart

Any bar / pie / stacked / scatter chart with a categorical x-axis MUST have a drill-down panel that opens on click.

Implementation:
- Hidden `<div class="dc-panel" id="dc-{slide_id}">` below the chart
- Plotly click handler: `chart.on('plotly_click', e => openDC(slide_id, e.points[0].x))`
- Panel contains tabbed views (`dc-tabs`) for breakdowns by other dimensions ("By tier", "By region", "By time", etc.)
- Close button (×) sets `panel.classList.remove('open')`
- Drill-down data lives in a JS object: `DC = {slide_id: {segment_value: {tab_key: {label, rows}}}}`

Severity:
- BLOCKER if any aggregation chart lacks a drill-down when the underlying data has dimensional breakdowns
- WARNING if a chart's drill-down only has one tab (consider whether a second dimension would surface insight)
- INFO if the chart is a 2- or 3-row matrix where drill-down adds no value (e.g., a recommendation matrix). Document the exception.

### 3. View toggles when the data has a filter dimension

If the chart's underlying data contains a categorical filter dimension (segment, product, region, time window, metric type), expose it as a pill-button toggle group above the chart.

Implementation:
- `<div class="ot-toggle"><button class="active" onclick="setView('chart-id', 'all')">All</button>...</div>`
- `setView()` calls `Plotly.react(chartId, data, layout)` to swap data without re-mount
- Active button: `class="active"` styled with `background: var(--accent); color: white`

Common toggle dimensions:
- Metric: Dollars / Rate / Count
- Segment: All / Net New / Cross-sell
- Filter: All / [Top entity] only / Other [entities]
- Window: All / 60d / 90d / 180d

Severity:
- WARNING if a chart has an obvious filter dimension in its data but no toggle
- INFO if the dimension exists but has only 2 values (binary toggle works but a single button group may be overkill — judgment call)

### 4. Hover tooltips on technical terms

Any term that isn't immediately obvious to a non-analyst stakeholder gets a hover tooltip with a plain-English definition.

Implementation:
- Wrap term: `<span class="term" data-glossary="Definition text...">term</span>`
- CSS pseudo-elements render the tooltip: `.term:hover::after { content: attr(data-glossary); ... }`
- Definition text is duplicated in the glossary slide (next rule) — same wording

Auto-detect heuristics for "technical term":
- Any uppercase 2–5 letter abbreviation (KPI, MAE, ROC, AUC, CV, MRR, ARR)
- Internal product names, code names, codenames
- Domain jargon (industry-specific terms, internal product nomenclature, methodology shorthand)
- Statistical measures and model metrics

Severity:
- WARNING if a technical term appears without `data-glossary` attribute
- WARNING if a `data-glossary` term has no matching glossary slide entry
- BLOCKER if the report uses 5+ technical terms with zero tooltips (signals the rule was ignored, not just missed once)

### 5. Glossary section at the end

Every HTML report ends with a glossary slide. Lists every technical term used in the report with its plain-English definition. The glossary is the canonical reference; hover tooltips are the convenience layer.

Implementation:
- `<div class="slide" id="slide-glossary">`
- 2-column grid of `<dt>term</dt><dd>definition</dd>` pairs
- Definition text matches the `data-glossary` attribute of the same term used inline

Severity:
- BLOCKER if any `data-glossary` term has no matching glossary entry
- BLOCKER if the glossary slide is missing and the report uses any technical terms
- WARNING if the glossary has fewer than 80% of the technical terms used in the report

### 6. Help overlay

Floating "?" button bottom-right. Click opens a centered modal listing 3–5 interaction tips ("Click any bar to drill down", "Toggle filters above charts", "Hover underlined terms", "Use sidebar nav to jump").

First-time viewers don't know HTML reports are interactive. Discoverability is cheap.

Implementation:
- `<button class="help-btn">?</button>` fixed position bottom-right, 40px circle, accent-colored
- `<div class="help-overlay">` hidden by default, opens on button click, closes on backdrop click

Severity:
- WARNING if missing (recommended for every report; not blocking because a sufficiently obvious report can survive without it)

## Mandatory Self-Containment Rules

Every HTML report is a single `.html` file. No external CSS, no external fonts, no external images, no external JS except Plotly via CDN.

### Required:

| Rule | Severity |
|---|---|
| All CSS inline in `<style>` | BLOCKER if any `<link rel="stylesheet" href="http*">` |
| Plotly via CDN, single `<script src="https://cdn.plot.ly/plotly-*">` tag | BLOCKER if missing or duplicated |
| System font stack only | BLOCKER if Google Fonts / external font CDN linked |
| Images embedded as base64 data URIs OR omitted | BLOCKER if `<img src="http*">` |
| `<title>` and `<meta name="viewport">` present | BLOCKER if either missing |
| File size < 5 MB | WARNING; BLOCKER if > 10 MB |

### Why:

The file gets emailed, pasted into Slack, attached to tickets, opened from network shares with no internet, archived alongside the analysis output. Every external dependency is a future broken report.

## Layout Decision

Two layouts available:

- **vertical** (default) — slides stack top-to-bottom, scroll naturally. Best for exec briefings, email distribution, async review, mobile.
- **horizontal** — slides snap one-per-viewport via CSS `scroll-snap`. Best for workshops, live talks, presentation-style consumption on a big screen.

### Auto-selection rules

| Signal | Layout |
|---|---|
| `CONTEXT` in {workshop, talk, live_demo, presentation} | horizontal |
| `AUDIENCE` = exec AND `CONTEXT` in {email_distribution, async_review} | vertical |
| Storyboard has > 12 sections | vertical (horizontal scroll becomes unwieldy) |
| Storyboard has < 6 sections AND `CONTEXT` includes "presentation" | horizontal |
| Default | vertical |

User can override via `{{LAYOUT}}` variable.

Severity: WARNING if horizontal layout used with > 12 sections (the format breaks down — viewers lose orientation).

## Content Density Rules

Each slide has a density budget. Going over hides the headline.

| Slide kind | Max content | Notes |
|---|---|---|
| `title` | 1 headline + 1 subtitle + 1 metadata line | No KPIs, no charts |
| `section-intro` | 1 eyebrow label + 1 headline + 1 paragraph | Pure transition slide |
| `content` | 1 chart OR 1 table, 1–6 KPIs, 1 so-what callout, 1 source line | Detail goes in drill-down, not the slide |
| `recommendations` | 3–5 rec cards, 1 so-what | More than 5 = split into multiple slides |
| `glossary` | All terms, 2-column grid | Only at the end |

Severity:
- BLOCKER if a content slide has > 1 dense table without drill-down
- BLOCKER if a content slide has > 1 chart without space to breathe
- WARNING if a slide has > 6 KPIs without grouping (consider 2 KPI rows)
- WARNING if a slide has > 250 words of body text (push detail to drill-down or another slide)

## Source Citations

Every content slide cites its data source. Goes at the bottom of the slide as `<p class="source-note">`.

Format: `Source: {table or file}, {time range}, {filter notes}`. Be specific — not "internal data" but "Source: orders_summary.csv · 2026-Q1 · excludes refunds".

Severity:
- BLOCKER if any content slide has zero sources cited (other than `title`, `section-intro`, `glossary`)

## Anti-patterns

- **Static-only HTML** — if the report has zero `dc-panel`, zero `ot-toggle`, zero `data-glossary`, you've shipped a worse PDF. Severity: BLOCKER.
- **Drill-down with placeholder data** — if the underlying breakdown data isn't available, don't fake it. Either auto-generate from real source, prompt the user for it, or omit the drill-down with an explicit "data not available" banner. Silent fakery is worse than missing.
- **Tooltip without glossary entry** — every `data-glossary` term has a glossary slide entry, and vice versa.
- **Density-packed first impression** — five KPIs, two charts, three callouts on one slide. The headline disappears.
- **External CSS files / fonts / images** — breaks self-containment. Single-file rule, no exceptions.
- **Multiple Plotly script tags** — load once at the top of the document.
- **Slide IDs that aren't kebab-case** — must match `<a href="#...">` exactly. Use `slide-foo-bar`.
- **Content slides without source citation** — every finding cites where it came from.
- **Horizontal layout with > 12 sections** — viewers lose orientation. Use vertical.
- **Help overlay listing features the report doesn't have** — if there are no toggles, don't tell viewers to "use the toggles." Tailor the help text to the actual interactions present.

## Connections to Other Skills

- `html-report-maker` (agent) — implements this skill; the agent's input contract maps to these rules
- `presentation-themes` — Marp/deck sibling. Theme tokens (colors, fonts) should be consistent across HTML and Marp outputs so the same analysis can produce both formats with visual coherence
- `visualization-patterns` — chart conventions (action titles, highlight color, source notes) apply to Plotly charts inside HTML reports just as they apply to matplotlib charts
- `stakeholder-communication` — audience adaptation. Exec audience → vertical layout, denser KPIs, less methodology. DS audience → more detail tolerable, more drill-down depth
- `log-correction` / `feedback-capture` — when a stakeholder gives feedback on an HTML report, log corrections with `category: html-output` so this skill keeps evolving

## Provenance

Every rule in this skill traces to a real correction surfaced during iterative testing on stakeholder analyses — not invented from theory. When a future stakeholder gives feedback on an HTML report ("the so-what callout should be wider", "this section needs a toggle", "I didn't know I could click bars"), capture it via the `log-correction` skill (category=html-output). The next revision of this skill should add the new rule alongside the existing ones — severity-graded rules accumulate; they don't replace each other.
