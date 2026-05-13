<!-- CONTRACT_START
name: ml-autoresearch
description: Autonomous experimentation loop above bayesian-tuning. Widens the search space (algorithm, features, target, CV) by having an LLM agent edit a mutable pipeline file across many experiments, hill-climbing from the best-so-far snapshot.
inputs:
  - name: DATASET
    type: str
    source: user
    required: true
  - name: PRIMARY_METRIC
    type: str
    source: user
    required: true
  - name: BASELINE_PIPELINE
    type: file
    source: agent:ml-model-train
    required: false
  - name: BUDGET_HOURS
    type: float
    source: user
    required: false
  - name: BUDGET_USD
    type: float
    source: user
    required: false
  - name: MODEL
    type: str
    source: user
    required: false
  - name: HARNESS
    type: file
    source: user
    required: true
  - name: PROGRAM_MD
    type: file
    source: user
    required: false
outputs:
  - path: outputs/autoresearch_{{DATASET_NAME}}_{{DATE}}/experiments.jsonl
    type: jsonl
  - path: outputs/autoresearch_{{DATASET_NAME}}_{{DATE}}/leaderboard.md
    type: markdown
  - path: outputs/autoresearch_{{DATASET_NAME}}_{{DATE}}/best_pipeline.py
    type: python
  - path: outputs/autoresearch_{{DATASET_NAME}}_{{DATE}}/retuned_leaderboard.md
    type: markdown
  - path: outputs/autoresearch_{{DATASET_NAME}}_{{DATE}}/retuned_leaderboard.json
    type: json
  - path: outputs/autoresearch_{{DATASET_NAME}}_{{DATE}}/final_report.md
    type: markdown
depends_on:
  - ml-feature-prep
knowledge_context:
  - .knowledge/datasets/{active}/manifest.yaml
  - .knowledge/corrections/log.yaml
pipeline_step: 7
CONTRACT_END -->

# Agent: ML Autoresearch

Autonomous experimentation loop that widens the search space beyond `bayesian-tuning`. Optuna can tune hyperparameters within a fixed search space; it cannot decide to swap algorithms, redefine the target, change CV strategy, or invent feature transformations. This agent does. Apply the `autoresearch-loop` skill's architectural rules.

## When to use

- After `bayesian-tuning` has converged on a fixed feature set, and you want to explore structurally different configurations
- Before `ml-model-train` if you want broader exploration before standard CV+tuning
- On a real ML build where you have budget for ~30 min to 4 h of unattended runtime

**When NOT to use:**
- Deep learning, LLM fine-tuning, computer vision (out of scope)
- Datasets so small that hyperparameter tuning alone reaches the ceiling (the wider search doesn't help)
- When you need a deterministic build (autoresearch trajectories vary across runs)
- When you can't afford the LLM API cost

## Workflow

### Pre-flight

1. Confirm the harness file follows the protocol: `load_data() → X_train, y_train, X_holdout, y_holdout`, primary scorer locked, no agent-mutable state.
2. Confirm `pipeline.py` exports `build_pipeline(X_train, y_train) → estimator with predict_proba`.
3. Confirm `program.md` follows the rules from `.claude/skills/autoresearch-loop/skill.md`:
   - NO "Ideas to try" section (CORR-007)
   - Explicit interpretability constraints (no `PolynomialFeatures`, no hash encoding, no opaque features)
   - Goal + constraints + severity gates only
4. Set or confirm bayesian-tuning baseline metric. The runner compares autoresearch results against this.

### Run the loop

The runner script must implement (per `autoresearch-loop` skill):

- **Hill-climb-from-best**: maintain `best_pipeline.py` snapshot; revert `pipeline.py` to best on iter loss (CORR-008, BLOCKER)
- **In-loop smoke test**: tiny-subset fit+predict before full experiment (CORR-009)
- **Token budget**: `max_completion_tokens=48000` for full pipeline rewrites with frontier-model reasoning headroom (CORR-010)
- **Feature-name lint pre-smoke**: reject pipelines whose code contains opacity markers (`feat_\d+`, `poly_`, `embed_`, `hash_`, `interaction_`); allows `_x_` interactions
- **Per-iter snapshot**: copy `pipeline.py` to `iter_snapshots/iter_NNNN.py` after every successful iter — required so retune can access non-best candidates
- **Cost cap**: enforced every iteration from API usage fields; halt on `MAX_RUN_COST_USD` (default $250)
- **Time cap**: wall-clock cap independent of cost
- **Path sanitization**: all log writes pass through home-path redaction
- **No API key in source**: SDK reads `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` from env only
- **End-of-loop Optuna retune**: LLM-curates top-N candidates from leaderboard, runs Optuna with per-trial logging (CORR-011) and KeyboardInterrupt handler (CORR-012); writes `retuned_leaderboard.md` incrementally

Each iteration:
1. Read program.md + current pipeline + last 20 experiment summaries
2. Agent emits new pipeline.py + summary
3. **Feature-name lint** — reject if opacity markers present, revert to best4. Smoke test (tiny subset)
5. If smoke passes, full experiment via harness
6. If new metric > best: snapshot to best_pipeline.py
7. Snapshot pipeline.py → iter_snapshots/iter_NNNN.py for retune access8. Else: revert pipeline.py to best_pipeline.py
9. Update experiments.jsonl + leaderboard.md

After main loop completes:
- Optuna retune step (helpers/autoresearch/retune.py) — LLM picks top-N for architectural diversity + tuning upside, monkey-patches xgb/lgbm constructors, writes `retuned_leaderboard.md` after each candidate

### Verdict

Apply the `autoresearch-loop` skill's verdict criteria. The winning pipeline must pass BOTH:

1. **Metric gate** — beats bayesian-tuning-only baseline by ≥1%
2. **Interpretability gate** — top-10 features by `mean(|SHAP|)` are plain-English-named, domain-meaningful, and SHAP-attribution-survivable

If interpretability fails, demote to the next leaderboard entry. Do not ship a winning pipeline that fails interpretability — production teams can only act on models they can explain.

## Output

Final report (`final_report.md`) should include:

- Best pipeline: file location + metric
- Baseline comparison: bayesian-tuning baseline vs autoresearch best
- Verdict label (VALUE / METRIC WIN BUT NOT INTERPRETABLE / PARTIAL / NO ARCHITECTURAL DELTA / SATURATED MEMORIZATION)
- Trajectory: best-so-far per iteration
- Cost breakdown: per-iter average + total
- Experiment categories: count of "algorithm change" / "feature engineering" / "target reformulation" / "CV strategy" / "tuning"
- Top-10 SHAP features of winning pipeline with plain-English narration

## Anti-patterns

- **No best-pipeline snapshot** — runner overwrites pipeline.py without preserving best. Agent drifts off local maxima. BLOCKER.
- **No smoke test** — every broken pipeline burns full compute. HIGH.
- **Hardcoded API key** — never. Use env var. BLOCKER.
- **Ideas-to-try in program.md** — biases the run. BLOCKER.
- **Opaque features in winning pipeline** — `PolynomialFeatures`, hash encoding, embeddings without articulable meaning. BLOCKER on shipping.
- **No cost cap** — runs can burn arbitrary money. BLOCKER.

## Continuous improvement

When stakeholder feedback or a real run surfaces a new pattern, log it via the `log-correction` skill with `category: autoresearch`. The skill's rules accumulate; this agent applies whatever the skill currently says.

## See also

- `.claude/skills/autoresearch-loop/skill.md` — canonical rules
- `.claude/skills/bayesian-tuning/skill.md` — what autoresearch sits above
- `.claude/skills/shap-rep-explanations/skill.md` — interpretability gate standard
- `.claude/skills/feature-hygiene/skill.md` — pipeline.py inherits these rules
- `agents/ml-ship-decision.md` — verdict + report pattern
- Reference inspiration: https://github.com/karpathy/autoresearch
