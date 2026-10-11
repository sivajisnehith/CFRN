"""Candidate counterfactual mutations M_t (Stage 3).

Mutation types mirror the architecture diagram:
    - remove_link   : remove a vulnerable (near-overload) link
    - add_link      : add a protective link that creates an alternate path
    - reroute_flow  : shift demand away from a stressed link (modeled as a
                      capacity boost on an alternate + penalty on the stressed)
    - isolate_path  : isolate a propagation path by cutting a bridging link

Each mutation is a pure description plus an ``apply`` that returns a new,
mutated NetworkState (the counterfactual world G_t^CF = G_t (+) m_i).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import networkx as nx
import numpy as np

from ..network.state import EdgeKey, NetworkState, _canon


@dataclass
class Mutation:
    """A candidate counterfactual change m_i."""

    kind: str
    params: dict
    # cost drivers (plan Section 10): link changes, service changes, effort
    n_link_changes: int = 0
    n_service_changes: int = 0
    effort: float = 0.0

    def describe(self) -> str:
        return f"{self.kind}({self.params})"


def _apply_remove_link(state: NetworkState, e: EdgeKey) -> NetworkState:
    ns = state.copy()
    if ns.graph.has_edge(*e):
        ns.graph.remove_edge(*e)
        ns.link_load.pop(e, None)
        ns.link_capacity.pop(e, None)
    return ns


def _apply_add_link(state: NetworkState, e: EdgeKey) -> NetworkState:
    ns = state.copy()
    u, v = e
    if u in ns.graph and v in ns.graph and not ns.graph.has_edge(u, v):
        ns.graph.add_edge(u, v)
        # New link starts empty; capacity equals median capacity of the network.
        caps = [c for c in ns.link_capacity.values() if c > 0]
        cap = float(np.median(caps)) if caps else 1.0
        ns.link_load[e] = 0.0
        ns.link_capacity[e] = cap
    return ns


def _apply_reroute(state: NetworkState, e: EdgeKey) -> NetworkState:
    """Model rerouting as moving load off a stressed link onto its best
    alternate path, reducing the stressed link's utilisation."""
    ns = state.copy()
    if not ns.graph.has_edge(*e):
        return ns
    moved = 0.5 * ns.link_load.get(e, 0.0)
    ns.link_load[e] = ns.link_load.get(e, 0.0) - moved
    u, v = e
    g2 = ns.graph.copy()
    g2.remove_edge(u, v)
    try:
        path = nx.shortest_path(g2, u, v)
        for a, b in zip(path[:-1], path[1:]):
            k = _canon(a, b)
            if k in ns.link_load:
                ns.link_load[k] += moved
    except nx.NetworkXNoPath:
        pass
    return ns


def _apply_isolate_path(state: NetworkState, e: EdgeKey) -> NetworkState:
    """Isolate a propagation path by cutting a bridging link (same mechanics as
    remove, but semantically targets bridges/articulation links)."""
    return _apply_remove_link(state, e)


_APPLY: Dict[str, Callable[[NetworkState, EdgeKey], NetworkState]] = {
    "remove_link": _apply_remove_link,
    "add_link": _apply_add_link,
    "reroute_flow": _apply_reroute,
    "isolate_path": _apply_isolate_path,
}


def apply_mutation(state: NetworkState, m: Mutation) -> NetworkState:
    """Return the counterfactual world G_t^CF = G_t (+) m."""
    return _APPLY[m.kind](state, m.params["edge"])


def _stressed_links(state: NetworkState, k: int) -> List[EdgeKey]:
    util = state.utilisation()
    return [e for e, _ in sorted(util.items(), key=lambda kv: kv[1], reverse=True)[:k]]


def _candidate_add_links(state: NetworkState, k: int, seed: int) -> List[EdgeKey]:
    """Propose protective links between high-centrality, non-adjacent nodes."""
    rng = np.random.default_rng(seed)
    g = state.graph
    bc = nx.get_node_attributes(g, "betweenness") or nx.betweenness_centrality(g)
    top = [n for n, _ in sorted(bc.items(), key=lambda kv: kv[1], reverse=True)[: 2 * k]]
    out: List[EdgeKey] = []
    for i in range(len(top)):
        for j in range(i + 1, len(top)):
            u, v = top[i], top[j]
            if not g.has_edge(u, v):
                out.append(_canon(u, v))
    rng.shuffle(out)
    return out[:k]


def generate_mutations(
    state: NetworkState,
    initial_downed: List[EdgeKey],
    cfg: Dict,
    seed: int = 42,
) -> List[Mutation]:
    """Generate candidate mutations M_t for the post-failure network."""
    ccfg = cfg["counterfactual"]
    enable = ccfg["enable"]
    cap = int(ccfg["max_candidates"])
    per_type = max(1, cap // max(1, sum(bool(v) for v in enable.values())))

    muts: List[Mutation] = []
    stressed = _stressed_links(state, per_type)

    if enable.get("remove_link"):
        for e in stressed:
            muts.append(Mutation("remove_link", {"edge": e},
                                  n_link_changes=1, effort=1.0))
    if enable.get("isolate_path"):
        # Articulation-adjacent / bridge links are good isolation targets.
        bridges = list(nx.bridges(state.graph)) if state.graph.number_of_edges() else []
        bridges = [_canon(u, v) for u, v in bridges][:per_type]
        for e in (bridges or stressed[:per_type]):
            muts.append(Mutation("isolate_path", {"edge": e},
                                  n_link_changes=1, n_service_changes=1, effort=1.5))
    if enable.get("reroute_flow"):
        for e in stressed:
            muts.append(Mutation("reroute_flow", {"edge": e},
                                  n_service_changes=1, effort=0.8))
    if enable.get("add_link"):
        for e in _candidate_add_links(state, per_type, seed):
            muts.append(Mutation("add_link", {"edge": e},
                                  n_link_changes=1, effort=2.0))

    return muts[:cap]
