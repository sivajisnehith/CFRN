"""Run the full CFRN experiment and write results + plots (Phase 10).

Usage:
    python -m experiments.run_experiment            # uses config/default.yaml
    python -m experiments.run_experiment --config config/default.yaml --scenarios 60
"""
from __future__ import annotations

import argparse
import os
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

# Make the project importable whether run as module or script.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.config import Paths, load_config, seed_everything  # noqa: E402
from src.pipeline import CFRNPipeline  # noqa: E402


def _plots(df: pd.DataFrame, out_dir: str) -> None:
    # 1) Baseline vs CFRN propagation debt.
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(df["scenario"], df["baseline_cpd"], label="Baseline CPD (do nothing)",
            marker="o", ms=3)
    ax.plot(df["scenario"], df["cfrn_cpd"], label="CFRN CPD (m*)",
            marker="s", ms=3)
    ax.set_xlabel("Scenario")
    ax.set_ylabel("Counterfactual Propagation Debt")
    ax.set_title("CFRN vs Baseline: Propagation Debt per Scenario")
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "cpd_comparison.png"), dpi=120)
    plt.close(fig)

    # 2) Cascade size reduction.
    fig, ax = plt.subplots(figsize=(6, 4))
    ax.bar(["Baseline", "CFRN"],
           [df["baseline_cascade_size"].mean(), df["cfrn_cascade_size"].mean()],
           color=["#c0504d", "#4472c4"])
    ax.set_ylabel("Mean cascade size (links failed)")
    ax.set_title("Mean Cascade Size: Baseline vs CFRN")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "cascade_size.png"), dpi=120)
    plt.close(fig)

    # 3) Mutation selection frequency.
    fig, ax = plt.subplots(figsize=(6, 4))
    counts = df["best_mutation"].value_counts()
    ax.bar(counts.index.astype(str), counts.values, color="#4472c4")
    ax.set_ylabel("Times selected as m*")
    ax.set_title("Selected Mutation Types")
    plt.setp(ax.get_xticklabels(), rotation=20, ha="right")
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "mutation_selection.png"), dpi=120)
    plt.close(fig)

    # 4) Learning: surrogate prediction error over scenarios.
    err = df["prediction_error"].dropna()
    if len(err) > 0:
        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(err.index, err.values, marker="o", ms=3, color="#7030a0")
        ax.set_xlabel("Scenario")
        ax.set_ylabel("|predicted - observed| CPD")
        ax.set_title("Self-Evolution: Surrogate CPD Prediction Error")
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "learning_error.png"), dpi=120)
        plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run CFRN experiment")
    ap.add_argument("--config", default=None)
    ap.add_argument("--scenarios", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    seed_everything(int(cfg["seed"]))
    paths = Paths(_ROOT).ensure()

    n = args.scenarios or int(cfg["experiment"]["n_scenarios"])
    out_dir = os.path.join(_ROOT, cfg["experiment"]["out_dir"])
    os.makedirs(out_dir, exist_ok=True)

    print(f"Building CFRN environment (seed={cfg['seed']}) ...")
    pipe = CFRNPipeline(cfg, seed=int(cfg["seed"]))
    print(f"  topology source : {pipe.graph.graph.get('source')}")
    print(f"  topology        : {pipe.topo_feats['n_nodes']} nodes, "
          f"{pipe.topo_feats['n_links']} links")
    print(f"  traffic source  : {pipe.traffic.attrs.get('source')}")
    print(f"  traffic series  : {pipe.traffic.shape[0]}")
    print(f"Running {n} closed-loop scenarios ...")

    df = pipe.run_experiment(n)

    csv_path = os.path.join(out_dir, "cfrn_results.csv")
    df.to_csv(csv_path, index=False)
    _plots(df, out_dir)

    # Save evolutionary surrogate model.
    model_path = os.path.join(_ROOT, cfg["learning"]["model_path"])
    pipe.knowledge.save(model_path)

    # Summary.
    base = df["baseline_cpd"].mean()
    cfrn = df["cfrn_cpd"].mean()
    reduction = 100.0 * (base - cfrn) / base if base else 0.0
    print("\n==================== CFRN SUMMARY ====================")
    print(f"Scenarios run              : {len(df)}")
    print(f"Mean baseline CPD          : {base:.4f}")
    print(f"Mean CFRN CPD (m*)         : {cfrn:.4f}")
    print(f"Mean propagation-debt drop : {reduction:.1f}%")
    print(f"Mean cascade size baseline : {df['baseline_cascade_size'].mean():.2f}")
    print(f"Mean cascade size CFRN     : {df['cfrn_cascade_size'].mean():.2f}")
    print(f"Learning summary           : {pipe.knowledge.summary()}")
    print(f"Results CSV                : {csv_path}")
    print(f"Plots + model              : {out_dir}, {model_path}")
    print("======================================================")


if __name__ == "__main__":
    main()
