# HTML Output Guide

The analyst pipeline can produce either a Marp slide deck (PDF) or an interactive HTML report. This guide explains when to choose which, how the HTML format works, and how to extend it.

## TL;DR

| Use Marp/PDF when… | Use HTML when… |
|---|---|
| Report needs to print well or be PDF-attached to a ticket | Report will be shared via Slack, email, or browser link |
| Audience reads it once, top-to-bottom, sequentially | Audience wants to explore — drill into segments, filter views |
| Static slides at a workshop projector | Stakeholders self-serve their own questions from the same report |
| Compliance or archival requires a fixed PDF | Findings change as you click — interactivity *is* the value prop |

The fork's analyst pipeline supports both. Step 16 of the default workflow routes by `{{FORMAT}}`: `marp` (default) → Deck Creator agent; `html` → HTML Report Maker agent.

## What an HTML report includes

Every HTML report produced by `html-report-maker` is **a single self-contained `.html` file** with these mandatory components (enforced by the `html-output-patterns` skill):

- **Sidebar nav** with section labels and active-link tracking
- **Drill-down panels** on every aggregation chart — click a bar, see breakdowns by other dimensions in tabbed views
- **View toggles** when the data has a filter dimension — pill-button group above the chart that swaps views via `Plotly.react()`
- **Hover tooltips** on technical terms — `<span class="term" data-glossary="...">` with CSS pseudo-element popups
- **Glossary section** at the end — canonical list of every technical term used
- **Help overlay** — floating "?" button bottom-right that explains the interaction patterns
- **Inline CSS, no external dependencies** — Plotly via CDN is the only network reference; everything else is bundled

See `docs/example_report_vertical.html` for a working demo using synthetic data.

## Foundational principle: progressive disclosure

> HTML reports are interactive exploration tools, not static documents. If a stakeholder gets the same information from a static screenshot of the report, the format failed.

This is the rule that distinguishes "HTML done well" from "Marp deck rendered as HTML." Apply it relentlessly:

- Slide first impression = headline + one chart + one so-what
- Detail goes behind drill-down, view-toggle, or hover tooltip
- A dense table without drill-down on a content slide is a BLOCKER
- Six KPIs on a single slide without grouping is a WARNING

The skill (`.claude/skills/html-output-patterns/skill.md`) has the complete severity-graded rule set.

## Layout: vertical vs horizontal

Two layouts:

- **vertical** (default) — slides stack top-to-bottom, scroll naturally. Best for exec briefings, email distribution, async review, mobile.
- **horizontal** — slides snap one-per-viewport via CSS `scroll-snap`. Best for workshops, live talks, presentation-style consumption on a big screen.

### Auto-selection rules

| Signal | Layout |
|---|---|
| `{{CONTEXT}}` in {workshop, talk, live_demo, presentation} | horizontal |
| `{{AUDIENCE}}` = exec AND `{{CONTEXT}}` in {email_distribution, async_review} | vertical |
| Storyboard has > 12 sections | vertical (horizontal scroll becomes unwieldy) |
| Storyboard has < 6 sections AND `{{CONTEXT}}` includes "presentation" | horizontal |
| Default | vertical |

Override via `{{LAYOUT}}` = `vertical` | `horizontal` | `auto`.

## Invoking the agent

```
{{FORMAT}}: html
{{LAYOUT}}: vertical              # or horizontal | auto (default)
{{REPORT_TITLE}}: My Report       # short title for sidebar logo
{{NARRATIVE}}: working/narrative_{{DATASET}}.md
{{CHARTS}}: working/charts_manifest.json
{{STORYBOARD}}: working/storyboard_{{DATASET}}.md   # optional but recommended
```

The agent reads narrative + charts + storyboard, builds a slide spec, and writes a single `.html` file under `outputs/report_{{DATASET}}_{{DATE}}.html`.

## Customization

### Theme tokens

CSS variables at the top of every report:

```css
:root {
  --accent: #2563EB;       /* primary brand color, used for active states */
  --positive: #059669;     /* green — good outcomes */
  --negative: #DC2626;     /* red — flagged outcomes */
  --amber: #F59E0B;        /* warning callouts */
  --purple: #7C3AED;       /* tertiary highlight */
  --surface: #F8FAFC;      /* subdued background for cards */
  --border: #E2E8F0;       /* card and table borders */
  --text: #1E293B;         /* primary text */
  --text-sec: #64748B;     /* secondary text, captions */
}
```

To rebrand, change these in the agent's CSS template. To support multiple brands, extend with a `themes/` lookup pattern (V2).

### Drill-down data

When invoking the agent, pass charts with optional `drilldown` fields:

```json
{
  "id": "chart-segment",
  "type": "bar",
  "data": [...],
  "layout": {...},
  "drilldown": {
    "Mid-Market": {
      "by_region": {"label": "By region", "rows": [...]},
      "by_plan": {"label": "By plan tier", "rows": [...]}
    },
    "Enterprise": {...},
    "SMB": {...}
  }
}
```

If the chart data has dimensional breakdowns but `drilldown` isn't supplied, the agent prompts for it or renders with a "drill-down data not available" banner — not silently.

### View toggles

```json
{
  "id": "chart-segment",
  "view_toggles": [
    {
      "label": "View by",
      "key": "metric",
      "variants": [
        {"name": "dollars", "data": [...], "layout": {...}},
        {"name": "rate", "data": [...], "layout": {...}},
        {"name": "count", "data": [...], "layout": {...}}
      ]
    }
  ]
}
```

### Glossary

Pass `{{GLOSSARY}}` as a dict of term → definition. The agent uses it both for hover tooltips (`data-glossary="..."`) and for the glossary slide.

## Validation

Run the visual-design-critic against any HTML report — it applies the `HTML Report Review Path` checks:

```
{{DECK_FILE}}: outputs/report_{{DATASET}}_{{DATE}}.html
```

Checks: self-containment, drill-down coverage, glossary completeness, anchor resolution, Plotly init coverage, source citations, file size. Verdict logic identical to chart review: BLOCKER → NEEDS REVISION; WARNINGs only → APPROVED WITH FIXES; clean → APPROVED.

## Continuous improvement

When stakeholder feedback surfaces a new pattern or breaks an existing one:

1. Use the `log-correction` skill to record the feedback with `category: html-output`
2. The next iteration of the `html-output-patterns` skill should add the new rule alongside existing ones
3. The `html-report-maker` agent's templates evolve to match

The skill is the canonical rule-set. The agent applies whatever the skill says. The corrections drive the skill forward.

## What's not in V1 (open for V2)

- **Dark theme** — color tokens defined but no full dark-mode pass yet
- **Print-CSS** — prints OK but doesn't reflow elegantly to letter-size pages
- **Branded themes** — single default theme; per-org branding requires extending the theme-loader pattern
- **Comms-drafter HTML brief** — Slack/email summaries don't yet have an HTML variant
- **Chart Maker output schema** — currently html-report-maker accepts charts in the existing schema; richer drill-down/toggle annotations from upstream Chart Maker is a separate enhancement

## See also

- `.claude/skills/html-output-patterns/skill.md` — the canonical rules
- `agents/html-report-maker.md` — the agent that implements them
- `agents/visual-design-critic.md` — the HTML review path
- `docs/example_report_vertical.html` — working demo
