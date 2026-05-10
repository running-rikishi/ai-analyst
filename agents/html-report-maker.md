<!-- CONTRACT_START
name: html-report-maker
description: Produce a self-contained interactive HTML report from a narrative + charts. Two layouts (vertical scroll for exec/email, horizontal scroll-snap for workshops/talks). Plotly charts via CDN, inline CSS, sidebar nav, KPI cards, so-what callouts.
inputs:
  - name: NARRATIVE
    type: file
    source: agent:storytelling
    required: true
  - name: CHARTS
    type: file
    source: agent:chart-maker
    required: true
  - name: STORYBOARD
    type: file
    source: agent:story-architect
    required: false
  - name: LAYOUT
    type: str
    source: user
    required: false
  - name: THEME
    type: str
    source: user
    required: false
  - name: REPORT_TITLE
    type: str
    source: user
    required: false
  - name: CONTEXT
    type: str
    source: user
    required: false
  - name: AUDIENCE
    type: str
    source: user
    required: false
outputs:
  - path: outputs/report_{{DATASET_NAME}}_{{DATE}}.html
    type: html
depends_on:
  - storytelling
knowledge_context:
  - .knowledge/datasets/{active}/manifest.yaml
pipeline_step: 16
CONTRACT_END -->

# Agent: HTML Report Maker

Produces interactive HTML reports as a parallel deliverable to Marp/PDF decks. Implements the standards defined in `.claude/skills/html-output-patterns/skill.md`. When stakeholder feedback surfaces a new pattern, log a correction via the `log-correction` skill (category=html-output) — the skill evolves from accumulated corrections, this agent applies whatever the skill currently says.

## Foundational principle

**HTML reports are interactive exploration tools, not static documents.** If a stakeholder gets the same information from a static screenshot of the report, the format failed.

Every slide is a question. The interactions (drill-downs, toggles, hover tooltips) are the answers. Keep first-impression density LOW. Push detail behind interaction.

## Purpose

Produce a single self-contained `.html` interactive report from analysis outputs. The report is consumed in a browser (no server required), shareable as a single file, with embedded interactive Plotly charts, drill-down panels, view toggles, hover tooltips, and a sidebar nav.

Two layouts:
- **vertical** (default) — slides stack top-to-bottom, scroll naturally. Best for exec briefings, email distribution, async review, mobile.
- **horizontal** — slides snap one-per-viewport via CSS `scroll-snap`. Best for workshops, live talks, presentation-style consumption on a big screen.

## Inputs

- `{{NARRATIVE}}`: path to the storytelling narrative (markdown). Provides exec summary, findings, insight, recommendations.
- `{{CHARTS}}`: path to the charts manifest (Plotly figure specs as JSON). Each chart has an `id`, `type`, `data`, `layout`, a slide assignment, and ideally:
  - `drilldown`: dict mapping segment values (e.g., bar x-values) to detail breakdowns. Each detail can be a table (rows of `{label, value, ...}`) or a sub-chart Plotly spec. Multiple breakdowns per segment supported as named tabs (e.g., `{"by_product": [...], "by_tier": [...]}`).
  - `view_toggles`: list of filter dimensions. Each toggle is `{label, key, variants: [{name, data, layout?}]}`. Renders as a pill-button group above the chart; clicking a button calls `Plotly.react()` to swap data.
  - `tooltip_terms`: dict of technical term → plain-English explanation, for any technical labels appearing in the chart's axes or legend.
- `{{GLOSSARY}}` *(required if any technical term used)*: dict of `term: definition` pairs. Used to populate the glossary slide AND power hover tooltips throughout the report.
- `{{STORYBOARD}}` *(optional)*: storyboard YAML/markdown if available. Provides explicit beat/section structure.
- `{{LAYOUT}}` *(optional)*: `vertical` | `horizontal` | `auto`. Default `auto`.
- `{{THEME}}` *(optional)*: `analytics` (light) | `analytics-dark`. Default `analytics`.
- `{{REPORT_TITLE}}` *(optional)*: short title for the sidebar logo. Default: derive from narrative title.
- `{{CONTEXT}}` *(optional)*: usage context — `workshop` | `exec_brief` | `email_distribution` | `async_review` | `live_demo`. Drives auto-layout choice.
- `{{AUDIENCE}}` *(optional)*: `exec` | `pm` | `ds` | `eng` | `cs` | `mixed`. Drives default layout when CONTEXT not set.

### When upstream inputs are missing

If `{{CHARTS}}` doesn't include `drilldown` or `view_toggles` for a chart that obviously needs them (any aggregation chart with categorical x-axis or filter dimensions), the agent MUST do one of:
1. **Auto-generate** drill-downs from the underlying analysis data if accessible (e.g., re-aggregate by other dimensions present in the source data)
2. **Prompt the user** for the drill-down structure inline ("This chart aggregates by Product. What other dimensions should drill-down expose? Common: Tier, Region, Time")
3. **Document the gap** — render the chart without drill-down and add a banner "Drill-down data not available; rerender with `drilldown` field" so the omission is visible, not silent

Same logic for `tooltip_terms` and `{{GLOSSARY}}` — if technical terms appear and no glossary is supplied, prompt for definitions or render with a "MISSING DEFINITION" placeholder, never silently.

## Workflow

### Step 1: Resolve layout

If `{{LAYOUT}}` set explicitly to `vertical` or `horizontal`, use it.

If `auto` or unset, apply this decision:

| Signal | Layout |
|---|---|
| `CONTEXT` in {workshop, talk, live_demo, presentation} | horizontal |
| `AUDIENCE` = exec AND `CONTEXT` in {email_distribution, async_review} | vertical |
| Storyboard has > 12 sections | vertical (horizontal scroll becomes unwieldy) |
| Storyboard has < 6 sections AND `CONTEXT` includes "presentation" | horizontal |
| Default | vertical |

Document the chosen layout in the report's title slide subtitle.

### Step 2: Resolve theme tokens

Read theme based on `{{THEME}}`:

```python
ANALYTICS_LIGHT = {
    "accent": "#2563EB",
    "positive": "#059669",
    "negative": "#DC2626",
    "amber": "#F59E0B",
    "purple": "#7C3AED",
    "surface": "#F8FAFC",
    "border": "#E2E8F0",
    "text": "#1E293B",
    "text_sec": "#64748B",
    "page_bg": "#F1F5F9",
    "nav_bg": "#1E293B",
    "nav_text": "#CBD5E1",
    "nav_label": "#64748B",
    "section_intro_grad": "linear-gradient(135deg, #1E293B 0%, #334155 100%)",
    "callout_bg": "#FEF3C7",
    "callout_border": "#F59E0B",
    "callout_text_link": "#92400E",
}
```

(Dark theme is V2 — V0 ships light only.)

### Step 3: Build the slide spec

Walk the narrative + storyboard + charts and assemble a list of slide specs. Each slide is one of:

| kind | When to use | Required fields |
|---|---|---|
| `title` | First slide of the report | `title`, `subtitle`, optional `date` |
| `section-intro` | Before each major section | `section_label` (e.g., "01 / CONTEXT"), `title`, `subtitle` |
| `content` | Standard finding/analysis slide | `title`, optional `subtitle`, body content (KPIs / chart / table / text), optional `so_what`, `source` |
| `recommendations` | Final/CTA slide | `title`, list of `rec_card`s with `number`, `action`, `rationale` |
| `glossary` / `appendix` | End-of-report reference | `title`, list of `term: def` pairs or freeform body |

Slide content fields (any combination, used per-slide):
- `kpis`: list of `{value, label, delta?, color?}` — renders as KPI cards in a row
- `chart`: Plotly figure spec (id + data + layout) — renders as embedded Plotly div
- `table`: HTML table or list of rows — renders as styled table
- `body_md`: markdown text — renders as paragraph(s)
- `so_what`: 1–3 sentence amber callout explaining the implication
- `source`: short citation, rendered as italic footer

Group slides into `sections`. Each section has a `label` (uppercase, used as nav header) and a list of slide IDs.

Output the spec to `working/html_spec.json` for traceability.

### Step 4: Render HTML

Construct a single `.html` document with this structure:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{report_title}</title>
  <script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
  <style>{INLINE_CSS}</style>
</head>
<body>
  <nav>{SIDEBAR_NAV}</nav>
  <main>{SLIDES}</main>
  <script>{INIT_JS}</script>
</body>
</html>
```

Use the templates in the next section.

### Step 5: Validate (BLOCKER-graded checks before writing)

| Check | Severity | Rule |
|---|---|---|
| File contains no `<link rel="stylesheet" href="http`</> | BLOCKER | All CSS must be inline; no external stylesheets |
| Plotly script tag present and points at `https://cdn.plot.ly/plotly-*` | BLOCKER | Charts won't render without it |
| Every `<a href="#section-xxx">` in nav has a matching `id="section-xxx"` in main | BLOCKER | Broken anchors break navigation |
| Every Plotly chart has a unique `id` and a corresponding `Plotly.newPlot('id', data, layout)` call | BLOCKER | Charts render only when JS init matches DOM ids |
| `<title>` and `<meta name="viewport">` both present | BLOCKER | Required for rendering and mobile |
| File size < 5 MB | WARNING | Larger files load slowly and email-attach poorly |
| Every slide has either `source` or is `kind: section-intro/title/glossary` | WARNING | Findings without sources fail the always-cite-data-source rule |

If any BLOCKER fails → fix and re-render. Don't write the file with a known bug.

### Step 6: Write to output

Write to `outputs/report_{{DATASET_NAME}}_{{DATE}}.html`. Where DATASET_NAME comes from `.knowledge/active.yaml` and DATE is today.

Print:
- File path
- Layout chosen
- Slide count by kind
- Plotly chart count
- File size
- The browser-open command: `open <path>` (macOS) or equivalent

## Templates (V0)

Use these verbatim where possible. Substitute `{var}` placeholders.

### CSS (vertical layout, light theme)

```css
:root {
  --accent: #2563EB; --positive: #059669; --negative: #DC2626;
  --amber: #F59E0B; --purple: #7C3AED;
  --surface: #F8FAFC; --border: #E2E8F0;
  --text: #1E293B; --text-sec: #64748B;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #F1F5F9; color: var(--text); line-height: 1.5;
}
nav {
  position: fixed; top: 0; left: 0; width: 240px; height: 100vh;
  background: #1E293B; color: #CBD5E1; overflow-y: auto; z-index: 100;
  padding: 20px 0; font-size: 13px;
}
nav .logo { padding: 0 20px 16px; font-size: 15px; font-weight: 700; color: #F8FAFC; }
nav a {
  display: block; padding: 8px 20px; color: #94A3B8; text-decoration: none;
  border-left: 3px solid transparent; transition: all 0.15s;
}
nav a:hover, nav a.active { color: #F8FAFC; background: #334155; border-left-color: var(--accent); }
nav .section-label {
  padding: 16px 20px 4px; font-size: 10px; text-transform: uppercase;
  letter-spacing: 1px; color: #64748B; font-weight: 600;
  position: sticky; top: 0; background: #1E293B; z-index: 1;
}
main { margin-left: 240px; padding: 32px 24px; max-width: 1200px; }
.slide {
  background: white; border-radius: 12px; padding: 40px;
  margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08);
  min-height: calc(100vh - 64px); display: flex; flex-direction: column; justify-content: center;
}
.slide h2 { font-size: 28px; font-weight: 700; margin-bottom: 6px; color: var(--text); line-height: 1.3; }
.slide .subtitle { font-size: 13px; color: var(--text-sec); margin-bottom: 16px; }
.slide .source-note { font-size: 11px; color: #94A3B8; margin-top: 12px; font-style: italic; }
.slide .so-what {
  background: #FEF3C7; border-left: 4px solid #F59E0B;
  padding: 16px 20px; font-size: 15px; line-height: 1.7; margin-top: 16px; border-radius: 6px;
}
.section-intro {
  background: linear-gradient(135deg, #1E293B 0%, #334155 100%);
  border-radius: 12px; padding: 48px 56px; margin-bottom: 24px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.12);
  min-height: calc(100vh - 64px); display: flex; flex-direction: column; justify-content: center;
  color: #F8FAFC;
}
.section-intro h2 { font-size: 32px; font-weight: 800; margin-bottom: 12px; color: #F8FAFC; }
.section-intro p { font-size: 17px; color: #94A3B8; line-height: 1.6; max-width: 600px; }
.kpi-row { display: flex; gap: 16px; margin: 20px 0; align-items: stretch; flex-wrap: wrap; }
.kpi-card {
  flex: 1; min-width: 160px; background: var(--surface); border-radius: 8px; padding: 20px;
  text-align: center; border: 1px solid var(--border);
}
.kpi-value { font-size: 36px; font-weight: 800; }
.kpi-value.accent { color: var(--accent); }
.kpi-value.negative { color: var(--negative); }
.kpi-value.positive { color: var(--positive); }
.kpi-label { font-size: 13px; color: var(--text-sec); margin-top: 4px; }
.kpi-delta { font-size: 13px; color: var(--text-sec); margin-top: 2px; }
.plotly-chart { width: 100%; min-height: 380px; }
.rec-card {
  display: flex; align-items: flex-start; gap: 16px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  padding: 20px; margin-bottom: 12px;
}
.rec-number {
  flex-shrink: 0; width: 32px; height: 32px; border-radius: 50%;
  background: var(--accent); font-weight: 700; font-size: 14px;
  display: flex; align-items: center; justify-content: center; color: white;
}
.rec-action { font-weight: 700; font-size: 15px; margin-bottom: 6px; }
.rec-rationale { font-size: 13px; color: var(--text-sec); line-height: 1.6; }
table.report-table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 12px 0; }
table.report-table th {
  text-align: left; padding: 8px 12px; background: #E2E8F0;
  font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px;
}
table.report-table td { padding: 8px 12px; border-bottom: 1px solid var(--border); }
table.report-table tr:hover { background: #F1F5F9; }
@media (max-width: 900px) {
  nav { display: none; }
  main { margin-left: 0; padding: 16px; }
}
@media print {
  nav { display: none !important; }
  main { margin-left: 0 !important; padding: 16px !important; max-width: 100% !important; }
  .slide { break-inside: avoid; box-shadow: none; border: 1px solid #E2E8F0; min-height: auto !important; }
  .section-intro { break-inside: avoid; box-shadow: none; }
  body { background: white; }
}
```

### CSS additions for horizontal layout

When `LAYOUT == "horizontal"`, **prepend** this block to override the vertical defaults:

```css
html, body { height: 100%; overflow: hidden; }
main {
  position: fixed; top: 0; left: 240px; right: 0; bottom: 0;
  display: flex; overflow-x: auto; overflow-y: hidden;
  scroll-snap-type: x mandatory; scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
  margin-left: 0 !important; padding: 0 !important; max-width: none !important;
}
.slide, .section-intro {
  flex: 0 0 calc(100vw - 240px); width: calc(100vw - 240px); height: 100vh;
  scroll-snap-align: start; overflow-y: auto;
  padding: 32px 48px; border-radius: 0; margin: 0; box-shadow: none;
  border-right: 1px solid var(--border); min-height: auto;
}
.section-intro { padding: 48px 56px; }
.plotly-chart { max-height: 45vh; }
.nav-progress {
  position: sticky; bottom: 0; background: #1E293B; padding: 12px 20px;
  border-top: 1px solid #334155;
}
.nav-progress .slide-counter { font-size: 11px; color: #64748B; margin-bottom: 6px; }
.nav-progress .progress-track {
  width: 100%; height: 3px; background: #334155; border-radius: 2px; overflow: hidden;
}
.nav-progress .progress-fill {
  height: 100%; background: var(--accent); transition: width 0.3s ease;
}
```

### Sidebar nav template

```html
<nav>
  <div class="logo">{report_title}</div>
  {for section in sections}
    <div class="section-label">{section.label}</div>
    {for slide in section.slides}
      <a href="#{slide.id}">{slide.nav_label or slide.title}</a>
    {endfor}
  {endfor}
  {if layout == "horizontal"}
    <div class="nav-progress">
      <div class="slide-counter">Slide <span id="current-slide">1</span> of {total_slides}</div>
      <div class="progress-track"><div class="progress-fill" id="progress-fill" style="width: 0%"></div></div>
    </div>
  {endif}
</nav>
```

### Title slide template

```html
<div id="slide-title" class="slide" style="background: linear-gradient(135deg, #1E293B 0%, #334155 100%); color: #F8FAFC;">
  <h1 style="font-size: 44px; font-weight: 800; color: #F8FAFC; margin-bottom: 12px;">{title}</h1>
  <p style="font-size: 18px; color: #94A3B8; margin-bottom: 32px; max-width: 700px;">{subtitle}</p>
  <p style="font-size: 13px; color: #64748B;">{date} · {layout} layout</p>
</div>
```

### Section-intro template

```html
<div id="{slide.id}" class="section-intro">
  <p style="font-size: 12px; color: #64748B; letter-spacing: 2px; text-transform: uppercase; margin-bottom: 8px;">{section_label}</p>
  <h2>{title}</h2>
  <p>{subtitle}</p>
</div>
```

### Content slide template

```html
<div id="{slide.id}" class="slide">
  <h2>{title}</h2>
  {if subtitle}<p class="subtitle">{subtitle}</p>{endif}
  {if kpis}
    <div class="kpi-row">
      {for kpi in kpis}
        <div class="kpi-card">
          <div class="kpi-value {kpi.color or ''}">{kpi.value}</div>
          <div class="kpi-label">{kpi.label}</div>
          {if kpi.delta}<div class="kpi-delta">{kpi.delta}</div>{endif}
        </div>
      {endfor}
    </div>
  {endif}
  {if chart}
    <div id="chart-{slide.id}" class="plotly-chart"></div>
  {endif}
  {if table}{table_html}{endif}
  {if body_md}<div>{body_md_rendered}</div>{endif}
  {if so_what}<div class="so-what">{so_what}</div>{endif}
  {if source}<p class="source-note">{source}</p>{endif}
</div>
```

### Recommendations slide template

```html
<div id="{slide.id}" class="slide">
  <h2>{title}</h2>
  {for rec in recs}
    <div class="rec-card">
      <div class="rec-number">{rec.number}</div>
      <div>
        <div class="rec-action">{rec.action}</div>
        <div class="rec-rationale">{rec.rationale}</div>
      </div>
    </div>
  {endfor}
  {if source}<p class="source-note">{source}</p>{endif}
</div>
```

### Plotly init script

```html
<script>
  // Render every chart in the spec
  {for chart in charts}
    Plotly.newPlot('chart-{chart.slide_id}', {chart.data_json}, {chart.layout_json}, {responsive: true, displayModeBar: false});
  {endfor}

  // Sidebar nav active-link tracking (vertical only)
  {if layout == "vertical"}
    const links = document.querySelectorAll('nav a');
    const slides = document.querySelectorAll('.slide, .section-intro');
    function updateActive() {
      let current = '';
      slides.forEach(s => {
        const rect = s.getBoundingClientRect();
        if (rect.top < window.innerHeight / 2) current = s.id;
      });
      links.forEach(a => {
        a.classList.toggle('active', a.getAttribute('href') === '#' + current);
      });
    }
    window.addEventListener('scroll', updateActive);
    updateActive();
  {endif}

  // Horizontal scroll progress bar
  {if layout == "horizontal"}
    const main = document.querySelector('main');
    const fill = document.getElementById('progress-fill');
    const counter = document.getElementById('current-slide');
    const totalSlides = {total_slides};
    main.addEventListener('scroll', () => {
      const max = main.scrollWidth - main.clientWidth;
      const pct = max ? (main.scrollLeft / max * 100) : 0;
      fill.style.width = pct + '%';
      const i = Math.round(main.scrollLeft / main.clientWidth) + 1;
      counter.textContent = Math.min(i, totalSlides);
    });
  {endif}
</script>
```

### Mandatory interactivity components (CSS additions)

Append these to the CSS block:

```css
/* Drill-down panel */
.dc-panel {
  display: none; background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 20px; margin-top: 16px;
}
.dc-panel.open { display: block; animation: slideDown 0.2s ease; }
@keyframes slideDown {
  from { opacity: 0; transform: translateY(-8px); }
  to { opacity: 1; transform: translateY(0); }
}
.dc-panel h3 {
  font-size: 15px; font-weight: 600; margin-bottom: 12px;
  display: flex; align-items: center; gap: 8px;
}
.dc-panel h3 .close-btn {
  margin-left: auto; cursor: pointer; color: var(--text-sec);
  font-size: 18px; line-height: 1;
}
.dc-panel h3 .close-btn:hover { color: var(--text); }
.dc-tabs { display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }
.dc-tab {
  padding: 4px 12px; font-size: 12px; border-radius: 16px;
  background: white; border: 1px solid var(--border); cursor: pointer;
  font-weight: 500; transition: all 0.1s;
}
.dc-tab:hover { border-color: var(--accent); }
.dc-tab.active { background: var(--accent); color: white; border-color: var(--accent); }
.dc-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.dc-table th { text-align: left; padding: 8px 12px; background: #E2E8F0;
  font-weight: 600; font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; }
.dc-table td { padding: 8px 12px; border-bottom: 1px solid var(--border); }
.dc-table tr:hover { background: #F1F5F9; }
.dc-hint { font-size: 11px; color: var(--text-sec); margin-top: 6px; font-style: italic; }

/* View-toggle pill group */
.ot-toggle {
  display: inline-flex; gap: 0; border: 1px solid var(--border);
  border-radius: 6px; overflow: hidden; margin-bottom: 12px;
}
.ot-toggle button {
  padding: 6px 14px; font-size: 12px; font-weight: 600; border: none;
  background: var(--surface); color: var(--text-sec);
  cursor: pointer; transition: all 0.15s;
}
.ot-toggle button:not(:last-child) { border-right: 1px solid var(--border); }
.ot-toggle button.active { background: var(--accent); color: white; }
.ot-toggle button:hover:not(.active) { background: #E2E8F0; }

/* Glossary term hover tooltip */
.term {
  border-bottom: 1px dotted var(--text-sec); cursor: help; position: relative;
}
.term:hover::after {
  content: attr(data-glossary);
  position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%);
  background: #1E293B; color: white; padding: 8px 12px; border-radius: 6px;
  font-size: 12px; line-height: 1.4; white-space: normal; width: max-content;
  max-width: 320px; z-index: 1000; box-shadow: 0 4px 12px rgba(0,0,0,0.2);
  margin-bottom: 6px; font-weight: normal;
}
.term:hover::before {
  content: ''; position: absolute; bottom: 100%; left: 50%; transform: translateX(-50%);
  border: 6px solid transparent; border-top-color: #1E293B;
  z-index: 1000; margin-bottom: -6px;
}

/* Glossary slide */
.glossary-grid {
  display: grid; grid-template-columns: 200px 1fr; gap: 12px 24px;
  font-size: 14px; line-height: 1.6; margin-top: 16px;
}
.glossary-grid dt { font-weight: 700; color: var(--text); }
.glossary-grid dd { color: var(--text-sec); }
.glossary-grid dt:not(:first-of-type),
.glossary-grid dd:not(:nth-of-type(1)) {
  border-top: 1px solid var(--border); padding-top: 12px;
}

/* Help overlay */
.help-btn {
  position: fixed; bottom: 24px; right: 24px; width: 40px; height: 40px;
  border-radius: 50%; background: var(--accent); color: white;
  display: flex; align-items: center; justify-content: center;
  font-size: 20px; font-weight: 700; cursor: pointer; z-index: 9999;
  box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
  transition: transform 0.15s;
}
.help-btn:hover { transform: scale(1.1); }
.help-overlay {
  display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%;
  background: rgba(0, 0, 0, 0.5); z-index: 9998; align-items: center; justify-content: center;
}
.help-overlay.open { display: flex; }
.help-modal {
  background: white; border-radius: 12px; padding: 32px; max-width: 480px; width: 90%;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.3);
}
.help-modal h3 {
  font-size: 18px; font-weight: 700; margin-bottom: 16px;
  display: flex; justify-content: space-between; align-items: center;
}
.help-modal .close-btn {
  cursor: pointer; color: var(--text-sec); font-size: 24px; line-height: 1;
}
.help-modal ul { list-style: none; padding: 0; }
.help-modal li {
  padding: 10px 0; border-bottom: 1px solid var(--border); font-size: 14px;
  display: flex; align-items: flex-start; gap: 12px;
}
.help-modal li:last-child { border-bottom: none; }
.help-modal li::before {
  content: '→'; color: var(--accent); font-weight: 700; flex-shrink: 0;
}
```

### Drill-down panel template

```html
<!-- inside a slide that has a chart -->
<div id="chart-{slide.id}" class="plotly-chart"></div>
<p class="dc-hint">Click any bar to drill down</p>
<div class="dc-panel" id="dc-{slide.id}">
  <h3><span id="dc-{slide.id}-title">Details</span> <span class="close-btn" onclick="closeDC('dc-{slide.id}')">&times;</span></h3>
  <div class="dc-tabs" id="dc-{slide.id}-tabs"></div>
  <div id="dc-{slide.id}-table"></div>
</div>
```

JS pattern:

```js
// Drill-down data stored as: DC = {slide_id: {segment_value: {tab_key: {label, rows}, ...}}}
const DC = {
  'conversion_by_segment': {
    'Segment A': {
      'by_tier': {label: 'By Tier', rows: [{tier: 'Enterprise', rate: '4.2%', n: 88}, ...]},
      'by_window': {label: 'By Window', rows: [{window: '60d', rate: '2.8%'}, ...]},
    },
    'Segment B': {...},
    'Segment C': {...},
  },
};
function openDC(slideId, segmentKey) {
  const data = DC[slideId][segmentKey];
  if (!data) return;
  const panel = document.getElementById('dc-' + slideId);
  const titleEl = document.getElementById('dc-' + slideId + '-title');
  const tabsEl = document.getElementById('dc-' + slideId + '-tabs');
  const tableEl = document.getElementById('dc-' + slideId + '-table');
  titleEl.textContent = segmentKey + ' — Drill Down';
  tabsEl.innerHTML = '';
  Object.entries(data).forEach(([key, tab], i) => {
    const btn = document.createElement('div');
    btn.className = 'dc-tab' + (i === 0 ? ' active' : '');
    btn.textContent = tab.label;
    btn.onclick = () => {
      tabsEl.querySelectorAll('.dc-tab').forEach(t => t.classList.remove('active'));
      btn.classList.add('active');
      renderDCTable(tableEl, tab.rows);
    };
    tabsEl.appendChild(btn);
  });
  // Default first tab
  renderDCTable(tableEl, Object.values(data)[0].rows);
  panel.classList.add('open');
}
function closeDC(panelId) {
  document.getElementById(panelId).classList.remove('open');
}
function renderDCTable(container, rows) {
  if (!rows.length) { container.innerHTML = '<p>No data</p>'; return; }
  const cols = Object.keys(rows[0]);
  let html = '<table class="dc-table"><thead><tr>';
  cols.forEach(c => html += `<th>${c}</th>`);
  html += '</tr></thead><tbody>';
  rows.forEach(r => {
    html += '<tr>';
    cols.forEach(c => html += `<td>${r[c]}</td>`);
    html += '</tr>';
  });
  html += '</tbody></table>';
  container.innerHTML = html;
}
// Wire up Plotly click → openDC for every chart that has drilldown:
document.getElementById('chart-posrates').on('plotly_click', e => {
  const segment = e.points[0].x;  // or .y for horizontal bars
  openDC('posrates', segment);
});
```

### View-toggle template

```html
<!-- above a chart -->
<div class="ot-toggle">
  <button class="active" onclick="setView('chart-id', 'all')">All</button>
  <button onclick="setView('chart-id', 'net_new')">Net New</button>
  <button onclick="setView('chart-id', 'cross_sell')">Cross-sell</button>
</div>
<div id="chart-id" class="plotly-chart"></div>
```

JS:

```js
const VIEWS = {
  'chart-id': {
    'all': {data: [...], layout: {...}},
    'net_new': {data: [...], layout: {...}},
    'cross_sell': {data: [...], layout: {...}},
  },
};
function setView(chartId, viewKey) {
  const v = VIEWS[chartId][viewKey];
  Plotly.react(chartId, v.data, v.layout);
  // Update active button styling
  const buttons = document.querySelectorAll(`[onclick*="setView('${chartId}'"]`);
  buttons.forEach(b => b.classList.toggle('active', b.getAttribute('onclick').includes(`'${viewKey}'`)));
}
```

### Hover tooltip template

Wrap any technical term in a slide:

```html
<span class="term" data-glossary="Precision-Recall AUC: a model evaluation metric for imbalanced classification. Higher = better at identifying positives without false alarms.">PR-AUC</span>
```

The CSS handles the tooltip rendering on hover. Definition text is duplicated between `data-glossary` (for hover) and the glossary slide (for end-of-report reference).

### Glossary slide template

Always emit as the last slide. Even if all terms have hover tooltips, the glossary section is the canonical reference.

```html
<div id="slide-glossary" class="slide">
  <h2>Glossary</h2>
  <p class="subtitle">Definitions for technical terms used throughout this report</p>
  <dl class="glossary-grid">
    <dt>MAE</dt>
    <dd>Mean Absolute Error. The average absolute difference between predicted and observed values. Lower = more accurate.</dd>
    <dt>Conversion rate</dt>
    <dd>The percentage of accounts in the population that completed the target action within the measurement window.</dd>
    <dt>Cohort</dt>
    <dd>A group of accounts that share a defining attribute (signup month, plan tier, region) and are tracked together over time.</dd>
    <!-- ... -->
  </dl>
</div>
```

### Help overlay template

Always emit. Floating button bottom-right + hidden modal.

```html
<button class="help-btn" onclick="document.getElementById('help-overlay').classList.add('open')">?</button>

<div class="help-overlay" id="help-overlay" onclick="if(event.target===this)this.classList.remove('open')">
  <div class="help-modal">
    <h3>How to use this report <span class="close-btn" onclick="document.getElementById('help-overlay').classList.remove('open')">&times;</span></h3>
    <ul>
      <li>Click any bar or chart segment to open a drill-down panel with detail breakdowns.</li>
      <li>Use the All / segment toggles above charts to filter the view.</li>
      <li>Hover any underlined term for a plain-English definition.</li>
      <li>The full glossary is at the end of the report.</li>
      <li>Use the sidebar nav to jump between sections.</li>
    </ul>
  </div>
</div>
```

## Anti-patterns

- **Static-only HTML** — if the report is just slides rendered as HTML with zero interactivity, you've shipped a worse PDF. Every aggregation chart must have drill-down. Every chart with a filter dimension must have toggles. Every technical term must have a hover tooltip. **BLOCKER: report has zero `dc-panel` or `ot-toggle` elements.**
- **Technical terms without glossary entries** — every term used in a `<span class="term">` must appear in the glossary slide, and vice versa. **BLOCKER: mismatch.**
- **Slides packed with detail upfront** — first impression should show the headline. Detail goes behind interaction. **BLOCKER: any slide with > 1 dense table or > 6 KPIs without grouping.**
- **External CSS files** — use only inline `<style>`. The report must be one self-contained file.
- **External fonts (Google Fonts, etc.)** — use system font stack only. External fonts break offline / corp-network distribution.
- **Bare `<img>` tags pointing at network URLs** — embed images as base64 data URIs, or skip the image.
- **Multiple Plotly script tags** — load Plotly once at the top.
- **Slide IDs that aren't kebab-case** — must match `<a href="#...">` exactly. Use `slide-foo-bar`.
- **Content slides without source citation** — every finding cites its data source.
- **Horizontal layout with > 12 sections** — becomes unwieldy; use vertical instead.

## Continuous improvement

When stakeholder feedback surfaces a new pattern or breaks an existing one, log a correction via the `log-correction` skill with `category: html-output`. The `html-output-patterns` skill is the canonical rule-set; this agent follows whatever the skill says. New rules accumulate in the skill; this agent's templates evolve to match.
