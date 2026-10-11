"""Learning & self-evolution (Stage 8).

Two responsibilities from the architecture diagram:
  1. Observe post-reconfiguration propagation, compute prediction error, and
     update a surrogate model that predicts CPD from scenario features. Over
     scenarios this model becomes a fast predictor the loop can use to screen
     candidates before running full cascade simulations.
  2. Store successful mutations as evolutionary knowledge (a memory keyed by
     scenario signature -> best mutation kind), so recurring situations resolve
     with the historically best action.
"""
from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

try:  # scikit-learn is optional at import time; required to train.
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error
    import joblib
    _SKLEARN = True
except Exception:  # pragma: no cover
    _SKLEARN = False


@dataclass
class EvolutionaryKnowledge:
    """Self-evolution memory and surrogate CPD model."""

    model: Optional[object] = None
    X: List[List[float]] = field(default_factory=list)
    y: List[float] = field(default_factory=list)
    # scenario signature -> {mutation_kind: [ces,...]}
    best_actions: Dict[str, Dict[str, List[float]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )
    prediction_errors: List[float] = field(default_factory=list)

    # ---- surrogate model -------------------------------------------------
    def observe(self, features: List[float], observed_cpd: float) -> Optional[float]:
        """Record a (features -> observed CPD) sample. Returns the prediction
        error if a model already exists, else None."""
        err = None
        if self.model is not None:
            pred = float(self.model.predict([features])[0])
            err = abs(pred - observed_cpd)
            self.prediction_errors.append(err)
        self.X.append(list(features))
        self.y.append(float(observed_cpd))
        return err

    def maybe_train(self, min_samples: int) -> bool:
        """Train/refresh the surrogate once enough samples are gathered."""
        if not _SKLEARN or len(self.y) < min_samples:
            return False
        model = GradientBoostingRegressor(random_state=0)
        model.fit(np.array(self.X), np.array(self.y))
        self.model = model
        return True

    def predict_cpd(self, features: List[float]) -> Optional[float]:
        if self.model is None:
            return None
        return float(self.model.predict([features])[0])

    # ---- evolutionary action memory -------------------------------------
    def record_success(self, signature: str, mutation_kind: str, ces: float) -> None:
        self.best_actions[signature][mutation_kind].append(ces)

    def recommend_action(self, signature: str) -> Optional[str]:
        """Return the historically best mutation kind for a scenario signature."""
        memory = self.best_actions.get(signature)
        if not memory:
            return None
        return max(memory, key=lambda k: float(np.mean(memory[k])))

    # ---- persistence -----------------------------------------------------
    def save(self, path: str) -> None:
        if not _SKLEARN or self.model is None:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        joblib.dump(
            {"model": self.model, "n_samples": len(self.y)}, path
        )

    def summary(self) -> Dict[str, float]:
        mae = (
            float(np.mean(self.prediction_errors))
            if self.prediction_errors
            else float("nan")
        )
        return {
            "n_samples": len(self.y),
            "model_trained": self.model is not None,
            "mean_prediction_error": mae,
            "n_signatures_learned": len(self.best_actions),
        }
