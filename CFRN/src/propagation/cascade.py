"""Multi-step failure-propagation and propagation debt (Stages 2, 4, 5).

Model
-----
When a link fails, its load is redistributed onto the best alternate path
between its endpoints. Redistribution can overload other links; an overloaded
link fails (probabilistically) at the next step, propagating the cascade. This
repeats for H steps.

Failure-propagation debt at a single step is the modeled severity of the
network state:

    D_FP = w_over * overloaded_fraction
         + w_disc * disconnected_fraction
         + w_lost * unserved_load_fraction

Counterfactual propagation debt accumulates discounted step debt over the
horizon (plan Section 4, Stage 4):

    CPD = sum_{tau=1..H} gamma^{tau-1} * D_FP(G_{t+tau})
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

import networkx as nx
import numpy as np

from ..network.state import EdgeKey, NetworkState, _canon

# Debt component weights.
_W_OVER = 0.4
_W_DISC = 0.4
_W_LOST = 0.2


@dataclass
class CascadeStep:
    step: int
    newly_failed: List[EdgeKey]
    overloaded: List[EdgeKey]
    d_fp: float


@dataclass
class CascadeResult:
    steps: List[CascadeStep] = field(default_factory=list)
    cpd: float = 0.0
    cascade_size: int = 0        # total links failed across the cascade
    cascade_depth: int = 0       # number of steps with new failures
    disconnected_nodes: int = 0
    unserved_load: float = 0.0

    def step_debts(self) -> List[float]:
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
    """Fraction of original demand that can no longer be routed."""
    g = state.graph
    total = sum(d for _, _, d in state.demands) or 1.0
    served = 0.0
    for s, d, demand in state.demands:
        if s in g and d in g and nx.has_path(g, s, d):
            served += demand
    unserved = total - served
    return unserved / total, unserved


def _congestion_pressure(state: NetworkState) -> float:
    """Smooth, continuous congestion severity in [0, inf).

    Each link contributes ``max(0, util - knee)`` where ``knee`` is a soft
    onset below the hard overload threshold. This makes debt reliably positive
    and differentiable w.r.t. load, so mutations that shave peak utilisation
    are rewarded even before any link strictly exceeds 1.0. Links at or beyond
    the overload threshold contribute super-linearly.
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
        # Super-linear penalty once past the hard threshold.
        if u > state.overload_threshold:
            excess += 2.0 * (u - state.overload_threshold)
        total += excess
    return total / len(util)


def _step_debt(state: NetworkState) -> Tuple[float, List[EdgeKey]]:
    edges = state.active_edges()
    congestion = _congestion_pressure(state)
    disc_frac, _ = _disconnected_fraction(state)
    lost_frac, _ = _unserved_load(state)
    d_fp = _W_OVER * congestion + _W_DISC * disc_frac + _W_LOST * lost_frac
    return d_fp, state.overloaded_links()


def _redistribute(state: NetworkState, downed: List[EdgeKey], frac: float) -> None:
    """Push a fraction of each downed link's load onto an alternate path."""
    g = state.graph
    for (u, v) in downed:
        load = frac * state._pending_load.get((u, v), 0.0)
        if load <= 0 or u not in g or v not in g:
            continue
        try:
            path = nx.shortest_path(g, u, v)
        except nx.NetworkXNoPath:
            continue  # load is lost -> shows up as unserved demand
        for a, b in zip(path[:-1], path[1:]):
            e = _canon(a, b)
            if e in state.link_load:
                state.link_load[e] += load


def simulate_cascade(
    state: NetworkState,
    initial_downed: List[EdgeKey],
    cfg: Dict,
    seed: int = 42,
) -> CascadeResult:
    """Simulate an H-step cascade and compute CPD.

    ``state`` is mutated to the post-cascade network. Pass a copy if the
    original is needed afterwards.
    """
    rng = np.random.default_rng(seed)
    pcfg = cfg["propagation"]
    horizon = int(pcfg["horizon"])
    gamma = float(pcfg["gamma"])
    redistribution = float(pcfg["redistribution"])
    fail_prob = float(pcfg["overload_fail_prob"])

    # Record pre-failure loads so redistribution knows how much to move.
    state._pending_load = dict(state.link_load)  # type: ignore[attr-defined]

    result = CascadeResult()
    downed = list(initial_downed)
    cpd = 0.0
    total_failed = len(downed)
    depth = 0

    for step in range(1, horizon + 1):
        # Redistribute the load from the links that failed at the previous step.
        _redistribute(state, downed, redistribution)

        d_fp, overloaded = _step_debt(state)
        cpd += (gamma ** (step - 1)) * d_fp

        # Decide which overloaded links fail next (probabilistic).
        newly_failed: List[EdgeKey] = []
        for e in overloaded:
            if rng.random() < fail_prob and state.graph.has_edge(*e):
                newly_failed.append(e)

        result.steps.append(
            CascadeStep(step=step, newly_failed=list(newly_failed),
                        overloaded=list(overloaded), d_fp=d_fp)
        )

        if not newly_failed:
            downed = []
            continue

        depth += 1
        total_failed += len(newly_failed)
        state._pending_load = {}  # type: ignore[attr-defined]
        for e in newly_failed:
            state._pending_load[e] = state.link_load.get(e, 0.0)  # type: ignore[attr-defined]
            if state.graph.has_edge(*e):
                state.graph.remove_edge(*e)
            state.link_load.pop(e, None)
            state.link_capacity.pop(e, None)
        downed = newly_failed

    _, disc_nodes = _disconnected_fraction(state)
    _, unserved = _unserved_load(state)
    result.cpd = cpd
    result.cascade_size = total_failed
    result.cascade_depth = depth
    result.disconnected_nodes = disc_nodes
    result.unserved_load = unserved
    return result


def propagation_graph(
    state: NetworkState, result: CascadeResult
) -> nx.DiGraph:
    """Build the failure-propagation graph PG_t (Stage 2).

    Directed edges link a failed element at step k to elements that failed at
    step k+1, representing the propagation path.
    """
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
