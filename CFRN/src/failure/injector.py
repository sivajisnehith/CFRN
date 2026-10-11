"""Controlled failure injection into the network state.

Supports node and link failures. The initiating failure F_t can be chosen
explicitly or by a strategy (highest betweenness element), which produces the
most interesting cascades for evaluation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple, Union

import networkx as nx

from ..network.state import EdgeKey, NetworkState, _canon

FailureTarget = Union[str, EdgeKey]


@dataclass
class Failure:
    """A single initiating failure event F_t."""

    kind: str                      # "node" or "link"
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


def select_failure(state: NetworkState, cfg: Dict, seed: int = 42) -> Failure:
    """Choose the initiating failure from config or by strategy."""
    fcfg = cfg["failure"]
    kind = fcfg["type"]
    target = fcfg.get("target")

    if target is not None:
        if kind == "link" and isinstance(target, (list, tuple)):
            target = _canon(target[0], target[1])
        return Failure(kind=kind, target=target)

    g = state.graph
    if kind == "node":
        return Failure(kind="node", target=_highest_betweenness_node(g))
    return Failure(kind="link", target=_highest_betweenness_link(g))


def apply_failure(state: NetworkState, failure: Failure) -> List[EdgeKey]:
    """Remove the failed element from ``state`` (mutates in place).

    Returns the list of links that were taken down (for a node failure this is
    all its incident links). The load that was on those links is returned to
    the propagation model for redistribution.
    """
    g = state.graph
    downed: List[EdgeKey] = []

    if failure.kind == "node":
        node = failure.target
        if node in g:
            downed = [_canon(node, nb) for nb in list(g.neighbors(node))]
            g.remove_node(node)
    else:  # link
        e = failure.target
        if g.has_edge(*e):
            g.remove_edge(*e)
            downed = [e]

    for e in downed:
        state.link_load.pop(e, None)
        state.link_capacity.pop(e, None)
    return downed
