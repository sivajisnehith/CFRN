"""CAIDA ITDK topology loading (Phase 2 - Topology processing).

Builds a NetworkX graph from a CAIDA ITDK release when the official
``*.nodes`` / ``*.links`` files are provided. When they are not available a
scale-free (Barabasi-Albert) synthetic graph is generated that mimics the
degree distribution of Internet router-level topology, so the pipeline is
fully reproducible without the multi-gigabyte raw download.

CAIDA ITDK reference:
https://www.caida.org/catalog/datasets/internet-topology-data-kit/release-2026-03/
"""
from __future__ import annotations

import os
from typing import Dict, Optional

import networkx as nx
import numpy as np


def _parse_itdk_links(links_file: str) -> nx.Graph:
    """Parse a CAIDA ITDK ``.links`` file into an undirected graph.

    ITDK link lines look like::

        link L:<id>:  N<node1> N<node2> [N<node3> ...]

    Each line lists the node ids attached to a shared medium; we add an edge
    between the first node and every other node on that medium.
    """
    g = nx.Graph()
    with open(links_file, "r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if not line.startswith("link "):
                continue
            # tokens after the ':' separator are the node references
            try:
                _, rest = line.split(":", 1)
            except ValueError:
                continue
            nodes = [tok for tok in rest.split() if tok.startswith("N")]
            nodes = [n.split(":")[0] for n in nodes]  # strip interface addr
            if len(nodes) < 2:
                continue
            hub = nodes[0]
            for other in nodes[1:]:
                if hub != other:
                    g.add_edge(hub, other)
    return g


def load_topology(cfg: Dict, seed: int = 42) -> nx.Graph:
    """Return a connected, undirected topology graph.

    Node attributes added:
        - ``betweenness``: betweenness centrality (routing importance)
        - ``degree``:      node degree
    """
    data_cfg = cfg["data"]["caida"]
    links_file: Optional[str] = data_cfg.get("links_file")

    if links_file and os.path.exists(links_file):
        g = _parse_itdk_links(links_file)
        source = f"CAIDA ITDK ({os.path.basename(links_file)})"
    else:
        syn = data_cfg["synthetic"]
        g = nx.barabasi_albert_graph(
            n=int(syn["n_nodes"]), m=int(syn["attach_m"]), seed=seed
        )
        g = nx.relabel_nodes(g, {i: f"N{i}" for i in g.nodes()})
        source = "synthetic Barabasi-Albert (CAIDA fallback)"

    # Keep the largest connected component so routing is always well-defined.
    if g.number_of_nodes() == 0:
        raise ValueError("Loaded topology graph is empty.")
    if not nx.is_connected(g):
        largest = max(nx.connected_components(g), key=len)
        g = g.subgraph(largest).copy()

    bc = nx.betweenness_centrality(g, seed=seed)
    nx.set_node_attributes(g, bc, "betweenness")
    nx.set_node_attributes(g, dict(g.degree()), "degree")
    g.graph["source"] = source
    return g


def topology_features(g: nx.Graph) -> Dict[str, float]:
    """Summary topology features (Section 6 - topology feature extraction)."""
    deg = np.array([d for _, d in g.degree()], dtype=float)
    return {
        "n_nodes": g.number_of_nodes(),
        "n_links": g.number_of_edges(),
        "avg_degree": float(deg.mean()),
        "max_degree": float(deg.max()),
        "density": nx.density(g),
        "avg_clustering": nx.average_clustering(g),
    }
