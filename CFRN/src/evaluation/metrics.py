"""Evaluation metrics (plan Section 10).

Metrics computed per scenario from a CascadeResult + network state:
    propagation_debt (C-CPD), legacy_cpd, direct_damage, secondary_debt,
    higher_order_debt, cascade_size, cascade_depth, traffic_overload,
    connectivity_loss, conventional_resilience, recovery_time.
"""
from __future__ import annotations

from typing import Dict

import networkx as nx

from ..network.state import NetworkState
from ..propagation.cascade import CascadeResult


def connectivity_loss(state: NetworkState) -> float:
    """Fraction of nodes severed from the giant connected component."""
    g = state.graph
    n = g.number_of_nodes()
    if n == 0:
        return 1.0

    largest = max((len(c) for c in nx.connected_components(g)), default=0)
    return (n - largest) / n


def traffic_overload(state: NetworkState) -> float:
    """Fraction of active surviving links operating above the overload threshold."""
    edges = state.active_edges()
    if not edges:
        return 1.0
    return len(state.overloaded_links()) / len(edges)


def static_connectivity_resilience(state: NetworkState) -> float:
    """Conventional resilience metric: fraction of nodes in the giant component."""
    return 1.0 - connectivity_loss(state)


def static_capacity_resilience(state: NetworkState) -> float:
    """Conventional metric: total active capacity remaining."""
    caps = state.link_capacity.values()
    return float(sum(caps)) if caps else 0.0


def conventional_resilience_index(state: NetworkState) -> float:
    """Standard weighted static resilience index R_conv.

    Combines static connectivity retention (giant component fraction) and
    instantaneous non-overloaded link fraction at the post-failure evaluation state.
    """
    conn = static_connectivity_resilience(state)
    overload_ratio = traffic_overload(state)
    # Higher is more resilient in conventional benchmarks.
    return 0.6 * conn + 0.4 * (1.0 - overload_ratio)


def recovery_time(result: CascadeResult) -> int:
    """Steps until the cascade stops producing new failures (plan: steps to
    return to an acceptable state)."""
    for cs in result.steps:
        if not cs.newly_failed:
            return cs.step
    return len(result.steps)


def scenario_metrics(result: CascadeResult, post_state: NetworkState) -> Dict[str, float]:
    """Compute comprehensive performance and resilience metrics for a scenario."""
    return {
        "propagation_debt": result.causal_cpd,
        "causal_cpd": result.causal_cpd,
        "legacy_cpd": result.legacy_cpd,
        "direct_damage": result.direct_damage,
        "secondary_debt": result.secondary_debt,
        "higher_order_debt": result.higher_order_debt,
        "cascade_size": float(result.cascade_size),
        "cascade_depth": float(result.cascade_depth),
        "traffic_overload": traffic_overload(post_state),
        "connectivity_loss": connectivity_loss(post_state),
        "conventional_resilience": conventional_resilience_index(post_state),
        "recovery_time": float(recovery_time(result)),
    }
