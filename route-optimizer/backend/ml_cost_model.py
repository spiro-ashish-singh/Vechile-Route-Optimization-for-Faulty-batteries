"""
ml_cost_model.py

This is the ML core of the project.

THE IDEA:
Normal routing says: "cost of this road segment = its length in meters."
That's why Google Maps' "shortest" route sometimes takes you down a
narrow, accident-prone back street just because it's 200m shorter.

Instead, we train a model that looks at a road segment's FEATURES
(length, road type, accident risk, congestion variance) and predicts
a "true cost" — a number that isn't just distance, but reflects how
much you'd actually want to avoid or prefer that road.

HOW WE TRAIN IT (important to understand):
We don't have real labeled data saying "this route was good/bad" —
nobody does, that's the whole challenge of this kind of project.
So we use a well-established trick: WEAK SUPERVISION. We define a
formula for what "true cost" SHOULD look like based on domain
knowledge (e.g. accident-prone roads should cost more), generate
that as training labels, then train a model to approximate that
formula from the raw features.

Why not just use the formula directly instead of training a model?
Two honest reasons:
  1. It demonstrates the ML skill (feature engineering, training,
     evaluating a model) rather than just hardcoding an if/else.
  2. In a real system, you'd eventually replace the synthetic
     formula with REAL labels (e.g. actual driver ratings, actual
     accident data) and retrain — the model doesn't change, just
     what it's trained on. That's the whole point of doing it this way.
"""

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error


FEATURE_NAMES = ["length", "speed_kph", "accident_score", "congestion_variance", "is_residential", "is_primary"]


def _extract_features(edge_attrs: dict) -> list:
    """
    Turns a road segment's raw attributes into a numeric feature vector
    the model can use. Road type is "one-hot encoded" (turned into
    0/1 flags) since it's a category, not a number.
    """
    road_type = edge_attrs.get("road_type", "residential")
    return [
        edge_attrs.get("length", 0),
        edge_attrs.get("speed_kph", 30),
        edge_attrs.get("accident_score", 0),
        edge_attrs.get("congestion_variance", 0),
        1.0 if road_type == "residential" else 0.0,
        1.0 if road_type == "primary" else 0.0,
    ]


def _synthetic_true_cost(edge_attrs: dict) -> float:
    """
    The "weak supervision" formula mentioned above. This defines what
    we WANT a good routing cost to reflect, beyond raw distance:

      - base cost = distance (you still care about not going wildly out of the way)
      - + heavy penalty for accident-prone roads (safety matters)
      - + penalty for unpredictable/congested roads (reliability matters)
      - small discount for primary roads (they're built for through-traffic)

    In a real deployment, you'd replace this function's OUTPUT with
    real labels (e.g. from user feedback or accident datasets) — the
    rest of this file (model training/prediction) stays the same.
    """
    length = edge_attrs.get("length", 0)
    accident_score = edge_attrs.get("accident_score", 0)
    congestion_variance = edge_attrs.get("congestion_variance", 0)
    road_type = edge_attrs.get("road_type", "residential")

    cost = length
    cost += length * accident_score * 1.5       # accident-prone roads cost up to 150% more
    cost += length * congestion_variance * 0.8   # unpredictable roads cost up to 80% more
    if road_type == "primary":
        cost *= 0.9  # primary roads are slightly preferred, all else equal

    return cost


class LearnedCostModel:
    """
    Wraps a trained regression model. Once trained, call
    `.predict_edge_cost(edge_attrs)` on any road segment to get its
    learned cost — this is what gets plugged into A* instead of
    raw distance (see routing_engine.route_learned).
    """

    def __init__(self):
        self.model = GradientBoostingRegressor(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.1,
            random_state=42,
        )
        self.is_trained = False

    def train(self, graph, verbose=True):
        """
        Builds a training set from every edge in the graph:
          X = engineered features per edge
          y = the synthetic "true cost" label for that edge
        Then fits a gradient boosted tree model to predict y from X.

        We hold out 20% of edges to check the model actually
        generalizes (isn't just memorizing), which is standard
        practice for any ML model, not just this one.
        """
        X, y = [], []
        for u, v, k, data in graph.edges(keys=True, data=True):
            X.append(_extract_features(data))
            y.append(_synthetic_true_cost(data))

        X = np.array(X)
        y = np.array(y)

        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42
        )

        self.model.fit(X_train, y_train)
        self.is_trained = True

        if verbose:
            preds = self.model.predict(X_test)
            mae = mean_absolute_error(y_test, preds)
            print(f"Model trained on {len(X_train)} edges, tested on {len(X_test)} edges.")
            print(f"Mean Absolute Error on held-out edges: {mae:.2f} "
                  f"(avg true cost was {y_test.mean():.2f}, so this is "
                  f"{100 * mae / y_test.mean():.1f}% relative error)")

    def predict_edge_cost(self, edge_attrs: dict) -> float:
        """
        Predicts the learned cost for a single road segment.
        This is called once per edge when building a route (see
        routing_engine.route_learned).
        """
        if not self.is_trained:
            raise RuntimeError("Model must be trained before predicting. Call .train(graph) first.")
        features = np.array([_extract_features(edge_attrs)])
        return float(self.model.predict(features)[0])

    def feature_importance_report(self) -> dict:
        """
        Shows which features the model actually learned to rely on.
        This is useful for your write-up: it proves the model learned
        something sensible (e.g. accident_score should matter a lot)
        rather than being a black box.
        """
        if not self.is_trained:
            raise RuntimeError("Model must be trained first.")
        importances = self.model.feature_importances_
        return dict(sorted(
            zip(FEATURE_NAMES, importances),
            key=lambda pair: pair[1],
            reverse=True,
        ))


# ---------------------------------------------------------------------------
# Quick manual test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from routing_engine import build_synthetic_graph

    G = build_synthetic_graph()

    cost_model = LearnedCostModel()
    cost_model.train(G)

    print("\nFeature importance (what the model learned matters most):")
    for name, importance in cost_model.feature_importance_report().items():
        print(f"  {name:22s} {importance:.3f}")
