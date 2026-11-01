"""Comparative Baseline Reconfiguration Strategies (Correction 9).

Provides standardized, competitive baseline implementations evaluated under identical
network topologies, failure incidents, traffic demands, and intervention budgets:

1. Do-Nothing Baseline (unmitigated failure cascade)
2. Shortest-Path Rerouting (SPR: distributed shortest path bypass, no topology changes)
3. Greedy Overload Reduction (GOR: myopic single-step peak utilization reduction)
4. Centrality-Based Intervention (CBI: betweenness centrality prioritized intervention)
5. Conventional Resilience Optimization (CRO: maximizing instantaneous giant component & spectral expansion)
6. Constrained Multi-Objective Reconfiguration (CMOR: static multi-commodity flow optimization without causal debt)
7. Supervised Learning Policy (SLP: static feature regressor ranking)
8. CFRN (Uncertainty-Aware Causal Marginal Cascade-Risk Optimization)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import networkx as nx
import numpy as np

from ..counterfactual.mutations import Mutation, apply_mutation
from ..evaluation.metrics import conventional_resilience_index
from ..network.state import EdgeKey, NetworkState, _canon
from ..propagation.cascade import (
    CascadeResult,
    _disconnected_fraction,
    _unserved_load,
    simulate_cascade,
)
from .ces import _adaptability, check_feasibility, mutation_cost


@dataclass
class BaselineDecision:
    strategy_name: str
    selected_mutation: Optional[Mutation]
    cf_state: NetworkState
    cascade_result: CascadeResult
    causal_cpd: float
    secondary_debt: float
    higher_order_debt: float
    cascade_size: int
    disconnected_nodes: int
    unserved_load: float
    opex_cost: float
    is_feasible: bool


def evaluate_decision(
    strategy_name: str,
    selected_mutation: Optional[Mutation],
    base_state: NetworkState,
    downed: List[EdgeKey],
    cfg: Dict,
    seed: int = 42,
) -> BaselineDecision:
    """Simulate the post-reconfiguration cascade for a chosen mutation under standard evaluation."""
    if selected_mutation is not None:
        cf_state = apply_mutation(base_state, selected_mutation)
        is_feas, _ = check_feasibility(cf_state, selected_mutation, cfg)
        cost = mutation_cost(selected_mutation, cfg)
    else:
        cf_state = base_state.copy()
        is_feas = True
        cost = 0.0

    cascade = simulate_cascade(cf_state.copy(), list(downed), cfg, seed=seed)
    return BaselineDecision(
        strategy_name=strategy_name,
        selected_mutation=selected_mutation,
        cf_state=cf_state,
        cascade_result=cascade,
        causal_cpd=cascade.causal_cpd,
        secondary_debt=cascade.secondary_debt,
        higher_order_debt=cascade.higher_order_debt,
        cascade_size=cascade.cascade_size,
        disconnected_nodes=cascade.disconnected_nodes,
        unserved_load=cascade.unserved_load,
        opex_cost=cost,
        is_feasible=is_feas,
    )


def run_shortest_path_rerouting(
    base_state: NetworkState,
    downed: List[EdgeKey],
    mutations: List[Mutation],
    cfg: Dict,
    seed: int = 42,
) -> BaselineDecision:
    """Baseline 1: Shortest-Path Rerouting (SPR).

    Selects pure flow rerouting candidates that bypass failed links along shortest paths
    without executing any physical link addition or removal.
    """
    reroute_cands = [m for m in mutations if m.kind == "reroute_flow"]
    if not reroute_cands:
        return evaluate_decision("Shortest-Path Rerouting (SPR)", None, base_state, downed, cfg, seed)

    # Pick the reroute candidate that provides the cleanest shortest path bypass
    # with minimum routing churn
    best_cand = min(reroute_cands, key=lambda m: m.effort)
    return evaluate_decision("Shortest-Path Rerouting (SPR)", best_cand, base_state, downed, cfg, seed)


def run_greedy_overload_reduction(
    base_state: NetworkState,
    downed: List[EdgeKey],
    mutations: List[Mutation],
    cfg: Dict,
    seed: int = 42,
) -> BaselineDecision:
    """Baseline 2: Greedy Overload Reduction (GOR).

    Myopically selects the mutation that maximizes the immediate reduction in
    peak link utilization max_e u(e) on the initial state without simulating cascades.
    """
    init_util = base_state.utilisation()
    init_peak = max(init_util.values()) if init_util else 1.0

    best_cand = None
    best_peak_reduction = -float("inf")

    for m in mutations:
        cf = apply_mutation(base_state, m)
        is_feas, _ = check_feasibility(cf, m, cfg)
        if not is_feas:
            continue
        u_after = cf.utilisation()
        peak_after = max(u_after.values()) if u_after else 1.0
        reduction = init_peak - peak_after
        if reduction > best_peak_reduction:
            best_peak_reduction = reduction
            best_cand = m

    return evaluate_decision("Greedy Overload Reduction (GOR)", best_cand, base_state, downed, cfg, seed)


def run_centrality_based_intervention(
    base_state: NetworkState,
    downed: List[EdgeKey],
    mutations: List[Mutation],
    cfg: Dict,
    seed: int = 42,
) -> BaselineDecision:
    """Baseline 3: Centrality-Based Intervention (CBI).

    Prioritizes actions touching elements with the highest betweenness centrality,
    a standard topological heuristic in structural network defense.
    """
    g = base_state.graph
    eb = nx.edge_betweenness_centrality(g) if g.number_of_edges() > 0 else {}

    best_cand = None
    best_centrality = -1.0

    for m in mutations:
        cf = apply_mutation(base_state, m)
        is_feas, _ = check_feasibility(cf, m, cfg)
        if not is_feas:
            continue
        edge = m.params.get("edge")
        if edge is not None:
            e_can = _canon(*edge) if isinstance(edge, (list, tuple)) else edge
            cent = eb.get(e_can, 0.0)
            if cent > best_centrality:
                best_centrality = cent
                best_cand = m

    return evaluate_decision("Centrality-Based Intervention (CBI)", best_cand, base_state, downed, cfg, seed)


def run_conventional_resilience_optimization(
    base_state: NetworkState,
    downed: List[EdgeKey],
    mutations: List[Mutation],
    cfg: Dict,
    seed: int = 42,
) -> BaselineDecision:
    """Baseline 4: Conventional Resilience Optimization (CRO).

    Optimizes standard static graph resilience: maximizes giant component retention
    and spectral expansion (algebraic connectivity) immediately post-reconfiguration.
    """
    best_cand = None
    best_resilience = -float("inf")

    for m in mutations:
        cf = apply_mutation(base_state, m)
        is_feas, _ = check_feasibility(cf, m, cfg)
        if not is_feas:
            continue
        res_score = conventional_resilience_index(cf)
        if res_score > best_resilience:
            best_resilience = res_score
            best_cand = m

    return evaluate_decision("Conventional Resilience Optimization (CRO)", best_cand, base_state, downed, cfg, seed)


def run_constrained_multiobjective_reconfiguration(
    base_state: NetworkState,
    downed: List[EdgeKey],
    mutations: List[Mutation],
    cfg: Dict,
    seed: int = 42,
) -> BaselineDecision:
    """Baseline 5: Constrained Multi-Objective Reconfiguration (CMOR).

    Standard static multi-objective optimization: maximizes instantaneous demand served
    and minimizes static congestion penalty subject to operational constraints, but without
    multi-step causal cascade propagation modeling.
    """
    best_cand = None
    best_obj = -float("inf")

    for m in mutations:
        cf = apply_mutation(base_state, m)
        is_feas, _ = check_feasibility(cf, m, cfg)
        if not is_feas:
            continue

        lost_frac, _ = _unserved_load(cf)
        u_vals = cf.utilisation().values()
        mean_u = float(np.mean(list(u_vals))) if u_vals else 1.0
        cost = mutation_cost(m, cfg)

        # Static utility function: served fraction - congestion penalty - cost
        static_utility = (1.0 - lost_frac) - 0.4 * mean_u - 0.2 * cost
        if static_utility > best_obj:
            best_obj = static_utility
            best_cand = m

    return evaluate_decision("Constrained Multi-Objective (CMOR)", best_cand, base_state, downed, cfg, seed)


def run_supervised_learning_policy(
    base_state: NetworkState,
    downed: List[EdgeKey],
    mutations: List[Mutation],
    cfg: Dict,
    seed: int = 42,
) -> BaselineDecision:
    """Baseline 6: Supervised Learning Policy (SLP).

    Mimics a static learning-based agent trained to rank mutations using immediate
    structural features (degree, load shift, capacity) without active screening or causal attribution.
    """
    # Deterministic static heuristic scoring mimicking trained regression weights
    best_cand = None
    best_score = -float("inf")

    for m in mutations:
        cf = apply_mutation(base_state, m)
        is_feas, _ = check_feasibility(cf, m, cfg)
        if not is_feas:
            continue

        cost = mutation_cost(m, cfg)
        u_vals = cf.utilisation().values()
        peak_u = max(u_vals) if u_vals else 1.0
        adapt = _adaptability(cf)

        # Supervised linear ranking score
        slp_score = 1.2 * adapt - 0.8 * peak_u - 0.3 * cost
        if slp_score > best_score:
            best_score = slp_score
            best_cand = m

    return evaluate_decision("Supervised Learning Policy (SLP)", best_cand, base_state, downed, cfg, seed)


def evaluate_all_baselines(
    base_state: NetworkState,
    downed: List[EdgeKey],
    mutations: List[Mutation],
    cfg: Dict,
    seed: int = 42,
) -> Dict[str, BaselineDecision]:
    """Execute and evaluate all comparative baseline strategies under identical scenario conditions."""
    results = {}
    # 0. Do-Nothing baseline
    results["Do-Nothing (Baseline)"] = evaluate_decision(
        "Do-Nothing (Baseline)", None, base_state, downed, cfg, seed
    )
    # 1. Shortest-Path Rerouting
    results["Shortest-Path Rerouting (SPR)"] = run_shortest_path_rerouting(
        base_state, downed, mutations, cfg, seed
    )
    # 2. Greedy Overload Reduction
    results["Greedy Overload Reduction (GOR)"] = run_greedy_overload_reduction(
        base_state, downed, mutations, cfg, seed
    )
    # 3. Centrality-Based Intervention
    results["Centrality-Based Intervention (CBI)"] = run_centrality_based_intervention(
        base_state, downed, mutations, cfg, seed
    )
    # 4. Conventional Resilience Optimization
    results["Conventional Resilience (CRO)"] = run_conventional_resilience_optimization(
        base_state, downed, mutations, cfg, seed
    )
    # 5. Constrained Multi-Objective Reconfiguration
    results["Constrained Multi-Objective (CMOR)"] = run_constrained_multiobjective_reconfiguration(
        base_state, downed, mutations, cfg, seed
    )
    # 6. Supervised Learning Policy
    results["Supervised Learning Policy (SLP)"] = run_supervised_learning_policy(
        base_state, downed, mutations, cfg, seed
    )
    return results
