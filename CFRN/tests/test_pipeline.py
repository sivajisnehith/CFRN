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
from src.counterfactual.mutations import apply_mutation, generate_mutations
from src.data.caida import load_topology, topology_features
from src.data.cesnet import load_traffic, traffic_features
from src.failure.injector import apply_failure, select_failure
from src.network.state import build_network_state
from src.optimization.ces import optimize, select_best
from src.pipeline import CFRNPipeline
from src.propagation.cascade import simulate_cascade


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
    assert res.cpd >= 0
    assert len(res.steps) == cfg["propagation"]["horizon"]


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
    evals = optimize(post, downed2, muts, baseline.cpd, cfg, seed=cfg["seed"])
    best = select_best(evals)
    assert best is not None
    # Evaluations are sorted by CES descending.
    ces_vals = [e.ces for e in evals]
    assert ces_vals == sorted(ces_vals, reverse=True)


def test_full_experiment_runs_and_reduces_debt_on_average(cfg):
    pipe = CFRNPipeline(cfg, seed=cfg["seed"])
    df = pipe.run_experiment(12)
    assert len(df) == 12
    # CFRN should not do worse than baseline on average CPD.
    assert df["cfrn_cpd"].mean() <= df["baseline_cpd"].mean() + 1e-9
    # Learning memory accumulated something.
    assert pipe.knowledge.summary()["n_samples"] == 12
