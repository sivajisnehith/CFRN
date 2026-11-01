"""Candidate counterfactual mutations M_t (Stage 3).

Mutation types mirror the architecture diagram:
    - remove_link   : selective link deactivation to mitigate Braess's paradox
                      and prevent failure contagion onto vulnerable paths.
    - add_link      : protective activation of pre-provisioned standby dark fiber /
                      optical channels between feasible high-centrality PoPs.
    - reroute_flow  : surgical flow diversion shifting traffic off stressed links
                      with adaptive or calibrated diversion fractions.
    - isolate_path  : isolate a propagation path by cutting a bridging link to
                      contain localized cascading failures away from the core.
    - composite     : sequential composition of compatible atomic mutations.

Each mutation is a pure description plus an ``apply`` that returns a new,
mutated NetworkState (the counterfactual world G_t^CF = G_t (+) m_i).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple, Union

import networkx as nx
import numpy as np

from ..network.state import EdgeKey, NetworkState, _canon


@dataclass
class Mutation:
    """A candidate counterfactual change m_i."""

    kind: str
    params: dict
    # Cost drivers: link changes, service changes, effort (OpEx formulation)
    n_link_changes: int = 0
    n_service_changes: int = 0
    effort: float = 0.0

    def describe(self) -> str:
        return f"{self.kind}({self.params})"


def calculate_adaptive_fraction(
    state: NetworkState, e: EdgeKey, target_util: float = 0.8
) -> float:
    """Compute the mathematically justified flow diversion fraction f_divert.

    To bring link e's utilization down to a safe target threshold
    u_target = target_util * theta_over, the required load reduction is:
        Delta l_target = max(0, l(e) - u_target * c(e))

    The diversion fraction is bounded by the residual capacity of the
    alternate shortest bypass path P_alt:
        c_residual(P_alt) = min_{e' in P_alt} (c(e') - l(e'))
    so that rerouted traffic does not immediately overload the bypass.
    """
    load = state.link_load.get(e, 0.0)
    cap = state.link_capacity.get(e, 1.0)
    if load <= 0.0 or cap <= 0.0:
        return 0.5

    target_load = target_util * cap
    excess_load = max(0.0, load - target_load)
    if excess_load <= 0.0:
        return 0.25

    # Check alternate path residual headroom
    u, v = e
    g_alt = state.graph.copy()
    if g_alt.has_edge(u, v):
        g_alt.remove_edge(u, v)

    try:
        path = nx.shortest_path(g_alt, u, v)
        headrooms = []
        for a, b in zip(path[:-1], path[1:]):
            edge = _canon(a, b)
            h = state.link_capacity.get(edge, 1.0) - state.link_load.get(edge, 0.0)
            headrooms.append(max(0.0, h))
        path_headroom = min(headrooms) if headrooms else excess_load
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        path_headroom = 0.0

    if path_headroom <= 0.0:
        return 0.25

    # Bounded diversion: enough to relieve link e without exceeding bypass headroom
    safe_diversion = min(excess_load, path_headroom)
    return float(np.clip(safe_diversion / load, 0.15, 0.85))


def _apply_remove_link(state: NetworkState, params: Union[dict, EdgeKey]) -> NetworkState:
    """Proactively deactivates a link to arrest cascading failure contagion.

    Unlike an uncontrolled failure under peak load that dumps shockwaves onto
    adjacent buffers, controlled link removal is an orderly administrative
    shutdown (e.g. BGP teardown / metric poisoning) that forces early diversion
    around congestion chokepoints, mitigating Braess's Paradox.
    """
    e = params["edge"] if isinstance(params, dict) else params
    ns = state.copy()
    if ns.graph.has_edge(*e):
        ns.graph.remove_edge(*e)
        ns.link_load.pop(e, None)
        ns.link_capacity.pop(e, None)
    return ns


def _apply_add_link(state: NetworkState, params: Union[dict, EdgeKey]) -> NetworkState:
    """Protective activation of pre-provisioned dark fiber / optical transponders.

    Represents lighting an existing unlit standby optical wavelength channel
    between two PoPs rather than laying new physical conduits.
    """
    e = params["edge"] if isinstance(params, dict) else params
    ns = state.copy()
    u, v = e
    if u in ns.graph and v in ns.graph and not ns.graph.has_edge(u, v):
        ns.graph.add_edge(u, v)
        caps = [c for c in ns.link_capacity.values() if c > 0]
        cap = float(np.median(caps)) if caps else 1.0
        ns.link_load[e] = 0.0
        ns.link_capacity[e] = cap
    return ns


def _apply_reroute(state: NetworkState, params: Union[dict, EdgeKey]) -> NetworkState:
    """Model rerouting as moving load off a stressed link onto its alternate path."""
    if isinstance(params, dict):
        e = params["edge"]
        fraction = float(params.get("fraction", 0.5))
    else:
        e = params
        fraction = 0.5

    ns = state.copy()
    if not ns.graph.has_edge(*e):
        return ns

    curr_load = ns.link_load.get(e, 0.0)
    moved = fraction * curr_load
    if moved <= 0:
        return ns

    u, v = e
    g2 = ns.graph.copy()
    g2.remove_edge(u, v)
    try:
        path = nx.shortest_path(g2, u, v)
        # Apply load shift
        ns.link_load[e] = max(0.0, curr_load - moved)
        for a, b in zip(path[:-1], path[1:]):
            k = _canon(a, b)
            if k in ns.link_load:
                ns.link_load[k] += moved
    except nx.NetworkXNoPath:
        # If no alternate path exists, load cannot be rerouted
        pass
    return ns


def _apply_isolate_path(state: NetworkState, params: Union[dict, EdgeKey]) -> NetworkState:
    """Isolate a propagation path by cutting a bridging link to prevent

    failure contagion from crossing between distinct topological clusters.
    """
    return _apply_remove_link(state, params)


def _apply_composite(state: NetworkState, params: dict) -> NetworkState:
    """Sequentially apply a list of verified compatible component mutations."""
    mutations: List[Mutation] = params.get("mutations", [])
    curr = state
    for m in mutations:
        curr = apply_mutation(curr, m)
    return curr


_APPLY: Dict[str, Callable[[NetworkState, Union[dict, EdgeKey]], NetworkState]] = {
    "remove_link": _apply_remove_link,
    "add_link": _apply_add_link,
    "reroute_flow": _apply_reroute,
    "isolate_path": _apply_isolate_path,
    "composite": _apply_composite,
}


def apply_mutation(state: NetworkState, m: Mutation) -> NetworkState:
    """Return the counterfactual world G_t^CF = G_t (+) m."""
    if m.kind == "composite":
        return _apply_composite(state, m.params)
    param_arg = m.params.get("edge", m.params)
    if m.kind == "reroute_flow":
        param_arg = m.params
    return _APPLY[m.kind](state, param_arg)


# -----------------------------------------------------------------------------
# Mutation Composition & Conflict Rejection
# -----------------------------------------------------------------------------

def are_mutations_compatible(m1: Mutation, m2: Mutation) -> Tuple[bool, str]:
    """Verify that two candidate mutations do not execute conflicting actions.

    Conflict Rejection Rules:
    1. Topology conflict: Cannot add and remove the same link.
    2. Routing conflict: Cannot reroute flow onto an edge that is removed or isolated.
    3. Target overlap: Cannot perform duplicate contradictory operations on same edge.
    """
    e1 = m1.params.get("edge")
    e2 = m2.params.get("edge")

    if e1 is None or e2 is None:
        return True, "valid"

    e1_c = _canon(*e1) if isinstance(e1, (list, tuple)) else e1
    e2_c = _canon(*e2) if isinstance(e2, (list, tuple)) else e2

    # Direct identity conflict
    if e1_c == e2_c:
        if m1.kind == "add_link" and m2.kind in ("remove_link", "isolate_path"):
            return False, f"Topology conflict: adding and removing edge {e1_c}"
        if m2.kind == "add_link" and m1.kind in ("remove_link", "isolate_path"):
            return False, f"Topology conflict: removing and adding edge {e1_c}"
        if m1.kind == m2.kind:
            return False, f"Duplicate redundant mutation on edge {e1_c}"

    # Routing onto a severed edge
    if m1.kind in ("remove_link", "isolate_path") and m2.kind == "reroute_flow":
        if e1_c == e2_c:
            return False, f"Routing conflict: rerouting traffic off/through severed edge {e1_c}"

    return True, "valid"


def compose_mutations(
    m1: Mutation, m2: Mutation, max_budget: float = 2.5
) -> Optional[Mutation]:
    """Compose two compatible atomic mutations into a composite mutation.

    Rejects conflicting interventions or composite mutations exceeding budget B_max.
    """
    compat, reason = are_mutations_compatible(m1, m2)
    if not compat:
        return None

    total_links = m1.n_link_changes + m2.n_link_changes
    total_service = m1.n_service_changes + m2.n_service_changes
    total_effort = m1.effort + m2.effort

    # Approximate OpEx cost
    cost = 1.0 * total_links + 0.5 * total_service + 0.3 * total_effort
    if cost > max_budget:
        return None

    return Mutation(
        kind="composite",
        params={"mutations": [m1, m2]},
        n_link_changes=total_links,
        n_service_changes=total_service,
        effort=total_effort,
    )


# -----------------------------------------------------------------------------
# Candidate Generation
# -----------------------------------------------------------------------------

def _stressed_links(state: NetworkState, k: int) -> List[EdgeKey]:
    util = state.utilisation()
    return [e for e, _ in sorted(util.items(), key=lambda kv: kv[1], reverse=True)[:k]]


def _candidate_add_links(state: NetworkState, k: int, seed: int) -> List[EdgeKey]:
    """Propose protective links between feasible high-centrality, non-adjacent nodes.

    Physical Feasibility Constraints:
    - Nodes must not already be connected.
    - Nodes must be within optical transport reachability (shortest path distance <= 3
      in the existing backbone topology, representing adjacent optical rings/PoPs).
    """
    rng = np.random.default_rng(seed)
    g = state.graph
    bc = nx.get_node_attributes(g, "betweenness") or nx.betweenness_centrality(g)
    top = [n for n, _ in sorted(bc.items(), key=lambda kv: kv[1], reverse=True)[: 2 * k]]
    out: List[EdgeKey] = []
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            u, v = top[i], top[j]
            if not g.has_edge(u, v):
                # Verify reachability in the underlying network
                try:
                    dist = nx.shortest_path_length(g, u, v)
                    if 2 <= dist <= 3:
                        out.append(_canon(u, v))
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    pass
    rng.shuffle(out)
    return out[:k]


def generate_mutations(
    state: NetworkState,
    initial_downed: List[EdgeKey],
    cfg: Dict,
    seed: int = 42,
) -> List[Mutation]:
    """Generate candidate mutations M_t for the post-failure network."""
    ccfg = cfg.get("counterfactual", {})
    enable = ccfg.get("enable", {})
    cap = int(ccfg.get("max_candidates", 24))
    per_type = max(1, cap // max(1, sum(bool(v) for v in enable.values())))

    muts: List[Mutation] = []
    stressed = _stressed_links(state, per_type * 2)

    # 1. Selective link removal (Braess's paradox mitigation)
    if enable.get("remove_link"):
        for e in stressed[:per_type]:
            muts.append(
                Mutation("remove_link", {"edge": e}, n_link_changes=1, effort=1.0)
            )

    # 2. Path isolation (articulation bridge cutting to contain cascade)
    if enable.get("isolate_path"):
        bridges = list(nx.bridges(state.graph)) if state.graph.number_of_edges() else []
        bridges = [_canon(u, v) for u, v in bridges][:per_type]
        for e in (bridges or stressed[:per_type]):
            muts.append(
                Mutation(
                    "isolate_path",
                    {"edge": e},
                    n_link_changes=1,
                    n_service_changes=1,
                    effort=1.5,
                )
            )

    # 3. Surgical flow rerouting (evaluating adaptive and discrete diversion fractions)
    if enable.get("reroute_flow"):
        diversion_fractions = ccfg.get("reroute_fractions", [0.25, 0.5, 0.75])
        for e in stressed[:per_type]:
            # Always generate the adaptive headroom-calibrated fraction
            adapt_frac = calculate_adaptive_fraction(state, e)
            muts.append(
                Mutation(
                    "reroute_flow",
                    {"edge": e, "fraction": adapt_frac},
                    n_service_changes=1,
                    effort=0.8,
                )
            )
            # Add secondary calibrated fraction for trade-off evaluation if budget permits
            for frac in diversion_fractions:
                if abs(frac - adapt_frac) > 0.15:
                    muts.append(
                        Mutation(
                            "reroute_flow",
                            {"edge": e, "fraction": frac},
                            n_service_changes=1,
                            effort=0.8,
                        )
                    )
                    break

    # 4. Protective link addition (standby optical channel illumination)
    if enable.get("add_link"):
        for e in _candidate_add_links(state, per_type, seed):
            muts.append(
                Mutation("add_link", {"edge": e}, n_link_changes=1, effort=2.0)
            )

    # 5. Composite mutations: pair compatible high-value mutations if capacity permits
    if len(muts) >= 2 and len(muts) < cap:
        m1 = muts[0]
        for m2 in muts[1:]:
            comp = compose_mutations(m1, m2, max_budget=float(cfg.get("optimization", {}).get("budget_max", 2.5)))
            if comp is not None:
                muts.append(comp)
                break

    return muts[:cap]
