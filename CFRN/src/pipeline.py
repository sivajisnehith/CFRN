"""CFRN closed-loop pipeline wiring all 8 architecture stages.

    Monitor & Detect -> Predict & Evaluate -> Reconfigure & Evolve ->
    Learn & Adapt -> Continuous Improvement.

A single scenario run executes:
  Stage 1  Failure detection & monitoring   (select + apply failure)
  Stage 2  Failure-propagation graph        (baseline cascade + PG_t)
  Stage 3  Counterfactual mutation gen      (candidate mutations M_t)
  Stage 4  Counterfactual cascade sim       (simulate each candidate)
  Stage 5  Debt reduction & assessment      (dCPD, A, MC)
  Stage 6  CES optimisation                 (CES, select m*)
  Stage 7  Topology reconfiguration         (apply m* to the network)
  Stage 8  Learning & self-evolution        (update model + knowledge)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import networkx as nx
import numpy as np
import pandas as pd

from .counterfactual.mutations import apply_mutation, generate_mutations
from .data.caida import load_topology, topology_features
from .data.cesnet import load_traffic, traffic_features
from .evaluation.metrics import scenario_metrics
from .failure.injector import Failure, apply_failure, select_failure
from .learning.evolve import EvolutionaryKnowledge
from .network.state import NetworkState, build_network_state
from .optimization.ces import Evaluation, optimize, select_best
from .propagation.cascade import (
    CascadeResult,
    propagation_graph,
    simulate_cascade,
)


@dataclass
class ScenarioResult:
    scenario_id: int
    failure: str
    baseline_cpd: float
    baseline_metrics: Dict[str, float]
    best_mutation: Optional[str]
    cfrn_cpd: float
    cfrn_metrics: Dict[str, float]
    delta_cpd: float
    ces: float
    n_candidates: int
    prediction_error: Optional[float]
    evaluations: List[Evaluation] = field(default_factory=list)
    # Benchmark comparisons
    legacy_mutation: Optional[str] = None
    legacy_metrics: Dict[str, float] = field(default_factory=dict)
    conv_mutation: Optional[str] = None
    conv_metrics: Dict[str, float] = field(default_factory=dict)
    # Multi-mechanism optimization results
    opt_mechanism: str = "constrained"
    constrained_mutation: Optional[str] = None
    constrained_delta_cpd: float = 0.0
    constrained_cost: float = 0.0
    constrained_feasible: bool = True
    robust_mutation: Optional[str] = None
    robust_delta_cpd: float = 0.0
    robust_cost: float = 0.0
    robust_feasible: bool = True
    robust_std_cpd: float = 0.0
    ratio_mutation: Optional[str] = None
    ratio_delta_cpd: float = 0.0
    ratio_cost: float = 0.0
    ratio_feasible: bool = True
    weighted_mutation: Optional[str] = None
    weighted_delta_cpd: float = 0.0
    weighted_cost: float = 0.0
    weighted_feasible: bool = True
    n_feasible_candidates: int = 0
    # Self-evolution operational metrics (Correction 3)
    n_simulations_executed: int = 0
    n_simulations_avoided: int = 0
    pct_simulations_avoided: float = 0.0
    surrogate_mae: float = 0.0
    drift_detected: bool = False


def _scenario_signature(state: NetworkState, failure: Failure) -> str:
    """Compact signature grouping similar scenarios for evolutionary memory."""
    n_bucket = state.graph.number_of_nodes() // 10
    over = len(state.overloaded_links())
    return f"{failure.kind}|n{n_bucket}|o{over}"


def _scenario_features(
    topo_feats: Dict[str, float], state: NetworkState, failure: Failure
) -> List[float]:
    """Feature vector for the surrogate CPD model (plan Section 6)."""
    return [
        topo_feats["n_nodes"],
        topo_feats["n_links"],
        topo_feats["avg_degree"],
        topo_feats["density"],
        topo_feats["avg_clustering"],
        float(len(state.overloaded_links())),
        state.total_served_load(),
        1.0 if failure.kind == "node" else 0.0,
    ]


class CFRNPipeline:
    """Builds the base environment once, then runs many closed-loop scenarios."""

    def __init__(self, cfg: Dict, seed: int = 42):
        self.cfg = cfg
        self.seed = seed
        self.graph: nx.Graph = load_topology(cfg, seed=seed)
        self.topo_feats = topology_features(self.graph)
        self.traffic = load_traffic(cfg, seed=seed)
        self.traffic_feats = traffic_features(self.traffic)
        self.knowledge = EvolutionaryKnowledge()

    # ---- environment -----------------------------------------------------
    def base_state(self, seed: int) -> NetworkState:
        return build_network_state(
            self.graph, self.traffic_feats, self.cfg, seed=seed
        )

    # ---- one closed-loop scenario ---------------------------------------
    def run_scenario(self, scenario_id: int) -> ScenarioResult:
        cfg, base_seed = self.cfg, self.seed + scenario_id
        opt_cfg = cfg.get("optimization", {})
        active_mechanism = opt_cfg.get("mechanism", "constrained")

        # Stage 1 - monitor & detect: build state, choose + apply failure.
        state0 = self.base_state(base_seed)
        failure = select_failure(state0, cfg, seed=base_seed)
        sig = _scenario_signature(state0, failure)
        feats = _scenario_features(self.topo_feats, state0, failure)

        # Stage 2 - baseline failure-propagation (do-nothing world).
        baseline_state = state0.copy()
        downed = apply_failure(baseline_state, failure)
        baseline_cascade = simulate_cascade(
            baseline_state, list(downed), cfg, seed=base_seed
        )
        baseline_metrics = scenario_metrics(baseline_cascade, baseline_state)
        baseline_cpd = baseline_cascade.causal_cpd

        # Stage 3 - generate candidate mutations on the post-failure network.
        post_failure_state = state0.copy()
        downed2 = apply_failure(post_failure_state, failure)
        mutations = generate_mutations(post_failure_state, downed2, cfg, seed=base_seed)

        # Stages 4-6 - simulate each candidate, score with principled optimization
        evals = optimize(
            post_failure_state,
            downed2,
            mutations,
            baseline_cpd,
            cfg,
            seed=base_seed,
            criterion="causal",
            baseline_legacy_cpd=baseline_cascade.legacy_cpd,
            baseline_conv_resilience=baseline_metrics["conventional_resilience"],
            mechanism=active_mechanism,
            knowledge=self.knowledge,
        )

        # Multi-mechanism candidate selections for rigorous comparison
        best = select_best(evals, mechanism=active_mechanism)
        best_constrained = select_best(evals, mechanism="constrained")
        best_robust = select_best(evals, mechanism="robust")
        best_ratio = select_best(evals, mechanism="ratio", require_beneficial=False)
        best_weighted = select_best(evals, mechanism="weighted")

        # Identify candidate chosen by Legacy CPD
        evals_legacy = sorted(evals, key=lambda e: e.delta_legacy_cpd, reverse=True)
        best_legacy = evals_legacy[0] if evals_legacy else None

        # Identify candidate chosen by Conventional Resilience
        evals_conv = sorted(evals, key=lambda e: e.conventional_resilience, reverse=True)
        best_conv = evals_conv[0] if evals_conv else None

        # Stage 7 - reconfigure: re-simulate the chosen CFRN world for final metrics.
        if best is not None:
            cf_state = apply_mutation(post_failure_state, best.mutation)
            final_cascade = simulate_cascade(cf_state, list(downed2), cfg, seed=base_seed)
            cfrn_metrics = scenario_metrics(final_cascade, cf_state)
            cfrn_cpd = final_cascade.causal_cpd
            best_kind = best.mutation.kind
            delta = best.delta_cpd
            ces = best.ces
        else:
            cfrn_metrics = baseline_metrics
            cfrn_cpd = baseline_cpd
            best_kind = None
            delta = 0.0
            ces = 0.0

        # Simulate Legacy CPD reconfigured world for comparison
        if best_legacy is not None:
            cf_legacy = apply_mutation(post_failure_state, best_legacy.mutation)
            leg_cascade = simulate_cascade(cf_legacy, list(downed2), cfg, seed=base_seed)
            legacy_metrics = scenario_metrics(leg_cascade, cf_legacy)
            legacy_kind = best_legacy.mutation.kind
        else:
            legacy_metrics = baseline_metrics
            legacy_kind = None

        # Simulate Conventional Resilience reconfigured world for comparison
        if best_conv is not None:
            cf_conv = apply_mutation(post_failure_state, best_conv.mutation)
            conv_cascade = simulate_cascade(cf_conv, list(downed2), cfg, seed=base_seed)
            conv_metrics = scenario_metrics(conv_cascade, cf_conv)
            conv_kind = best_conv.mutation.kind
        else:
            conv_metrics = baseline_metrics
            conv_kind = None

        # Stage 8 - learn & self-evolve.
        self.knowledge.step_epoch()
        retrain_int = int(cfg.get("learning", {}).get("retrain_interval", 10))
        min_samp = int(cfg.get("learning", {}).get("min_samples_to_train", 20))
        if scenario_id % retrain_int == 0 or self.knowledge.cooldown_epochs > 0:
            self.knowledge.maybe_train(min_samples=min_samp)

        if best is not None:
            self.knowledge.record_success(sig, best_kind, ces)

        n_simulated = sum(1 for e in evals if e.was_simulated)
        n_avoided = sum(1 for e in evals if not e.was_simulated)
        pct_avoided = 100.0 * n_avoided / len(evals) if evals else 0.0
        rolling_mae = self.knowledge.drift_detector.rolling_mae
        drift_flag = self.knowledge.drift_detector.drift_detected
        pred_err = rolling_mae

        return ScenarioResult(
            scenario_id=scenario_id,
            failure=failure.describe(),
            baseline_cpd=baseline_cpd,
            baseline_metrics=baseline_metrics,
            best_mutation=best_kind,
            cfrn_cpd=cfrn_cpd,
            cfrn_metrics=cfrn_metrics,
            delta_cpd=delta,
            ces=ces,
            n_candidates=len(mutations),
            prediction_error=pred_err,
            evaluations=evals,
            legacy_mutation=legacy_kind,
            legacy_metrics=legacy_metrics,
            conv_mutation=conv_kind,
            conv_metrics=conv_metrics,
            opt_mechanism=active_mechanism,
            constrained_mutation=best_constrained.mutation.kind if best_constrained else None,
            constrained_delta_cpd=best_constrained.delta_cpd if best_constrained else 0.0,
            constrained_cost=best_constrained.mutation_cost if best_constrained else 0.0,
            constrained_feasible=best_constrained.is_feasible if best_constrained else True,
            robust_mutation=best_robust.mutation.kind if best_robust else None,
            robust_delta_cpd=best_robust.delta_cpd if best_robust else 0.0,
            robust_cost=best_robust.mutation_cost if best_robust else 0.0,
            robust_feasible=best_robust.is_feasible if best_robust else True,
            robust_std_cpd=best_robust.robust_std_cpd if best_robust else 0.0,
            ratio_mutation=best_ratio.mutation.kind if best_ratio else None,
            ratio_delta_cpd=best_ratio.delta_cpd if best_ratio else 0.0,
            ratio_cost=best_ratio.mutation_cost if best_ratio else 0.0,
            ratio_feasible=best_ratio.is_feasible if best_ratio else True,
            weighted_mutation=best_weighted.mutation.kind if best_weighted else None,
            weighted_delta_cpd=best_weighted.delta_cpd if best_weighted else 0.0,
            weighted_cost=best_weighted.mutation_cost if best_weighted else 0.0,
            weighted_feasible=best_weighted.is_feasible if best_weighted else True,
            n_feasible_candidates=sum(1 for e in evals if e.is_feasible),
            n_simulations_executed=n_simulated,
            n_simulations_avoided=n_avoided,
            pct_simulations_avoided=pct_avoided,
            surrogate_mae=rolling_mae,
            drift_detected=drift_flag,
        )

    # ---- experiment driver ----------------------------------------------
    def run_experiment(self, n_scenarios: int) -> pd.DataFrame:
        rows = []
        for i in range(n_scenarios):
            r = self.run_scenario(i)
            rows.append(
                {
                    "scenario": r.scenario_id,
                    "failure": r.failure,
                    # Primary C-CPD
                    "baseline_cpd": r.baseline_cpd,
                    "cfrn_cpd": r.cfrn_cpd,
                    "delta_cpd": r.delta_cpd,
                    "ces": r.ces,
                    "best_mutation": r.best_mutation,
                    "n_candidates": r.n_candidates,
                    "n_feasible_candidates": r.n_feasible_candidates,
                    # Baseline detailed orders
                    "baseline_direct_damage": r.baseline_metrics.get("direct_damage", 0.0),
                    "baseline_secondary_debt": r.baseline_metrics.get("secondary_debt", 0.0),
                    "baseline_higher_order_debt": r.baseline_metrics.get("higher_order_debt", 0.0),
                    "baseline_cascade_size": r.baseline_metrics["cascade_size"],
                    "baseline_cascade_depth": r.baseline_metrics["cascade_depth"],
                    "baseline_conn_loss": r.baseline_metrics["connectivity_loss"],
                    "baseline_recovery": r.baseline_metrics["recovery_time"],
                    "baseline_legacy_cpd": r.baseline_metrics.get("legacy_cpd", 0.0),
                    # CFRN (C-CPD) detailed orders
                    "cfrn_direct_damage": r.cfrn_metrics.get("direct_damage", 0.0),
                    "cfrn_secondary_debt": r.cfrn_metrics.get("secondary_debt", 0.0),
                    "cfrn_higher_order_debt": r.cfrn_metrics.get("higher_order_debt", 0.0),
                    "cfrn_cascade_size": r.cfrn_metrics["cascade_size"],
                    "cfrn_cascade_depth": r.cfrn_metrics["cascade_depth"],
                    "cfrn_conn_loss": r.cfrn_metrics["connectivity_loss"],
                    "cfrn_recovery": r.cfrn_metrics["recovery_time"],
                    "cfrn_legacy_cpd": r.cfrn_metrics.get("legacy_cpd", 0.0),
                    # Legacy CPD Benchmark
                    "legacy_best_mutation": r.legacy_mutation,
                    "legacy_cpd_cascade_size": r.legacy_metrics.get("cascade_size", 0.0),
                    "legacy_cpd_secondary_debt": r.legacy_metrics.get("secondary_debt", 0.0),
                    "legacy_cpd_higher_order_debt": r.legacy_metrics.get("higher_order_debt", 0.0),
                    "legacy_cpd_score": r.legacy_metrics.get("legacy_cpd", 0.0),
                    # Conventional Resilience Benchmark
                    "conv_best_mutation": r.conv_mutation,
                    "conv_cascade_size": r.conv_metrics.get("cascade_size", 0.0),
                    "conv_secondary_debt": r.conv_metrics.get("secondary_debt", 0.0),
                    "conv_higher_order_debt": r.conv_metrics.get("higher_order_debt", 0.0),
                    "conv_resilience": r.conv_metrics.get("conventional_resilience", 0.0),
                    # Multi-mechanism optimization comparisons
                    "opt_mechanism": r.opt_mechanism,
                    "constrained_mutation": r.constrained_mutation,
                    "constrained_delta_cpd": r.constrained_delta_cpd,
                    "constrained_cost": r.constrained_cost,
                    "constrained_feasible": r.constrained_feasible,
                    "robust_mutation": r.robust_mutation,
                    "robust_delta_cpd": r.robust_delta_cpd,
                    "robust_cost": r.robust_cost,
                    "robust_feasible": r.robust_feasible,
                    "robust_std_cpd": r.robust_std_cpd,
                    "ratio_mutation": r.ratio_mutation,
                    "ratio_delta_cpd": r.ratio_delta_cpd,
                    "ratio_cost": r.ratio_cost,
                    "ratio_feasible": r.ratio_feasible,
                    "weighted_mutation": r.weighted_mutation,
                    "weighted_delta_cpd": r.weighted_delta_cpd,
                    "weighted_cost": r.weighted_cost,
                    "weighted_feasible": r.weighted_feasible,
                    "n_feasible_candidates": r.n_feasible_candidates,
                    # Learning & Self-Evolution (Correction 3)
                    "prediction_error": r.prediction_error,
                    "n_simulations_executed": r.n_simulations_executed,
                    "n_simulations_avoided": r.n_simulations_avoided,
                    "pct_simulations_avoided": r.pct_simulations_avoided,
                    "surrogate_mae": r.surrogate_mae,
                    "drift_detected": r.drift_detected,
                }
            )
        return pd.DataFrame(rows)
