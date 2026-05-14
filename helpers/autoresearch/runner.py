"""
Autoresearch runner — applies the autoresearch-loop skill.

Reads `program.md`, `pipeline.py`, and the experiment history; drives an LLM
agent to iterate on `pipeline.py`; logs every experiment; maintains a
`best_pipeline.py` snapshot for hill-climbing.

Safety rails (all required per the skill):
  - API key read from env only — NEVER passed as a parameter, NEVER hardcoded
  - Hard cost cap, enforced every iteration from API usage fields
  - Wall-clock time cap
  - Per-experiment timeout enforced by the harness
  - Path sanitization on all log writes
  - Response objects never serialized to disk (only specific usage fields)
  - In-loop smoke test (tiny subset) catches runtime bugs before full compute
  - Hill-climb-from-best: revert pipeline.py to best on iter loss
  - max_completion_tokens sized for full-file rewrites with reasoning headroom

Supports OpenAI (default) and Anthropic backends. Swap by import.

Usage:
  export OPENAI_API_KEY=sk-...                          # or ANTHROPIC_API_KEY
  python -m helpers.autoresearch.runner \\
      --harness path/to/harness.py \\
      --pipeline path/to/pipeline.py \\
      --program path/to/program.md \\
      --baseline-metric 0.9218 \\
      --max-hours 4 --max-cost 30 --model gpt-5.5
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Pricing dict — per-million-token cost in USD. Update from provider pricing
# pages. The cost cap is the real safety floor; this dict just makes the cap
# kick in at the right point. Conservative estimates are safer than tight ones.
PRICING = {
    "gpt-5.5":      {"input": 6.00, "output": 50.00, "cached_input": 0.60},
    "gpt-5.5-pro":  {"input": 15.0, "output": 100.0, "cached_input": 1.50},
    "gpt-5.4":      {"input": 5.00, "output": 40.00, "cached_input": 0.50},
    "gpt-5.4-mini": {"input": 0.50, "output":  4.00, "cached_input": 0.05},
    "gpt-4o":       {"input": 2.50, "output": 10.00, "cached_input": 1.25},
    "gpt-4o-mini":  {"input": 0.15, "output":  0.60, "cached_input": 0.075},
}

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_MAX_HOURS = 4.0
DEFAULT_MAX_COST_USD = 30.0  # safe default for new users; raise via --max-cost for serious research runs
DEFAULT_TIMEOUT_SEC = 600
DEFAULT_MAX_COMPLETION_TOKENS = 48000  # frontier-model reasoning headroom (gpt-5.x reserves tokens for CoT) — CORR-010
HISTORY_TAIL = 20
SMOKE_SAMPLE_N = 1000

# Feature-name lint patterns. Reject pipelines whose code introduces feature
# names matching opacity markers. Defense-in-depth on top of program.md's
# BLOCKER rules. `_x_` (interpretable interactions like `age_x_sex`) is
# intentionally NOT here.
LINT_PATTERNS = [
    (r"feat_\d+", "numeric-suffix opaque names (use plain-English feature names)"),
    (r"\bpoly_", "polynomial features (banned by program.md)"),
    (r"\bembed_", "embedding features (banned by program.md)"),
    (r"\bhash_", "hash encoding (banned by program.md)"),
    (r"\binteraction_", "anonymous interaction markers (name the specific interaction)"),
]


# ---------- safety ----------

def _verify_api_key() -> str:
    """Confirm an API key is in env; never log the value."""
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if os.environ.get(var):
            return var
    sys.exit("ERROR: neither OPENAI_API_KEY nor ANTHROPIC_API_KEY set in environment.")


def _sanitize(s):
    """Strip local paths from log strings."""
    if not isinstance(s, str):
        return s
    return s.replace(str(Path.home()), "<HOME>")


# ---------- file management ----------

@dataclass
class Paths:
    harness: Path
    pipeline: Path
    program: Path
    best_pipeline: Path
    experiments: Path
    leaderboard: Path
    runner_log: Path
    snapshots_dir: Path


def _make_paths(args) -> Paths:
    pipeline = Path(args.pipeline).resolve()
    out_dir = pipeline.parent
    return Paths(
        harness=Path(args.harness).resolve(),
        pipeline=pipeline,
        program=Path(args.program).resolve(),
        best_pipeline=out_dir / "best_pipeline.py",
        experiments=out_dir / "experiments.jsonl",
        leaderboard=out_dir / "leaderboard.md",
        runner_log=out_dir / "runner_log.jsonl",
        snapshots_dir=out_dir / "iter_snapshots",
    )


def _snapshot_best(p: Paths) -> None:
    p.best_pipeline.write_text(p.pipeline.read_text())


def _revert_to_best(p: Paths) -> None:
    if p.best_pipeline.exists():
        p.pipeline.write_text(p.best_pipeline.read_text())


def _snapshot_iter(p: Paths, iter_num: int) -> Path:
    """Save current pipeline content as a per-iter snapshot for retune access."""

    p.snapshots_dir.mkdir(exist_ok=True)
    snap = p.snapshots_dir / f"iter_{iter_num:04d}.py"
    snap.write_text(p.pipeline.read_text())
    return snap


def _lint_feature_names(pipeline_code: str) -> tuple[bool, str | None]:
    """Reject pipelines whose code contains opacity-pattern feature names.

    Defense-in-depth on top of program.md's BLOCKER rules. Returns (ok, error).
    `_x_` (interpretable interactions) is allowed; patterns target numeric-suffix
    names, polynomial/embedding/hash prefixes, and anonymous interaction markers.
    """
    for pattern, reason in LINT_PATTERNS:
        match = re.search(pattern, pipeline_code)
        if match:
            return False, f"lint failed: matched `{match.group(0)}` — {reason}"
    return True, None


# ---------- harness interaction ----------

def _import_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_full_experiment(p: Paths, timeout: int) -> dict:
    """Delegate to the harness's run_experiment function."""
    harness = _import_module(p.harness, "autoresearch_harness")
    if not hasattr(harness, "run_experiment"):
        raise AttributeError("harness must define run_experiment(pipeline_path, timeout_seconds) -> dict")
    return harness.run_experiment(p.pipeline, timeout_seconds=timeout)


def _smoke_test(p: Paths) -> tuple[bool, str | None]:
    """Tiny-subset fit+predict catches runtime bugs in seconds (CORR-009)."""
    try:
        pipeline_mod = _import_module(p.pipeline, "smoke_pipeline")
        if not hasattr(pipeline_mod, "build_pipeline"):
            return False, "build_pipeline not defined"

        harness = _import_module(p.harness, "smoke_harness")
        if not hasattr(harness, "load_data"):
            return False, "harness missing load_data"
        X_train, y_train, X_holdout, _ = harness.load_data()
        n_train = min(SMOKE_SAMPLE_N, len(X_train))
        n_test = max(100, n_train // 10)
        X_t, y_t = X_train.iloc[:n_train].reset_index(drop=True), y_train.iloc[:n_train].reset_index(drop=True)
        X_h = X_holdout.iloc[:n_test].reset_index(drop=True)

        est = pipeline_mod.build_pipeline(X_t, y_t)
        if not hasattr(est, "predict_proba"):
            return False, "estimator missing predict_proba"
        _ = est.predict_proba(X_h)
        return True, None
    except Exception as e:
        return False, _sanitize(f"{type(e).__name__}: {e}")


def _read_experiments(p: Paths) -> list[dict]:
    if not p.experiments.exists():
        return []
    return [json.loads(line) for line in p.experiments.open() if line.strip()]


def _log_experiment(p: Paths, iter_num: int, summary: str, result: dict, best: float) -> bool:
    metric = result.get("roc_auc") or result.get("metric")
    is_best = metric is not None and metric > best
    entry = {
        "iter": iter_num, "ts": int(time.time()), "summary": summary,
        "metric": metric, "success": result.get("success", False),
        "error": result.get("error"), "fit_time_s": result.get("fit_time_s"),
        "is_best": is_best,
    }
    with p.experiments.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return is_best


def _update_leaderboard(p: Paths, top_n: int = 10) -> None:
    exps = _read_experiments(p)
    succ = sorted(
        (e for e in exps if e.get("success") and e.get("metric") is not None),
        key=lambda e: e["metric"], reverse=True,
    )
    lines = [
        "# Autoresearch Leaderboard", "",
        f"Total experiments: {len(exps)} · Successful: {len(succ)}", "",
        "| Rank | Iter | Metric | Summary |", "|---:|---:|---:|:---|",
    ]
    for rank, e in enumerate(succ[:top_n], 1):
        lines.append(f"| {rank} | {e['iter']} | {e['metric']:.4f} | {e['summary']} |")
    p.leaderboard.write_text("\n".join(lines) + "\n")


def _best_metric_so_far(p: Paths) -> float:
    return max(
        (e["metric"] for e in _read_experiments(p) if e.get("success") and e.get("metric") is not None),
        default=0.0,
    )


def _log_event(p: Paths, event: dict) -> None:
    """Append a structured event to runner_log.jsonl with path sanitization."""
    sanitized = {k: _sanitize(v) for k, v in event.items()}
    sanitized["ts"] = int(time.time())
    with p.runner_log.open("a") as f:
        f.write(json.dumps(sanitized) + "\n")


# ---------- LLM client (OpenAI; swap import for Anthropic) ----------

def _build_messages(program: str, current_pipeline: str, history: list[dict]):
    summary = "\n".join(
        f"- iter {h['iter']}: {h['summary']} → "
        + (f"{h['metric']:.4f}" if h.get("metric") is not None else f"FAILED ({h.get('error', 'unknown')})")
        + (" ⭐ best" if h.get("is_best") else "")
        for h in history
    ) or "(no prior experiments)"

    system = (
        "You are an autoresearch agent operating on a tabular ML problem. "
        "Read program.md and the current pipeline.py, look at the experiment history, "
        "and propose ONE new pipeline.py that explores a meaningful direction. "
        "Output ONLY the complete new pipeline.py file inside a ```python fenced code block, "
        "followed by a single-line summary on a line starting with `SUMMARY: `. "
        "No other text outside the code block and summary line."
    )
    user = (
        "## program.md\n\n" + program + "\n\n"
        "## current pipeline.py\n\n```python\n" + current_pipeline + "\n```\n\n"
        f"## Recent experiment history (last {len(history)})\n\n" + summary
        + "\n\nPropose the next pipeline.py."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}], system


def _extract(text: str) -> tuple[str | None, str | None]:
    code = re.search(r"```python\n(.*?)```", text or "", re.DOTALL)
    summ = re.search(r"^SUMMARY:\s*(.+)$", text or "", re.MULTILINE)
    return (code.group(1).strip() if code else None), (summ.group(1).strip() if summ else None)


def _compute_cost_openai(model: str, usage) -> float:
    if model not in PRICING:
        return 0.0  # unknown model — cap will trip on time, not cost
    rates = PRICING[model]
    pt = getattr(usage, "prompt_tokens", 0) or 0
    ct = getattr(usage, "completion_tokens", 0) or 0
    cached = 0
    if hasattr(usage, "prompt_tokens_details"):
        cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0) or 0
    return ((pt - cached) * rates["input"] + cached * rates["cached_input"] + ct * rates["output"]) / 1e6


# ---------- main loop ----------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--harness", required=True, help="Path to harness.py")
    parser.add_argument("--pipeline", required=True, help="Path to pipeline.py (mutable)")
    parser.add_argument("--program", required=True, help="Path to program.md")
    parser.add_argument("--baseline-metric", type=float, default=0.0,
                        help="Bayesian-tuning baseline to compare against")
    parser.add_argument("--max-hours", type=float, default=DEFAULT_MAX_HOURS)
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--max-completion-tokens", type=int, default=DEFAULT_MAX_COMPLETION_TOKENS)
    parser.add_argument("--per-iter-timeout", type=int, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--retune-top-n", type=int, default=3,
                        help="LLM-curated top-N retune candidates at end of loop; 0 to disable")
    parser.add_argument("--retune-trials", type=int, default=50,
                        help="Optuna trials per retune candidate")
    parser.add_argument("--no-retune", action="store_true",
                        help="Skip end-of-loop Optuna retune")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    key_var = _verify_api_key()
    p = _make_paths(args)
    if not all(f.exists() for f in (p.harness, p.pipeline, p.program)):
        sys.exit(f"ERROR: harness/pipeline/program file missing")

    program = p.program.read_text()
    print(f"Pre-flight: running current pipeline once...")
    pre = _run_full_experiment(p, args.per_iter_timeout)
    if not pre.get("success"):
        sys.exit(f"Pre-flight failed: {pre.get('error')}")
    metric = pre.get('roc_auc') or pre.get('metric')
    print(f"Pre-flight OK: metric = {metric:.4f}")

    if not p.best_pipeline.exists():
        _snapshot_best(p)
        print(f"Initialized best_pipeline.py snapshot")

    if args.dry_run:
        print(f"DRY RUN OK — would run {args.max_hours}h, ${args.max_cost} cap, model {args.model}")
        return

    # OpenAI client (swap to anthropic.Anthropic() for Anthropic backend)
    from openai import OpenAI, APIError
    client = OpenAI()

    cost = 0.0
    started = time.time()
    iter_num = len(_read_experiments(p))
    _log_event(p, {"event": "run_start", "model": args.model, "max_hours": args.max_hours,
                   "max_cost": args.max_cost, "starting_iter": iter_num})

    while True:
        elapsed_h = (time.time() - started) / 3600
        if elapsed_h >= args.max_hours:
            _log_event(p, {"event": "halt", "reason": "time_cap", "elapsed_hours": elapsed_h, "cost": cost})
            print(f"\n=== TIME CAP: {elapsed_h:.2f}h ==="); break
        if cost >= args.max_cost:
            _log_event(p, {"event": "halt", "reason": "cost_cap", "cost": cost})
            print(f"\n=== COST CAP: ${cost:.2f} ==="); break

        current_pipeline = p.pipeline.read_text()
        history = _read_experiments(p)[-HISTORY_TAIL:]
        messages, system_text = _build_messages(program, current_pipeline, history)

        try:
            response = client.chat.completions.create(
                model=args.model, messages=messages,
                max_completion_tokens=args.max_completion_tokens,
            )
        except APIError as e:
            err = _sanitize(f"{type(e).__name__}: {e}")
            _log_event(p, {"event": "api_error", "iter": iter_num, "error": err})
            print(f"  iter {iter_num}: API error — {err}")
            time.sleep(5); continue

        cost_this = _compute_cost_openai(args.model, response.usage)
        cost += cost_this
        agent_text = response.choices[0].message.content or ""
        new_pipeline, summary = _extract(agent_text)

        if not new_pipeline or not summary:
            _log_event(p, {
                "event": "parse_error", "iter": iter_num, "cost_usd": cost_this,
                "finish_reason": getattr(response.choices[0], "finish_reason", "?"),
                "response_len": len(agent_text), "has_fence": "```python" in agent_text,
                "has_summary": "SUMMARY:" in agent_text,
                "response_head": _sanitize(agent_text[:300]),
            })
            print(f"  iter {iter_num}: parse error (finish={getattr(response.choices[0], 'finish_reason', '?')})")
            iter_num += 1; continue

        # Feature-name lint pre-smoke — defense-in-depth on top of program.md
        lint_ok, lint_err = _lint_feature_names(new_pipeline)
        if not lint_ok:
            failed = {"success": False, "error": lint_err, "metric": None, "fit_time_s": None}
            _log_experiment(p, iter_num, summary, failed, _best_metric_so_far(p))
            _update_leaderboard(p)
            # Do NOT write the proposed pipeline; current pipeline.py stays as the best
            _log_event(p, {"event": "lint_failed", "iter": iter_num, "cost_usd": cost_this,
                           "accumulated_cost_usd": cost, "error": lint_err})
            print(f"  iter {iter_num}: LINT FAILED — {lint_err}")
            iter_num += 1; continue

        # Apply, smoke-test, then full-run if smoke passes
        p.pipeline.write_text(new_pipeline)
        smoke_ok, smoke_err = _smoke_test(p)
        if not smoke_ok:
            failed = {"success": False, "error": f"smoke_test_failed: {smoke_err}",
                      "metric": None, "fit_time_s": None}
            _log_experiment(p, iter_num, summary, failed, _best_metric_so_far(p))
            _update_leaderboard(p)
            _revert_to_best(p)
            _log_event(p, {"event": "smoke_failed", "iter": iter_num, "cost_usd": cost_this,
                           "accumulated_cost_usd": cost, "error": smoke_err})
            print(f"  iter {iter_num}: SMOKE FAILED — {smoke_err[:80]}")
            iter_num += 1; continue

        exp_result = _run_full_experiment(p, args.per_iter_timeout)
        is_best = _log_experiment(p, iter_num, summary, exp_result, _best_metric_so_far(p))
        _update_leaderboard(p)

        # Snapshot every successful iter so retune-top-N can access non-best entries
        if exp_result.get("success"):
            _snapshot_iter(p, iter_num)

        # Hill-climb-from-best (CORR-008)
        if exp_result.get("success") and is_best:
            _snapshot_best(p)
        else:
            _revert_to_best(p)

        _log_event(p, {
            "event": "iter_complete", "iter": iter_num, "model": args.model,
            "cost_usd": cost_this, "accumulated_cost_usd": cost, "elapsed_hours": elapsed_h,
            "experiment_success": exp_result.get("success"),
            "metric": exp_result.get("roc_auc") or exp_result.get("metric"),
            "is_best": is_best,
        })

        marker = " ⭐ NEW BEST" if is_best else ""
        m = exp_result.get('roc_auc') or exp_result.get('metric')
        if exp_result.get("success"):
            print(f"  iter {iter_num}: metric {m:.4f}{marker} "
                  f"(${cost_this:.3f}, total ${cost:.2f}) — {summary[:80]}")
        else:
            print(f"  iter {iter_num}: FAILED — {(exp_result.get('error') or '')[:80]}")

        iter_num += 1

    print(f"\nFinal: best {_best_metric_so_far(p):.4f} over {iter_num} iters, ${cost:.2f}")

    # Optuna-retune-top-N at end of loop — LLM-curated picks, per-trial logging
    if not args.no_retune and args.retune_top_n > 0:
        print(f"\n=== Starting Optuna retune on LLM-curated top-{args.retune_top_n} ===")
        _log_event(p, {"event": "retune_start", "top_n": args.retune_top_n,
                       "trials": args.retune_trials})
        try:
            from .retune import retune_top_n
            harness = _import_module(p.harness, "autoresearch_harness")
            retune_top_n(
                experiments_path=p.experiments,
                snapshots_dir=p.snapshots_dir,
                output_dir=p.pipeline.parent,
                top_n=args.retune_top_n,
                n_trials=args.retune_trials,
                harness_module=harness,
                runner_log_path=p.runner_log,
            )
            _log_event(p, {"event": "retune_complete"})
        except Exception as e:
            err = _sanitize(f"{type(e).__name__}: {e}")
            _log_event(p, {"event": "retune_failed", "error": err})
            print(f"Retune failed: {err}")


if __name__ == "__main__":
    main()
