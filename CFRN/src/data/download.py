"""Dataset acquisition helper (Phase 1 - Environment & datasets).

This does NOT auto-download the multi-gigabyte raw releases (they require
agreement to each provider's terms). It prints the official sources, the
expected on-disk layout, and how to point the config at local copies. The
pipeline runs with synthetic fallbacks until real files are supplied.
"""
from __future__ import annotations

import os
import textwrap

CESNET_URL = "https://cesnet.github.io/cesnet-tszoo/cesnet_timeseries24/"
CAIDA_URL = (
    "https://www.caida.org/catalog/datasets/"
    "internet-topology-data-kit/release-2026-03/"
)


def print_instructions(root: str) -> None:
    raw = os.path.join(root, "data", "raw")
    msg = f"""
    ======================================================================
    CFRN datasets - Phase 1 (Environment & datasets)
    ======================================================================
    The raw releases are large and license-gated; download them manually and
    keep the raw files UNCHANGED (plan Section 9, First Milestone).

    1) CESNET-TimeSeries24 (traffic, 2023-2024)
       {CESNET_URL}
       -> place the exported time-series CSV/parquet under:
          {os.path.join(raw, 'cesnet')}/
       -> set data.cesnet.traffic_file in config to that file.

    2) CAIDA ITDK-2026-03 (Internet topology)
       {CAIDA_URL}
       -> place the *.nodes / *.links files under:
          {os.path.join(raw, 'caida')}/
       -> set data.caida.links_file in config to the *.links file.

    Record the exact release/version and download date in README.md.

    Until real files are configured, CFRN uses reproducible synthetic
    generators that mimic each dataset's schema, so the whole pipeline runs
    end-to-end right now with:
        python -m experiments.run_experiment
    ======================================================================
    """
    print(textwrap.dedent(msg))
    for sub in ("cesnet", "caida"):
        os.makedirs(os.path.join(raw, sub), exist_ok=True)


if __name__ == "__main__":
    _root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    print_instructions(_root)
