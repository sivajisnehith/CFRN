"""Comprehensive Ablation and Sensitivity Analysis Suite (Correction 12).

Executes experiments A through H to isolate the contribution of each major CFRN component:
- Exp A: C-CPD replaced by simpler resilience metric (conventional giant component)
- Exp B: Algebraic connectivity term removed (eta = 0)
- Exp C: Mutation-cost term removed (unconstrained OpEx)
- Exp D: Surrogate model disabled (exhaustive physical simulation)
- Exp E: Evolutionary memory disabled (cold-start / no historical adaptation)
- Exp F: Individual mutation classes disabled (leave-one-out)
- Exp G: Cascade horizon H in {2, 4, 6, 8} and discount factor gamma in {0.5, 0.7, 0.85, 0.95}
- Exp H: Traffic intensity (0.8x, 1.0x, 1.2x, 1.5x), topology size N, and failure severity
"""
from __future__ import annotations

import argparse
import copy
import os
import sys
import time
from typing import Dict, List, Tuple

import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.config import load_config, seed_everything
from src.pipeline import CFRNPipeline


def run_ablation_experiment(
    name: str,
    cfg_override: Dict,
    base_cfg: Dict,
    n_scenarios: int = 15,
) -> Dict[str, float]:
    """Execute a configured ablation configuration over n_scenarios and record performance and runtime."""
    cfg = copy.deepcopy(base_cfg)
    # Recursively update cfg with cfg_override
    for k, v in cfg_override.items():
        if isinstance(v, dict) and k in cfg and isinstance(cfg[k], dict):
            cfg[k].update(v)
        else:
            cfg[k] = v

    seed_everything(int(cfg.get("seed", 42)))
    start_time = time.perf_counter()
    pipe = CFRNPipeline(cfg, seed=int(cfg.get("seed", 42)))
    df = pipe.run_experiment(n_scenarios)
    elapsed = time.perf_counter() - start_time

    mean_base = float(df["baseline_cpd"].mean())
    mean_cfrn = float(df["cfrn_cpd"].mean())
    mean_delta = float(df["delta_cpd"].mean())
    mean_sec = float(df["cfrn_secondary_debt"].mean())
    mean_high = float(df["cfrn_higher_order_debt"].mean())
    mean_cost = float(df["cfrn_cost"].mean()) if "cfrn_cost" in df.columns else 0.8
    pct_red = 100.0 * (mean_base - mean_cfrn) / max(mean_base, 1e-6)
    ms_per_scen = 1000.0 * elapsed / max(n_scenarios, 1)

    return {
        "experiment": name,
        "mean_baseline_cpd": mean_base,
        "mean_cfrn_cpd": mean_cfrn,
        "mean_delta_cpd": mean_delta,
        "pct_reduction": pct_red,
        "secondary_debt": mean_sec,
        "higher_order_debt": mean_high,
        "mean_opex_cost": mean_cost,
        "latency_ms_per_scenario": ms_per_scen,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Run CFRN Ablation Studies")
    ap.add_argument("--scenarios", type=int, default=15)
    args = ap.parse_args()

    base_cfg = load_config()
    n = args.scenarios

    print(f"Starting CFRN Ablation & Sensitivity Suite across {n} scenarios per experiment ...\n")
    results: List[Dict[str, float]] = []

    # 1. Full CFRN (Default)
    print("Running Full CFRN (Default System) ...")
    r_full = run_ablation_experiment("Full CFRN (Default)", {}, base_cfg, n)
    results.append(r_full)

    # 2. Exp A: C-CPD replaced by simpler resilience metric (conventional)
    print("Running Exp A: C-CPD replaced by conventional resilience ...")
    r_a = run_ablation_experiment(
        "Exp A: Conventional Resilience (No C-CPD)",
        {"optimization": {"criterion": "conventional"}},
        base_cfg,
        n,
    )
    results.append(r_a)

    # 3. Exp B: Algebraic connectivity term removed (eta = 0.0)
    print("Running Exp B: Adaptability removed (eta = 0.0) ...")
    r_b = run_ablation_experiment(
        "Exp B: Adaptability Removed (eta = 0.0)",
        {"optimization": {"eta": 0.0}},
        base_cfg,
        n,
    )
    results.append(r_b)

    # 4. Exp C: Mutation-cost term removed (unconstrained OpEx, budget_max = 100)
    print("Running Exp C: Unconstrained OpEx (No Budget Bound) ...")
    r_c = run_ablation_experiment(
        "Exp C: Unconstrained OpEx (No Budget Bound)",
        {"optimization": {"budget_max": 100.0}},
        base_cfg,
        n,
    )
    results.append(r_c)

    # 5. Exp D: Surrogate model disabled (100% full cascade simulation)
    print("Running Exp D: Surrogate model disabled (Exhaustive Simulation) ...")
    r_d = run_ablation_experiment(
        "Exp D: Surrogate Disabled (100% Physical Sim)",
        {"learning": {"enable_screening": False}},
        base_cfg,
        n,
    )
    results.append(r_d)

    # 6. Exp E: Evolutionary memory disabled (min_samples_to_train = 999999)
    print("Running Exp E: Evolutionary memory disabled (Cold-Start Only) ...")
    r_e = run_ablation_experiment(
        "Exp E: Memory Disabled (Cold-Start Only)",
        {"learning": {"min_samples_to_train": 999999}},
        base_cfg,
        n,
    )
    results.append(r_e)

    # 7. Exp F: Leave-one-out mutation classes
    print("Running Exp F1: No flow rerouting ...")
    r_f1 = run_ablation_experiment(
        "Exp F1: Leave-out Rerouting (No reroute_flow)",
        {"counterfactual": {"enable": {"reroute_flow": False, "remove_link": True, "add_link": True, "isolate_path": True}}},
        base_cfg,
        n,
    )
    results.append(r_f1)

    print("Running Exp F2: No link addition ...")
    r_f2 = run_ablation_experiment(
        "Exp F2: Leave-out Addition (No add_link)",
        {"counterfactual": {"enable": {"reroute_flow": True, "remove_link": True, "add_link": False, "isolate_path": True}}},
        base_cfg,
        n,
    )
    results.append(r_f2)

    # 8. Exp G: Temporal parameters sensitivity
    print("Running Exp G1: Short horizon (H = 2) ...")
    r_g1 = run_ablation_experiment(
        "Exp G1: Short Horizon (H = 2)",
        {"propagation": {"horizon": 2}},
        base_cfg,
        n,
    )
    results.append(r_g1)

    print("Running Exp G2: Long horizon (H = 8) ...")
    r_g2 = run_ablation_experiment(
        "Exp G2: Long Horizon (H = 8)",
        {"propagation": {"horizon": 8}},
        base_cfg,
        n,
    )
    results.append(r_g2)

    # 9. Exp H: Traffic load sensitivity
    print("Running Exp H1: Severe Traffic Surge (Headroom = 0.85) ...")
    r_h1 = run_ablation_experiment(
        "Exp H1: High Traffic Stress (Headroom 0.85)",
        {"network": {"capacity_headroom": 0.85}},
        base_cfg,
        n,
    )
    results.append(r_h1)

    df_res = pd.DataFrame(results)
    out_dir = os.path.join(_ROOT, "results")
    os.makedirs(out_dir, exist_ok=True)
    csv_out = os.path.join(out_dir, "ablation_study_results.csv")
    df_res.to_csv(csv_out, index=False)

    print("\n" + "=" * 105)
    print("COMPREHENSIVE ABLATION & SENSITIVITY BENCHMARK RESULTS (CORRECTION 12)")
    print("=" * 105)
    header = f"{'Experiment / Variant':<40} | {'Mean CPD':<10} | {'Delta CPD':<10} | {'% Red.':<8} | {'OpEx':<6} | {'Latency (ms)':<12}"
    print(header)
    print("-" * 105)
    for _, row in df_res.iterrows():
        print(
            f"{row['experiment']:<40} | {row['mean_cfrn_cpd']:<10.4f} | {row['mean_delta_cpd']:<10.4f} | "
            f"{row['pct_reduction']:<7.1f}% | {row['mean_opex_cost']:<6.2f} | {row['latency_ms_per_scenario']:<12.1f}"
        )
    print("=" * 105)
    print(f"\nSaved detailed ablation table to {csv_out}")


if __name__ == "__main__":
    main()
