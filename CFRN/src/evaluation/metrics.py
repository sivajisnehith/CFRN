"""Evaluation metrics (plan Section 10).

Metrics computed per scenario from a CascadeResult + network state:
    propagation_debt, cascade_size, cascade_depth, traffic_overload,
    connectivity_loss, delta_cpd, mutation_cost, adaptability, ces,
    recovery_time.
"""
from __future__ import annotations

from typing import Dict

from ..network.state import NetworkState
from ..propagation.cascade import CascadeResult


def connectivity_loss(state: NetworkState) -> float:
    g = state.graph
    n = g.number_of_nodes()
    if n == 0:
        return 1.0
    import networkx as nx

    largest = max((len(c) for c in nx.connected_components(g)), default=0)
    return (n - largest) / n


def traffic_overload(state: NetworkState) -> float:
    edges = state.active_edges()
    if not edges:
        return 1.0
    return len(state.overloaded_links()) / len(edges)


def recovery_time(result: CascadeResult) -> int:
    """Steps until the cascade stops producing new failures (plan: steps to
    return to an acceptable state)."""
    for cs in result.steps:
        if not cs.newly_failed:
            return cs.step
    return len(result.steps)


def scenario_metrics(result: CascadeResult, post_state: NetworkState) -> Dict[str, float]:
    return {
        "propagation_debt": result.cpd,
        "cascade_size": result.cascade_size,
        "cascade_depth": result.cascade_depth,
        "traffic_overload": traffic_overload(post_state),
        "connectivity_loss": connectivity_loss(post_state),
        "recovery_time": recovery_time(result),
    }
