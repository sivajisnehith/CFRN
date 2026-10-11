"""CESNET-TimeSeries24 traffic loading (Phase 3 - Traffic processing).

Loads real CESNET-TimeSeries24 traffic time series when a file is provided,
otherwise synthesises time series with realistic diurnal + weekly seasonality
and bursty noise. Produces per-series traffic-state features used to assign
load onto the topology graph.

CESNET reference:
https://cesnet.github.io/cesnet-tszoo/cesnet_timeseries24/

Methodological note (plan Section 11): CESNET and CAIDA are NOT the same
physical network. We use CESNET only to derive realistic traffic-state
*distributions*, then assign controlled loads onto the CAIDA graph.
"""
from __future__ import annotations

import os
from typing import Dict

import numpy as np
import pandas as pd


# Candidate column names found across CESNET-TimeSeries24 exports.
_VALUE_COLUMNS = ("n_bytes", "bytes", "n_packets", "packets", "n_flows", "value")


def _synthesise_traffic(cfg: Dict, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    syn = cfg["data"]["cesnet"]["synthetic"]
    n_series = int(syn["n_series"])
    n_t = int(syn["n_timesteps"])
    res = int(syn["resolution_minutes"])

    t = np.arange(n_t)
    hours = (t * res / 60.0) % 24.0
    days = (t * res / 60.0 / 24.0)
    # Diurnal (24h) + weekly (7d) seasonality, shared shape across series.
    diurnal = 0.5 * (1 + np.sin((hours - 8) / 24.0 * 2 * np.pi))
    weekly = 0.85 + 0.15 * np.sin(days / 7.0 * 2 * np.pi)
    base_shape = diurnal * weekly

    rows = []
    for s in range(n_series):
        scale = rng.lognormal(mean=12.0, sigma=1.0)      # per-series magnitude
        noise = rng.normal(1.0, 0.12, size=n_t).clip(0.3, 2.0)
        burst = (rng.random(n_t) < 0.02) * rng.uniform(1.5, 3.0, size=n_t)
        series = scale * base_shape * noise * (1 + burst)
        rows.append(series)

    mat = np.vstack(rows)
    df = pd.DataFrame(mat, index=[f"S{s}" for s in range(n_series)])
    df.index.name = "series_id"
    df.columns = [f"t{c}" for c in range(n_t)]
    df.attrs["source"] = "synthetic (CESNET fallback)"
    df.attrs["resolution_minutes"] = res
    return df


def _load_real_traffic(path: str) -> pd.DataFrame:
    if path.endswith(".parquet"):
        raw = pd.read_parquet(path)
    else:
        raw = pd.read_csv(path)
    value_col = next((c for c in _VALUE_COLUMNS if c in raw.columns), None)
    if value_col is None:
        raise ValueError(
            f"No known traffic value column in {path}; "
            f"expected one of {_VALUE_COLUMNS}"
        )
    id_col = "id_series" if "id_series" in raw.columns else raw.columns[0]
    time_col = "id_time" if "id_time" in raw.columns else None
    if time_col is not None:
        df = raw.pivot_table(
            index=id_col, columns=time_col, values=value_col, aggfunc="mean"
        )
    else:
        df = raw.set_index(id_col)[[value_col]]
    df = df.fillna(0.0)
    df.index.name = "series_id"
    df.attrs["source"] = f"CESNET-TimeSeries24 ({os.path.basename(path)})"
    return df


def load_traffic(cfg: Dict, seed: int = 42) -> pd.DataFrame:
    """Return a (series x timestep) traffic matrix as a DataFrame."""
    path = cfg["data"]["cesnet"].get("traffic_file")
    if path and os.path.exists(path):
        return _load_real_traffic(path)
    return _synthesise_traffic(cfg, seed)


def traffic_features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-series traffic-state features (plan Section 6 - traffic features).

    Columns: mean, std, peak, p95, variance (coefficient of variation) and the
    burstiness ratio peak/mean.
    """
    values = df.to_numpy(dtype=float)
    mean = values.mean(axis=1)
    std = values.std(axis=1)
    peak = values.max(axis=1)
    p95 = np.percentile(values, 95, axis=1)
    cov = np.divide(std, mean, out=np.zeros_like(std), where=mean > 0)
    burstiness = np.divide(peak, mean, out=np.ones_like(peak), where=mean > 0)
    feats = pd.DataFrame(
        {
            "mean": mean,
            "std": std,
            "peak": peak,
            "p95": p95,
            "cov": cov,
            "burstiness": burstiness,
        },
        index=df.index,
    )
    return feats
