"""Counterfactual Propagation-Evolution Score optimisation (Stages 5-6).

For each candidate mutation m_i we build the counterfactual world, simulate the
same cascade, and compute:

    dCPD(m_i) = CPD(G_t) - CPD(m_i)            # propagation-debt reduction
    A(m_i)                                     # future adaptability gain
    MC(m_i) = alpha*L + beta*S + chi*E         # mutation / evolutionary cost
    CES(m_i) = (dCPD(m_i) + eta*A(m_i)) / (MC(m_i) + epsilon)

The optimal mutation is  m* = argmax_i CES(m_i).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import networkx as nx

from ..counterfactual.mutations import Mutation, apply_mutation
from ..network.state import EdgeKey, NetworkState
from ..propagation.cascade import CascadeResult, simulate_cascade


@dataclass
class Evaluation:
    """CES evaluation record for one candidate mutation."""

    mutation: Mutation
    cpd: float
    delta_cpd: float
    adaptability: float
    mutation_cost: float
    ces: float
    cascade: CascadeResult


def _adaptability(state_after: NetworkState) -> float:
    """Future adaptability gain A(m): how much routing slack / redundancy the
    mutated network retains. Uses algebraic connectivity (Fiedler value) of the
    largest component, which measures robustness to further partition."""
    g = state_after.graph
    if g.number_of_nodes() < 2 or g.number_of_edges() == 0:
        return 0.0
    comps = list(nx.connected_components(g))
    largest = max(comps, key=len)
    sub = g.subgraph(largest)
    if sub.number_of_nodes() < 2:
        return 0.0
    try:
        return float(nx.algebraic_connectivity(sub, seed=0))
    except Exception:
        # Fallback: average degree normalised.
        return float(2 * sub.number_of_edges() / sub.number_of_nodes())


def mutation_cost(m: Mutation, cfg: Dict) -> float:
    o = cfg["optimization"]
    return (
        o["alpha"] * m.n_link_changes
        + o["beta"] * m.n_service_changes
        + o["chi"] * m.effort
    )


def evaluate_mutation(
    base_state: NetworkState,
    initial_downed: List[EdgeKey],
    m: Mutation,
    baseline_cpd: float,
    cfg: Dict,
    seed: int = 42,
) -> Evaluation:
    """Build G_t^CF, simulate the cascade and score the mutation."""
    cf_state = apply_mutation(base_state, m)
    cascade = simulate_cascade(cf_state, list(initial_downed), cfg, seed=seed)
    delta = baseline_cpd - cascade.cpd
    adapt = _adaptability(cf_state)
    mc = mutation_cost(m, cfg)
    eps = float(cfg["optimization"]["epsilon"])
    eta = float(cfg["optimization"]["eta"])
    ces = (delta + eta * adapt) / (mc + eps)
    return Evaluation(
        mutation=m,
        cpd=cascade.cpd,
        delta_cpd=delta,
        adaptability=adapt,
        mutation_cost=mc,
        ces=ces,
        cascade=cascade,
    )


def optimize(
    base_state: NetworkState,
    initial_downed: List[EdgeKey],
    mutations: List[Mutation],
    baseline_cpd: float,
    cfg: Dict,
    seed: int = 42,
) -> List[Evaluation]:
    """Evaluate all candidates and return them sorted by CES descending."""
    evals = [
        evaluate_mutation(base_state, initial_downed, m, baseline_cpd, cfg, seed=seed)
        for m in mutations
    ]
    evals.sort(key=lambda e: e.ces, reverse=True)
    return evals


def select_best(evals: List[Evaluation]) -> Optional[Evaluation]:
    """m* = argmax CES."""
    return evals[0] if evals else None
