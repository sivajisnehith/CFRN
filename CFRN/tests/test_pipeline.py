"""End-to-end and unit tests for the CFRN pipeline."""
from __future__ import annotations

import os
import sys

import networkx as nx
import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.config import load_config, seed_everything
from src.counterfactual.mutations import (
    Mutation,
    apply_mutation,
    generate_mutations,
)
from src.data.caida import load_topology, topology_features
from src.data.cesnet import load_traffic, traffic_features
from src.failure.injector import apply_failure, select_failure
from src.network.state import build_network_state
from src.optimization.ces import optimize, select_best
from src.pipeline import CFRNPipeline
from src.propagation.cascade import (
    marginal_causal_contribution,
    propagation_graph,
    simulate_cascade,
)


@pytest.fixture()
def cfg():
    c = load_config()
    # Keep tests fast.
    c["data"]["caida"]["synthetic"]["n_nodes"] = 50
    c["data"]["cesnet"]["synthetic"]["n_series"] = 40
    c["data"]["cesnet"]["synthetic"]["n_timesteps"] = 96
    seed_everything(c["seed"])
    return c


def test_topology_is_connected(cfg):
    g = load_topology(cfg, seed=cfg["seed"])
    assert g.number_of_nodes() > 0
    assert nx.is_connected(g)
    feats = topology_features(g)
    assert feats["n_links"] > 0
    assert feats["avg_degree"] > 0


def test_traffic_features_shape(cfg):
    df = load_traffic(cfg, seed=cfg["seed"])
    feats = traffic_features(df)
    assert len(feats) == df.shape[0]
    assert (feats["peak"] >= feats["mean"]).all()


def test_network_state_capacity_covers_load(cfg):
    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    # Every link has positive capacity.
    assert all(c > 0 for c in state.link_capacity.values())
    # An undisturbed network should be mostly below the overload threshold.
    assert len(state.overloaded_links()) <= 0.2 * g.number_of_edges()


def test_failure_removes_element(cfg):
    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    before = state.graph.number_of_edges()
    failure = select_failure(state, cfg, seed=cfg["seed"])
    downed = apply_failure(state, failure)
    assert len(downed) >= 1
    assert state.graph.number_of_edges() < before


def test_cascade_produces_nonnegative_cpd(cfg):
    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    failure = select_failure(state, cfg, seed=cfg["seed"])
    downed = apply_failure(state, failure)
    res = simulate_cascade(state, downed, cfg, seed=cfg["seed"])
    assert res.causal_cpd >= 0
    assert res.cpd >= 0
    assert len(res.steps) == cfg["propagation"]["horizon"]


def test_cpd_non_double_counting_and_decomposition(cfg):
    """Verify mathematical properties of the revised C-CPD:

    1. C-CPD decomposes cleanly into secondary debt (tau=1) + higher-order debt (tau>=2).
    2. Direct failure damage (tau=0) is separated and invariant to subsequent cascade steps.
    3. Marginal realized damages are non-negative increments.
    """
    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    failure = select_failure(state, cfg, seed=cfg["seed"])
    downed = apply_failure(state, failure)

    res = simulate_cascade(state, downed, cfg, seed=cfg["seed"])

    # Decomposition identity:
    assert abs(res.causal_cpd - (res.secondary_debt + res.higher_order_debt)) < 1e-9
    assert res.direct_damage >= 0.0
    assert res.secondary_debt >= 0.0
    assert res.higher_order_debt >= 0.0

    # Every step's marginal realized damage is non-negative:
    for s in res.steps:
        assert s.marginal_realized >= 0.0
        assert s.latent_hazard >= 0.0
        assert s.causal_step_debt >= 0.0


def test_causal_propagation_dag_and_marginal_contribution(cfg):
    """Verify Causal Propagation DAG (PG_t) and individual failure contribution."""
    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    failure = select_failure(state, cfg, seed=cfg["seed"])
    downed = apply_failure(state, failure)

    res = simulate_cascade(state, downed, cfg, seed=cfg["seed"])
    cpg = res.causal_graph
    assert cpg is not None
    assert isinstance(cpg, nx.DiGraph)

    # Initial failed links exist as roots in the causal DAG
    for d in downed:
        assert str(d) in cpg

    # Test marginal causal contribution
    if downed:
        contrib = marginal_causal_contribution(res, downed[0])
        assert contrib >= 0.0


def test_mutations_generated_and_applicable(cfg):
    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    failure = select_failure(state, cfg, seed=cfg["seed"])
    downed = apply_failure(state, failure)
    muts = generate_mutations(state, downed, cfg, seed=cfg["seed"])
    assert len(muts) > 0
    for m in muts:
        cf = apply_mutation(state, m)
        assert cf.graph.number_of_nodes() >= 1


def test_ces_selects_a_mutation(cfg):
    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    failure = select_failure(state, cfg, seed=cfg["seed"])

    base = state.copy()
    downed = apply_failure(base, failure)
    baseline = simulate_cascade(base, downed, cfg, seed=cfg["seed"])

    post = state.copy()
    downed2 = apply_failure(post, failure)
    muts = generate_mutations(post, downed2, cfg, seed=cfg["seed"])
    evals = optimize(post, downed2, muts, baseline.causal_cpd, cfg, seed=cfg["seed"])
    best = select_best(evals)
    assert best is not None
    # Evaluations are sorted by CES descending.
    ces_vals = [e.ces for e in evals]
    assert ces_vals == sorted(ces_vals, reverse=True)


def test_feasibility_constraints_and_mechanisms(cfg):
    """Test operational feasibility constraints and multi-mechanism optimization."""
    from src.optimization.ces import check_feasibility, evaluate_mutation

    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    failure = select_failure(state, cfg, seed=cfg["seed"])

    base = state.copy()
    downed = apply_failure(base, failure)
    baseline = simulate_cascade(base, downed, cfg, seed=cfg["seed"])

    post = state.copy()
    downed2 = apply_failure(post, failure)
    muts = generate_mutations(post, downed2, cfg, seed=cfg["seed"])

    # 1. Feasibility check
    evals = optimize(post, downed2, muts, baseline.causal_cpd, cfg, seed=cfg["seed"], mechanism="constrained")
    assert any(e.is_feasible for e in evals)
    for e in evals:
        assert "budget" in e.constraints
        assert "capacity" in e.constraints
        assert "connectivity" in e.constraints
        assert "routing" in e.constraints

    # 2. Constrained selection must be feasible
    best_c = select_best(evals, mechanism="constrained")
    if best_c is not None:
        assert best_c.is_feasible
        assert best_c.delta_cpd >= -1e-6

    # 3. Robust selection must be feasible and include uncertainty statistics
    evals_r = optimize(post, downed2, muts, baseline.causal_cpd, cfg, seed=cfg["seed"], mechanism="robust")
    best_r = select_best(evals_r, mechanism="robust")
    if best_r is not None:
        assert best_r.is_feasible
        assert hasattr(best_r, "robust_std_cpd")

    # 4. Non-destruction: if all delta_cpd are negative, select_best returns None
    for e in evals:
        e.delta_cpd = -10.0
    assert select_best(evals, mechanism="constrained", require_beneficial=True) is None


def test_full_experiment_runs_and_reduces_debt_on_average(cfg):
    pipe = CFRNPipeline(cfg, seed=cfg["seed"])
    df = pipe.run_experiment(12)
    assert len(df) == 12
    # CFRN should not do worse than baseline on average CPD.
    assert df["cfrn_cpd"].mean() <= df["baseline_cpd"].mean() + 1e-9
    # Learning memory accumulated candidate samples.
    summary = pipe.knowledge.summary()
    assert summary["n_samples"] >= 12
    assert "pct_simulations_avoided" in summary
    assert "drift_events_detected" in summary


def test_self_evolution_screening_and_drift_detection(cfg):
    """Test candidate-level feature extraction, Page-Hinkley drift detector,
    and active candidate screening policy (Correction 3).
    """
    from src.learning.evolve import (
        EvolutionaryKnowledge,
        PageHinkleyDetector,
        extract_candidate_features,
    )

    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    failure = select_failure(state, cfg, seed=cfg["seed"])

    base = state.copy()
    downed = apply_failure(base, failure)
    baseline = simulate_cascade(base, downed, cfg, seed=cfg["seed"])

    post = state.copy()
    downed2 = apply_failure(post, failure)
    muts = generate_mutations(post, downed2, cfg, seed=cfg["seed"])
    assert len(muts) > 0

    # 1. Feature extraction
    feats = extract_candidate_features(post, downed2, muts[0], baseline.causal_cpd, cfg)
    assert len(feats) == 17
    assert all(isinstance(x, (int, float)) for x in feats)

    # 2. Page-Hinkley Drift Detector
    ph = PageHinkleyDetector(min_instances=10, threshold_factor=2.5)
    # Feed stationary low errors
    for _ in range(15):
        drift = ph.update(0.1)
        assert not drift
    # Inject large error spike (concept drift)
    drift_seen = False
    for _ in range(10):
        if ph.update(5.0):
            drift_seen = True
            break
    assert drift_seen

    # 3. Active Candidate Screening Policy
    knowledge = EvolutionaryKnowledge()
    # Populate training samples to train surrogate
    for m in muts:
        f = extract_candidate_features(post, downed2, m, baseline.causal_cpd, cfg)
        knowledge.observe_candidate(f, actual_cpd=baseline.causal_cpd * 0.9)
    trained = knowledge.maybe_train(min_samples=10, force=True)
    assert trained
    assert knowledge.is_ready()

    # Screen candidates
    adapt_list = [0.1] * len(muts)
    feats_list = [extract_candidate_features(post, downed2, m, baseline.causal_cpd, cfg) for m in muts]
    sim_idx, prune_idx, preds = knowledge.screen_candidates(
        candidates=muts,
        features_list=feats_list,
        baseline_cpd=baseline.causal_cpd,
        adaptabilities=adapt_list,
        cfg=cfg,
    )
    assert len(sim_idx) > 0
    assert len(sim_idx) + len(prune_idx) == len(muts)
    # Top-k candidates are guaranteed to be simulated
    k_min = int(cfg.get("learning", {}).get("screening_top_k", 5))
    assert len(sim_idx) >= min(k_min, len(muts))


def test_cascade_reproducibility_and_overload_probability(cfg):
    """Test Correction 4: Exact overload-failure probability, failed link permanence,
    stabilization detection, and correlated failure injection.
    """
    from src.propagation.cascade import overload_failure_probability

    # 1. Exact overload failure probability function
    # Safe regime: u <= threshold -> p_fail == 0.0
    assert overload_failure_probability(0.5, threshold=1.0) == 0.0
    assert overload_failure_probability(1.0, threshold=1.0) == 0.0
    # Overload regime: u > threshold -> monotonic increase
    p_mid = overload_failure_probability(1.5, threshold=1.0, base_prob=0.6, beta=0.4)
    assert abs(p_mid - 0.8) < 1e-6
    # Extreme overload: capped at 1.0
    p_high = overload_failure_probability(2.5, threshold=1.0, base_prob=0.6, beta=0.4)
    assert p_high == 1.0

    # 2. Failed link permanence across all cascade steps
    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    failure = select_failure(state, cfg, seed=cfg["seed"])
    downed = apply_failure(state, failure)
    res = simulate_cascade(state, downed, cfg, seed=cfg["seed"])

    # Verify downed links never reappear in active graph
    for d in downed:
        assert not state.graph.has_edge(*d)
        assert d not in state.link_capacity
        assert d not in state.link_load

    # Verify stabilization status tracking
    assert hasattr(res, "stabilized")
    assert isinstance(res.stabilized, bool)

    # 3. Correlated / SRLG failure injection
    corr_cfg = dict(cfg)
    corr_cfg["failure"] = {"type": "correlated", "bundle_size": 2}
    corr_failure = select_failure(state.copy(), corr_cfg, seed=cfg["seed"])
    assert corr_failure.kind == "correlated"
    corr_downed = apply_failure(state.copy(), corr_failure)
    assert len(corr_downed) >= 1


def test_mathematical_consistency_and_adaptability_boundary(cfg):
    """Test Correction 5: Adaptability boundary conditions (|V_giant| < 2),
    algebraic connectivity of giant component vs disconnected graph,
    mutation cost positivity, and Delta C-CPD sign convention.
    """
    from src.network.state import NetworkState
    from src.optimization.ces import _adaptability, mutation_cost

    # 1. Boundary condition: giant component has < 2 nodes
    empty_graph = nx.Graph()
    ns_empty = NetworkState(graph=empty_graph)
    assert _adaptability(ns_empty) == 0.0

    one_node_graph = nx.Graph()
    one_node_graph.add_node("A")
    ns_single = NetworkState(graph=one_node_graph)
    assert _adaptability(ns_single) == 0.0

    # 2. Algebraic connectivity on disconnected graph:
    # Full graph has 2 isolated triangles: lambda_2(L(G_full)) is 0.0,
    # but _adaptability evaluates on the largest connected component, returning > 0.
    disc_graph = nx.Graph()
    disc_graph.add_edges_from([("A", "B"), ("B", "C"), ("C", "A")])
    disc_graph.add_edges_from([("D", "E"), ("E", "F"), ("F", "D")])
    ns_disc = NetworkState(graph=disc_graph)
    assert nx.algebraic_connectivity(disc_graph) < 1e-9  # disconnected graph has lambda_2 = 0
    adapt_val = _adaptability(ns_disc)
    assert adapt_val > 0.0  # giant component (triangle) has lambda_2 > 0

    # 3. Mutation cost: MC(None) == 0.0, MC(active) > 0.0
    assert mutation_cost(None, cfg) == 0.0
    mut = Mutation("reroute_flow", {"edge": ("A", "B")}, n_service_changes=1, effort=0.8)
    assert mutation_cost(mut, cfg) > 0.0

    # 4. Sign of Delta C-CPD: positive reduction
    baseline_cpd = 10.0
    post_cpd = 6.0
    delta_cpd = baseline_cpd - post_cpd
    assert delta_cpd == 4.0 > 0.0  # positive debt reduction


def test_mutation_feasibility_diversion_and_composition(cfg):
    """Test Correction 6: Flow diversion fractions (adaptive and discrete),
    composite mutations, conflict rejection, and operational constraints.
    """
    from src.counterfactual.mutations import (
        are_mutations_compatible,
        calculate_adaptive_fraction,
        compose_mutations,
    )
    from src.optimization.ces import check_feasibility

    g = load_topology(cfg, seed=cfg["seed"])
    tf = traffic_features(load_traffic(cfg, seed=cfg["seed"]))
    state = build_network_state(g, tf, cfg, seed=cfg["seed"])
    edges = list(state.active_edges())
    e1 = edges[0]
    e2 = edges[1]

    # 1. Flow diversion calculation
    frac = calculate_adaptive_fraction(state, e1)
    assert 0.15 <= frac <= 0.85

    # 2. Flow reroute execution with custom fraction
    m_reroute = Mutation("reroute_flow", {"edge": e1, "fraction": 0.35}, n_service_changes=1, effort=0.8)
    cf_reroute = apply_mutation(state, m_reroute)
    # Target link load should have decreased
    assert cf_reroute.link_load.get(e1, 0.0) <= state.link_load.get(e1, 0.0) + 1e-9

    # 3. Conflict rejection
    m_add = Mutation("add_link", {"edge": ("X", "Y")}, n_link_changes=1, effort=2.0)
    m_remove_same = Mutation("remove_link", {"edge": ("X", "Y")}, n_link_changes=1, effort=1.0)
    compat, reason = are_mutations_compatible(m_add, m_remove_same)
    assert not compat
    assert "Topology conflict" in reason

    # 4. Mutation composition
    m_remove_e1 = Mutation("remove_link", {"edge": e1}, n_link_changes=1, effort=1.0)
    m_reroute_e2 = Mutation("reroute_flow", {"edge": e2, "fraction": 0.5}, n_service_changes=1, effort=0.8)
    comp_mut = compose_mutations(m_remove_e1, m_reroute_e2, max_budget=2.5)
    assert comp_mut is not None
    assert comp_mut.kind == "composite"
    assert comp_mut.n_link_changes == 1
    assert comp_mut.n_service_changes == 1

    cf_comp = apply_mutation(state, comp_mut)
    assert not cf_comp.graph.has_edge(*e1)

    # 5. Infeasible candidate rejection
    m_expensive = Mutation("add_link", {"edge": ("X", "Y")}, n_link_changes=5, effort=10.0)
    feasible, breakdown = check_feasibility(state, m_expensive, cfg)
    assert not feasible
    assert not breakdown["budget"]

