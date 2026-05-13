# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased] - fork additions

### Added (running-rikishi fork)

- Autoresearch framework for autonomous ML experimentation above bayesian-tuning
  - New skill `autoresearch-loop` codifying the canonical architectural rules: hill-climb-from-best, in-loop smoke test, token-budget headroom (48K) for full-file rewrites, feature-name lint pre-smoke, per-iter pipeline snapshots, Optuna retune with LLM-curated candidate selection, interpretability gate, and verdict criteria
  - New agent `ml-autoresearch` that runs the framework on any harness/pipeline pair
  - New module `helpers/autoresearch/retune.py`: post-loop Optuna retune of LLM-curated top-N candidates. LLM picks for architectural diversity + tuning upside (not just metric rank — top-N-by-metric tends to surface near-duplicate cousins, retuning them produces redundant numbers). Monkey-patches xgb/lgbm constructors so pipelines don't need a tuning hook. Per-trial events logged to `runner_log.jsonl` so multi-hour runs are observable. KeyboardInterrupt handler persists partial results so runs can be safely halted. Incremental leaderboard writes after each candidate.
  - Runner safety rails: $250 cost cap (production ML research budget, not POC), `max_completion_tokens=48000` for frontier-model reasoning headroom, env-only API key reads, path sanitization on logs, response objects never serialized to disk
  - **Battle-test result on IEEE-CIS Fraud Detection (public Kaggle): 0.9406 ROC-AUC — Kaggle gold tier (≥0.94)**, achieved by autoresearch loop + Optuna retune. Beats Optuna-tuned baseline (0.9218) by +2.04%. Single XGB+LGBM ensemble, ~$30 total LLM cost, ~10h wall-clock. All agent-engineered features pass interpretability gate (3 of top-15 SHAP features are plain-English agent-engineered; remaining 12 are Vesta-anonymized features that ship with the dataset — agent does not introduce opacity).
  - Inspired by https://github.com/karpathy/autoresearch, adapted for tabular supervised ML.
- HTML report deliverable as a parallel to Marp/PDF decks
  - New skill `html-output-patterns` with severity-graded rules for interactivity, progressive disclosure, glossary, and self-containment
  - New agent `html-report-maker` (pipeline_step: 16, parallel to deck-creator)
  - Workflow integration: step 16 routes by `{{FORMAT}}` (`marp` default → Deck Creator; `html` → HTML Report Maker)
  - New system variables: `{{FORMAT}}`, `{{LAYOUT}}`, `{{REPORT_TITLE}}`
  - Visual Design Critic gains an HTML Report Review Path with self-containment, drill-down coverage, glossary completeness, and anchor-resolution checks
  - Working demo at `docs/example_report_vertical.html` using synthetic data
  - Guide at `docs/html-output-guide.md` covering when to use HTML vs Marp, layout selection, customization, and continuous-improvement loop
- Earlier in this fork: ML framework (15 skills + 5 build agents + production reference + 7 BUILD_RESULTS templates) — see prior fork PR

## [2.0.0] - 2026-02-23

### Added
- Interactive onboarding: `/setup` interview learns role, data sources, business context
- Knowledge infrastructure: corrections, learnings, query archaeology, organization knowledge
- Self-learning loop: feedback capture, correction logging, proven SQL pattern retrieval
- YAML-based brand theming with WCAG-compliant palettes (`themes/brands/`)
- Pipeline run tracking: `/runs` to list, inspect, compare, and clean up runs
- Comms drafter agent for Slack/email/exec summary output
- Business context system: glossary, metrics, products, teams per organization
- Notion ingest skill for importing business context from Notion workspaces
- Entity resolver for cross-dataset disambiguation
- 8 new slash commands: `/setup`, `/runs`, `/business`, `/log-correction`, `/architect`, `/notion-ingest`, `/setup-dev-context`, `/compare-datasets`
- 9 new skills: archaeology, feedback-capture, log-correction, setup, setup-dev-context, runs, business, notion-ingest, architect
- 606 tests with synthetic fixtures (no external data dependencies)
- Health check system for data connectivity diagnostics
- Schema migration helpers for knowledge file versioning

### Changed
- Fully dataset-agnostic: agents resolve tables/columns from active manifest, not hardcoded names
- Removed bundled NovaMart dataset — bring your own data with `/connect-data`
- Removed legacy setup scripts (`download-data.sh`, `build-duckdb.sh`) and setup docs
- Updated CLAUDE.md with V2 workflow, agent index, and skill table
- Python requirement bumped to 3.10+

### Fixed
- Pipeline resume reliability improved with persistent state management
- Chart palette now validates WCAG contrast ratios

## [1.0.0] - 2026-02-19

### Added
- Initial public release
- 17 specialized analysis agents with DAG-based parallel execution
- 30 auto-applied skills (question framing, data quality, visualization, validation)
- 14 slash commands for interactive use
- Example e-commerce dataset schema (13 tables)
- Tiered data system: Tier 1 in git, Tier 2 via GitHub Releases
- Setup scripts: `setup.sh`, `download-data.sh`, `build-duckdb.sh`
- Multi-warehouse support: DuckDB, MotherDuck, Postgres, BigQuery, Snowflake
- SWD-styled chart generation with collision detection
- Marp slide deck creation with branded HTML components
- 4-layer validation framework with A-F confidence scoring
- Knowledge system for cross-session memory
- Metric dictionary with standardized definitions
- Analysis archive with pattern extraction
