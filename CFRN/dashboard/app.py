"""CFRN Streamlit dashboard (Phase 11).

Visualises failures, cascades, propagation debt, candidate mutations and the
selected action m*, plus the aggregate baseline-vs-CFRN comparison.

Run:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import os
import sys

import networkx as nx
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from src.config import load_config, seed_everything  # noqa: E402
from src.counterfactual.mutations import apply_mutation  # noqa: E402
from src.failure.injector import apply_failure, select_failure  # noqa: E402
from src.pipeline import CFRNPipeline  # noqa: E402
from src.propagation.cascade import simulate_cascade  # noqa: E402

st.set_page_config(page_title="CFRN Dashboard", layout="wide")


@st.cache_resource
def _load_pipeline(config_path: str, n_nodes: int):
    cfg = load_config(config_path)
    cfg["data"]["caida"]["synthetic"]["n_nodes"] = int(n_nodes)
    seed_everything(int(cfg["seed"]))
    return CFRNPipeline(cfg, seed=int(cfg["seed"])), cfg


def _network_figure(graph: nx.Graph, failed_edges, title: str):
    pos = nx.spring_layout(graph, seed=1)
    failed = {tuple(sorted(e)) for e in failed_edges}

    edge_x, edge_y = [], []
    fedge_x, fedge_y = [], []
    for u, v in graph.edges():
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        if tuple(sorted((u, v))) in failed:
            fedge_x += [x0, x1, None]
            fedge_y += [y0, y1, None]
        else:
            edge_x += [x0, x1, None]
            edge_y += [y0, y1, None]

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=edge_x, y=edge_y, mode="lines",
                             line=dict(width=0.6, color="#9aa5b1"),
                             hoverinfo="none", name="link"))
    if fedge_x:
        fig.add_trace(go.Scatter(x=fedge_x, y=fedge_y, mode="lines",
                                 line=dict(width=2.5, color="#c0392b"),
                                 hoverinfo="none", name="failed link"))
    nx_x = [pos[n][0] for n in graph.nodes()]
    nx_y = [pos[n][1] for n in graph.nodes()]
    deg = [graph.degree(n) for n in graph.nodes()]
    fig.add_trace(go.Scatter(
        x=nx_x, y=nx_y, mode="markers",
        marker=dict(size=[6 + d for d in deg], color=deg, colorscale="Blues",
                    line=dict(width=0.5, color="#2c3e50")),
        text=[f"{n} (deg {graph.degree(n)})" for n in graph.nodes()],
        hoverinfo="text", name="node"))
    fig.update_layout(title=title, showlegend=False,
                      margin=dict(l=10, r=10, t=40, b=10),
                      xaxis=dict(visible=False), yaxis=dict(visible=False),
                      height=460)
    return fig


st.title("CFRN - Counterfactual Failure-Propagation Debt Optimization")
st.caption("Monitor & Detect -> Predict & Evaluate -> Reconfigure & Evolve -> "
           "Learn & Adapt")

with st.sidebar:
    st.header("Configuration")
    config_path = st.text_input("Config path",
                                os.path.join(_ROOT, "config", "default.yaml"))
    n_nodes = st.slider("Synthetic topology size (nodes)", 40, 300, 120, 10)
    scenario_id = st.number_input("Scenario id (seed offset)", 0, 999, 0, 1)
    run = st.button("Run scenario", type="primary")

pipe, cfg = _load_pipeline(config_path, n_nodes)

st.subheader("Environment")
c1, c2, c3, c4 = st.columns(4)
c1.metric("Topology nodes", pipe.topo_feats["n_nodes"])
c2.metric("Topology links", pipe.topo_feats["n_links"])
c3.metric("Avg degree", f"{pipe.topo_feats['avg_degree']:.2f}")
c4.metric("Traffic series", pipe.traffic.shape[0])
st.caption(f"Topology: {pipe.graph.graph.get('source')} | "
           f"Traffic: {pipe.traffic.attrs.get('source')}")

if run:
    base_seed = int(cfg["seed"]) + int(scenario_id)

    state0 = pipe.base_state(base_seed)
    failure = select_failure(state0, cfg, seed=base_seed)

    baseline_state = state0.copy()
    downed = apply_failure(baseline_state, failure)
    baseline_cascade = simulate_cascade(baseline_state, list(downed), cfg,
                                        seed=base_seed)

    result = pipe.run_scenario(int(scenario_id))

    st.subheader("Stage 1 - Detected failure")
    st.info(f"Initiating failure F_t = **{result.failure}** "
            f"(strategy: {cfg['failure']['strategy']})")

    st.subheader("Stages 2-6 - Debt, candidates and selected action")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Baseline CPD", f"{result.baseline_cpd:.4f}")
    m2.metric("CFRN CPD (m*)", f"{result.cfrn_cpd:.4f}",
              delta=f"-{result.delta_cpd:.4f}")
    m3.metric("Selected m*", str(result.best_mutation))
    m4.metric("Candidates evaluated", result.n_candidates)

    st.subheader("Stage 7 - Topology before vs after reconfiguration")
    cfa, cfb = st.columns(2)
    with cfa:
        st.plotly_chart(
            _network_figure(baseline_state.graph, [],
                            "Post-failure (baseline, do nothing)"),
            use_container_width=True)
    with cfb:
        post = state0.copy()
        d2 = apply_failure(post, failure)
        if result.evaluations:
            best = result.evaluations[0]
            cf = apply_mutation(post, best.mutation)
            st.plotly_chart(
                _network_figure(cf.graph, [],
                                f"After m* = {best.mutation.describe()}"),
                use_container_width=True)

    st.subheader("Candidate mutation ranking (by CES)")
    rows = [{
        "mutation": e.mutation.describe(),
        "CES": round(e.ces, 4),
        "dCPD": round(e.delta_cpd, 4),
        "adaptability": round(e.adaptability, 4),
        "cost": round(e.mutation_cost, 4),
    } for e in result.evaluations[:15]]
    st.dataframe(pd.DataFrame(rows), use_container_width=True)

    st.subheader("Baseline cascade propagation (per step)")
    step_df = pd.DataFrame({
        "step": [s.step for s in baseline_cascade.steps],
        "D_FP": [s.d_fp for s in baseline_cascade.steps],
        "newly_failed": [len(s.newly_failed) for s in baseline_cascade.steps],
        "overloaded": [len(s.overloaded) for s in baseline_cascade.steps],
    })
    st.line_chart(step_df.set_index("step")[["D_FP"]])
    st.bar_chart(step_df.set_index("step")[["newly_failed", "overloaded"]])

st.divider()
st.subheader("Aggregate results")
results_csv = os.path.join(_ROOT, cfg["experiment"]["out_dir"], "cfrn_results.csv")
if os.path.exists(results_csv):
    df = pd.read_csv(results_csv)
    a, b, c = st.columns(3)
    base = df["baseline_cpd"].mean()
    cfrn = df["cfrn_cpd"].mean()
    a.metric("Mean baseline CPD", f"{base:.4f}")
    b.metric("Mean CFRN CPD", f"{cfrn:.4f}")
    c.metric("Debt reduction", f"{100*(base-cfrn)/base:.1f}%" if base else "n/a")
    st.line_chart(df.set_index("scenario")[["baseline_cpd", "cfrn_cpd"]])
    st.caption(f"Loaded {len(df)} scenarios from {results_csv}")
else:
    st.warning("No aggregate results yet. Run: "
               "`python -m experiments.run_experiment`")
