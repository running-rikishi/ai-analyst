"""
Harness factory — turn a YAML config into a Harness ready for autoresearch.

The original autoresearch harness was hard-coded to one dataset (IEEE-CIS Fraud).
This factory generalizes it: point at a YAML config that describes your dataset
(main table, target column, split strategy, scorer, optional auxiliary tables),
and get back a Harness with the same load_data() / run_experiment() interface
the runner expects.

Two modes:

  1. **Single-table mode** (`multi_table: false` or no auxiliary tables):
     Harness pre-joins auxiliary tables onto the main table via the configured
     join keys (assumes 1:1 joins). `load_data()` returns
     `(X_train, y_train, X_holdout, y_holdout)`. The agent's pipeline.py
     receives `X_train, y_train` as before — fully backward compatible with
     the existing autoresearch contract.

  2. **Multi-table mode** (`multi_table: true`):
     Harness does NOT pre-join. `load_data()` returns
     `(tables_train, y_train, tables_holdout, y_holdout)` where `tables_*`
     is a `dict[str, DataFrame]` keyed by table name. The agent's pipeline.py
     receives the raw tables dict and must invent its own aggregations/joins.
     This is the mode for Kaggle-style multi-table competitions (Home Credit,
     etc.) where the agent's job includes inventing cross-table features.

Usage:

  >>> from helpers.autoresearch.harness_factory import build_harness
  >>> harness = build_harness("configs/home_credit.yaml")
  >>> X_train, y_train, X_holdout, y_holdout = harness.load_data()  # single-table
  >>> # or: tables_train, y_train, tables_holdout, y_holdout = harness.load_data()  # multi-table
  >>> result = harness.run_experiment(pipeline_path)
"""
from __future__ import annotations

import importlib.util
import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml
from sklearn.metrics import (
    roc_auc_score, mean_squared_error,
    accuracy_score, f1_score,
)


# ---------- scorers ----------

def _roc_auc(y_true, y_pred):
    p = y_pred[:, 1] if (hasattr(y_pred, "ndim") and y_pred.ndim > 1) else y_pred
    return float(roc_auc_score(y_true, p))


def _rmse_neg(y_true, y_pred):
    """Higher = better (negated RMSE) so the framework's max-direction works uniformly."""
    p = y_pred[:, 0] if (hasattr(y_pred, "ndim") and y_pred.ndim > 1 and y_pred.shape[1] == 1) else y_pred
    return float(-np.sqrt(mean_squared_error(y_true, p)))


def _accuracy(y_true, y_pred):
    if hasattr(y_pred, "ndim") and y_pred.ndim > 1 and y_pred.shape[1] > 1:
        labels = np.argmax(y_pred, axis=1)
    else:
        labels = (y_pred > 0.5).astype(int)
    return float(accuracy_score(y_true, labels))


def _f1(y_true, y_pred):
    if hasattr(y_pred, "ndim") and y_pred.ndim > 1 and y_pred.shape[1] > 1:
        labels = np.argmax(y_pred, axis=1)
    else:
        labels = (y_pred > 0.5).astype(int)
    return float(f1_score(y_true, labels))


SCORERS = {
    "roc_auc": _roc_auc,
    "rmse": _rmse_neg,        # returned negative so "higher is better" holds
    "accuracy": _accuracy,
    "f1": _f1,
}


# ---------- config dataclass ----------

@dataclass
class HarnessConfig:
    name: str
    data_dir: Path
    main_table_path: Path
    main_table_format: str  # "csv" or "parquet"
    target_column: str
    id_column: str
    auxiliary_tables: list[dict] = field(default_factory=list)
    multi_table: bool = False
    split_strategy: str = "random"  # "random" | "time_ordered" | "stratified"
    holdout_frac: float = 0.20
    time_column: str | None = None  # required for time_ordered
    random_seed: int = 42
    scorer: str = "roc_auc"
    timeout_seconds: int = 600


def _load_config(config_path: str | Path) -> HarnessConfig:
    config_path = Path(config_path).resolve()
    with config_path.open() as f:
        raw = yaml.safe_load(f)

    data_dir = Path(raw.get("data_dir", "."))
    if not data_dir.is_absolute():
        data_dir = (config_path.parent / data_dir).resolve()

    main = raw["main_table"]
    aux = raw.get("auxiliary_tables", []) or []
    multi_table = raw.get("multi_table", False)

    split = raw.get("split", {})

    return HarnessConfig(
        name=raw["name"],
        data_dir=data_dir,
        main_table_path=data_dir / main["path"],
        main_table_format=main.get("format", "csv"),
        target_column=raw["target_column"],
        id_column=raw["id_column"],
        auxiliary_tables=aux,
        multi_table=multi_table,
        split_strategy=split.get("strategy", "random"),
        holdout_frac=split.get("holdout_frac", 0.20),
        time_column=split.get("time_column"),
        random_seed=split.get("random_seed", 42),
        scorer=raw.get("scorer", "roc_auc"),
        timeout_seconds=raw.get("timeout_seconds", 600),
    )


# ---------- data loading ----------

def _read_table(path: Path, fmt: str) -> pd.DataFrame:
    if fmt == "csv":
        return pd.read_csv(path)
    if fmt == "parquet":
        return pd.read_parquet(path)
    raise ValueError(f"Unsupported table format: {fmt}")


def _load_all_tables(config: HarnessConfig) -> dict[str, pd.DataFrame]:
    """Load main + every auxiliary table into a dict keyed by configured name."""
    if not config.main_table_path.exists():
        raise FileNotFoundError(
            f"Main table not found at {config.main_table_path}. "
            f"Check data_dir + main_table.path in {config.name} config."
        )
    tables = {"main": _read_table(config.main_table_path, config.main_table_format)}
    for aux in config.auxiliary_tables:
        name = aux.get("name") or Path(aux["path"]).stem
        aux_path = config.data_dir / aux["path"]
        if not aux_path.exists():
            raise FileNotFoundError(f"Auxiliary table not found at {aux_path}")
        tables[name] = _read_table(aux_path, aux.get("format", "csv"))
    return tables


def _split_indices(df: pd.DataFrame, config: HarnessConfig) -> tuple[np.ndarray, np.ndarray]:
    """Return (train_idx, holdout_idx) for the main table per config.split_strategy."""
    n = len(df)
    rng = np.random.RandomState(config.random_seed)

    if config.split_strategy == "time_ordered":
        if not config.time_column or config.time_column not in df.columns:
            raise ValueError(f"time_ordered split requires time_column; got {config.time_column!r}")
        ordered_idx = df[config.time_column].argsort().values
        n_train = int(n * (1 - config.holdout_frac))
        return ordered_idx[:n_train], ordered_idx[n_train:]

    if config.split_strategy == "stratified":
        y = df[config.target_column].values
        # Per-class shuffle, then take last holdout_frac of each class
        train_idx, holdout_idx = [], []
        for cls in np.unique(y):
            cls_idx = np.where(y == cls)[0]
            rng.shuffle(cls_idx)
            n_holdout = int(len(cls_idx) * config.holdout_frac)
            holdout_idx.extend(cls_idx[:n_holdout])
            train_idx.extend(cls_idx[n_holdout:])
        return np.array(train_idx), np.array(holdout_idx)

    # random (default)
    perm = rng.permutation(n)
    n_train = int(n * (1 - config.holdout_frac))
    return perm[:n_train], perm[n_train:]


def _pre_join_aux(main: pd.DataFrame, aux_tables: dict[str, pd.DataFrame],
                  aux_specs: list[dict]) -> pd.DataFrame:
    """Single-table mode: pre-join all auxiliary tables onto main.

    Assumes 1:1 joins via the configured join_key. For 1:N auxiliaries, use
    multi_table mode instead — pre-joining 1:N expands rows in misleading ways.
    """
    out = main.copy()
    for aux in aux_specs:
        name = aux.get("name") or Path(aux["path"]).stem
        join_key = aux["join_key"]
        how = aux.get("join_strategy", "left")
        out = out.merge(aux_tables[name], on=join_key, how=how, suffixes=("", f"_{name}"))
    return out


# ---------- harness class ----------

class Harness:
    """A bound harness: dataset-specific config + autoresearch interface.

    The runner expects: load_data() returns a tuple, run_experiment() returns
    a dict, log_experiment / update_leaderboard / read_experiments /
    best_metric_so_far / SUMMARY mechanics.
    """

    def __init__(self, config: HarnessConfig, output_dir: Path):
        self.config = config
        self.output_dir = Path(output_dir).resolve()
        self.experiments_path = self.output_dir / "experiments.jsonl"
        self.leaderboard_path = self.output_dir / "leaderboard.md"
        self._cache: dict[str, Any] = {}

    # ----- data -----

    def load_data(self):
        """Single-table mode: returns (X_train, y_train, X_holdout, y_holdout).
        Multi-table mode:  returns (tables_train, y_train, tables_holdout, y_holdout)
        where tables_* are dicts of table-name -> DataFrame."""
        if self._cache:
            return self._cache["payload"]

        all_tables = _load_all_tables(self.config)
        main = all_tables["main"]
        train_idx, holdout_idx = _split_indices(main, self.config)

        y = main[self.config.target_column]
        y_train = y.iloc[train_idx].reset_index(drop=True)
        y_holdout = y.iloc[holdout_idx].reset_index(drop=True)

        if self.config.multi_table:
            # Hand the agent raw tables; let it aggregate/join itself.
            main_train = main.iloc[train_idx].drop(columns=[self.config.target_column]).reset_index(drop=True)
            main_holdout = main.iloc[holdout_idx].drop(columns=[self.config.target_column]).reset_index(drop=True)
            tables_train = {"main": main_train}
            tables_holdout = {"main": main_holdout}
            for aux_name, df in all_tables.items():
                if aux_name == "main":
                    continue
                # Auxiliary tables are passed in full to BOTH train and holdout;
                # the agent's pipeline filters them by join keys as needed.
                tables_train[aux_name] = df
                tables_holdout[aux_name] = df
            payload = (tables_train, y_train, tables_holdout, y_holdout)
        else:
            joined = _pre_join_aux(main, all_tables, self.config.auxiliary_tables)
            X_train = joined.iloc[train_idx].drop(columns=[self.config.target_column]).reset_index(drop=True)
            X_holdout = joined.iloc[holdout_idx].drop(columns=[self.config.target_column]).reset_index(drop=True)
            payload = (X_train, y_train, X_holdout, y_holdout)

        self._cache["payload"] = payload
        return payload

    # ----- experiment runner -----

    def _load_pipeline_module(self, pipeline_path: Path):
        spec = importlib.util.spec_from_file_location("pipeline_runtime", pipeline_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "build_pipeline"):
            raise AttributeError(f"pipeline.py at {pipeline_path} must define build_pipeline(...)")
        return module

    def run_experiment(self, pipeline_path: Path, timeout_seconds: int | None = None) -> dict:
        """Run one experiment: load pipeline, fit, score on holdout. Return result dict."""
        timeout_seconds = timeout_seconds or self.config.timeout_seconds
        result = {
            "metric_name": self.config.scorer,
            "metric": None, "fit_time_s": None, "predict_time_s": None,
            "total_time_s": None, "success": False, "error": None,
        }
        t0 = time.time()
        try:
            module = self._load_pipeline_module(Path(pipeline_path))
            payload = self.load_data()

            t_fit = time.time()
            if self.config.multi_table:
                tables_train, y_train, tables_holdout, y_holdout = payload
                # Multi-table contract: build_pipeline(tables, y_train)
                estimator = module.build_pipeline(tables_train, y_train)
            else:
                X_train, y_train, X_holdout, y_holdout = payload
                estimator = module.build_pipeline(X_train, y_train)
            result["fit_time_s"] = round(time.time() - t_fit, 2)
            if time.time() - t0 > timeout_seconds:
                raise TimeoutError(f"fit exceeded timeout ({timeout_seconds}s)")

            if not hasattr(estimator, "predict_proba") and not hasattr(estimator, "predict"):
                raise AttributeError("estimator must have predict_proba or predict")

            t_pred = time.time()
            if self.config.multi_table:
                pred_input = tables_holdout
            else:
                pred_input = X_holdout
            if hasattr(estimator, "predict_proba"):
                proba = estimator.predict_proba(pred_input)
            else:
                proba = estimator.predict(pred_input)
            result["predict_time_s"] = round(time.time() - t_pred, 2)

            scorer = SCORERS.get(self.config.scorer)
            if scorer is None:
                raise ValueError(f"Unknown scorer {self.config.scorer!r}; supported: {list(SCORERS)}")
            metric = scorer(y_holdout, proba)
            result["metric"] = float(metric)
            result["roc_auc"] = float(metric) if self.config.scorer == "roc_auc" else None
            result["success"] = True
        except Exception as exc:
            err = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
            home = str(Path.home())
            result["error"] = err.replace(home, "<HOME>")
        result["total_time_s"] = round(time.time() - t0, 2)
        return result

    # ----- experiment log + leaderboard (compat with runner) -----

    def log_experiment(self, iter_num: int, summary: str, result: dict, best_so_far: float) -> bool:
        metric = result.get("metric") if result.get("metric") is not None else result.get("roc_auc")
        is_best = (result.get("success") and metric is not None and metric > best_so_far)
        entry = {
            "iter": iter_num, "ts": int(time.time()), "summary": summary,
            "metric_name": result.get("metric_name", self.config.scorer),
            "metric": metric, "roc_auc": result.get("roc_auc"),
            "fit_time_s": result.get("fit_time_s"),
            "predict_time_s": result.get("predict_time_s"),
            "total_time_s": result.get("total_time_s"),
            "success": result.get("success", False),
            "error": result.get("error"),
            "is_best": is_best,
        }
        with self.experiments_path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return is_best

    def read_experiments(self) -> list[dict]:
        if not self.experiments_path.exists():
            return []
        with self.experiments_path.open() as f:
            return [json.loads(line) for line in f if line.strip()]

    def best_metric_so_far(self) -> float:
        return max(
            (e.get("metric") for e in self.read_experiments()
             if e.get("success") and e.get("metric") is not None),
            default=0.0,
        )

    def update_leaderboard(self, top_n: int = 10) -> None:
        exps = self.read_experiments()
        succ = sorted(
            (e for e in exps if e.get("success") and e.get("metric") is not None),
            key=lambda e: e["metric"], reverse=True,
        )
        lines = [
            f"# Leaderboard — {self.config.name}", "",
            f"Total experiments: {len(exps)} · Successful: {len(succ)} · Scorer: {self.config.scorer}", "",
            "| Rank | Iter | Metric | Summary |", "|---:|---:|---:|:---|",
        ]
        for rank, e in enumerate(succ[:top_n], 1):
            lines.append(f"| {rank} | {e['iter']} | {e['metric']:.4f} | {e['summary']} |")
        self.leaderboard_path.write_text("\n".join(lines) + "\n")


# ---------- factory ----------

def build_harness(config_path: str | Path, output_dir: str | Path | None = None) -> Harness:
    """Read YAML config, return a Harness ready for autoresearch.

    Args:
        config_path: path to a YAML file conforming to the harness config schema.
        output_dir: where to write experiments.jsonl + leaderboard.md. Defaults
                    to the config file's parent directory.
    """
    config = _load_config(config_path)
    if output_dir is None:
        output_dir = Path(config_path).parent
    harness = Harness(config, output_dir)
    return harness


# ---------- CLI smoke test ----------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Smoke-test a harness config.")
    parser.add_argument("config", help="Path to YAML config")
    parser.add_argument("--dry-run", action="store_true", help="Validate config + file existence only")
    args = parser.parse_args()

    config = _load_config(args.config)
    print(f"Config: {config.name}")
    print(f"Data dir: {config.data_dir}")
    print(f"Main table: {config.main_table_path} ({config.main_table_format})")
    print(f"Target: {config.target_column} | ID: {config.id_column}")
    print(f"Auxiliary tables: {len(config.auxiliary_tables)}")
    print(f"Multi-table mode: {config.multi_table}")
    print(f"Split: {config.split_strategy} (holdout {config.holdout_frac:.0%}, seed {config.random_seed})")
    print(f"Scorer: {config.scorer}")

    if not args.dry_run:
        harness = build_harness(args.config)
        payload = harness.load_data()
        if config.multi_table:
            tables_train, y_train, tables_holdout, y_holdout = payload
            print(f"\nMulti-table mode loaded:")
            for name, df in tables_train.items():
                print(f"  tables_train['{name}']: {df.shape}")
            print(f"  y_train: {y_train.shape} (pos rate {y_train.mean():.4f})")
            print(f"  y_holdout: {y_holdout.shape}")
        else:
            X_train, y_train, X_holdout, y_holdout = payload
            print(f"\nSingle-table mode loaded:")
            print(f"  X_train: {X_train.shape}, X_holdout: {X_holdout.shape}")
            print(f"  y_train: {y_train.shape} (pos rate {y_train.mean():.4f})")
