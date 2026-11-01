"""Demonstrate Adaptation under Network Drift (Correction 14).

Simulates 50 continuous operational epochs spanning 4 non-stationary drift phases:
  Phase 1 (Epochs 1-15):  Stationary baseline nominal traffic.
  Phase 2 (Epochs 16-25): Gradual traffic demand increase (+40% load ramp).
  Phase 3 (Epochs 26-35): Sudden burst spike (+80% surge) & repeated primary corridor cuts.
  Phase 4 (Epochs 36-50): Structural topology shift (permanent 50% capacity degradation on core links).

Compares:
  - Self-Evolving CFRN (Adaptive): Page-Hinkley drift detection + recency-weighted
    retraining + temporary simulation pool expansion + memory discounting.
  - Static CFRN (Non-Adaptive): Surrogate trained on initial 15 epochs, frozen, no drift adaptation.
"""
from __future__ import annotations

import copy
import os
import sys
import time
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt
import networkx as nx
import numpy as np
import pandas as pd

# Add repo root to path
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from CFRN.src.config import load_config, seed_everything
from CFRN.src.counterfactual.mutations import apply_mutation, generate_mutations
from CFRN.src.data.cesnet import load_traffic, traffic_features
from CFRN.src.failure.injector import select_failure, apply_failure, Failure
from CFRN.src.learning.evolve import EvolutionaryKnowledge, extract_candidate_features
from CFRN.src.network.state import build_network_state, _canon
from CFRN.src.optimization.ces import evaluate_mutation, select_best
from CFRN.src.propagation.cascade import simulate_cascade


def run_drift_simulation(cfg: Dict, n_epochs: int = 50) -> pd.DataFrame:
    print(f"\n--- Running 50-Epoch Network Drift & Self-Evolution Benchmark ---")
    seed_everything(42)

    # 1. Base Topology (N=120)
    g_base = nx.barabasi_albert_graph(120, 2, seed=42)
    traffic = load_traffic(cfg, seed=42)
    traffic_feats = traffic_features(traffic)

    # Models: Adaptive vs Static
    adaptive_knowledge = EvolutionaryKnowledge()
    static_knowledge = EvolutionaryKnowledge()

    records = []

    # Store top-betweenness link for repeated attacks in Phase 3
    eb = nx.edge_betweenness_centrality(g_base)
    primary_corridor = max(eb, key=eb.get)
    primary_canon = _canon(primary_corridor[0], primary_corridor[1])

    # Core degraded links for Phase 4
    sorted_eb = sorted(eb.items(), key=lambda kv: kv[1], reverse=True)
    degraded_links = [_canon(u, v) for (u, v), _ in sorted_eb[1:3]]

    for epoch in range(1, n_epochs + 1):
        # -------------------------------------------------------------
        # Phase Configuration & Environmental Non-Stationarity
        # -------------------------------------------------------------
        if epoch <= 15:
            phase_name = "Phase 1: Stationary"
            traffic_mult = 1.0
            fail_strategy = "betweenness"
            capacity_degradation = False
        elif epoch <= 25:
            phase_name = "Phase 2: Gradual Drift"
            # Linear ramp: 1.05 to 1.40
            ramp_frac = (epoch - 15) / 10.0
            traffic_mult = 1.0 + 0.40 * ramp_frac
            fail_strategy = "random"
            capacity_degradation = False
        elif epoch <= 35:
            phase_name = "Phase 3: Sudden Burst"
            traffic_mult = 1.80  # +80% surge
            fail_strategy = "targeted_corridor"  # repeated failure on primary corridor
            capacity_degradation = False
        else:
            phase_name = "Phase 4: Topology Shift"
            traffic_mult = 1.30
            fail_strategy = "betweenness"
            capacity_degradation = True  # permanent 50% capacity loss on core links

        # Build fresh network state with engineered baseline capacity on fresh graph copy
        state = build_network_state(copy.deepcopy(g_base), traffic_feats, cfg, seed=42)
        # Apply traffic volume drift to link loads
        if traffic_mult != 1.0:
            for e in state.link_load:
                state.link_load[e] *= traffic_mult

        # Apply structural topology shift if Phase 4
        if capacity_degradation:
            for e_deg in degraded_links:
                if e_deg in state.link_capacity:
                    state.link_capacity[e_deg] *= 0.50

        # Inject failure
        if fail_strategy == "targeted_corridor" and g_base.has_edge(*primary_canon):
            fail = Failure(kind="link", target=primary_canon)
        else:
            fail = select_failure(state, cfg, seed=epoch)

        downed = apply_failure(state, fail)
        post_fail = state

        # Run baseline cascade
        base_res = simulate_cascade(post_fail, downed, cfg, seed=epoch)
        base_cpd = base_res.causal_cpd

        # Generate candidates
        candidates = generate_mutations(post_fail, downed, cfg, seed=epoch)
        n_cands = len(candidates)

        # -------------------------------------------------------------
        # 1. Self-Evolving CFRN (Adaptive Evaluation)
        # -------------------------------------------------------------
        # Screen candidates using adaptive knowledge
        adaptive_knowledge.current_epoch = epoch
        features_list = [
            extract_candidate_features(post_fail, downed, c, base_cpd, cfg)
            for c in candidates
        ]

        # Determine screening pool
        if adaptive_knowledge.is_ready():
            cfg_adapt = copy.deepcopy(cfg)
            if adaptive_knowledge.cooldown_epochs > 0:
                cfg_adapt["learning"]["screening_top_k"] = 10
                cfg_adapt["learning"]["uncertainty_margin"] = 0.55
            sim_indices, _, _ = adaptive_knowledge.screen_candidates(
                candidates, features_list, base_cpd, [0.0] * n_cands, cfg_adapt
            )
        else:
            sim_indices = list(range(n_cands))

        # Physically evaluate selected candidates
        adapt_evals = []
        for idx in sim_indices:
            c = candidates[idx]
            ev = evaluate_mutation(post_fail, downed, c, base_cpd, cfg, seed=epoch)
            adapt_evals.append(ev)
            # Observe candidate
            adaptive_knowledge.observe_candidate(features_list[idx], ev.cpd)

        best_adapt = select_best(adapt_evals, mechanism="constrained")
        adapt_delta_cpd = best_adapt.delta_cpd if best_adapt else 0.0
        adapt_sims = len(sim_indices)

        # Train / update adaptive model
        if epoch == 15 and not adaptive_knowledge.is_ready():
            adaptive_knowledge.maybe_train(force=True)
        elif epoch > 15 and adaptive_knowledge.is_ready():
            # Check drift and retrain every 5 epochs or upon drift
            drift_flag = adaptive_knowledge.drift_detector.drift_detected
            if drift_flag or (epoch % 5 == 0):
                adaptive_knowledge.maybe_train(force=True)

        adapt_mae = adaptive_knowledge.drift_detector.rolling_mae

        # -------------------------------------------------------------
        # 2. Static CFRN (Non-Adaptive Evaluation)
        # -------------------------------------------------------------
        # Train static once at epoch 15, then freeze completely
        if epoch <= 15:
            # Full simulation in initial phase
            static_sim_indices = set(range(n_cands))
            static_evals = adapt_evals
            best_static = best_adapt
            static_delta_cpd = adapt_delta_cpd
            static_sims = n_cands
            for feat, ev in zip(features_list, adapt_evals):
                static_knowledge.observe_candidate(feat, ev.cpd)
            if epoch == 15:
                static_knowledge.maybe_train(force=True)
            static_mae = adapt_mae
        else:
            # Frozen surrogate model screening with fixed k_min=5, no retraining, no drift detection
            if static_knowledge.is_ready():
                static_sim_indices, _, _ = static_knowledge.screen_candidates(
                    candidates, features_list, base_cpd, [0.0] * n_cands, cfg
                )
            else:
                static_sim_indices = list(range(min(5, n_cands)))

            static_evals = []
            for idx in static_sim_indices:
                c = candidates[idx]
                ev = evaluate_mutation(post_fail, downed, c, base_cpd, cfg, seed=epoch)
                static_evals.append(ev)

            best_static = select_best(static_evals, mechanism="constrained")
            static_delta_cpd = best_static.delta_cpd if best_static else 0.0
            static_sims = len(static_sim_indices)

            # Static prediction error tracking (unadapted)
            static_preds = [static_knowledge.predict_candidate(features_list[idx]) for idx in static_sim_indices]
            static_actuals = [ev.cpd for ev in static_evals]
            errors = [abs(p - a) for p, a in zip(static_preds, static_actuals) if p is not None]
            static_mae = float(np.mean(errors)) if errors else static_mae

        drift_flag = adaptive_knowledge.drift_detector.drift_detected
        print(f"Epoch {epoch:2d} [{phase_name:22s}] | Base CPD: {base_cpd:5.2f} | Adapt dCPD: {adapt_delta_cpd:5.2f} (sims: {adapt_sims:2d}) | Static dCPD: {static_delta_cpd:5.2f} (sims: {static_sims:2d}) | Adapt MAE: {adapt_mae:4.2f} | Static MAE: {static_mae:4.2f} | Drift: {drift_flag}")

        records.append({
            "epoch": epoch,
            "phase": phase_name,
            "traffic_multiplier": traffic_mult,
            "baseline_cpd": base_cpd,
            "adaptive_delta_cpd": adapt_delta_cpd,
            "static_delta_cpd": static_delta_cpd,
            "adaptive_simulations": adapt_sims,
            "static_simulations": static_sims,
            "adaptive_mae": adapt_mae,
            "static_mae": static_mae,
            "drift_detected": drift_flag,
            "total_candidates": n_cands,
        })

    return pd.DataFrame(records)


def main():
    print("================================================================================")
    print(" CFRN DEMONSTRATION OF ADAPTATION UNDER NETWORK DRIFT (CORRECTION 14)")
    print("================================================================================")

    cfg = load_config()
    df_drift = run_drift_simulation(cfg, n_epochs=50)

    results_dir = os.path.join(_ROOT, "CFRN", "results")
    assets_dir = os.path.join(_ROOT, "assets")
    os.makedirs(results_dir, exist_ok=True)
    os.makedirs(assets_dir, exist_ok=True)

    csv_path = os.path.join(results_dir, "drift_adaptation_results.csv")
    df_drift.to_csv(csv_path, index=False)
    print(f"\n[Saved] Drift adaptation benchmark results saved to: {csv_path}")

    # Plot 4-panel figure
    fig, axes = plt.subplots(2, 2, figsize=(14, 10))

    epochs = df_drift["epoch"]

    # Panel (a): Cascade Debt Reduction (ΔC-CPD) across 50 epochs
    ax = axes[0, 0]
    ax.plot(epochs, df_drift["adaptive_delta_cpd"], color='#2ca02c', linewidth=2.2, label="Adaptive CFRN (Self-Evolving)")
    ax.plot(epochs, df_drift["static_delta_cpd"], color='#d62728', linestyle='--', linewidth=1.8, label="Static CFRN (Non-Adaptive)")
    # Mark phases
    ax.axvline(x=15.5, color='gray', linestyle=':', alpha=0.7)
    ax.axvline(x=25.5, color='gray', linestyle=':', alpha=0.7)
    ax.axvline(x=35.5, color='gray', linestyle=':', alpha=0.7)
    ax.text(7.5, ax.get_ylim()[1]*0.9, "Phase 1\n(Stationary)", ha='center', fontsize=9, alpha=0.8)
    ax.text(20.5, ax.get_ylim()[1]*0.9, "Phase 2\n(Gradual)", ha='center', fontsize=9, alpha=0.8)
    ax.text(30.5, ax.get_ylim()[1]*0.9, "Phase 3\n(Burst)", ha='center', fontsize=9, alpha=0.8)
    ax.text(42.5, ax.get_ylim()[1]*0.9, "Phase 4\n(Topo Shift)", ha='center', fontsize=9, alpha=0.8)
    ax.set_xlabel("Operational Epoch", fontsize=11, fontweight="bold")
    ax.set_ylabel("Cascade Debt Reduction (ΔC-CPD)", fontsize=11, fontweight="bold")
    ax.set_title("(a) Cascade Debt Reduction under Non-Stationary Drift", fontsize=12, fontweight="bold")
    ax.grid(True, linestyle="--", alpha=0.6)
    ax.legend(fontsize=10, loc="lower left")

    # Panel (b): Surrogate Residual MAE (Tracking Concept Drift)
    ax = axes[0, 1]
    ax.plot(epochs, df_drift["adaptive_mae"], color='#1f77b4', linewidth=2.2, label="Adaptive CFRN MAE")
    ax.plot(epochs, df_drift["static_mae"], color='#ff7f0e', linestyle='--', linewidth=1.8, label="Static Non-Adaptive MAE")
    # Mark drift points
    drift_epochs = df_drift[df_drift["drift_detected"]]["epoch"]
    if not drift_epochs.empty:
        ax.scatter(drift_epochs, df_drift.loc[df_drift["drift_detected"], "adaptive_mae"], color='red', marker='x', s=80, zorder=5, label="Page-Hinkley Drift Alarm")
    ax.axvline(x=15.5, color='gray', linestyle=':', alpha=0.7)
    ax.axvline(x=25.5, color='gray', linestyle=':', alpha=0.7)
    ax.axvline(x=35.5, color='gray', linestyle=':', alpha=0.7)
    ax.set_xlabel("Operational Epoch", fontsize=11, fontweight="bold")
    ax.set_ylabel("Surrogate Residual MAE", fontsize=11, fontweight="bold")
    ax.set_title("(b) Surrogate Prediction Error & Drift Recovery", fontsize=12, fontweight="bold")
    ax.grid(True, linestyle="--", alpha=0.6)
    ax.legend(fontsize=10)

    # Panel (c): Dynamic Simulation Pool Budget
    ax = axes[1, 0]
    ax.plot(epochs, df_drift["adaptive_simulations"], color='#2ca02c', linewidth=2, label="Adaptive CFRN Simulations")
    ax.plot(epochs, df_drift["static_simulations"], color='#7f7f7f', linestyle=':', linewidth=1.8, label="Static CFRN Simulations")
    ax.plot(epochs, df_drift["total_candidates"], color='black', linestyle='--', alpha=0.5, label="Brute-Force (All Candidates)")
    ax.axvline(x=15.5, color='gray', linestyle=':', alpha=0.7)
    ax.axvline(x=25.5, color='gray', linestyle=':', alpha=0.7)
    ax.axvline(x=35.5, color='gray', linestyle=':', alpha=0.7)
    ax.set_xlabel("Operational Epoch", fontsize=11, fontweight="bold")
    ax.set_ylabel("Physical Simulations Executed", fontsize=11, fontweight="bold")
    ax.set_title("(c) Adaptive Simulation Budget Allocation", fontsize=12, fontweight="bold")
    ax.grid(True, linestyle="--", alpha=0.6)
    ax.legend(fontsize=10)

    # Panel (d): Cumulative Debt Reduction Salvaged
    ax = axes[1, 1]
    cum_adapt = np.cumsum(df_drift["adaptive_delta_cpd"])
    cum_static = np.cumsum(df_drift["static_delta_cpd"])
    ax.plot(epochs, cum_adapt, color='#2ca02c', linewidth=2.5, label="Adaptive CFRN Cumulative")
    ax.plot(epochs, cum_static, color='#d62728', linestyle='--', linewidth=2.0, label="Static Non-Adaptive Cumulative")
    ax.fill_between(epochs, cum_adapt, cum_static, color='#2ca02c', alpha=0.15, label="Cumulative Self-Evolution Advantage")
    ax.axvline(x=15.5, color='gray', linestyle=':', alpha=0.7)
    ax.axvline(x=25.5, color='gray', linestyle=':', alpha=0.7)
    ax.axvline(x=35.5, color='gray', linestyle=':', alpha=0.7)
    ax.set_xlabel("Operational Epoch", fontsize=11, fontweight="bold")
    ax.set_ylabel("Cumulative Debt Reduction", fontsize=11, fontweight="bold")
    ax.set_title("(d) Cumulative Resilience Advantage across Drift Regimes", fontsize=12, fontweight="bold")
    ax.grid(True, linestyle="--", alpha=0.6)
    ax.legend(fontsize=10)

    plt.tight_layout()
    fig_path1 = os.path.join(results_dir, "drift_adaptation.png")
    fig_path2 = os.path.join(assets_dir, "drift_adaptation.png")
    plt.savefig(fig_path1, dpi=300)
    plt.savefig(fig_path2, dpi=300)
    plt.close()
    print(f"[Saved] Drift adaptation figure saved to:\n  - {fig_path1}\n  - {fig_path2}")


if __name__ == "__main__":
    main()
