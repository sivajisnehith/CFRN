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

from .counterfactual.mutations import generate_mutations
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
        baseline_cpd = baseline_cascade.cpd

        # Stage 3 - generate candidate mutations on the post-failure network.
        post_failure_state = state0.copy()
        downed2 = apply_failure(post_failure_state, failure)
        mutations = generate_mutations(post_failure_state, downed2, cfg, seed=base_seed)

        # Stages 4-6 - simulate each candidate, score CES, select m*.
        evals = optimize(
            post_failure_state, downed2, mutations, baseline_cpd, cfg, seed=base_seed
        )
        best = select_best(evals)

        # Stage 7 - reconfigure: re-simulate the chosen world for final metrics.
        if best is not None:
            from .counterfactual.mutations import apply_mutation

            cf_state = apply_mutation(post_failure_state, best.mutation)
            final_cascade = simulate_cascade(cf_state, list(downed2), cfg, seed=base_seed)
            cfrn_metrics = scenario_metrics(final_cascade, cf_state)
            cfrn_cpd = final_cascade.cpd
            best_kind = best.mutation.kind
            delta = best.delta_cpd
            ces = best.ces
        else:
            cfrn_metrics = baseline_metrics
            cfrn_cpd = baseline_cpd
            best_kind = None
            delta = 0.0
            ces = 0.0

        # Stage 8 - learn & self-evolve.
        pred_err = self.knowledge.observe(feats, baseline_cpd)
        self.knowledge.maybe_train(int(cfg["learning"]["min_samples_to_train"]))
        if best is not None:
            self.knowledge.record_success(sig, best_kind, ces)

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
                    "baseline_cpd": r.baseline_cpd,
                    "cfrn_cpd": r.cfrn_cpd,
                    "delta_cpd": r.delta_cpd,
                    "ces": r.ces,
                    "best_mutation": r.best_mutation,
                    "n_candidates": r.n_candidates,
                    "baseline_cascade_size": r.baseline_metrics["cascade_size"],
                    "cfrn_cascade_size": r.cfrn_metrics["cascade_size"],
                    "baseline_conn_loss": r.baseline_metrics["connectivity_loss"],
                    "cfrn_conn_loss": r.cfrn_metrics["connectivity_loss"],
                    "baseline_recovery": r.baseline_metrics["recovery_time"],
                    "cfrn_recovery": r.cfrn_metrics["recovery_time"],
                    "prediction_error": r.prediction_error,
                }
            )
        return pd.DataFrame(rows)
