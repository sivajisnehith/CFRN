"""Multi-step failure-propagation and Causal Propagation Debt (CPD).

Mathematical Formulation
-------------------------
Let G_0 = (V, E) be the initial network state subjected to an initiating failure
event F_t (e.g., node removal or critical link severing) at step tau=0.

1. Structural Causal Model (SCM) of Failure Propagation:
   Under Pearl's SCM framework, the transition of network states G_{tau-1} -> G_tau
   under intervention do(F_t, m) is governed by three structural equations:
   - Topological transition:
       G_tau = G_{tau-1} \\ E_down^{(tau-1)}
   - Endogenous flow redistribution:
       When links fail, their carried load is redistributed onto surviving alternate
       shortest paths between their respective endpoints:
       l_tau(e) = l_{tau-1}(e) + sum_{e' in E_down^{(tau-1)}} rho * 1_{e in P(endpoints(e'))} * l_{tau-1}(e')
   - Stochastic overload failure:
       Link utilization u_tau(e) = l_tau(e) / c(e). If u_tau(e) > theta_over,
       link e fails stochastically with probability p_fail(u_tau(e)):
       Pr(e in E_down^{(tau)} | u_tau(e) > theta_over) = p_fail

2. Causal Disruption Orders:
   - Order-0 (Direct Failure Damage D_dir): Immediate, unavoidable loss at tau=0
     directly caused by F_t before any redistribution or secondary overloads occur.
   - Order-1 (Secondary Propagation Debt delta_sec): Downstream overloads and failures
     at tau=1 triggered by shedding the load of F_t onto alternative paths.
   - Order-k (Higher-Order Propagation Debt delta_high): Multi-hop cascade avalanches
     at tau >= 2 resulting from the cascading failure of secondary or later links.

3. Mathematically Justified Non-Double-Counting Causal CPD (C-CPD):
   Conventional resilience and legacy CPD objectives sum the total state-level damage
   at each step, repeatedly penalizing the same initially disconnected nodes and
   unserved flows across all H horizon steps.

   C-CPD eliminates double-counting by penalizing only:
   (a) Marginal Realized Propagation Damage:
       Delta D_prop^{(tau)} = w_disc * max(0, Psi_disc(G_tau) - Psi_disc(G_{tau-1}))
                            + w_lost * max(0, Omega_lost(G_tau) - Omega_lost(G_{tau-1}))
                            + w_fail * (|E_down^{(tau)}| / |E_0|)
       where the telescoping sum sum_{tau=1..H} Delta D_prop^{(tau)} equals the exact net
       incremental degradation caused by propagation.
   (b) Latent Cascading Hazard:
       H_latent(G_tau) = (1 / |E_tau|) * sum_{e in E_tau} phi(u_tau(e))
       measuring unresolved stress on surviving links that threatens subsequent cascades.

   The unified Causal Counterfactual Propagation Debt is:
       C-CPD(G) = sum_{tau=1..H} gamma^{tau-1} * [ Delta D_prop^{(tau)} + lambda_latent * H_latent(G_tau) ]
                = delta_sec (tau=1) + delta_high (tau >= 2)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import networkx as nx
import numpy as np

from ..network.state import EdgeKey, NetworkState, _canon

# Weights for legacy CPD (retained for comparative benchmarking)
_W_OVER = 0.4
_W_DISC = 0.4
_W_LOST = 0.2

# Weights for Causal CPD (C-CPD non-double-counting objective)
_W_DISC_MARGINAL = 0.4
_W_LOST_MARGINAL = 0.3
_W_FAIL_MARGINAL = 0.3
_LAMBDA_LATENT = 0.35


@dataclass
class CascadeStep:
    step: int
    newly_failed: List[EdgeKey]
    overloaded: List[EdgeKey]
    d_fp: float                          # Legacy step debt
    marginal_realized: float = 0.0       # Realized incremental damage at this step
    latent_hazard: float = 0.0           # Latent overload hazard on surviving active edges
    causal_step_debt: float = 0.0        # Combined causal step debt (marginal + lambda * latent)
    newly_disconnected_nodes: int = 0
    newly_unserved_load: float = 0.0


@dataclass
class CascadeResult:
    steps: List[CascadeStep] = field(default_factory=list)
    cpd: float = 0.0                     # Primary CPD score (points to causal_cpd)
    causal_cpd: float = 0.0              # Mathematically justified non-double-counting C-CPD
    legacy_cpd: float = 0.0              # Legacy discounted sum of static state damage
    direct_damage: float = 0.0           # Order-0 direct damage (at tau=0)
    secondary_debt: float = 0.0          # Order-1 secondary propagation debt (tau=1)
    higher_order_debt: float = 0.0       # Order >= 2 higher-order cascading debt (tau >= 2)
    cascade_size: int = 0                # Total secondary & higher-order links failed
    cascade_depth: int = 0               # Number of steps with newly failed links
    disconnected_nodes: int = 0          # Net disconnected nodes after cascade
    unserved_load: float = 0.0           # Net unserved demand after cascade
    causal_graph: Optional[nx.DiGraph] = None  # Causal Propagation DAG (PG_t)
    stabilized: bool = True              # Whether the cascade reached a stable equilibrium
    stabilization_step: Optional[int] = None  # Step tau at which cascade stabilized

    def step_debts(self) -> List[float]:
        """Returns the causal step debts."""
        return [s.causal_step_debt for s in self.steps]

    def legacy_step_debts(self) -> List[float]:
        """Returns legacy step debts for ablation and comparison."""
        return [s.d_fp for s in self.steps]


def _disconnected_fraction(state: NetworkState) -> Tuple[float, int]:
    g = state.graph
    n = g.number_of_nodes()
    if n == 0:
        return 1.0, 0
    largest = max((len(c) for c in nx.connected_components(g)), default=0)
    disconnected = n - largest
    return disconnected / n, disconnected


def _unserved_load(state: NetworkState) -> Tuple[float, float]:
    """Fraction and volume of original demand that can no longer be routed."""
    g = state.graph
    total = sum(d for _, _, d in state.demands) or 1.0
    served = 0.0
    for s, d, demand in state.demands:
        if s in g and d in g and nx.has_path(g, s, d):
            served += demand
    unserved = total - served
    return unserved / total, unserved


def _congestion_pressure(state: NetworkState) -> float:
    """Smooth, strictly convex latent congestion penalty on surviving active links.

    Each link contributes excess utilization above the safety knee (0.75 * threshold),
    with super-linear acceleration beyond the hard overload threshold.
    Evaluated strictly over surviving active links E(G_tau).
    """
    util = state.utilisation()
    if not util:
        return 1.0
    knee = 0.75 * state.overload_threshold
    total = 0.0
    for u in util.values():
        if u <= knee:
            continue
        excess = u - knee
        if u > state.overload_threshold:
            excess += 2.0 * ((u - state.overload_threshold) ** 2)
        total += excess
    return total / len(util)


def _step_debt_legacy(state: NetworkState) -> Tuple[float, List[EdgeKey]]:
    """Legacy state-level debt (double-counts persistent static disconnections)."""
    congestion = _congestion_pressure(state)
    disc_frac, _ = _disconnected_fraction(state)
    lost_frac, _ = _unserved_load(state)
    d_fp = _W_OVER * congestion + _W_DISC * disc_frac + _W_LOST * lost_frac
    return d_fp, state.overloaded_links()


def _redistribute(
    state: NetworkState, downed: List[EdgeKey], frac: float
) -> Dict[EdgeKey, List[EdgeKey]]:
    """Push a fraction of each downed link's load onto an alternate path.

    Returns a causal routing trace mapping: downed_link -> list of alternate
    path edges that absorbed its shed load.
    """
    g = state.graph
    causal_routes: Dict[EdgeKey, List[EdgeKey]] = {}
    for (u, v) in downed:
        e_down = _canon(u, v)
        load = frac * state._pending_load.get(e_down, 0.0)
        if load <= 0 or u not in g or v not in g:
            continue
        try:
            path = nx.shortest_path(g, u, v)
        except nx.NetworkXNoPath:
            continue  # load is unroutable -> manifests as unserved demand
        routed_edges: List[EdgeKey] = []
        for a, b in zip(path[:-1], path[1:]):
            e = _canon(a, b)
            if e in state.link_load:
                state.link_load[e] += load
                routed_edges.append(e)
        if routed_edges:
            causal_routes[e_down] = routed_edges
    return causal_routes


def overload_failure_probability(
    util: float,
    threshold: float = 1.0,
    base_prob: float = 0.6,
    beta: float = 0.4,
) -> float:
    """Exact, continuous overload-failure probability function p_fail(u).

    If utilization u <= threshold, link operates within engineered capacity;
    failure probability is strictly 0.0.

    If utilization u > threshold, the link experiences queue saturation, buffer bloat,
    and line-card thermal strain. Failure probability increases monotonically with
    excess relative overload:
        p_fail(u) = min(1.0, base_prob + beta * ((u - threshold) / threshold))

    Justification:
    - base_prob (default 0.60): Represents the baseline probability that sustained line
      saturation causes routing control-plane heartbeats (BGP/IS-IS hellos) to be dropped
      past the session hold-down timer (90s), inducing session reset and link tear-down.
    - beta (default 0.40): Captures acute packet-drop rate and automated circuit protection
      tripping under extreme traffic bursts (e.g., u >= 2.0 * threshold guarantees failure).
    """
    if util <= threshold:
        return 0.0
    excess_ratio = (util - threshold) / max(threshold, 1e-6)
    return float(np.clip(base_prob + beta * excess_ratio, 0.0, 1.0))


def simulate_cascade(
    state: NetworkState,
    initial_downed: List[EdgeKey],
    cfg: Dict,
    seed: int = 42,
) -> CascadeResult:
    """Simulate an H-step cascade and compute Causal Propagation Debt (C-CPD).

    Cascade Mechanics and Operational Reproducibility:
    --------------------------------------------------
    1. Failed links remain permanently unavailable throughout the horizon H
       (physical restoration operates on hours/days timescales, outside cascade window).
    2. At each step tau in {1..H}, operations execute in strict sequential order:
       a. Ingest downed links from previous step.
       b. Redistribute carried load across surviving shortest paths between endpoints.
          If no alternate path exists, unroutable load becomes unserved demand.
       c. Update link utilizations u_tau(e) and evaluate latent congestion hazard.
       d. Identify overloaded links O_tau = {e | u_tau(e) > theta_over}.
       e. Sample stochastic failures via exact p_fail(u_tau(e)).
       f. Prune newly failed links from active graph G_tau = G_{tau-1} \\ E_down^(tau).
       g. Check termination: if no failures and no overloaded links, cascade is stabilized.
    3. If cascade does not stabilize by horizon H, unresolved congestion is captured
       in latent hazard H_latent(G_H) and discounted by gamma^(H-1).
    """
    rng = np.random.default_rng(seed)
    pcfg = cfg.get("propagation", {})
    horizon = int(pcfg.get("horizon", 6))
    gamma = float(pcfg.get("gamma", 0.85))
    redistribution = float(pcfg.get("redistribution", 0.9))
    base_fail_prob = float(pcfg.get("overload_fail_prob", 0.6))
    beta_over = float(pcfg.get("overload_beta", 0.4))
    lambda_latent = float(pcfg.get("lambda_latent", _LAMBDA_LATENT))

    # Baseline network scale for relative failure fractions.
    total_initial_edges = len(state.active_edges()) + len(initial_downed)

    # -------------------------------------------------------------------------
    # Order-0: Direct Failure Damage (tau = 0, instantaneous exogenous shock)
    # -------------------------------------------------------------------------
    init_disc_frac, init_disc_nodes = _disconnected_fraction(state)
    init_lost_frac, init_lost_load = _unserved_load(state)
    init_failed_frac = len(initial_downed) / max(total_initial_edges, 1)
    direct_damage = (
        _W_DISC_MARGINAL * init_disc_frac
        + _W_LOST_MARGINAL * init_lost_frac
        + _W_FAIL_MARGINAL * init_failed_frac
    )

    # Track state metrics across steps to compute non-double-counting increments.
    prev_disc_frac = init_disc_frac
    prev_lost_frac = init_lost_frac
    prev_disc_nodes = init_disc_nodes
    prev_lost_vol = init_lost_load

    # Initialize Causal Propagation DAG (PG_t).
    cpg = nx.DiGraph()
    for e in initial_downed:
        cpg.add_node(str(e), step=0, order="direct", shed_load=state.link_load.get(e, 0.0))

    # Record pre-failure loads so redistribution knows how much to move.
    if not hasattr(state, "_pending_load") or state._pending_load is None:
        state._pending_load = {}  # type: ignore[attr-defined]
    for e, l in state.link_load.items():
        state._pending_load.setdefault(e, l)  # type: ignore[attr-defined]

    result = CascadeResult(direct_damage=direct_damage)
    downed = list(initial_downed)
    causal_cpd = 0.0
    legacy_cpd = 0.0
    secondary_debt = 0.0
    higher_order_debt = 0.0
    total_secondary_failed = 0
    depth = 0
    is_stabilized = False
    stabilization_step = None

    for step in range(1, horizon + 1):
        # 1. Endogenous flow redistribution with causal path logging.
        causal_routes = _redistribute(state, downed, redistribution)

        # 2. Latent hazard: convex congestion pressure on surviving active edges.
        latent_hazard = _congestion_pressure(state)

        # 3. Current topological state after redistribution.
        curr_disc_frac, curr_disc_nodes = _disconnected_fraction(state)
        curr_lost_frac, curr_lost_vol = _unserved_load(state)
        overloaded = state.overloaded_links()
        utils = state.utilisation()

        # 4. Stochastic failure generation on overloaded links via p_fail(u).
        newly_failed: List[EdgeKey] = []
        for e in overloaded:
            u_val = utils.get(e, 1.0)
            p_fail_val = overload_failure_probability(
                u_val, state.overload_threshold, base_fail_prob, beta_over
            )
            if rng.random() < p_fail_val and state.graph.has_edge(*e):
                newly_failed.append(e)

        # 5. Non-double-counting marginal realized propagation shock at step tau.
        delta_disc = max(0.0, curr_disc_frac - prev_disc_frac)
        delta_lost = max(0.0, curr_lost_frac - prev_lost_frac)
        delta_fail = len(newly_failed) / max(total_initial_edges, 1)

        marginal_realized = (
            _W_DISC_MARGINAL * delta_disc
            + _W_LOST_MARGINAL * delta_lost
            + _W_FAIL_MARGINAL * delta_fail
        )

        # Causal step debt combines marginal realized damage + discounted latent risk.
        causal_step_debt = marginal_realized + lambda_latent * latent_hazard

        # Legacy step debt (for comparative verification).
        legacy_step_debt = (
            _W_OVER * latent_hazard
            + _W_DISC * curr_disc_frac
            + _W_LOST * curr_lost_frac
        )

        # Temporal discounting.
        discount = gamma ** (step - 1)
        causal_cpd += discount * causal_step_debt
        legacy_cpd += discount * legacy_step_debt

        if step == 1:
            secondary_debt += discount * causal_step_debt
        else:
            higher_order_debt += discount * causal_step_debt

        # 6. Update Causal Propagation DAG with verified causal links.
        for e_child in newly_failed:
            order_label = "secondary" if step == 1 else "higher_order"
            cpg.add_node(str(e_child), step=step, order=order_label)
            # Find which parent links in downed caused traffic to cross e_child
            parents = [p for p, path_edges in causal_routes.items() if e_child in path_edges]
            if not parents and downed:
                # Fallback to general cascade predecessor if path was multi-hop
                parents = list(downed)
            for p in parents:
                cpg.add_edge(str(p), str(e_child), step=step)

        new_disc_count = max(0, curr_disc_nodes - prev_disc_nodes)
        new_lost_demand = max(0.0, curr_lost_vol - prev_lost_vol)

        result.steps.append(
            CascadeStep(
                step=step,
                newly_failed=list(newly_failed),
                overloaded=list(overloaded),
                d_fp=legacy_step_debt,
                marginal_realized=marginal_realized,
                latent_hazard=latent_hazard,
                causal_step_debt=causal_step_debt,
                newly_disconnected_nodes=new_disc_count,
                newly_unserved_load=new_lost_demand,
            )
        )

        # Advance state tracking.
        prev_disc_frac = curr_disc_frac
        prev_lost_frac = curr_lost_frac
        prev_disc_nodes = curr_disc_nodes
        prev_lost_vol = curr_lost_vol

        # Check stabilization condition: no new failures and no active overloads
        if not newly_failed and not overloaded:
            if not is_stabilized:
                is_stabilized = True
                stabilization_step = step

        if not newly_failed:
            downed = []
            continue

        depth += 1
        total_secondary_failed += len(newly_failed)
        state._pending_load = {}  # type: ignore[attr-defined]
        for e in newly_failed:
            state._pending_load[e] = state.link_load.get(e, 0.0)  # type: ignore[attr-defined]
            if state.graph.has_edge(*e):
                state.graph.remove_edge(*e)
            state.link_load.pop(e, None)
            state.link_capacity.pop(e, None)
        downed = newly_failed

    _, final_disc_nodes = _disconnected_fraction(state)
    _, final_unserved = _unserved_load(state)

    result.cpd = causal_cpd
    result.causal_cpd = causal_cpd
    result.legacy_cpd = legacy_cpd
    result.secondary_debt = secondary_debt
    result.higher_order_debt = higher_order_debt
    result.cascade_size = total_secondary_failed
    result.cascade_depth = depth
    result.disconnected_nodes = final_disc_nodes
    result.unserved_load = final_unserved
    result.causal_graph = cpg
    result.stabilized = is_stabilized
    result.stabilization_step = stabilization_step
    return result


def marginal_causal_contribution(
    result: CascadeResult, target_failure: EdgeKey
) -> float:
    """Quantify the downstream causal cascade contribution of an individual failure.

    Traces the forward reachability subgraph in the Causal Propagation DAG (PG_t)
    originating from target_failure and calculates the discounted downstream debt.
    """
    g = result.causal_graph
    target_str = str(_canon(*target_failure))
    if g is None or target_str not in g:
        return 0.0

    # Find all downstream cascade nodes causally triggered by target_failure.
    descendants = nx.descendants(g, target_str)
    descendants.add(target_str)

    # Accumulate step debt from steps where descendants failed.
    downstream_debt = 0.0
    for cs in result.steps:
        failed_descendants = [
            e for e in cs.newly_failed if str(e) in descendants
        ]
        if failed_descendants:
            weight = len(failed_descendants) / max(len(cs.newly_failed), 1)
            downstream_debt += weight * cs.causal_step_debt
    return downstream_debt


def propagation_graph(
    state: NetworkState, result: CascadeResult
) -> nx.DiGraph:
    """Retrieve or construct the causal failure-propagation graph PG_t."""
    if result.causal_graph is not None:
        return result.causal_graph

    # Fallback constructor if causal_graph was not pre-built.
    pg = nx.DiGraph()
    prev: List[EdgeKey] = []
    for cs in result.steps:
        for e in cs.newly_failed:
            pg.add_node(str(e), step=cs.step)
            for p in prev:
                pg.add_edge(str(p), str(e))
        if cs.newly_failed:
            prev = cs.newly_failed
    return pg
