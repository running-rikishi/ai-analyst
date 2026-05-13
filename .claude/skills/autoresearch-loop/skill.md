# Skill: Autoresearch Loop

## Purpose

Standards for an autonomous experimentation loop above `bayesian-tuning`. Where bayesian-tuning optimizes hyperparameters within a fixed search space, autoresearch widens the search space — algorithm choice, feature engineering, target reformulation, CV strategy — by having an LLM agent edit a mutable pipeline file across many experiments. The skill encodes the architectural rules; the `ml-autoresearch` agent applies them.

Inspired by [Karpathy's autoresearch](https://github.com/karpathy/autoresearch), adapted for tabular supervised ML.

## When to Use

- After `bayesian-tuning` has converged on a fixed feature set and you want to explore structurally different configurations
- When the value of better hyperparameter search would be small relative to a wider search space (different algorithm, novel features, alternate target)
- For tabular supervised ML only — classification, regression, ranking on tree-based or linear algorithms. Out of scope: deep learning, LLM fine-tuning, computer vision (see `agents/FRAMEWORK_GAPS.md`)

Pairs with: `bayesian-tuning`, `feature-hygiene`, `shap-rep-explanations`, `forward-chaining-cv`, `ml-baseline-gate`.

## Foundational Principle: Hill-Climb From Best

> The runner maintains a `best_pipeline.py` snapshot. Each iteration starts from the known-best pipeline, not from the previous iter's output. Failed or regression iterations revert to best before the next iteration.

This is the single most load-bearing architectural rule. Without it, the agent drifts away from local maxima as it explores, loses good work to subsequent worse iterations, and produces flat-after-iter-N trajectories. With it, improvements compound — each new best becomes the next starting point.

**Severity: BLOCKER.** A runner without best-snapshot management is structurally broken.

## Three-File Architecture

| File | Role | Mutability |
|---|---|---|
| `harness.py` (or `harness_*.py`) | Fixed — data load, train/test or CV split, scorer function | Agent never edits |
| `pipeline.py` (or `pipeline_*.py`) | Mutable — feature engineering + model. Must export `build_pipeline(X_train, y_train) → estimator` | Agent rewrites every iteration |
| `program.md` | Human-edited instructions for the agent — goal, constraints, severity gates | Iterated by humans, not the agent |
| `best_pipeline.py` | Runner-managed snapshot of highest-scoring pipeline | Runner writes; agent never reads it directly (it appears as the "current pipeline" each iter) |

Severity: BLOCKER if the harness exposes anything mutable to the agent, or if program.md includes specific implementation suggestions (CORR-007: handing the agent recipes biases the result).

## Mandatory Runner Components

### 1. Hill-climb-from-best (CORR-008)

- On run start: if `best_pipeline.py` doesn't exist, snapshot current `pipeline.py` to it
- After each iteration:
  - If new metric > best: copy `pipeline.py` to `best_pipeline.py` (new best)
  - Else: revert `pipeline.py` to `best_pipeline.py` (next iter starts from best)
- Severity: BLOCKER

### 2. In-loop smoke test (CORR-009)

Before running the full experiment, test the agent's proposed `pipeline.py` on a tiny subset (~1000 rows):

- Call `build_pipeline(X_tiny, y_tiny)` → fit
- Call `estimator.predict_proba(X_tiny_holdout)` → predict
- If any exception: log as failed experiment with the error, revert `pipeline.py` to best, skip full run

Speed: ~3–10 sec vs 30–90 sec for full xgboost fit. Catches `KeyError`, `UnboundLocalError`, type-mismatch bugs cheaply. Severity: HIGH.

### 3. Token-budget headroom (CORR-010)

The agent's required output is "full `pipeline.py` rewrite + summary line." As the pipeline accumulates features iteratively, the rewrite grows in token count. Default to **`max_completion_tokens=48000`** — enough for pipelines up to ~50 KB AND substantial reasoning-token headroom for frontier models like gpt-5.5 and gpt-5.5-pro that use internal chain-of-thought tokens.

If the agent uses a reasoning model (gpt-5.x, o-series), reasoning tokens count against `max_completion_tokens`. Account for this — model emits visible code AFTER its internal reasoning. 48K handles the typical frontier-model + grown-pipeline combination.

When parse fails (no code block or summary in response), log diagnostics: `finish_reason`, response head/tail, char count, presence of fence/summary markers. Without this, parse failures are silent black boxes.

Severity: HIGH.

### 4. Feature-name lint pre-smoke

Before the smoke test fires, scan the agent's proposed pipeline for opacity-marker feature names. Reject with a `lint_failed` event and revert to best if any pattern matches:

- `feat_\d+` — numeric-suffix opaque names (e.g., `feat_42`)
- `\bpoly_` — polynomial features
- `\bembed_` — embedding features
- `\bhash_` — hash encoding
- `\binteraction_` — anonymous interaction markers

Intentionally NOT in patterns: `_x_` (e.g., `age_x_sex`) — these are interpretable interactions and should be allowed.

Why: program.md's "forbidden at all times" rules are instructions to the agent. Agents follow them ~99% of the time but drift on long runs. The lint converts those instructions into runtime enforcement.

Severity: HIGH. False-positive cost (one wasted iter from a legitimate name matching) is asymmetric vs the false-negative cost (shipping an opaque feature past the gate).

### 5. Cost cap + path sanitization (CORR-008 to CORR-010 implications)

- Hard `MAX_RUN_COST_USD` cap (default **$30** — safe for first-time users; raise via `--max-cost 250` for production ML research runs), enforced every iteration from API usage fields. The default is intentionally conservative so a script bug can't burn budget overnight.
- Cap precedence: cost cap and time cap (`--max-hours`, default 4h) both halt at the next iteration boundary. Whichever fires first wins.
- All log writes pass through path sanitization (strip `$HOME` to `<HOME>`)
- API key read from environment only — never as a parameter, never logged, never in source
- Response objects never serialized to disk — extract specific usage fields only

Severity: BLOCKER. This is the first framework component with write access to user spending. CORR-015: defaulting to $250 was too permissive for unattended runs by colleagues; the framework now requires explicit opt-in for higher caps.

### 6. Per-iter pipeline snapshots

After every successful iteration, copy the current `pipeline.py` to `iter_snapshots/iter_NNNN.py` BEFORE the hill-climb-from-best revert. Required for the retune step (below) to access non-best-but-high-quality candidates after the loop completes.

Without per-iter snapshots, only the latest best survives — retune can only tune the loop winner, losing the ability to tune diverse near-winners that an LLM might pick for retune diversity.

Severity: HIGH (required by retune step).

## Program.md Rules

The human-edited `program.md` is the agent's brief. Critical rules:

### Allowed sections

- **Goal** — what metric to maximize, evaluated how
- **Dimensions you can vary** — algorithm class, feature engineering, imputation, calibration, hyperparameters
- **Hard constraints** — what's off-limits (modifying harness, target leakage, imports outside allowlist)
- **Interpretability constraints** — see next section
- **Output format** — fenced python block + SUMMARY line
- **Severity gates** — KEEP/DISCARD/BLOCKER rules

### Forbidden sections (CORR-007)

- ❌ "Ideas to try" — specific implementation suggestions
- ❌ Numbered recipes — "first add X, then Y, then Z"
- ❌ Citations of canonical solutions to the dataset

These bias the evaluation. The agent's job is to generate ideas from the goal + constraints. Handing it recipes proves the framework architecture but not its ability to explore.

Severity: BLOCKER. Pre-commit check that any new program.md does not contain a numbered list under headings like "Ideas to try", "Suggestions", or "Recipes".

## Interpretability Constraint

**Production models must be SHAP-explainable to stakeholders. The framework's value is in production-shippable models, not metric-best-at-any-cost.**

Every feature the agent creates must be:

1. **Plain-English nameable** — a stakeholder can guess what each feature represents from its name alone
2. **Domain-meaningfully defined** — explainable in one sentence why this feature might relate to the outcome
3. **SHAP-attribution-survivable** — when SHAP is computed on the trained model, the agent can articulate why each top-10 feature has the importance and direction it does

### Forbidden at all times (BLOCKER)

- Auto-generated polynomial / quadratic / kitchen-sink interaction terms (e.g., `PolynomialFeatures(degree=3)`)
- Cross-feature multiplications with no semantic relationship (e.g., `age × favorite_book × toenail_length`)
- Learned/autoencoder embedding features whose meaning isn't articulable
- Hash-encoded features for high-cardinality categoricals — use target encoding or frequency encoding with explanation
- Any feature whose name you'd struggle to translate into one plain-English sentence

Reference: pair with `shap-rep-explanations` (canonical interpretability rules) and `feature-hygiene` (drop-list rules).

### When the dataset itself has anonymized features

Some datasets ship with pre-anonymized features (e.g., Kaggle competitions where the data owner obfuscates column names to prevent re-identification — `V1`...`V339`, `C1`...`C14`, etc.). The interpretability gate's job is to prevent the AGENT from adding new opacity, not to remove the dataset's inherent opacity.

If SHAP top-10 includes anonymized features that came with the dataset:
- The gate still passes IF the agent's added features are interpretable
- Document the dataset's opacity in the final report as a caveat, not a framework failure
- Note that for user-controlled datasets, this caveat doesn't apply

## Optuna Retune

After the autoresearch loop completes, retune top-N candidates with Optuna over their hyperparameters. Closes the methodological gap: the bayesian-tuning baseline gets Optuna; without retune, the autoresearch winner doesn't, making the comparison `(baseline features, tuned hyperparams)` vs `(autoresearch features, agent-default hyperparams)` — systematically understating autoresearch's value.

### LLM-curated picks (CORR-013-aware)

Top-N by metric tends to surface near-duplicate cousins (variations on the same ensemble + small FE tweak). Retuning them produces N near-identical numbers — methodological dead weight.

Better: show the LLM the top-20 leaderboard + each candidate's architectural fingerprint, ask for N picks (default 3) that maximize information — architectural diversity + tuning upside. Falls back to top-N by metric if the LLM call fails.

### Tuning approach: monkey-patch

Each Optuna trial monkey-patches `XGBClassifier.__init__` (and `LGBMClassifier.__init__` if present) to overlay Optuna-suggested params on whatever the agent's pipeline used. Avoids requiring pipelines to expose a tuning hook, but means we tune model params only — FE and other pipeline structure stay frozen.

Limitation: only XGB and LGBM are auto-tuned. CatBoost, sklearn LR, etc. skip retune and report loop-output metric.

### Per-trial logging (CORR-011)

Pass `callbacks=[on_trial_end]` to `study.optimize()`. The callback writes one `retune_trial_complete` event per trial with `{iter, trial, metric, elapsed_s, state, phase}` to `runner_log.jsonl`. Without per-trial events, multi-hour retunes are completely opaque to observers.

Plus per-candidate events: `retune_candidate_start` and `retune_candidate_complete` with metadata.

### KeyboardInterrupt handler (CORR-012)

Wrap `study.optimize()` in `try/except KeyboardInterrupt`. On interrupt, persist the partial result (best params + best metric + completed-trial-count) before re-raising. Without this, halting a multi-hour retune mid-flight loses all hyperparameter discoveries.

Plus incremental persistence: write `retuned_leaderboard.md` after EVERY successful candidate, not just at the end. So even if the final candidate is killed, the leaderboard reflects completed candidates.

### Default settings

- `top_n = 3` (LLM-curated for diversity, not 5 mechanical-by-metric — CORR-013 finding)
- `trials_per_candidate = 50` (TPE convergence; smaller searches plateau early)
- Search space: 9 XGB params + 8 LGBM params, narrow ranges proven on IEEE-CIS Fraud

### Don't widen the search space (CORR-013)

Before widening any Optuna dimension's bounds or adding new dimensions, check whether the prior best params hit the boundary:
- If best params are interior (within 5-95% of range), widening will hurt — more bad regions, same trial budget
- If best params are at boundary (within ~5% of edge), widening is justified

Empirical lesson from a failed widening: a study widened `reg_lambda` 10→50 and added `gamma` + `max_delta_step` while prior best had `reg_lambda=6.88` (interior to the original range). Result: regressed from 0.9393 to 0.9383 over 11 trials. Running the same narrow space with a new seed for more trials hit 0.9406 at trial 4. Lesson: more trials in the proven-good space beats widening into bad regions.

## Verdict Criteria

The autoresearch run's output must pass BOTH gates:

| Gate | Pass condition |
|---|---|
| **Metric gate** | Best autoresearch result beats bayesian-tuning-only baseline by ≥1% on the primary metric |
| **Interpretability gate** | Top-10 features by `mean(|SHAP|)` all have plain-English names that a stakeholder could understand without reading the code |

Verdict labels:

- ✅ **VALUE** — passes BOTH gates AND the gain came from changes Optuna structurally couldn't have made (algorithm swap, target reformulation, semantic feature transformation, CV strategy)
- ⚠️ **METRIC WIN BUT NOT INTERPRETABLE** — passes metric gate but fails interpretability gate. DO NOT SHIP the winning pipeline; demote to next leaderboard entry and re-check.
- ⚠️ **PARTIAL** — matched or marginally beat baseline; useful experiment categories surfaced but no clear win
- ❌ **NO ARCHITECTURAL DELTA** — autoresearch failed to beat tuning-alone OR all gains were within Optuna's reach
- ⚠️ **SATURATED MEMORIZATION** — both methods converge near public-leaderboard ceiling; cannot distinguish architectural value from training-data memorization

## Framing Discipline

The framework's value prop is **wider search space than bayesian-tuning**. That claim is architectural and survives any question about provenance of specific ideas — both methods sit on the same agent and same training data, so memorization helps both equally. The differential is the architectural delta.

What this skill's outputs may claim:
- "On dataset X, autoresearch's best result beat bayesian-tuning-only's best by Y%"
- "Useful when you've already tuned hyperparameters and want to explore structurally different configurations"

What this skill's outputs do NOT claim (not because forbidden — because they're not the value prop):
- "Discovers novel ideas autonomously"
- "Invents techniques no human has tried"
- "AI-driven ML research"

These claims shift the value prop from "tool that helps you find better models" (defensible) to "agent that does research" (a different and harder product). Stay scoped.

## Anti-patterns

- **No best-snapshot management** — runner overwrites pipeline.py without preserving the best. Agent drifts. BLOCKER.
- **No smoke test** — every failed agent-generated pipeline burns full xgboost compute. HIGH.
- **Too-tight token budget** — `max_completion_tokens=8000` truncates rewrites of growing pipelines. HIGH.
- **API key in source** — `Anthropic(api_key=...)` or `OpenAI(api_key=...)` parameter. BLOCKER.
- **Ideas-to-try in program.md** — biases the agent toward known solutions. BLOCKER.
- **Opaque features in winning pipeline** — `PolynomialFeatures`, hash encoding, embeddings. BLOCKER on shipping.
- **Cost-per-iter not tracked** — no enforcement of `MAX_RUN_COST_USD`. BLOCKER (the runner has write access to your wallet).
- **Saturated dataset** — Titanic, Iris, MNIST. Tests architecture but not novel-idea capability. WARNING.

## Connections to Other Skills

- `bayesian-tuning` — autoresearch sits above this; runs after tuning has converged
- `feature-hygiene` — pipeline.py inherits hygiene rules (drop IDs, dates, leakage-prone columns)
- `forward-chaining-cv` / `oot-window-selection` — harness reuses CV patterns
- `shap-rep-explanations` — the interpretability gate references this skill's standards
- `ml-baseline-gate` — autoresearch's verdict criteria parallel this skill's ship/demote logic
- `log-correction` — when autoresearch surfaces new patterns, log them with `category: autoresearch`

## Provenance

Every rule traces to a real correction surfaced during iterative testing:

| Rule | Source |
|---|---|
| No "Ideas to try" in program.md | CORR-007 |
| Hill-climb-from-best architecture | CORR-008 (critical) |
| In-loop smoke test | CORR-009 |
| 48K token budget + parse diagnostics | CORR-010 |
| Per-trial logging in retune | CORR-011 |
| KeyboardInterrupt handler + incremental persistence in retune | CORR-012 |
| Don't widen Optuna space when optima are interior | CORR-013 |
| Interpretability gate | Stakeholder constraint, 2026-05-10 |
| Framing discipline | User discussion on novelty vs. value-prop, 2026-05-10 |
| Dataset-anonymized-features caveat | SHAP audit finding, 2026-05-12 |

Future revisions should extend this skill — don't replace rules. Severity-graded rules accumulate.

## Applying the framework to your problem

This framework is **general**, not template-specific. Two ways to bring your own dataset:

### Fast path: YAML config + harness factory

For most tabular problems, write a YAML config and let the factory build the harness:

```yaml
# configs/your_dataset.yaml
name: "Your dataset"
data_dir: ./data/your_dataset/
main_table: { path: train.csv, format: csv }
target_column: TARGET
id_column: row_id
auxiliary_tables: []       # or list of {path, name, join_key} for multi-table
multi_table: false         # set true for 1:N auxiliary tables (Home Credit style)
split:
  strategy: stratified     # random | time_ordered | stratified
  holdout_frac: 0.20
  random_seed: 42
scorer: roc_auc            # roc_auc | rmse | accuracy | f1
```

Then: `harness = build_harness("configs/your_dataset.yaml")` and you're ready. Two example configs ship: `configs/ieee_cis_fraud.yaml` (single-table, time-ordered) and `configs/home_credit.yaml` (multi-table, stratified). See `helpers/autoresearch/harness_factory.py` for the full schema.

**Single-table mode** (`multi_table: false`): auxiliary tables are pre-joined 1:1 onto main. Agent's `build_pipeline(X_train, y_train)` signature is unchanged.

**Multi-table mode** (`multi_table: true`): auxiliary tables are passed in as raw DataFrames. Agent's signature is `build_pipeline(tables: dict[str, DataFrame], y_train)`. The agent invents cross-table aggregations — this is what makes Kaggle-style multi-table competitions tractable.

### Custom path: hand-write the harness

For datasets too complex for YAML (Snowflake-backed, streaming, custom preprocessing), write your own:

- **`harness.py`** — defines `load_data() → (X_train, y_train, X_holdout, y_holdout)` and `run_experiment(pipeline_path, timeout_seconds) → dict` for YOUR dataset. The harness encodes data shape, train/holdout split logic, and the primary scorer. Whatever scale your data is — 5K rows, 5M rows, panel, cross-sectional, time-aware — the harness adapts to it.
- **`pipeline.py`** (initial) — a minimal default `build_pipeline(X_train, y_train)` for your problem. Could be a default xgboost, a logistic regression, anything that establishes the floor. The agent will iteratively rewrite this.
- **`program.md`** — what the agent should optimize, what's off-limits, severity gates specific to your domain. Reuses the same interpretability constraints (no PolynomialFeatures, etc.) but the goal/dimensions are yours to write.
- **Retune search space** (`helpers/autoresearch/retune.py`) — the default `XGB_SEARCH` / `LGBM_SEARCH` dicts are starting points sized for ~500K-row tabular data. For your problem you may want different ranges (e.g., regression problems often want smaller `learning_rate`, larger `n_estimators`; small datasets want tighter `max_depth`). Override by editing the module or by passing a custom search.

The framework does NOT assume:
- A specific dataset, scale, or domain
- ROC-AUC as the metric (any scorer the harness returns works)
- xgboost as the algorithm (the lint/retune handle xgb + lgbm; other algorithms are tuned at agent-default hyperparams)
- A specific train/test split shape (time-aware, random, stratified, group-aware — the harness owns this)

The framework DOES assume:
- Tabular supervised data (classification, regression, ranking on tree-based or linear algorithms)
- A clean primary metric the harness can compute objectively
- A budget for ~30 min to several hours of unattended runtime + ~$10–$100 of LLM API spend

## Validation (reference benchmark)

End-to-end validation on IEEE-CIS Fraud Detection (public Kaggle, ~590K rows, time-aware 80/20 holdout). This is the proof-point that the framework works at expert-tier on a saturated public dataset; your problem will look different in scale, target, and feature space — that's expected.

| Reference | ROC-AUC | vs result |
|---|---:|---|
| Default xgboost (no tuning, no FE) | 0.8986 | naive baseline |
| Bayesian-tuning baseline (50 Optuna trials, raw features) | 0.9218 | ≈ Kaggle median submission |
| Autoresearch loop best (gpt-5.5, hill-climb-from-best, 56 iters) | 0.9355 | +1.49% over baseline |
| **Autoresearch + Optuna retune (best)** | **0.9406** | **+2.04% over baseline ⭐ Kaggle gold tier (≥0.94)** |
| Kaggle silver tier (top ~5%) | ~0.94 | matched |
| Kaggle 1st place (private LB) | 0.94653 | −0.0059 (months of ensemble + 150+ FE incl. opaque UIDs) |

**Total LLM cost: ~$30. Total wall-clock: ~10h** (4h loop + retune). Single XGB+LGBM ensemble. Interpretability gate: passed — 3 of top-15 SHAP features are agent-engineered plain-English; the remaining 12 are Vesta-anonymized features that ship with the IEEE-CIS Fraud dataset (the agent didn't add opacity; the dataset has inherent opacity).

**Architectural delta is real:** autoresearch's wider search exposed feature-engineering wins (transaction-amount group-deviations, conditional-identity-typicality entropy, leakage-safe temporal density) that Optuna's hyperparameter-only search could not reach. Subsequent retune found tuned hyperparameters (stronger regularization, lower subsample) the agent didn't naturally pick.

**Caveats:**
- Single dataset; n=1 result. More runs needed for stronger claims.
- The gap to Kaggle #1 (0.94653) reflects two factors: (1) scale — winners used 24+ stacked ensembles + days of compute; (2) competition-specific UID-magic features that we deliberately exclude (they wouldn't pass our interpretability gate). On user-controlled datasets, the dataset-opacity factor disappears.
- The framework is best-applied to **tabular ML with clean targets**. For tacit-knowledge expert tasks (legal review, medical judgment), the technique library isn't well-represented in LLM training and autoresearch underperforms human experts substantially.
