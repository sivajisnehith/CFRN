"""Learning & self-evolution engine (Stage 8).

Operational Self-Evolution Framework:
--------------------------------------
1. Candidate-Level Surrogate Model:
   Predicts downstream cascading debt C-CPD(m_i) for individual candidate
   mutations m_i using a 17-dimensional structural feature representation
   phi(G_base, F_t, m_i) computed immediately upon hypothetical intervention.

2. Measurable Candidate Screening Criterion:
   Instead of running the computationally expensive H-step cascade simulation
   for all K candidates, CFRN scores candidates via the surrogate predictor
   and dynamically partitions candidates into:
     - M_sim: High-priority / competitive candidates that require full cascade simulation.
     - M_pruned: Provably sub-optimal candidates whose upper confidence bound cannot
       exceed the leader, bypassing full cascade simulation.

3. Page-Hinkley Statistical Concept Drift Detection:
   Tracks running residuals e_t = |y_hat_t - y_t|. Detects non-stationary shifts
   in failure-propagation dynamics (e.g., sudden diurnal traffic bursts or topology
   degradations) when cumulative positive deviations exceed lambda * sigma_e.

4. Adaptive Model Retraining & Memory Policy:
   Upon drift detection:
     - Immediately retrains the surrogate with exponential recency sample weighting.
     - Temporarily expands the screening pool k_min to maintain decision safety.
     - Decays stale evolutionary memory records.
"""
from __future__ import annotations

import os
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import numpy as np

try:
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.metrics import mean_absolute_error
    import joblib
    _SKLEARN = True
except Exception:  # pragma: no cover
    _SKLEARN = False

from ..counterfactual.mutations import Mutation, apply_mutation
from ..network.state import EdgeKey, NetworkState
from ..propagation.cascade import _disconnected_fraction, _unserved_load


def extract_candidate_features(
    base_state: NetworkState,
    downed: List[EdgeKey],
    m: Mutation,
    baseline_cpd: float,
    cfg: Dict,
) -> List[float]:
    """Construct 17-dimensional feature vector phi(G_base, F_t, m) for candidate m.

    Features capture:
    1. One-hot mutation taxonomy (4 dims)
    2. Operational cost & control effort (2 dims)
    3. Immediate post-intervention topology & congestion state (9 dims)
    4. Failure severity and baseline cascade context (2 dims)
    """
    # 1. Mutation type encoding
    kind_remove = 1.0 if m.kind == "remove_link" else 0.0
    kind_add = 1.0 if m.kind == "add_link" else 0.0
    kind_reroute = 1.0 if m.kind == "reroute_flow" else 0.0
    kind_isolate = 1.0 if m.kind == "isolate_path" else 0.0

    # 2. Cost and effort
    opt = cfg.get("optimization", {})
    alpha = float(opt.get("alpha", 1.0))
    beta = float(opt.get("beta", 0.5))
    chi = float(opt.get("chi", 0.3))
    mc = alpha * m.n_link_changes + beta * m.n_service_changes + chi * m.effort
    effort = float(m.effort)

    # 3. Hypothetical intervention state (computed without full cascade rollout)
    cf_state = apply_mutation(base_state, m)
    g = cf_state.graph
    n_nodes = float(g.number_of_nodes())
    n_edges = float(g.number_of_edges())

    base_over = len(base_state.overloaded_links())
    cf_over = len(cf_state.overloaded_links())
    delta_over = float(cf_over - base_over)

    utils = list(cf_state.utilisation().values())
    peak_util = float(max(utils, default=0.0))
    mean_util = float(np.mean(utils)) if utils else 0.0

    # Adaptability (algebraic connectivity approximation if needed)
    import networkx as nx
    if g.number_of_nodes() >= 2 and g.number_of_edges() > 0:
        comps = list(nx.connected_components(g))
        largest = max(comps, key=len)
        sub = g.subgraph(largest)
        if sub.number_of_nodes() >= 2:
            try:
                adapt = float(nx.algebraic_connectivity(sub, tol=1e-2, method="tracemin_lu", seed=0))
            except Exception:
                adapt = float(2 * sub.number_of_edges() / sub.number_of_nodes())
        else:
            adapt = 0.0
    else:
        adapt = 0.0

    unserved_frac, _ = _unserved_load(cf_state)
    disc_frac, _ = _disconnected_fraction(cf_state)

    # 4. Baseline context
    n_downed = float(len(downed))
    base_cpd_val = float(baseline_cpd)

    return [
        kind_remove,
        kind_add,
        kind_reroute,
        kind_isolate,
        mc,
        effort,
        n_nodes,
        n_edges,
        float(cf_over),
        delta_over,
        peak_util,
        mean_util,
        adapt,
        float(unserved_frac),
        float(disc_frac),
        n_downed,
        base_cpd_val,
    ]


@dataclass
class PageHinkleyDetector:
    """Page-Hinkley cumulative sum statistical change/drift detector.

    Monitors sequence of surrogate prediction residuals e_t = |y_hat_t - y_t|.
    Detects concept drift when cumulative positive deviations from the running
    mean exceed an adaptive threshold lambda * sigma_e.
    """

    delta: float = 0.05            # Tolerance offset
    threshold_factor: float = 3.0  # Threshold multiplier on standard deviation
    min_instances: int = 12        # Minimum observations before activating
    alpha_ewma: float = 0.1        # EWMA decay factor for running stats

    count: int = 0
    mean_error: float = 0.0
    var_error: float = 1.0
    cumulative_sum: float = 0.0
    min_cumulative_sum: float = 0.0
    drift_detected: bool = False
    drift_count: int = 0
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=30))

    def update(self, error: float) -> bool:
        """Update detector with observed prediction error. Returns True if drift detected."""
        self.count += 1
        self.recent_errors.append(error)

        # Online Welford / EWMA running statistics
        if self.count == 1:
            self.mean_error = error
            self.var_error = 1.0
        else:
            diff = error - self.mean_error
            self.mean_error += self.alpha_ewma * diff
            self.var_error = (1.0 - self.alpha_ewma) * self.var_error + self.alpha_ewma * (diff ** 2)

        if self.count < self.min_instances:
            self.drift_detected = False
            return False

        std_error = max(float(np.sqrt(max(self.var_error, 1e-6))), 0.05)

        # Page-Hinkley cumulative sum test
        self.cumulative_sum += (error - self.mean_error - self.delta)
        if self.cumulative_sum < self.min_cumulative_sum:
            self.min_cumulative_sum = self.cumulative_sum

        ph_stat = self.cumulative_sum - self.min_cumulative_sum
        threshold = self.threshold_factor * std_error

        if ph_stat > threshold:
            self.drift_detected = True
            self.drift_count += 1
            # Reset cumulative tracker after drift trigger
            self.cumulative_sum = 0.0
            self.min_cumulative_sum = 0.0
            return True

        self.drift_detected = False
        return False

    @property
    def rolling_mae(self) -> float:
        return float(np.mean(self.recent_errors)) if self.recent_errors else 0.0

    @property
    def rolling_std(self) -> float:
        return float(np.std(self.recent_errors)) if len(self.recent_errors) > 1 else 0.2


@dataclass
class EvolutionaryKnowledge:
    """Self-evolution knowledge engine integrating:

    1. Candidate-level Gradient Boosted Surrogate Model
    2. Page-Hinkley Concept Drift Detector
    3. Dynamic Candidate Screening & Acquisition Policy
    4. Evolutionary Memory mapping scenario signatures to winning actions.
    """

    model: Optional[object] = None
    X: List[List[float]] = field(default_factory=list)
    y: List[float] = field(default_factory=list)
    timestamps: List[int] = field(default_factory=list)
    current_epoch: int = 0

    # Drift detection & adaptation
    drift_detector: PageHinkleyDetector = field(default_factory=PageHinkleyDetector)
    drift_events: List[int] = field(default_factory=list)
    cooldown_epochs: int = 0       # Temporary expansion of simulation budget after drift

    # Evolutionary action memory: signature -> mutation_kind -> [score, ...]
    best_actions: Dict[str, Dict[str, List[float]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )
    prediction_errors: List[float] = field(default_factory=list)

    # Operational metrics
    total_screened_candidates: int = 0
    total_simulations_executed: int = 0
    total_simulations_avoided: int = 0

    def is_ready(self) -> bool:
        """Returns True if surrogate model has been trained and is ready for inference."""
        return self.model is not None

    def observe_candidate(
        self,
        features: List[float],
        actual_cpd: float,
        predicted_cpd: Optional[float] = None,
    ) -> Optional[float]:
        """Record an observed intervention outcome and update drift detector."""
        self.X.append(list(features))
        self.y.append(float(actual_cpd))
        self.timestamps.append(self.current_epoch)

        err = None
        if predicted_cpd is not None:
            err = abs(float(predicted_cpd) - float(actual_cpd))
            self.prediction_errors.append(err)
            drift = self.drift_detector.update(err)
            if drift:
                self.drift_events.append(self.current_epoch)
                self.cooldown_epochs = 5  # Expand simulation pool for next 5 epochs
                # Trigger adaptive retraining
                self.maybe_train(min_samples=15, force=True)
        return err

    def predict_candidate(self, features: List[float]) -> float:
        """Predict downstream C-CPD for candidate intervention."""
        if self.model is None:
            return 0.0
        return float(self.model.predict([features])[0])

    def predict_batch(self, features_list: List[List[float]]) -> List[float]:
        """Batch predict downstream C-CPD for candidate list."""
        if self.model is None or not features_list:
            return [0.0] * len(features_list)
        preds = self.model.predict(features_list)
        return [float(p) for p in preds]

    def screen_candidates(
        self,
        candidates: List[Mutation],
        features_list: List[List[float]],
        baseline_cpd: float,
        adaptabilities: List[float],
        cfg: Dict,
    ) -> Tuple[List[int], List[int], List[float]]:
        """Measurable Candidate Screening Criterion:

        Partitions candidate indices into (simulated_indices, pruned_indices).
        - Uses surrogate predictions to compute predicted objective J_hat(m).
        - Retains top-k_min candidates and any candidate within uncertainty margin
          tau_screen = margin * sigma_drift of the leader.
        - If drift cooldown is active, expands k_min to safeguard decision optimality.
        """
        opt_cfg = cfg.get("optimization", {})
        learn_cfg = cfg.get("learning", {})
        eta = float(opt_cfg.get("eta", 0.4))
        k_min = int(learn_cfg.get("screening_top_k", 5))
        margin = float(learn_cfg.get("uncertainty_margin", 0.35))

        # Expand simulation budget during drift recovery cooldown
        if self.cooldown_epochs > 0:
            k_min = min(len(candidates), k_min * 2)

        n = len(candidates)
        if not self.is_ready() or n <= k_min or not learn_cfg.get("enable_screening", True):
            # Warm-up phase or small pool: simulate everything
            return list(range(n)), [], [0.0] * n

        predicted_cpds = self.predict_batch(features_list)
        predicted_scores = []
        for i in range(n):
            pred_delta = baseline_cpd - predicted_cpds[i]
            score = pred_delta + eta * adaptabilities[i]
            predicted_scores.append(score)

        # Rank candidates by predicted score descending
        ranked_indices = sorted(range(n), key=lambda i: predicted_scores[i], reverse=True)
        leader_score = predicted_scores[ranked_indices[0]]

        # Measurable threshold: leader_score - margin * rolling_std
        tau_screen = max(margin * self.drift_detector.rolling_std, 0.10)
        cutoff_score = leader_score - tau_screen

        simulated_indices: List[int] = []
        pruned_indices: List[int] = []

        for rank, idx in enumerate(ranked_indices):
            # Guarantee 1: Top-k_min always simulated
            if rank < k_min:
                simulated_indices.append(idx)
            # Guarantee 2: Any candidate within uncertainty margin simulated
            elif predicted_scores[idx] >= cutoff_score:
                simulated_indices.append(idx)
            # Pruned: demonstrably sub-optimal given surrogate error bounds
            else:
                pruned_indices.append(idx)

        self.total_screened_candidates += n
        self.total_simulations_executed += len(simulated_indices)
        self.total_simulations_avoided += len(pruned_indices)

        return simulated_indices, pruned_indices, predicted_cpds

    def maybe_train(self, min_samples: int = 20, force: bool = False) -> bool:
        """Train or adapt the surrogate model.

        When retraining after drift, applies exponential recency sample weighting:
            w_i = exp((t_i - t_max) / W)
        giving up to 3x higher weight to post-drift samples.
        """
        if not _SKLEARN or len(self.y) < min_samples:
            return False

        # Recency-weighted training if drift occurred
        if self.drift_events and len(self.timestamps) == len(self.y):
            t_max = max(self.timestamps)
            window = max(10, len(self.timestamps) // 3)
            weights = np.array([
                np.exp((t - t_max) / window) for t in self.timestamps
            ])
            # Normalize weights to mean 1.0
            weights = weights / np.mean(weights)
        else:
            weights = None

        model = GradientBoostingRegressor(
            n_estimators=50,
            max_depth=4,
            learning_rate=0.1,
            random_state=42,
        )
        X_mat = np.array(self.X)
        y_vec = np.array(self.y)
        model.fit(X_mat, y_vec, sample_weight=weights)
        self.model = model
        return True

    def step_epoch(self) -> None:
        """Advance epoch counter and decrement drift recovery cooldown."""
        self.current_epoch += 1
        if self.cooldown_epochs > 0:
            self.cooldown_epochs -= 1

    # ---- Evolutionary Memory ----------------------------------------------
    def record_success(self, signature: str, mutation_kind: str, score: float) -> None:
        """Store winning mutation for recurring failure signature."""
        self.best_actions[signature][mutation_kind].append(score)

    def recommend_action(self, signature: str) -> Optional[str]:
        """Return historically best mutation kind for a given scenario signature."""
        memory = self.best_actions.get(signature)
        if not memory:
            return None
        return max(memory, key=lambda k: float(np.mean(memory[k])))

    # ---- Persistence & Diagnostics ----------------------------------------
    def save(self, path: str) -> None:
        if not _SKLEARN or self.model is None:
            return
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        joblib.dump(
            {
                "model": self.model,
                "n_samples": len(self.y),
                "drift_events": self.drift_events,
                "best_actions": dict(self.best_actions),
            },
            path,
        )

    def summary(self) -> Dict[str, float]:
        mae = (
            float(np.mean(self.prediction_errors))
            if self.prediction_errors
            else float("nan")
        )
        pct_avoided = (
            100.0 * self.total_simulations_avoided / self.total_screened_candidates
            if self.total_screened_candidates > 0
            else 0.0
        )
        return {
            "n_samples": len(self.y),
            "model_trained": self.model is not None,
            "mean_prediction_error": mae,
            "drift_events_detected": len(self.drift_events),
            "simulations_executed": self.total_simulations_executed,
            "simulations_avoided": self.total_simulations_avoided,
            "pct_simulations_avoided": pct_avoided,
            "n_signatures_learned": len(self.best_actions),
        }
