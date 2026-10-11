"""Configuration loading and global seeding utilities."""
from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Any, Dict

import numpy as np
import yaml

_DEFAULT_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "default.yaml",
)


def load_config(path: str | None = None) -> Dict[str, Any]:
    """Load a YAML config file, falling back to the packaged default."""
    path = path or _DEFAULT_CONFIG_PATH
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    return cfg


def seed_everything(seed: int) -> None:
    """Seed Python and NumPy RNGs for reproducibility."""
    random.seed(seed)
    np.random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)


@dataclass
class Paths:
    """Resolve and create standard project directories."""

    root: str
    data_raw: str = field(init=False)
    data_processed: str = field(init=False)
    models: str = field(init=False)
    results: str = field(init=False)

    def __post_init__(self) -> None:
        self.data_raw = os.path.join(self.root, "data", "raw")
        self.data_processed = os.path.join(self.root, "data", "processed")
        self.models = os.path.join(self.root, "models")
        self.results = os.path.join(self.root, "results")

    def ensure(self) -> "Paths":
        for p in (self.data_raw, self.data_processed, self.models, self.results):
            os.makedirs(p, exist_ok=True)
        return self
