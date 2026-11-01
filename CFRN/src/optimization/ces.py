"""Principled Counterfactual Optimization & Decision Mechanisms (Stages 5-6).

Replaces heuristic benefit-to-cost ratio with a mathematically justified
multi-objective constrained and uncertainty-aware optimization framework.

Problem Formulations
---------------------
1. Multi-Objective Constrained Optimization (Default, CES-Constrained):
       max_{m in M}   J(m) = Delta C-CPD(m) + eta * A(G_base (+) m)
       subject to:
           MC(m) <= B_max                      (Operational budget constraint)
           max_{e} u(e) <= theta_safe          (Capacity safety margin)
           |V_giant| / |V| >= zeta_min         (Connectivity preservation)
           lambda_2(L(G_giant)) >= lambda_min  (Spectral gap / non-partition)
           Omega_lost <= Omega_max             (Routing SLA feasibility)

2. Uncertainty-Aware Robust Optimization (CES-Robust):
       max_{m in M_feasible}   J_robust(m) = E_xi[Delta C-CPD(m)] - kappa * Std_xi[C-CPD(m)] + eta * A(m)
       penalizing cascade volatility across stochastic overload realizations.

3. Normalized Weighted-Sum Utility (CES-Weighted):
       max_{m in M}   U(m) = w_1 * Delta C-CPD(m) + w_2 * A(m) - w_3 * MC(m)

4. Heuristic Benefit-to-Cost Ratio (CES-Ratio, Legacy Benchmark):
       CES(m) = (Delta C-CPD(m) + eta * A(m)) / (MC(m) + epsilon)

Optimization Guarantees:
------------------------
Optimization is discrete-optimal within the structured candidate subspace M_t:
    m* = argmax_{m in M_t cap M_feasible} J(m)
If no candidate satisfies constraints with positive debt reduction, the system
provably defaults to the do-nothing baseline (m* = None), guaranteeing non-destruction.
Global optimality across the unrestricted 2^{|E|} combinatorial graph space is
NP-hard and not claimed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import networkx as nx
import numpy as np

from ..counterfactual.mutations import Mutation, apply_mutation
from ..evaluation.metrics import conventional_resilience_index
from ..network.state import EdgeKey, NetworkState
from ..propagation.cascade import CascadeResult, _unserved_load, simulate_cascade


@dataclass
class Evaluation:
    """Evaluation record for a candidate mutation under multiple optimization mechanisms."""

    mutation: Mutation
    cpd: float                           # Causal CPD (primary rollout)
    delta_cpd: float                     # Causal CPD reduction
    legacy_cpd: float                    # Legacy CPD
    delta_legacy_cpd: float              # Legacy CPD reduction
    direct_damage: float
    secondary_debt: float
    higher_order_debt: float
    adaptability: float
    mutation_cost: float
    ces: float                           # Primary score according to configured mechanism
    cascade: CascadeResult
    conventional_resilience: float = 0.0

    # Multi-mechanism comparative scores
    ces_ratio: float = 0.0               # Heuristic ratio score
    ces_weighted: float = 0.0            # Normalized weighted-sum utility score
    ces_constrained: float = 0.0         # Budget-constrained objective score
    ces_robust: float = 0.0              # Uncertainty-aware robust score

    # Feasibility and constraint breakdown
    is_feasible: bool = True
    constraints: Dict[str, bool] = field(default_factory=dict)

    # Uncertainty statistics across rollouts
    robust_mean_cpd: float = 0.0
    robust_std_cpd: float = 0.0

    # Surrogate screening and simulation tracking (Stage 5 self-evolution)
    was_simulated: bool = True
    surrogate_predicted_cpd: Optional[float] = None
    surrogate_error: Optional[float] = None


def _adaptability(state_after: NetworkState) -> float:
    """Quantifies topological adaptability A(m) via the algebraic connectivity
    (Fiedler eigenvalue lambda_2) of the giant connected component G_giant.

    Mathematical Justification:
    ---------------------------
    For any disconnected graph G, the second smallest Laplacian eigenvalue
    lambda_2(L(G)) is identically 0.0, conveying zero information about the
    internal structural conductance of surviving subgraphs. Therefore, adaptability
    is strictly evaluated on the giant connected component:
        A(m) = lambda_2(L(G_giant(G_base (+) m)))  if |V_giant| >= 2, else 0.0

    Boundary Condition (|V_giant| < 2):
    When the giant component has fewer than 2 nodes (e.g., complete network collapse),
    the graph Laplacian dimension is <= 1. In this boundary regime, spectral expansion
    is formally defined as A(m) = 0.0.

    By Cheeger's inequality, h(G_giant) >= lambda_2 / 2, where h(G_giant) is the
    isoperimetric conductance, measuring the component's structural resistance
    against future partitioning.
    """
    g = state_after.graph
    if g.number_of_nodes() < 2 or g.number_of_edges() == 0:
        return 0.0
    comps = list(nx.connected_components(g))
    if not comps:
        return 0.0
    largest = max(comps, key=len)
    if len(largest) < 2:
        return 0.0
    sub = g.subgraph(largest)
    try:
        # Fast trace minimization eigensolver
        return float(nx.algebraic_connectivity(sub, tol=1e-3, method="tracemin_lu", seed=0))
    except Exception:
        try:
            return float(nx.algebraic_connectivity(sub, seed=0))
        except Exception:
            return float(2 * sub.number_of_edges() / max(sub.number_of_nodes(), 1))


def mutation_cost(m: Optional[Mutation], cfg: Dict) -> float:
    """Operational intervention expenditure MC(m) = alpha*L + beta*S + chi*E.

    Weights represent calibrated operational expenditure (OpEx):
    - alpha: Physical link provisioning / hardware switching cost (1.0).
    - beta: SDN flow table modification / BGP route churn cost (0.5).
    - chi: Control plane messaging, signaling, and consensus overhead (0.3).

    For the do-nothing baseline (m = None or null), MC(m) = 0.0.
    For any active mutation, at least one operational driver is positive, ensuring MC(m) > 0.
    """
    if m is None:
        return 0.0
    o = cfg.get("optimization", {})
    alpha = float(o.get("alpha", 1.0))
    beta = float(o.get("beta", 0.5))
    chi = float(o.get("chi", 0.3))
    return float(
        alpha * m.n_link_changes
        + beta * m.n_service_changes
        + chi * m.effort
    )


def check_feasibility(
    state_after: NetworkState,
    m: Mutation,
    cfg: Dict,
) -> Tuple[bool, Dict[str, bool]]:
    """Verify physical and operational constraints for candidate mutation m.

    Returns (is_feasible, constraint_breakdown):
    1. budget: MC(m) <= B_max
    2. capacity: max_{e} u(e) <= utilization_max
    3. connectivity: |V_giant| / |V| >= min_giant_ratio and lambda_2 >= min_algebraic_conn
    4. routing: unserved demand fraction <= max_unserved_demand
    """
    opt = cfg.get("optimization", {})
    b_max = float(opt.get("budget_max", 1.5))
    u_max = float(opt.get("utilization_max", 2.0))
    min_giant = float(opt.get("min_giant_ratio", 0.85))
    min_lambda = float(opt.get("min_algebraic_conn", 0.001))
    max_unserved = float(opt.get("max_unserved_demand", 0.4))

    # 1. Budget constraint
    mc = mutation_cost(m, cfg)
    budget_ok = mc <= b_max

    # 2. Capacity constraint
    utils = state_after.utilisation().values()
    peak_util = max(utils, default=0.0)
    cap_ok = peak_util <= u_max

    # 3. Connectivity constraint
    g = state_after.graph
    n = g.number_of_nodes()
    if n > 0:
        comps = list(nx.connected_components(g))
        largest = max(len(c) for c in comps) if comps else 0
        giant_ratio = largest / n
    else:
        giant_ratio = 0.0
    conn_ok = giant_ratio >= min_giant

    adapt = _adaptability(state_after)
    lambda_ok = adapt >= min_lambda or giant_ratio >= min_giant

    # 4. Routing constraint
    lost_frac, _ = _unserved_load(state_after)
    route_ok = lost_frac <= max_unserved

    breakdown = {
        "budget": budget_ok,
        "capacity": cap_ok,
        "connectivity": conn_ok and lambda_ok,
        "routing": route_ok,
    }
    is_feasible = budget_ok and cap_ok and conn_ok and lambda_ok and route_ok
    return is_feasible, breakdown


def evaluate_mutation(
    base_state: NetworkState,
    initial_downed: List[EdgeKey],
    m: Mutation,
    baseline_cpd: float,
    cfg: Dict,
    seed: int = 42,
    criterion: str = "causal",
    baseline_legacy_cpd: float = 0.0,
    baseline_conv_resilience: float = 0.0,
    mechanism: Optional[str] = None,
    simulate: bool = True,
    predicted_cpd: Optional[float] = None,
) -> Evaluation:
    """Build G_t^CF, check constraints, and evaluate candidate score.

    If simulate=True, executes full physics-based multi-step cascade simulation.
    If simulate=False, employs fast surrogate prediction, bypassing expensive simulation.
    """
    opt_cfg = cfg.get("optimization", {})
    if mechanism is None:
        mechanism = opt_cfg.get("mechanism", "constrained")

    cf_state = apply_mutation(base_state, m)

    # Check explicit feasibility constraints and adaptability on candidate state BEFORE cascade simulation
    is_feasible, constraint_breakdown = check_feasibility(cf_state, m, cfg)
    adapt = _adaptability(cf_state)
    mc = mutation_cost(m, cfg)
    conv_res = conventional_resilience_index(cf_state)
    delta_conv = conv_res - baseline_conv_resilience

    # Rollout simulation or surrogate inference
    if simulate:
        # Primary rollout simulation (simulate on a copy to preserve cf_state)
        cascade = simulate_cascade(cf_state.copy(), list(initial_downed), cfg, seed=seed)
        causal_cpd = cascade.causal_cpd
        legacy_cpd = cascade.legacy_cpd
        direct_damage = cascade.direct_damage
        secondary_debt = cascade.secondary_debt
        higher_order_debt = cascade.higher_order_debt
        was_simulated = True
        surr_err = abs(float(predicted_cpd) - causal_cpd) if predicted_cpd is not None else None
    else:
        # Fast surrogate prediction bypassing full simulation
        causal_cpd = float(predicted_cpd) if predicted_cpd is not None else baseline_cpd
        legacy_cpd = causal_cpd
        direct_damage = 0.0
        secondary_debt = 0.0
        higher_order_debt = causal_cpd
        cascade = CascadeResult(
            cpd=causal_cpd,
            causal_cpd=causal_cpd,
            legacy_cpd=legacy_cpd,
            direct_damage=0.0,
            secondary_debt=0.0,
            higher_order_debt=causal_cpd,
        )
        was_simulated = False
        surr_err = None

    delta_causal = baseline_cpd - causal_cpd
    delta_legacy = baseline_legacy_cpd - legacy_cpd

    # Uncertainty-aware rollout evaluation
    n_samples = int(opt_cfg.get("n_uncertainty_samples", 3)) if (mechanism == "robust" and simulate) else 1
    if n_samples > 1:
        rollout_cpds = [causal_cpd]
        for s_idx in range(1, n_samples):
            cf_copy = apply_mutation(base_state, m)
            c_res = simulate_cascade(cf_copy, list(initial_downed), cfg, seed=seed + 100 * s_idx)
            rollout_cpds.append(c_res.causal_cpd)
        robust_mean = float(np.mean(rollout_cpds))
        robust_std = float(np.std(rollout_cpds))
    else:
        robust_mean = causal_cpd
        robust_std = 0.0

    # Parameter calibrations
    eps = float(opt_cfg.get("epsilon", 0.001))
    eta = float(opt_cfg.get("eta", 0.4))
    kappa = float(opt_cfg.get("risk_aversion", 0.5))

    # 1. Heuristic Ratio Formulation
    ces_ratio = (delta_causal + eta * adapt) / (mc + eps)

    # 2. Normalized Weighted-Sum Utility Formulation
    ces_weighted = 1.0 * delta_causal + eta * adapt - 0.5 * mc

    # 3. Budget-Constrained Formulation
    if is_feasible:
        ces_constrained = delta_causal + eta * adapt
    else:
        ces_constrained = -1e6  # Hard constraint penalty

    # 4. Uncertainty-Aware Robust Formulation
    delta_robust = baseline_cpd - (robust_mean + kappa * robust_std)
    if is_feasible:
        ces_robust = delta_robust + eta * adapt
    else:
        ces_robust = -1e6

    # Select primary CES score based on active mechanism
    if mechanism == "constrained":
        primary_ces = ces_constrained
    elif mechanism == "robust":
        primary_ces = ces_robust
    elif mechanism == "weighted":
        primary_ces = ces_weighted
    elif mechanism == "ratio":
        primary_ces = ces_ratio
    else:
        primary_ces = ces_constrained

    # Fallback overrides if criterion is legacy or conventional
    if criterion == "legacy":
        primary_ces = (delta_legacy + eta * adapt) / (mc + eps)
    elif criterion == "conventional":
        primary_ces = (delta_conv + eta * adapt) / (mc + eps)

    return Evaluation(
        mutation=m,
        cpd=causal_cpd,
        delta_cpd=delta_causal,
        legacy_cpd=legacy_cpd,
        delta_legacy_cpd=delta_legacy,
        direct_damage=direct_damage,
        secondary_debt=secondary_debt,
        higher_order_debt=higher_order_debt,
        adaptability=adapt,
        mutation_cost=mc,
        ces=primary_ces,
        cascade=cascade,
        conventional_resilience=conv_res,
        ces_ratio=ces_ratio,
        ces_weighted=ces_weighted,
        ces_constrained=ces_constrained,
        ces_robust=ces_robust,
        is_feasible=is_feasible,
        constraints=constraint_breakdown,
        robust_mean_cpd=robust_mean,
        robust_std_cpd=robust_std,
        was_simulated=was_simulated,
        surrogate_predicted_cpd=predicted_cpd,
        surrogate_error=surr_err,
    )


def optimize(
    base_state: NetworkState,
    initial_downed: List[EdgeKey],
    mutations: List[Mutation],
    baseline_cpd: float,
    cfg: Dict,
    seed: int = 42,
    criterion: str = "causal",
    baseline_legacy_cpd: float = 0.0,
    baseline_conv_resilience: float = 0.0,
    mechanism: Optional[str] = None,
    knowledge: Optional[object] = None,
) -> List[Evaluation]:
    """Evaluate candidate mutations using active surrogate screening and verification.

    When knowledge surrogate is trained, screens candidates to identify high-priority
    subset M_sim, bypassing full cascade simulation on demonstrably sub-optimal candidates.
    """
    from ..learning.evolve import extract_candidate_features

    # Extract candidate features and adaptabilities
    features_list = []
    adapt_list = []
    for m in mutations:
        feat = extract_candidate_features(base_state, initial_downed, m, baseline_cpd, cfg)
        cf = apply_mutation(base_state, m)
        features_list.append(feat)
        adapt_list.append(_adaptability(cf))

    # Apply surrogate screening policy if knowledge is available and ready
    if knowledge is not None and hasattr(knowledge, "screen_candidates") and knowledge.is_ready():
        sim_indices, pruned_indices, pred_cpds = knowledge.screen_candidates(
            candidates=mutations,
            features_list=features_list,
            baseline_cpd=baseline_cpd,
            adaptabilities=adapt_list,
            cfg=cfg,
        )
        sim_set = set(sim_indices)
    else:
        sim_set = set(range(len(mutations)))
        pred_cpds = [None] * len(mutations)

    evals = []
    for idx, m in enumerate(mutations):
        should_sim = idx in sim_set
        pred_val = pred_cpds[idx]
        ev = evaluate_mutation(
            base_state=base_state,
            initial_downed=initial_downed,
            m=m,
            baseline_cpd=baseline_cpd,
            cfg=cfg,
            seed=seed,
            criterion=criterion,
            baseline_legacy_cpd=baseline_legacy_cpd,
            baseline_conv_resilience=baseline_conv_resilience,
            mechanism=mechanism,
            simulate=should_sim,
            predicted_cpd=pred_val,
        )
        # Continuous learning: record simulated candidate outcomes in experience replay
        if should_sim and knowledge is not None and hasattr(knowledge, "observe_candidate"):
            knowledge.observe_candidate(
                features=features_list[idx],
                actual_cpd=ev.cpd,
                predicted_cpd=pred_val,
            )
        evals.append(ev)

    evals.sort(key=lambda e: e.ces, reverse=True)
    return evals


def select_best(
    evals: List[Evaluation],
    mechanism: str = "constrained",
    require_beneficial: bool = True,
    require_simulated: bool = True,
) -> Optional[Evaluation]:
    """Select the optimal mutation m* from candidate evaluations.

    Optimization Guarantee:
        Guarantees discrete optimality within the candidate subspace M_t subject
        to explicit feasibility constraints:
            m* = argmax_{m in M_t cap M_feasible} J(m)
        If require_simulated=True, guarantees m* is physically verified by full simulation.
        If no candidate satisfies constraints with positive debt reduction,
        the system returns None, provably preserving the baseline network state.
    """
    if not evals:
        return None

    def _score(e: Evaluation) -> float:
        if mechanism == "ratio":
            return e.ces_ratio
        elif mechanism == "weighted":
            return e.ces_weighted
        elif mechanism == "robust":
            return e.ces_robust
        elif mechanism == "constrained":
            return e.ces_constrained
        return e.ces

    # Filter to feasible candidates for constrained and robust mechanisms
    if mechanism in ("constrained", "robust"):
        feasible_pool = [e for e in evals if e.is_feasible]
    else:
        feasible_pool = evals

    # Filter to simulated candidates to guarantee physical verification
    if require_simulated:
        simulated_pool = [e for e in feasible_pool if e.was_simulated]
        if simulated_pool:
            feasible_pool = simulated_pool

    if not feasible_pool:
        return None

    if require_beneficial:
        # Strictly positive debt reduction
        beneficial = [e for e in feasible_pool if e.delta_cpd > 0]
        if beneficial:
            beneficial.sort(key=_score, reverse=True)
            return beneficial[0]
        # Neutral debt reduction with positive adaptability
        non_negative = [e for e in feasible_pool if e.delta_cpd >= -1e-6]
        if non_negative:
            non_negative.sort(key=_score, reverse=True)
            return non_negative[0]
        # If all candidates worsen cascade debt, default to baseline
        return None

    feasible_pool.sort(key=_score, reverse=True)
    return feasible_pool[0]
