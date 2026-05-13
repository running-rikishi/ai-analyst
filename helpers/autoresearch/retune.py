"""
Optuna-retune — fixes the unfair tuned-vs-untuned comparison.

After the autoresearch loop, an LLM curates N candidates from the top-20
leaderboard (picking for architectural diversity + tuning upside, not just
metric rank). Each picked snapshot is re-run with Optuna tuning the gradient-
boosted model's hyperparameters while feature engineering + algorithm choice
stay locked. Result: a "retuned leaderboard" comparable apples-to-apples to
the Optuna-tuned baseline.

Selection strategy:
  - The top-N by metric tend to be near-duplicates (variations on the same
    ensemble + small FE tweak). Retuning them produces N near-identical
    numbers — methodological dead weight.
  - LLM-curated selection: show top-20 + architectural fingerprint + the
    agent's own summary. Ask for N picks that maximize information.
  - Falls back to top-N by metric if the LLM call fails.

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
    top_n: int = 5,
    n_trials: int = 30,
    harness_module: Any = None,
    runner_log_path: Path | None = None,
) -> list[dict]:
    """Run Optuna over LLM-curated top-N pipeline snapshots; write retuned_leaderboard.md.

    Logs per-trial and per-candidate events to runner_log_path if provided. Writes
    incremental retuned_leaderboard.md after each candidate completes so partial
    results survive a kill mid-flight (CORR-011).
    """
    try:
        import optuna
    except ImportError:
        print("retune: optuna not installed — skipping")
        return []

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    # Read experiments, pick top-N successful entries
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
    # Show the LLM the top 20 (more than top_n) so it can spot diverse picks
    pool = exps_sorted[:max(20, top_n * 4)]
    print(f"retune: pool of {len(pool)} candidates — asking LLM to pick top-{top_n} for diversity + upside...")
    candidates, rationale = _llm_curate_picks(pool, snapshots_dir, n_picks=top_n)
    print(f"retune: rationale: {rationale[:300]}")

    results = []
    for rank, exp in enumerate(candidates, 1):
        iter_num = exp["iter"]
        snap = snapshots_dir / f"iter_{iter_num:04d}.py"
        if not snap.exists():
            print(f"retune: iter {iter_num} snapshot missing at {snap} — skipping")
            results.append({"rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
                            "retuned_metric": None, "delta": None, "skipped": "missing_snapshot"})
            continue

        uses_xgb, uses_lgbm = _detect_algorithms(snap)
        if not uses_xgb and not uses_lgbm:
            print(f"retune: iter {iter_num} uses neither XGB nor LGBM — skipping")
            results.append({"rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
                            "retuned_metric": None, "delta": None,
                            "skipped": "unsupported_algorithm"})
            continue

        algo_str = ('xgb' if uses_xgb else '') + ('+' if uses_xgb and uses_lgbm else '') + ('lgbm' if uses_lgbm else '')
        print(f"retune: iter {iter_num} ({n_trials} trials, {algo_str}) ...")
        _log_event(runner_log_path, {
            "event": "retune_candidate_start", "rank": rank, "iter": iter_num,
            "loop_metric": exp["metric"], "n_trials": n_trials, "algos": algo_str,
        })

        # Per-candidate trials log — one line per Optuna trial with full params.
        # Reproducibility (CORR-014): a colleague can grep this file to find
        # the exact params of any historical trial, including the best, without
        # needing to re-run the study to recover them.
        trials_log_path = output_dir / f"trials_log_iter_{iter_num:04d}.jsonl"
        # Truncate prior log if re-running this candidate
        if trials_log_path.exists():
            trials_log_path.unlink()

        t0 = time.time()
        objective = _make_objective(snap, harness_module, uses_xgb, uses_lgbm)
        study = optuna.create_study(
            direction="maximize",
            sampler=optuna.samplers.TPESampler(seed=42 + iter_num, multivariate=True),
            study_name=f"retune_iter_{iter_num}",
        )

        trial_start_times = {}

        def _on_trial_start(study_, trial_):
            trial_start_times[trial_.number] = time.time()

        def _on_trial_end(study_, trial_):
            elapsed_s = round(time.time() - trial_start_times.get(trial_.number, time.time()), 1)
            # Emit the lightweight event to runner_log (no params — keep it skim-friendly)
            _log_event(runner_log_path, {
                "event": "retune_trial_complete", "rank": rank, "iter": iter_num,
                "trial": trial_.number, "metric": trial_.value, "elapsed_s": elapsed_s,
                "state": str(trial_.state),
            })
            # Persist params per-trial to the per-candidate trials_log (CORR-014).
            # This file is the source of truth for "what params did trial N use?"
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
                pass  # never crash on logging

        try:
            study.optimize(
                objective, n_trials=n_trials, show_progress_bar=False,
                callbacks=[_on_trial_end],
            )
            retuned = float(study.best_value)
        except KeyboardInterrupt:
            # Capture partial: best trial completed so far
            partial = float(study.best_value) if len(study.trials) > 0 else None
            results.append({"rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
                            "retuned_metric": partial, "delta": (partial - exp["metric"]) if partial else None,
                            "n_trials": len(study.trials), "partial": True,
                            "best_params": study.best_params if partial else None})
            _log_event(runner_log_path, {"event": "retune_candidate_interrupted",
                                         "rank": rank, "iter": iter_num,
                                         "completed_trials": len(study.trials), "partial_best": partial})
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
            continue
        elapsed_min = (time.time() - t0) / 60
        delta = retuned - exp["metric"]
        results.append({
            "rank": rank, "iter": iter_num, "loop_metric": exp["metric"],
            "retuned_metric": retuned, "delta": delta,
            "elapsed_min": round(elapsed_min, 1),
            "best_params": study.best_params,
            "n_trials": n_trials,
        })
        _log_event(runner_log_path, {
            "event": "retune_candidate_complete", "rank": rank, "iter": iter_num,
            "loop_metric": exp["metric"], "retuned_metric": retuned, "delta": delta,
            "elapsed_min": round(elapsed_min, 1), "n_trials": n_trials,
            "best_params": study.best_params,
        })
        print(f"retune: iter {iter_num} loop={exp['metric']:.4f} → retuned={retuned:.4f} "
              f"(Δ={delta:+.4f}, {elapsed_min:.1f} min)")

        # Incremental persistence: write partial leaderboard after each candidate
        # so partial results survive a kill mid-flight (CORR-011).
        _write_leaderboard(output_dir, results, candidates, rationale, n_trials, partial=True)

    # Re-rank by retuned metric (None values go last)
    ranked = sorted(results, key=lambda r: (r.get("retuned_metric") or -1.0), reverse=True)

    _write_leaderboard(output_dir, results, candidates, rationale, n_trials, partial=False)
    print(f"\nretune: wrote retuned_leaderboard.md (final)")

    return ranked
