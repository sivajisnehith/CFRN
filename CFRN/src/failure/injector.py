"""Controlled failure injection into the network state.

Supports node, link, and correlated/SRLG (Shared Risk Link Group) failures.
The initiating failure F_t can be chosen explicitly or by a strategy
(e.g., highest betweenness element or multi-link bundle), producing realistic
cascades for resilience evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

import networkx as nx

from ..network.state import EdgeKey, NetworkState, _canon

FailureTarget = Union[str, EdgeKey, List[EdgeKey]]


@dataclass
class Failure:
    """A single initiating failure event F_t."""

    kind: str                      # "node", "link", "correlated", or "srlg"
    target: FailureTarget

    def describe(self) -> str:
        return f"{self.kind}:{self.target}"


def _highest_betweenness_node(g: nx.Graph) -> str:
    bc = nx.get_node_attributes(g, "betweenness")
    if not bc:
        bc = nx.betweenness_centrality(g)
    return max(bc, key=bc.get)


def _highest_betweenness_link(g: nx.Graph) -> EdgeKey:
    eb = nx.edge_betweenness_centrality(g)
    u, v = max(eb, key=eb.get)
    return _canon(u, v)


def _correlated_bundle_links(g: nx.Graph, bundle_size: int = 2) -> List[EdgeKey]:
    """Select a group of high-centrality, structurally adjacent links (SRLG)."""
    eb = nx.edge_betweenness_centrality(g)
    sorted_links = [k for k, _ in sorted(eb.items(), key=lambda kv: kv[1], reverse=True)]
    if not sorted_links:
        return []
    primary = sorted_links[0]
    bundle = [_canon(*primary)]
    u, v = primary
    for other in sorted_links[1:]:
        if len(bundle) >= bundle_size:
            break
        # Pick links sharing an endpoint (same conduit / central PoP)
        if other[0] in (u, v) or other[1] in (u, v):
            bundle.append(_canon(*other))
    return bundle


def select_failure(state: NetworkState, cfg: Dict, seed: int = 42) -> Failure:
    """Choose the initiating failure from config or by strategy."""
    fcfg = cfg["failure"]
    kind = fcfg.get("type", "link")
    target = fcfg.get("target")

    if target is not None:
        if kind == "link" and isinstance(target, (list, tuple)) and len(target) == 2 and isinstance(target[0], str):
            target = _canon(target[0], target[1])
        elif kind in ("correlated", "srlg") and isinstance(target, list):
            target = [_canon(e[0], e[1]) for e in target]
        return Failure(kind=kind, target=target)

    g = state.graph
    if kind == "node":
        return Failure(kind="node", target=_highest_betweenness_node(g))
    elif kind in ("correlated", "srlg"):
        bundle_size = int(fcfg.get("bundle_size", 2))
        return Failure(kind=kind, target=_correlated_bundle_links(g, bundle_size))
    return Failure(kind="link", target=_highest_betweenness_link(g))


def apply_failure(state: NetworkState, failure: Failure) -> List[EdgeKey]:
    """Remove the failed element(s) from ``state`` (mutates in place).

    Returns the list of links that were taken down.
    - Node failure: removes the node and all its incident links.
    - Link failure: removes the single targeted link.
    - Correlated/SRLG failure: removes multiple co-dependent or conduit links.
    The load that was on those links is staged in ``_pending_load`` for
    endogenous cascade redistribution.
    """
    g = state.graph
    downed: List[EdgeKey] = []

    if failure.kind == "node":
        node = str(failure.target)
        if node in g:
            downed = [_canon(node, nb) for nb in list(g.neighbors(node))]
            g.remove_node(node)
    elif failure.kind in ("correlated", "srlg"):
        targets = failure.target if isinstance(failure.target, list) else [failure.target]
        for e in targets:
            if isinstance(e, (list, tuple)) and len(e) == 2:
                e_can = _canon(e[0], e[1])
                if g.has_edge(*e_can):
                    g.remove_edge(*e_can)
                    downed.append(e_can)
    else:  # link
        e = failure.target
        if isinstance(e, (list, tuple)) and len(e) == 2:
            e_can = _canon(e[0], e[1])
            if g.has_edge(*e_can):
                g.remove_edge(*e_can)
                downed = [e_can]

    if not hasattr(state, "_pending_load") or state._pending_load is None:
        state._pending_load = {}  # type: ignore[attr-defined]

    for e in downed:
        load = state.link_load.pop(e, 0.0)
        state._pending_load[e] = load  # type: ignore[attr-defined]
        state.link_capacity.pop(e, None)
    return downed

