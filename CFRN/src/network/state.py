"""Experimental network state (Stage 1 / plan Section 5).

Combines the CAIDA topology graph with CESNET-derived traffic-state features to
produce a working network with per-link ``load`` and ``capacity``. This is the
``experimental network state`` that controlled failures are injected into.

Loads are assigned by routing a set of origin-destination (OD) demands, each
drawn from the CESNET traffic feature distribution, over shortest paths. Link
capacity is set to a headroom multiple of its baseline load so that an
undisturbed network is below the overload threshold but a cascade can push
links over it.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import networkx as nx
import numpy as np
import pandas as pd


EdgeKey = Tuple[str, str]


def _canon(u: str, v: str) -> EdgeKey:
    """Canonical undirected edge key."""
    return (u, v) if u <= v else (v, u)


@dataclass
class NetworkState:
    """A routed network with per-link load and capacity."""

    graph: nx.Graph
    link_load: Dict[EdgeKey, float] = field(default_factory=dict)
    link_capacity: Dict[EdgeKey, float] = field(default_factory=dict)
    demands: List[Tuple[str, str, float]] = field(default_factory=list)
    overload_threshold: float = 1.0

    # ---- construction ----------------------------------------------------
    def copy(self) -> "NetworkState":
        """Deep-ish copy used to build counterfactual worlds."""
        return NetworkState(
            graph=self.graph.copy(),
            link_load=dict(self.link_load),
            link_capacity=dict(self.link_capacity),
            demands=list(self.demands),
            overload_threshold=self.overload_threshold,
        )

    # ---- queries ----------------------------------------------------------
    def utilisation(self) -> Dict[EdgeKey, float]:
        out = {}
        for e, cap in self.link_capacity.items():
            load = self.link_load.get(e, 0.0)
            out[e] = load / cap if cap > 0 else float("inf")
        return out

    def overloaded_links(self) -> List[EdgeKey]:
        return [
            e for e, u in self.utilisation().items() if u > self.overload_threshold
        ]

    def active_edges(self) -> List[EdgeKey]:
        return [_canon(u, v) for u, v in self.graph.edges()]

    def total_served_load(self) -> float:
        return float(sum(self.link_load.values()))


def build_network_state(
    graph: nx.Graph,
    traffic_feats: pd.DataFrame,
    cfg: Dict,
    seed: int = 42,
) -> NetworkState:
    """Assign CESNET-derived demands onto the CAIDA graph via shortest paths."""
    rng = np.random.default_rng(seed)
    nodes = list(graph.nodes())
    n = len(nodes)

    # Number of OD demands scales with network size; magnitudes sampled from
    # the empirical CESNET per-series mean distribution.
    n_demands = max(2 * n, 50)
    magnitudes = traffic_feats["mean"].to_numpy(dtype=float)
    magnitudes = magnitudes[magnitudes > 0]
    if magnitudes.size == 0:
        magnitudes = np.array([1.0])

    link_load: Dict[EdgeKey, float] = {_canon(u, v): 0.0 for u, v in graph.edges()}
    demands: List[Tuple[str, str, float]] = []

    for _ in range(n_demands):
        src, dst = rng.choice(n, size=2, replace=False)
        s, d = nodes[src], nodes[dst]
        demand = float(rng.choice(magnitudes))
        try:
            path = nx.shortest_path(graph, s, d)
        except nx.NetworkXNoPath:
            continue
        demands.append((s, d, demand))
        for u, v in zip(path[:-1], path[1:]):
            link_load[_canon(u, v)] += demand

    # Capacity model.
    #
    # A per-link ``headroom * own_load`` rule makes every link sit at the same
    # utilisation (1/headroom), so a single failure almost never cascades --
    # which is unrealistic. Real backbones cascade because load concentrates on
    # a few high-centrality links while capacity is provisioned in coarse,
    # shared tiers. We therefore set capacity from a *network reference* load
    # (a high percentile) rather than each link's own load. Heavily loaded
    # links then run close to capacity and are genuinely vulnerable, while
    # lightly loaded links keep headroom.
    headroom = float(cfg["network"]["capacity_headroom"])
    nonzero = [l for l in link_load.values() if l > 0]
    ref = float(np.percentile(nonzero, 75)) if nonzero else 1.0
    floor = ref * 0.05
    link_capacity = {}
    for e, load in link_load.items():
        # Capacity is the reference provisioning tier scaled by headroom, but
        # never below a small multiple of the link's own load (so no link is
        # born already failed). Busy links (load > ref) run hot.
        cap = max(ref * headroom, load * 1.05, floor)
        link_capacity[e] = cap

    return NetworkState(
        graph=graph,
        link_load=link_load,
        link_capacity=link_capacity,
        demands=demands,
        overload_threshold=float(cfg["network"]["overload_threshold"]),
    )
