"""
Hidden tests for the scikit-learn KNN lab.

This file must NEVER be included in student repositories.

The tests verify the expected behavior of the notebook rather than
requiring one exact implementation strategy.
"""

import numpy as np
import pandas as pd

from sklearn.pipeline import Pipeline
from sklearn.neighbors import KNeighborsClassifier


def _passed():
    print("\033[92mAll tests passed!\033[0m")


def _fail(message):
    raise AssertionError(message)


# ============================================================
# 1. Train / validation / test split
# ============================================================

def split_test(X_train, X_val, X_test, y_train, y_val, y_test):
    """Verify an approximately 60 / 20 / 20 split."""

    X_train = np.asarray(X_train)
    X_val = np.asarray(X_val)
    X_test = np.asarray(X_test)

    y_train = np.asarray(y_train)
    y_val = np.asarray(y_val)
    y_test = np.asarray(y_test)

    if X_train.ndim != 2 or X_val.ndim != 2 or X_test.ndim != 2:
        _fail("All feature splits must be 2D.")

    if y_train.ndim != 1 or y_val.ndim != 1 or y_test.ndim != 1:
        _fail("All target splits must be 1D.")

    for X_part, y_part, name in [
        (X_train, y_train, "train"),
        (X_val, y_val, "validation"),
        (X_test, y_test, "test"),
    ]:
        if len(X_part) != len(y_part):
            _fail(f"{name} X and y have different sample counts.")

    if X_train.shape[1] != X_val.shape[1] != X_test.shape[1]:
        _fail("All feature splits must have the same number of features.")

    total = len(X_train) + len(X_val) + len(X_test)
    if total == 0:
        _fail("The splits must not be empty.")

    ratios = (
        len(X_train) / total,
        len(X_val) / total,
        len(X_test) / total,
    )

    expected = (0.60, 0.20, 0.20)

    for actual, target, name in zip(
        ratios,
        expected,
        ["training", "validation", "test"],
    ):
        if not np.isclose(actual, target, atol=0.02):
            _fail(
                f"{name} split should be approximately {target:.0%}; "
                f"got {actual:.3f}."
            )

    _passed()


# ============================================================
# 2. Normal KNN model
# ============================================================

def knn_model_test(knn):
    """
    Verify that the main ordinary KNN model is a fitted
    KNeighborsClassifier.

    Pipeline is deliberately NOT required here. This corresponds
    to the explicit KNN workflow taught before Pipeline.
    """

    if not isinstance(knn, KNeighborsClassifier):
        _fail("knn must be a KNeighborsClassifier.")

    if knn.n_neighbors <= 0:
        _fail("n_neighbors must be positive.")

    if knn.weights not in {"uniform", "distance"}:
        _fail("weights must be 'uniform' or 'distance'.")

    allowed_metrics = {
        "euclidean",
        "manhattan",
        "minkowski",
        "chebyshev",
    }

    if knn.metric not in allowed_metrics:
        _fail(f"Unexpected KNN metric: {knn.metric!r}.")

    if not hasattr(knn, "_fit_X"):
        _fail("knn does not appear to have been fitted.")

    if knn._fit_X is None:
        _fail("knn does not contain fitted training data.")

    _passed()


# ============================================================
# 3. Pipeline
# ============================================================

def pipeline_test(knn_pipeline):
    """Verify the pipeline introduced at the end of the notebook."""

    if not isinstance(knn_pipeline, Pipeline):
        _fail("knn_pipeline must be an sklearn Pipeline.")

    if "scaler" not in knn_pipeline.named_steps:
        _fail("knn_pipeline must contain a 'scaler' step.")

    if "knn" not in knn_pipeline.named_steps:
        _fail("knn_pipeline must contain a 'knn' step.")

    knn = knn_pipeline.named_steps["knn"]

    if not isinstance(knn, KNeighborsClassifier):
        _fail("The pipeline 'knn' step must be a KNeighborsClassifier.")

    if not hasattr(knn, "_fit_X") or knn._fit_X is None:
        _fail("The KNN step inside knn_pipeline must be fitted.")

    _passed()


# ============================================================
# 4. k selection
# ============================================================

def k_selection_test(k_values, cv_scores, best_k):
    """
    Verify the k-selection experiment.

    The notebook currently uses the validation set for this experiment;
    cv_scores is kept as the generic score sequence used for selection.
    """

    k_values = list(k_values)
    cv_scores = np.asarray(cv_scores)

    if len(k_values) == 0:
        _fail("k_values must not be empty.")

    if len(k_values) != len(cv_scores):
        _fail("k_values and cv_scores must have the same length.")

    if any(
        isinstance(k, (bool, np.bool_))
        or not isinstance(k, (int, np.integer))
        or k <= 0
        for k in k_values
    ):
        _fail("All k values must be positive integers.")

    if cv_scores.ndim != 1:
        _fail("cv_scores must be one-dimensional.")

    if not np.all(np.isfinite(cv_scores)):
        _fail("cv_scores must contain finite values.")

    if np.any((cv_scores < 0) | (cv_scores > 1)):
        _fail("Scores must be between 0 and 1.")

    expected_best_k = k_values[int(np.argmax(cv_scores))]

    if best_k != expected_best_k:
        _fail(
            f"best_k should correspond to the highest score. "
            f"Expected {expected_best_k}, got {best_k}."
        )

    _passed()


# ============================================================
# 5. Final model
# ============================================================

def final_model_test(final_knn, X_test, y_test):
    """Verify that the final pipeline can predict the test set."""

    if not isinstance(final_knn, Pipeline):
        _fail("final_knn must be an sklearn Pipeline.")

    if "scaler" not in final_knn.named_steps:
        _fail("final_knn must contain a 'scaler' step.")

    if "knn" not in final_knn.named_steps:
        _fail("final_knn must contain a 'knn' step.")

    try:
        predictions = final_knn.predict(X_test)
    except Exception as exc:
        _fail(f"final_knn.predict(X_test) failed: {exc}")

    predictions = np.asarray(predictions)
    y_test = np.asarray(y_test)

    if predictions.shape != (len(X_test),):
        _fail(
            f"Predictions have shape {predictions.shape}; "
            f"expected ({len(X_test)},)."
        )

    if len(predictions) != len(y_test):
        _fail("Number of predictions does not match y_test.")

    accuracy = np.mean(predictions == y_test)

    if not (0.0 <= accuracy <= 1.0):
        _fail("Final test accuracy is outside [0, 1].")

    _passed()