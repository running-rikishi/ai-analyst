"""
Optuna-retune — iterative-with-threshold cascade.

After the autoresearch loop, retune the loop's best snapshot with Optuna.
If the retuned metric crosses a domain-specific threshold (gold tier, 1st
place, etc.), halt the cascade — done. Otherwise, ask the LLM to pick the
next candidate from the top-20 leaderboard (with knowledge of what's already
been tried and their retuned scores) and run another candidate. Up to `top_n`
candidates can cascade before the run completes.

Why iterative-with-threshold (vs the prior "LLM-curated parallel" pattern):
  - On clean metric problems (Home Credit, IEEE-CIS Fraud), the loop's #1
    snapshot is usually the right thing to retune. Most diversity-picking
    happens in the LOOP, not in retune selection.
  - Time-based budget (5h per candidate) lets Optuna sample more deeply than
    a fixed n_trials budget — TPE convergence benefits from more trials in
    the proven narrow space (see CORR-013).
  - Threshold-driven halting matches the user's actual decision criterion:
    "did we cross gold?" rather than "did we tune top-3 in parallel?". Single
    candidate cascades cleanly when threshold IS crossed; LLM-picks-next
    handles the case where it isn't.

Tuning approach: monkey-patch XGBClassifier (and LGBMClassifier if present)
so each trial's instantiation gets Optuna-suggested params overlaid on
whatever the agent's pipeline used. Avoids requiring pipelines to expose a
tuning hook, but means we tune XGB/LGBM model params only — FE and other
pipeline structure stay frozen.

Limitations:
  - Only XGBClassifier and LGBMClassifier are auto-tuned. Other algorithms
    (sklearn LR, CatBoost, etc.) skip the retune and report the loop-output
    metric. Document this in the final report if any winner falls outside.
  - Each trial re-imports the pipeline (fresh module + fresh data load via
    cached harness fixtures). Trial cost ≈ fit time for that pipeline.
  - Thresholds are problem-specific. Default `None` = no early halt; the
    cascade always runs through `top_n` candidates. Users set
    `RETUNE_THRESHOLD_GOLD` / `RETUNE_THRESHOLD_FIRST` for their problem
    (see comments below for examples).
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

# Hyperparameter search spaces. Sized to be reasonable for IEEE-CIS Fraud
# scale (~470K train rows); user can override by editing this module.
XGB_SEARCH = {
    "n_estimators":     ("int",   100, 2000),
    "max_depth":        ("int",   3,   12),
    "learning_rate":    ("float", 0.005, 0.2, "log"),
    "subsample":        ("float", 0.5, 1.0),
    "colsample_bytree": ("float", 0.5, 1.0),
    "min_child_weight": ("int",   1,   20),
    "reg_alpha":        ("float", 1e-3, 10.0, "log"),
    "reg_lambda":       ("float", 1e-3, 10.0, "log"),
    "scale_pos_weight": ("float", 1.0, 30.0),
}

LGBM_SEARCH = {
    "n_estimators":     ("int",   100, 2000),
    "num_leaves":       ("int",   15,  200),
    "learning_rate":    ("float", 0.005, 0.2, "log"),
    "subsample":        ("float", 0.5, 1.0),
    "colsample_bytree": ("float", 0.5, 1.0),
    "min_child_samples":("int",   5,   100),
    "reg_alpha":        ("float", 1e-3, 10.0, "log"),
    "reg_lambda":       ("float", 1e-3, 10.0, "log"),
}

# Iterative-with-threshold cascade config. Set thresholds per-problem to enable
# early halt when retuned metric crosses a known boundary. None defaults mean
# the cascade always runs through `top_n` candidates without checking.
#
# Examples (override in your driver script):
#   IEEE-CIS Fraud: RETUNE_THRESHOLD_GOLD = 0.94, RETUNE_THRESHOLD_FIRST = 0.94653
#   Home Credit:    RETUNE_THRESHOLD_GOLD = 0.80110, RETUNE_THRESHOLD_FIRST = 0.80570
RETUNE_PER_CANDIDATE_HOURS = 5.0   # max wall-clock budget for Optuna on one candidate
RETUNE_THRESHOLD_GOLD: float | None = None
RETUNE_THRESHOLD_FIRST: float | None = None


def _suggest(trial, search: dict, prefix: str = "") -> dict:
    out = {}
    for name, spec in search.items():
        kind = spec[0]
        if kind == "int":
            out[name] = trial.suggest_int(f"{prefix}{name}", spec[1], spec[2])
        elif kind == "float":
            log = len(spec) > 3 and spec[3] == "log"
            out[name] = trial.suggest_float(f"{prefix}{name}", spec[1], spec[2], log=log)
    return out


def _import_pipeline_fresh(snapshot_path: Path, mod_name: str):
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, snapshot_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_objective(snapshot_path: Path, harness_module, tune_xgb: bool, tune_lgbm: bool):
    """Build an Optuna objective that monkey-patches model classes for this trial."""
    from sklearn.metrics import roc_auc_score
    X_train, y_train, X_holdout, y_holdout = harness_module.load_data()

    # Identify which classes we'll patch
    try:
        import xgboost
    except ImportError:
        xgboost = None
    try:
        import lightgbm
    except ImportError:
        lightgbm = None

    def objective(trial) -> float:
        # Suggest params for whichever models the pipeline uses
        xgb_params = _suggest(trial, XGB_SEARCH, prefix="xgb_") if tune_xgb else {}
        lgbm_params = _suggest(trial, LGBM_SEARCH, prefix="lgbm_") if tune_lgbm else {}

        # Monkey-patch the constructors. Each patch overlays Optuna params on top
        # of whatever the agent's pipeline passes — Optuna wins on collisions.
        patches = []
        if tune_xgb and xgboost is not None:
            orig_xgb_init = xgboost.XGBClassifier.__init__

            def patched_xgb_init(self, **kwargs):
                merged = {**kwargs, **xgb_params}
                orig_xgb_init(self, **merged)
            xgboost.XGBClassifier.__init__ = patched_xgb_init
            patches.append(("xgb", orig_xgb_init))
        if tune_lgbm and lightgbm is not None:
            orig_lgbm_init = lightgbm.LGBMClassifier.__init__

            def patched_lgbm_init(self, **kwargs):
                merged = {**kwargs, **lgbm_params}
                orig_lgbm_init(self, **merged)
            lightgbm.LGBMClassifier.__init__ = patched_lgbm_init
            patches.append(("lgbm", orig_lgbm_init))

        try:
            module = _import_pipeline_fresh(snapshot_path, f"retune_trial_{trial.number}")
            estimator = module.build_pipeline(X_train, y_train)
            proba = estimator.predict_proba(X_holdout)[:, 1]
            return float(roc_auc_score(y_holdout, proba))
        finally:
            # Restore originals so other trials / pipelines aren't polluted
            for kind, orig in patches:
                if kind == "xgb" and xgboost is not None:
                    xgboost.XGBClassifier.__init__ = orig
                elif kind == "lgbm" and lightgbm is not None:
                    lightgbm.LGBMClassifier.__init__ = orig

    return objective


def _detect_algorithms(snapshot_path: Path) -> tuple[bool, bool]:
    """Scan pipeline source for which algorithms are used. Returns (uses_xgb, uses_lgbm)."""
    src = snapshot_path.read_text()
    return ("XGBClassifier" in src), ("LGBMClassifier" in src)


def _fingerprint(snapshot_path: Path) -> str:
    """Short architectural fingerprint for LLM selection context."""
    src = snapshot_path.read_text() if snapshot_path.exists() else ""
    algos = []
    if "XGBClassifier" in src: algos.append("XGB")
    if "LGBMClassifier" in src: algos.append("LGBM")
    if "CatBoostClassifier" in src: algos.append("CatBoost")
    if "LogisticRegression" in src: algos.append("LR")
    n_funcs = src.count("\ndef ")
    n_lines = src.count("\n")
    has_target_enc = "target" in src.lower() and ("encod" in src.lower() or "smooth" in src.lower())
    has_ensemble = ("VotingClassifier" in src) or ("avg" in src.lower() and len(algos) > 1) or ("AverageModel" in src)
    return f"algos={'+'.join(algos) or 'other'} funcs={n_funcs} lines={n_lines} target_enc={has_target_enc} ensemble={has_ensemble}"


def _llm_curate_picks(
    candidates: list[dict],
    snapshots_dir: Path,
    n_picks: int = 4,
    model: str = "gpt-5.5",
) -> tuple[list[dict], str]:
    """Ask the LLM to pick N candidates for retune, optimizing for diversity + upside.

    Returns (picked_candidates_in_order, reasoning_text). Falls back to top-N
    if the call fails or the response can't be parsed.
    """
    import os
    if not os.environ.get("OPENAI_API_KEY"):
        print("retune: OPENAI_API_KEY not in env — falling back to top-N by metric")
        return candidates[:n_picks], "fallback: no API key"
    try:
        from openai import OpenAI
    except ImportError:
        print("retune: openai not installed — falling back to top-N")
        return candidates[:n_picks], "fallback: openai not installed"

    # Build the leaderboard digest with fingerprints
    rows = []
    for rank, e in enumerate(candidates, 1):
        snap = snapshots_dir / f"iter_{e['iter']:04d}.py"
        fp = _fingerprint(snap) if snap.exists() else "snapshot missing"
        rows.append(f"#{rank}  iter={e['iter']}  metric={e['metric']:.4f}  {fp}\n     summary: {e['summary']}")

    leaderboard_text = "\n".join(rows)

    system = (
        "You are selecting autoresearch pipeline snapshots for hyperparameter retuning with Optuna. "
        "The goal is to make the autoresearch-vs-bayesian-tuning comparison fair (the autoresearch winners "
        "used whatever hyperparams the LLM agent chose during the loop; Optuna can now tune them). "
        "Pick candidates that MAXIMIZE INFORMATION: span different architectures and have unrealized "
        "tuning headroom. Avoid retuning four near-identical ensembles."
    )
    user = (
        f"Pick exactly {n_picks} iters from this leaderboard to retune. Criteria:\n"
        "  1. ARCHITECTURAL DIVERSITY — different algorithm sets, different feature-engineering families\n"
        "  2. TUNING UPSIDE — entries where the agent likely used default hyperparams (vs entries that already look tuned)\n"
        "  3. AVOID REDUNDANCY — don't pick 4 variants of the same ensemble\n"
        "  4. The top-1 by metric should usually be included (defensible: 'we tuned the winner')\n\n"
        "Leaderboard (top 20 by metric, with architectural fingerprint):\n\n"
        + leaderboard_text +
        f"\n\nOutput format — exactly {n_picks} lines, each on its own line:\n"
        "PICK: iter=<N>  reason=<one short sentence>\n\n"
        "Then a final line:\nRATIONALE: <one-paragraph summary of the diversity/upside logic across all picks>"
    )

    try:
        client = OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_completion_tokens=8000,
        )
        text = response.choices[0].message.content or ""
    except Exception as e:
        print(f"retune: LLM curation API error ({type(e).__name__}: {e}) — falling back to top-N")
        return candidates[:n_picks], f"fallback: api error {type(e).__name__}"

    import re
    picked_iters: list[int] = []
    pick_reasons: dict[int, str] = {}
    for line in text.split("\n"):
        m = re.match(r"PICK:\s*iter=(\d+)\s*reason=(.*)", line.strip())
        if m:
            it = int(m.group(1))
            if it not in pick_reasons:
                picked_iters.append(it)
                pick_reasons[it] = m.group(2).strip()
    rationale_m = re.search(r"RATIONALE:\s*(.+)", text, re.DOTALL)
    rationale = rationale_m.group(1).strip() if rationale_m else ""

    if not picked_iters:
        print("retune: LLM picks unparseable — falling back to top-N")
        return candidates[:n_picks], "fallback: unparseable response"

    by_iter = {e["iter"]: e for e in candidates}
    picked = [by_iter[i] for i in picked_iters if i in by_iter]
    if not picked:
        print("retune: LLM picked iters not in candidate set — falling back to top-N")
        return candidates[:n_picks], "fallback: picks not in candidate set"

    print(f"retune: LLM picked {len(picked)} candidates:")
    for c in picked:
        print(f"  iter {c['iter']} ({c['metric']:.4f}): {pick_reasons.get(c['iter'], '')}")

    # Attach reasoning to each pick for the leaderboard
    for c in picked:
        c["_pick_reason"] = pick_reasons.get(c["iter"], "")
    return picked, rationale


def _llm_pick_next_candidate(
    pool: list[dict],
    tried_results: list[dict],
    snapshots_dir: Path,
    model: str = "gpt-5.5",
) -> tuple[dict | None, str]:
    """Pick the next candidate to retune given what's already been tried.

    Sees the prior tried iters + their retuned scores, plus the remaining pool
    (with architectural fingerprints). Asks for ONE next pick prioritizing
    architectural difference from prior tries + high loop metric.

    Falls back to highest-metric-untried if the API call fails or LLM picks
    an iter that isn't in the remaining pool.
    """
    import os, re
    tried_iters = {r["iter"] for r in tried_results}
    remaining = [c for c in pool if c["iter"] not in tried_iters]
    if not remaining:
        return None, "no remaining candidates"
    if not os.environ.get("OPENAI_API_KEY"):
        return remaining[0], "fallback: no API key, picking highest-metric untried"
    try:
        from openai import OpenAI
    except ImportError:
        return remaining[0], "fallback: openai not installed"

    tried_lines = []
    for r in tried_results:
        rm = r.get("retuned_metric")
        rm_s = f"{rm:.4f}" if rm is not None else "skipped"
        tried_lines.append(f"  iter={r['iter']}  loop={r['loop_metric']:.4f} → retuned={rm_s}")
    remaining_lines = []
    for rank, e in enumerate(remaining, 1):
        snap = snapshots_dir / f"iter_{e['iter']:04d}.py"
        fp = _fingerprint(snap) if snap.exists() else "snapshot missing"
        remaining_lines.append(f"  #{rank} iter={e['iter']}  loop_metric={e['metric']:.4f}  {fp}\n      summary: {e['summary']}")

    system = (
        "You are selecting the NEXT autoresearch pipeline snapshot to retune. "
        "Prior candidates have already been tuned and didn't hit the threshold. "
        "Pick the single best remaining candidate — prioritize architectural difference "
        "from what was already tried (so retune has a chance of breaking past where the "
        "previous candidate plateaued) and high loop metric."
    )
    user = (
        "Already tried (loop metric → retuned metric):\n"
        + "\n".join(tried_lines) +
        "\n\nRemaining candidates (top-20 by loop metric, with architectural fingerprint):\n\n"
        + "\n".join(remaining_lines) +
        "\n\nOutput format — exactly ONE pick:\n"
        "PICK: iter=<N>  reason=<one short sentence on why this one is different/promising>"
    )
    try:
        client = OpenAI()
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_completion_tokens=4000,
        )
        text = response.choices[0].message.content or ""
    except Exception as e:
        return remaining[0], f"fallback: api error {type(e).__name__}"

    m = re.search(r"PICK:\s*iter=(\d+)\s*reason=(.*)", text)
    if not m:
        return remaining[0], "fallback: unparseable response"
    picked_iter = int(m.group(1))
    reason = m.group(2).strip()
    by_iter = {e["iter"]: e for e in remaining}
    if picked_iter not in by_iter:
        return remaining[0], f"fallback: LLM picked iter={picked_iter} not in remaining"
    picked = by_iter[picked_iter]
    picked["_pick_reason"] = reason
    return picked, reason


def _log_event(runner_log_path: Path | None, event: dict) -> None:
    """Append an event to runner_log.jsonl, with path sanitization."""
    if runner_log_path is None:
        return
    sanitized = {k: (v.replace(str(Path.home()), "<HOME>") if isinstance(v, str) else v)
                 for k, v in event.items()}
    sanitized["ts"] = int(time.time())
    try:
        with runner_log_path.open("a") as f:
            f.write(json.dumps(sanitized, default=str) + "\n")
    except Exception:
        pass  # never crash on logging


def _write_leaderboard(output_dir: Path, results: list[dict], candidates: list[dict],
                       rationale: str, n_trials: int, partial: bool = False) -> None:
    """Write retuned_leaderboard.md + .json. Called incrementally + at end."""
    pick_reasons = {c["iter"]: c.get("_pick_reason", "") for c in candidates}
    ranked = sorted(results, key=lambda r: (r.get("retuned_metric") or -1.0), reverse=True)

    title_suffix = " (partial — in flight)" if partial else ""
    lines = [
        f"# Retuned Leaderboard (Optuna-tuned LLM-curated picks){title_suffix}", "",
        f"LLM-curated {len(candidates)} candidates from autoresearch loop, re-tuned with up to {n_trials} Optuna trials each.",
        "Selection: architectural diversity + tuning upside. Hyperparameters tuned; FE + algorithm locked.", "",
        f"**LLM rationale:** {rationale}", "",
        "| Retuned Rank | Iter | Loop Metric | Retuned Metric | Δ | Trials | Pick Reason | Notes |",
        "|---:|---:|---:|---:|---:|---:|:---|:---|",
    ]
    for rank, r in enumerate(ranked, 1):
        reason = pick_reasons.get(r["iter"], "")
        if r.get("retuned_metric") is None:
            lines.append(
                f"| {rank} | {r['iter']} | {r['loop_metric']:.4f} | — | — | — | {reason} | {r.get('skipped', '')} |"
            )
        else:
            partial_marker = " (partial)" if r.get("partial") else ""
            lines.append(
                f"| {rank} | {r['iter']} | {r['loop_metric']:.4f} | {r['retuned_metric']:.4f}{partial_marker} | "
                f"{r['delta']:+.4f} | {r['n_trials']} | {reason} | |"
            )
    out_path = output_dir / "retuned_leaderboard.md"
    out_path.write_text("\n".join(lines) + "\n")

    json_path = output_dir / "retuned_leaderboard.json"
    json_path.write_text(json.dumps(ranked, indent=2, default=str))


def retune_top_n(
    experiments_path: Path,
    snapshots_dir: Path,
    output_dir: Path,
    top_n: int = 3,
    n_trials: int = 50,
    harness_module: Any = None,
    runner_log_path: Path | None = None,
) -> list[dict]:
    """Iterative-with-threshold retune cascade.

    Sequential cascade: try the best candidate first with a `RETUNE_PER_CANDIDATE_HOURS`
    Optuna time budget. If retuned metric crosses `RETUNE_THRESHOLD_GOLD` or
    `RETUNE_THRESHOLD_FIRST`, halt the cascade. Otherwise, ask the LLM to pick
    the next candidate from the remaining pool (with prior retuned scores in
    context). Repeat up to `top_n` candidates.

    Args:
        top_n: max candidates to cascade through (default 3 — more rarely helps;
            if the top-1 plus 2 LLM-picked alternates don't crack threshold,
            additional candidates usually don't either)
        n_trials: ADVISORY — replaced by `RETUNE_PER_CANDIDATE_HOURS` time
            budget in this mode. Kept in signature for runner CLI compatibility.

    Per-candidate Optuna budget: `RETUNE_PER_CANDIDATE_HOURS` hours.
    Thresholds: `RETUNE_THRESHOLD_GOLD` / `RETUNE_THRESHOLD_FIRST` (module-level
    constants; users set per-problem; None = no early halt).

    Logs per-trial events + per-candidate events to runner_log_path. Writes
    incremental retuned_leaderboard.md after each candidate so partial results
    survive a kill mid-flight (CORR-011, CORR-012). Per-candidate trials_log_iter_NNNN.jsonl
    persists every trial's full params for reproducibility (CORR-014).
    """
    try:
        import optuna
    except ImportError:
        print("retune: optuna not installed — skipping")
        return []

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    if not experiments_path.exists():
        print(f"retune: {experiments_path} not found — skipping")
        return []
    exps = []
    with experiments_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            e = json.loads(line)
            if e.get("success") and e.get("metric") is not None:
                exps.append(e)
    if not exps:
        print("retune: no successful experiments — skipping")
        return []

    exps_sorted = sorted(exps, key=lambda e: e["metric"], reverse=True)
    pool = exps_sorted[:max(20, top_n * 4)]
    per_cand_seconds = int(RETUNE_PER_CANDIDATE_HOURS * 3600)
    gold_s = f"{RETUNE_THRESHOLD_GOLD}" if RETUNE_THRESHOLD_GOLD is not None else "None (no early halt)"
    first_s = f"{RETUNE_THRESHOLD_FIRST}" if RETUNE_THRESHOLD_FIRST is not None else "None (no early halt)"
    print(f"retune (iterative-with-threshold): pool of {len(pool)} candidates")
    print(f"  per-candidate budget: {RETUNE_PER_CANDIDATE_HOURS}h Optuna timeout")
    print(f"  thresholds: gold={gold_s}, 1st={first_s}")
    print(f"  max cascade depth: {top_n}")

    # First candidate is always the loop best (#1 by metric)
    first = pool[0]
    first["_pick_reason"] = "loop best (#1 by metric)"
    rationale = (
        f"Iterative-with-threshold cascade: start with loop best (iter {first['iter']}, "
        f"metric {first['metric']:.4f}). If retuned doesn't hit gold ({gold_s}) or "
        f"1st place ({first_s}), LLM picks next candidate."
    )

    results: list[dict] = []
    candidates_tried: list[dict] = []
    next_candidate: dict | None = first
    rank = 0

    while next_candidate is not None and rank < top_n:
        rank += 1
        candidates_tried.append(next_candidate)
        exp = next_candidate
        iter_num = exp["iter"]
        snap = snapshots_dir / f"iter_{iter_num:04d}.py"

        if not snap.exists():
            print(f"retune: iter {iter_num} snapshot missing at {snap} — skipping")
            results.append({"rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
                            "retuned_metric": None, "delta": None, "skipped": "missing_snapshot"})
            _write_leaderboard(output_dir, results, candidates_tried, rationale, 0, partial=True)
            next_candidate, _ = _llm_pick_next_candidate(pool, results, snapshots_dir)
            continue

        uses_xgb, uses_lgbm = _detect_algorithms(snap)
        if not uses_xgb and not uses_lgbm:
            print(f"retune: iter {iter_num} uses neither XGB nor LGBM — skipping")
            results.append({"rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
                            "retuned_metric": None, "delta": None,
                            "skipped": "unsupported_algorithm"})
            _write_leaderboard(output_dir, results, candidates_tried, rationale, 0, partial=True)
            next_candidate, _ = _llm_pick_next_candidate(pool, results, snapshots_dir)
            continue

        algo_str = ('xgb' if uses_xgb else '') + ('+' if uses_xgb and uses_lgbm else '') + ('lgbm' if uses_lgbm else '')
        print(f"\nretune cascade #{rank}: iter {iter_num} (loop={exp['metric']:.4f}, algos={algo_str})")
        print(f"  reason: {exp.get('_pick_reason', '(no reason)')}")
        print(f"  budget: {RETUNE_PER_CANDIDATE_HOURS}h Optuna timeout")
        _log_event(runner_log_path, {
            "event": "retune_candidate_start", "rank": rank, "iter": iter_num,
            "loop_metric": exp["metric"], "budget_hours": RETUNE_PER_CANDIDATE_HOURS,
            "algos": algo_str, "pick_reason": exp.get("_pick_reason", ""),
        })

        # Per-candidate trials log — one line per Optuna trial with full params.
        # Reproducibility (CORR-014): grep this file to find the exact params of
        # any historical trial without needing to re-run the study.
        trials_log_path = output_dir / f"trials_log_iter_{iter_num:04d}.jsonl"
        if trials_log_path.exists():
            trials_log_path.unlink()

        t0 = time.time()
        objective = _make_objective(snap, harness_module, uses_xgb, uses_lgbm)
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42 + iter_num, multivariate=True),
            study_name=f"retune_iter_{iter_num}",
        )

        trial_start_times: dict[int, float] = {}

        def _on_trial_start(study_, trial_):
            trial_start_times[trial_.number] = time.time()

        def _on_trial_end(study_, trial_, _rank=rank, _iter=iter_num):
            elapsed_s = round(time.time() - trial_start_times.get(trial_.number, time.time()), 1)
            _log_event(runner_log_path, {
                "event": "retune_trial_complete", "rank": _rank, "iter": _iter,
                "trial": trial_.number, "metric": trial_.value, "elapsed_s": elapsed_s,
                "state": str(trial_.state),
            })
            try:
                with trials_log_path.open("a") as f:
                    f.write(json.dumps({
                        "trial": trial_.number,
                        "metric": trial_.value,
                        "elapsed_s": elapsed_s,
                        "state": str(trial_.state),
                        "params": dict(trial_.params),
                        "ts": int(time.time()),
                    }, default=str) + "\n")
            except Exception:
                pass

        try:
            study.optimize(
                objective,
                timeout=per_cand_seconds,
                show_progress_bar=False,
                callbacks=[_on_trial_start, _on_trial_end],
            )
            retuned = float(study.best_value) if len(study.trials) > 0 else None
        except KeyboardInterrupt:
            partial = float(study.best_value) if len(study.trials) > 0 else None
            results.append({"rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
                            "retuned_metric": partial, "delta": (partial - exp["metric"]) if partial else None,
                            "n_trials": len(study.trials), "partial": True,
                            "best_params": study.best_params if partial else None})
            _log_event(runner_log_path, {"event": "retune_candidate_interrupted",
                                         "rank": rank, "iter": iter_num,
                                         "completed_trials": len(study.trials),
                                         "partial_best": partial})
            _write_leaderboard(output_dir, results, candidates_tried, rationale,
                               len(study.trials), partial=True)
            print(f"retune: iter {iter_num} interrupted at trial {len(study.trials)}, partial best={partial}")
            raise
        except Exception as e:
            print(f"retune: iter {iter_num} failed: {type(e).__name__}: {e}")
            _log_event(runner_log_path, {"event": "retune_candidate_failed",
                                         "rank": rank, "iter": iter_num,
                                         "error": f"{type(e).__name__}: {e}"})
            results.append({"rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
                            "retuned_metric": None, "delta": None,
                            "skipped": f"trial_error: {type(e).__name__}"})
            _write_leaderboard(output_dir, results, candidates_tried, rationale, 0, partial=True)
            next_candidate, _ = _llm_pick_next_candidate(pool, results, snapshots_dir)
            continue

        elapsed_min = (time.time() - t0) / 60
        n_trials_completed = len(study.trials)
        delta = (retuned - exp["metric"]) if retuned is not None else None
        results.append({
            "rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
            "retuned_metric": retuned, "delta": delta,
            "elapsed_min": round(elapsed_min, 1),
            "best_params": study.best_params if retuned is not None else None,
            "n_trials": n_trials_completed,
        })
        _log_event(runner_log_path, {
            "event": "retune_candidate_complete", "rank": rank, "iter": iter_num,
            "loop_metric": exp["metric"], "retuned_metric": retuned, "delta": delta,
            "elapsed_min": round(elapsed_min, 1), "n_trials": n_trials_completed,
            "best_params": study.best_params if retuned is not None else None,
        })
        retuned_s = f"{retuned:.4f}" if retuned is not None else "None"
        delta_s = f"{delta:+.4f}" if delta is not None else "None"
        print(f"retune: cascade #{rank} iter {iter_num} loop={exp['metric']:.4f} → "
              f"retuned={retuned_s} (Δ={delta_s}, {elapsed_min:.1f} min, "
              f"{n_trials_completed} trials)")
        _write_leaderboard(output_dir, results, candidates_tried, rationale,
                           n_trials_completed, partial=True)

        # Threshold check — halt cascade if we crossed
        if retuned is not None and RETUNE_THRESHOLD_FIRST is not None and retuned >= RETUNE_THRESHOLD_FIRST:
            print(f"\n[CASCADE HALT] 1st-place threshold ({RETUNE_THRESHOLD_FIRST}) crossed at iter {iter_num}.")
            _log_event(runner_log_path, {
                "event": "retune_cascade_halt_threshold", "threshold": "first_place",
                "metric": retuned, "iter": iter_num,
            })
            break
        if retuned is not None and RETUNE_THRESHOLD_GOLD is not None and retuned >= RETUNE_THRESHOLD_GOLD:
            print(f"\n[CASCADE HALT] Gold threshold ({RETUNE_THRESHOLD_GOLD}) crossed at iter {iter_num}.")
            _log_event(runner_log_path, {
                "event": "retune_cascade_halt_threshold", "threshold": "gold",
                "metric": retuned, "iter": iter_num,
            })
            break

        if rank >= top_n:
            print(f"\nCascade depth limit ({top_n}) reached; threshold not crossed.")
            break

        # Pick the next candidate
        next_candidate, pick_rationale = _llm_pick_next_candidate(pool, results, snapshots_dir)
        if next_candidate is None:
            print("No remaining candidates; halting cascade.")
            break
        print(f"  next pick: iter {next_candidate['iter']} — {pick_rationale[:120]}")

    ranked = sorted(results, key=lambda r: (r.get("retuned_metric") or -1.0), reverse=True)
    _write_leaderboard(output_dir, results, candidates_tried, rationale,
                       results[-1].get("n_trials", 0) if results else 0, partial=False)
    print(f"\nretune: wrote retuned_leaderboard.md (final, {len(candidates_tried)} candidates cascaded)")
    return ranked
