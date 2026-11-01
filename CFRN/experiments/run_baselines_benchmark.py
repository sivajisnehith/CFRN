"""Comprehensive Multi-Baseline & Statistical Significance Benchmark (Corrections 9 & 11).

Evaluates CFRN against 6 distinct recovery paradigms across identical scenarios:
1. Do-Nothing Baseline (unmitigated failure cascade)
2. Shortest-Path Rerouting (SPR: local shortest path bypass)
3. Greedy Overload Reduction (GOR: myopic single-step peak utilization reduction)
4. Centrality-Based Intervention (CBI: high betweenness priority)
5. Conventional Resilience Optimization (CRO: giant component & spectral expansion)
6. Constrained Multi-Objective Reconfiguration (CMOR: static multi-commodity flow)
7. Supervised Learning Policy (SLP: static feature regressor ranking)
8. CFRN (Uncertainty-Aware Causal Marginal Cascade-Risk Optimization)

Computes paired Student's t-test, Wilcoxon signed-rank test, Cohen's d effect sizes,
and 95% confidence intervals.
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Dict, List

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_REPO_ROOT = os.path.dirname(_ROOT)

from src.config import load_config, seed_everything
from src.counterfactual.mutations import generate_mutations
from src.evaluation.statistics import (
    ComparisonStats,
    compute_comparison_stats,
    compute_metric_stats,
    format_statistical_table,
    partition_scenarios,
)
from src.failure.injector import apply_failure, select_failure
from src.network.state import build_network_state
from src.optimization.baselines import evaluate_all_baselines
from src.optimization.ces import optimize, select_best
from src.pipeline import CFRNPipeline


def run_benchmark(n_scenarios: int = 30) -> Tuple[pd.DataFrame, List[ComparisonStats]]:
    cfg = load_config()
    seed_everything(int(cfg["seed"]))

    print(f"Initializing Multi-Baseline Benchmark across {n_scenarios} scenarios ...")
    pipe = CFRNPipeline(cfg, seed=int(cfg["seed"]))

    records = []

    for s_idx in range(n_scenarios):
        base_seed = int(cfg["seed"]) + s_idx
        state0 = pipe.base_state(base_seed)
        failure = select_failure(state0, cfg, seed=base_seed)

        # Baseline unmitigated state
        post_state = state0.copy()
        downed = apply_failure(post_state, failure)

        # Generate candidate mutations under identical budget and graph rules
        mutations = generate_mutations(post_state, downed, cfg, seed=base_seed)

        # 1. Evaluate all comparative baselines
        baseline_results = evaluate_all_baselines(post_state, downed, mutations, cfg, seed=base_seed)

        # 2. Evaluate CFRN (Active screening + C-CPD constrained optimization)
        b_res = baseline_results["Do-Nothing (Baseline)"].cascade_result
        evals = optimize(
            post_state,
            downed,
            mutations,
            b_res.causal_cpd,
            cfg,
            seed=base_seed,
            mechanism="constrained",
            knowledge=pipe.knowledge,
        )
        best_cfrn = select_best(evals, mechanism="constrained")
        if best_cfrn is not None and best_cfrn.delta_cpd >= 0.0:
            cfrn_m = best_cfrn.mutation
            cfrn_cf = best_cfrn.cascade
            cfrn_cpd = cfrn_cf.causal_cpd
            cfrn_sec = cfrn_cf.secondary_debt
            cfrn_high = cfrn_cf.higher_order_debt
            cfrn_size = cfrn_cf.cascade_size
            cfrn_cost = best_cfrn.mutation_cost
        else:
            cfrn_cpd = b_res.causal_cpd
            cfrn_sec = b_res.secondary_debt
            cfrn_high = b_res.higher_order_debt
            cfrn_size = b_res.cascade_size
            cfrn_cost = 0.0

        rec = {
            "scenario": s_idx,
            "failure": failure.describe(),
            "baseline_cpd": b_res.causal_cpd,
            "spr_cpd": baseline_results["Shortest-Path Rerouting (SPR)"].causal_cpd,
            "gor_cpd": baseline_results["Greedy Overload Reduction (GOR)"].causal_cpd,
            "cbi_cpd": baseline_results["Centrality-Based Intervention (CBI)"].causal_cpd,
            "cro_cpd": baseline_results["Conventional Resilience (CRO)"].causal_cpd,
            "cmor_cpd": baseline_results["Constrained Multi-Objective (CMOR)"].causal_cpd,
            "slp_cpd": baseline_results["Supervised Learning Policy (SLP)"].causal_cpd,
            "cfrn_cpd": cfrn_cpd,
            "cfrn_sec_debt": cfrn_sec,
            "cfrn_high_debt": cfrn_high,
            "cfrn_cascade_size": cfrn_size,
            "cfrn_opex_cost": cfrn_cost,
        }
        records.append(rec)

        # Update surrogate model
        pipe.knowledge.step_epoch()
        if s_idx % 10 == 0:
            pipe.knowledge.maybe_train(min_samples=15)

    df = pd.DataFrame(records)

    # Statistical significance comparisons of CFRN vs every baseline
    comparisons = [
        compute_comparison_stats(df["baseline_cpd"], df["cfrn_cpd"], "Do-Nothing (Baseline)", "CFRN (C-CPD)"),
        compute_comparison_stats(df["spr_cpd"], df["cfrn_cpd"], "Shortest-Path (SPR)", "CFRN (C-CPD)"),
        compute_comparison_stats(df["gor_cpd"], df["cfrn_cpd"], "Greedy Overload (GOR)", "CFRN (C-CPD)"),
        compute_comparison_stats(df["cbi_cpd"], df["cfrn_cpd"], "Centrality-Based (CBI)", "CFRN (C-CPD)"),
        compute_comparison_stats(df["cro_cpd"], df["cfrn_cpd"], "Conventional Resilience (CRO)", "CFRN (C-CPD)"),
        compute_comparison_stats(df["cmor_cpd"], df["cfrn_cpd"], "Constrained Multi-Obj (CMOR)", "CFRN (C-CPD)"),
        compute_comparison_stats(df["slp_cpd"], df["cfrn_cpd"], "Supervised Learning (SLP)", "CFRN (C-CPD)"),
    ]

    return df, comparisons


def plot_baseline_comparison(df: pd.DataFrame, out_dir: str) -> None:
    """Generate comparative visualization across all 8 strategies."""
    methods = [
        ("Do-Nothing (Baseline)", "baseline_cpd", "#c0504d"),
        ("Shortest-Path (SPR)", "spr_cpd", "#ed7d31"),
        ("Greedy Overload (GOR)", "gor_cpd", "#a5a5a5"),
        ("Centrality-Based (CBI)", "cbi_cpd", "#ffc000"),
        ("Conventional Res. (CRO)", "cro_cpd", "#4472c4"),
        ("Constrained Multi-Obj (CMOR)", "cmor_cpd", "#70ad47"),
        ("Supervised Learning (SLP)", "slp_cpd", "#7030a0"),
        ("CFRN (C-CPD, Ours)", "cfrn_cpd", "#1f4e79"),
    ]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 5))

    names = [m[0] for m in methods]
    means = [df[m[1]].mean() for m in methods]
    stds = [df[m[1]].std() for m in methods]
    colors = [m[2] for m in methods]

    # Panel 1: Bar chart with error bars
    bars = ax1.bar(range(len(names)), means, yerr=stds, capsize=4, color=colors, width=0.6, edgecolor="#333333")
    ax1.set_xticks(range(len(names)))
    ax1.set_xticklabels(names, rotation=30, ha="right", fontsize=9)
    ax1.set_ylabel("Mean Causal Propagation Debt (C-CPD)", fontsize=10)
    ax1.set_title("(a) Mean Cascade Debt Across Reconfiguration Strategies", fontsize=11, fontweight="bold")
    ax1.grid(axis="y", linestyle="--", alpha=0.4)
    for b in bars:
        ax1.text(b.get_x() + b.get_width() / 2.0, b.get_height() + 0.1, f"{b.get_height():.2f}", ha="center", va="bottom", fontsize=8.5, fontweight="bold")

    # Panel 2: Scenario trajectory comparison
    scens = df["scenario"]
    ax2.plot(scens, df["baseline_cpd"], label="Do-Nothing", color="#c0504d", linestyle=":", alpha=0.7)
    ax2.plot(scens, df["cro_cpd"], label="Conventional (CRO)", color="#4472c4", alpha=0.6)
    ax2.plot(scens, df["cmor_cpd"], label="Multi-Obj (CMOR)", color="#70ad47", alpha=0.6)
    ax2.plot(scens, df["cfrn_cpd"], label="CFRN (Ours)", color="#1f4e79", linewidth=2.0, marker="s", ms=3)
    ax2.set_xlabel("Scenario Index", fontsize=10)
    ax2.set_ylabel("C-CPD", fontsize=10)
    ax2.set_title("(b) Scenario-by-Scenario Resilience Trajectories", fontsize=11, fontweight="bold")
    ax2.legend(frameon=True, fontsize=9)
    ax2.grid(True, linestyle="--", alpha=0.4)

    fig.tight_layout()
    fig_path = os.path.join(out_dir, "baseline_comparison.png")
    fig.savefig(fig_path, dpi=150)
    plt.close(fig)

    # Sync to assets
    assets_dir = os.path.join(_REPO_ROOT, "assets")
    if os.path.isdir(assets_dir):
        import shutil
        shutil.copy2(fig_path, os.path.join(assets_dir, "baseline_comparison.png"))


def main() -> None:
    ap = argparse.ArgumentParser(description="Run Multi-Baseline Benchmark")
    ap.add_argument("--scenarios", type=int, default=30)
    args = ap.parse_args()

    out_dir = os.path.join(_ROOT, "results")
    os.makedirs(out_dir, exist_ok=True)

    df, comparisons = run_benchmark(args.scenarios)
    csv_path = os.path.join(out_dir, "multi_baseline_benchmark.csv")
    df.to_csv(csv_path, index=False)

    plot_baseline_comparison(df, out_dir)

    print("\n" + "=" * 115)
    print("COMPREHENSIVE MULTI-BASELINE BENCHMARK & STATISTICAL SIGNIFICANCE (CORRECTIONS 9 & 11)")
    print("=" * 115)
    table_md = format_statistical_table(comparisons)
    print(table_md)
    print("=" * 115)

    # Metric summaries with 95% CIs
    print("\nMETRIC ESTIMATION WITH 95% CONFIDENCE INTERVALS:")
    for col, name in [
        ("baseline_cpd", "Do-Nothing (Baseline)"),
        ("spr_cpd", "Shortest-Path Rerouting (SPR)"),
        ("gor_cpd", "Greedy Overload Reduction (GOR)"),
        ("cbi_cpd", "Centrality-Based Intervention (CBI)"),
        ("cro_cpd", "Conventional Resilience (CRO)"),
        ("cmor_cpd", "Constrained Multi-Objective (CMOR)"),
        ("slp_cpd", "Supervised Learning Policy (SLP)"),
        ("cfrn_cpd", "CFRN (C-CPD, Proposed)"),
    ]:
        st = compute_metric_stats(df[col], name)
        print(f"  {name:<36}: {st.describe()}")
    print("=" * 115)


if __name__ == "__main__":
    main()
